#!/usr/bin/env python3
"""Ornith 模型版本升级请求的领域规则与拓扑转换。"""

from __future__ import annotations

import re
from typing import Any


DEFAULT_ORNITH_TARGET_MODEL = "ornith-ai/Ornith-1.5-35B-A3B-FP8"
IMMUTABLE_REVISION_RE = re.compile(r"^[0-9a-f]{40,64}$")
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")


def upgrade_profiles() -> list[dict[str, Any]]:
    """返回管理页面和 CLI 共用的升级目标目录。

    revision 留空表示计划阶段读取 Hub 当前提交并转换为不可变 SHA；执行阶段只
    接受已经固定的 SHA，因此上游 `main` 后续变化不会改变已确认计划。
    """

    return [
        {
            "id": "ornith-1.5-35b-a3b-fp8",
            "family": "ornith",
            "label": "Ornith 1.5 35B-A3B FP8",
            "model_id": DEFAULT_ORNITH_TARGET_MODEL,
            "revision": "",
            "recommended_max_model_len": 32768,
            "description": "与现有 Ornith 1.0 35B FP8 规模接近的官方升级目标。",
        }
    ]


def select_source_deployment(
    registry: dict[str, Any], deployment_id: str = ""
) -> dict[str, Any]:
    """选择一个已启用的本机 Ornith 部署作为升级来源。

    参数：
        registry: 已通过部署注册表校验的完整快照。
        deployment_id: 管理员显式选择的部署 ID；空值只在唯一候选时自动选择。

    返回：
        当前运行的 Ornith 部署对象。

    异常：
        ValueError: 来源不存在、未启用、不是 Ornith 或没有本机实例。
    """

    deployments = registry.get("deployments", {})
    candidates = [
        item
        for item in deployments.values()
        if isinstance(item, dict)
        and item.get("enabled", True)
        and "ornith" in str(item.get("model_id", "")).lower()
        and any(
            instance.get("kind") == "local" and instance.get("enabled", True)
            for instance in item.get("instances", [])
            if isinstance(instance, dict)
        )
    ]
    if deployment_id:
        source = deployments.get(deployment_id)
        if source not in candidates:
            raise ValueError("所选来源必须是已启用且包含本机实例的 Ornith 部署")
        return source
    if len(candidates) != 1:
        raise ValueError("请明确选择一个要升级的 Ornith 部署")
    return candidates[0]


def requested_upgrade_target(
    source: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    """规范化升级目标，并为目录检查提供固定输入。

    参数：
        source: 当前来源部署。
        payload: CLI 或页面提交的升级参数。

    返回：
        包含目标模型、可选 revision、上下文和显存利用率的目录检查参数。
    """

    model_id = str(payload.get("target_model_id") or DEFAULT_ORNITH_TARGET_MODEL).strip()
    if not MODEL_ID_RE.fullmatch(model_id) or "ornith" not in model_id.lower():
        raise ValueError("Ornith 版本升级只能选择 Ornith 目标模型")
    revision = str(payload.get("target_revision") or "").strip().lower()
    if revision and not IMMUTABLE_REVISION_RE.fullmatch(revision):
        raise ValueError("目标 revision 必须为空或使用完整不可变提交 SHA")
    source_runtime = source.get("runtime", {})
    try:
        max_model_len = int(
            payload.get("max_model_len")
            or min(int(source_runtime.get("max_model_len", 32768)), 32768)
        )
    except (TypeError, ValueError) as error:
        raise ValueError("目标最大上下文必须是整数") from error
    if max_model_len < 8192 or max_model_len > 262144:
        raise ValueError("目标最大上下文必须位于 8192-262144")
    return {
        "model_id": model_id,
        "revision": revision,
        "max_model_len": max_model_len,
        "gpu_memory_utilization": float(
            source_runtime.get("gpu_memory_utilization", 0.9)
        ),
    }


def _worker_slots(
    registry: dict[str, Any], source: dict[str, Any], instance_count: int
) -> list[tuple[int, int]]:
    """为目标实例分配不与其他部署冲突的 Worker ID 和端口。

    现有来源槽位优先复用，使常见 TP1→TP2 升级只收缩实例数量。确需更多槽位
    时从空闲 Worker ID 中补齐，并沿用来源部署的端口基数。
    """

    source_instances = sorted(
        (
            item
            for item in source.get("instances", [])
            if item.get("kind") == "local" and item.get("enabled", True)
        ),
        key=lambda item: int(item["worker_id"]),
    )
    source_ids = {int(item["worker_id"]) for item in source_instances}
    other_ids: set[int] = set()
    other_ports: set[int] = set()
    for deployment in registry.get("deployments", {}).values():
        if deployment is source or not deployment.get("enabled", True):
            continue
        for instance in deployment.get("instances", []):
            if instance.get("kind") != "local" or not instance.get("enabled", True):
                continue
            other_ids.add(int(instance["worker_id"]))
            other_ports.add(int(instance["port"]))
    bases = [
        int(item["port"]) - int(item["worker_id"])
        for item in source_instances
    ]
    port_base = min(bases) if bases else 8100
    existing_ports = {
        int(item["worker_id"]): int(item["port"]) for item in source_instances
    }
    ordered_ids = [int(item["worker_id"]) for item in source_instances]
    ordered_ids.extend(
        worker_id
        for worker_id in range(256)
        if worker_id not in source_ids and worker_id not in other_ids
    )
    result: list[tuple[int, int]] = []
    for worker_id in ordered_ids:
        port = existing_ports.get(worker_id, port_base + worker_id)
        if port < 1024 or port > 65535 or port in other_ports:
            continue
        result.append((worker_id, port))
        if len(result) == instance_count:
            return result
    raise ValueError("没有足够的空闲 Worker ID 和端口承载目标拓扑")


def build_upgrade_request(
    registry: dict[str, Any],
    source: dict[str, Any],
    payload: dict[str, Any],
    catalog: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """把目录检查结果转换成标准部署请求和可展示升级摘要。

    参数：
        registry: 当前部署注册表。
        source: 选中的 Ornith 来源部署。
        payload: 管理员提交的升级参数。
        catalog: `model_catalog inspect --json` 的受校验结果。

    返回：
        标准部署请求和升级摘要。标准请求继续由模型部署控制器执行唯一的冲突、
        下载、快照、Worker 变更和回滚状态机。
    """

    target = requested_upgrade_target(source, payload)
    if not catalog.get("installable") or not isinstance(catalog.get("plan"), dict):
        reasons = "；".join(str(item) for item in catalog.get("rejection_reasons", []))
        raise ValueError(f"目标模型未通过本机部署门禁：{reasons or '目录没有返回可执行计划'}")
    if str(catalog.get("id", "")) != target["model_id"]:
        raise ValueError("目录检查返回的模型身份与升级目标不一致")
    revision = str(catalog.get("revision", "")).lower()
    if not IMMUTABLE_REVISION_RE.fullmatch(revision):
        raise ValueError("目录检查没有解析出不可变目标 revision")
    if target["revision"] and target["revision"] != revision:
        raise ValueError("目录检查返回的 revision 与管理员固定值不一致")
    if str(source.get("model_id", "")) == target["model_id"] and str(
        registry.get("artifacts", {}).get(source.get("artifact_id"), {}).get("revision", "")
    ).lower() == revision:
        raise ValueError("当前部署已经使用该目标模型和 revision")

    plan = catalog["plan"]
    tp_size = int(plan.get("tp", 0))
    if tp_size < 1 or tp_size > 16:
        raise ValueError("目录返回的张量并行数无效")
    source_instances = [
        item
        for item in source.get("instances", [])
        if item.get("kind") == "local" and item.get("enabled", True)
    ]
    gpu_ids = sorted(
        int(gpu)
        for instance in source_instances
        for gpu in instance.get("gpu_devices", [])
    )
    if not gpu_ids or len(gpu_ids) != len(set(gpu_ids)):
        raise ValueError("来源部署的 GPU 清单为空或包含重复项")
    if len(gpu_ids) % tp_size:
        raise ValueError(
            f"来源部署共有 {len(gpu_ids)} 张 GPU，不能按目标 TP{tp_size} 完整分组"
        )
    instance_count = len(gpu_ids) // tp_size
    slots = _worker_slots(registry, source, instance_count)
    instances = [
        {
            "id": f"local-worker-{worker_id}",
            "kind": "local",
            "worker_id": worker_id,
            "gpu_devices": gpu_ids[index * tp_size : (index + 1) * tp_size],
            "port": port,
            "enabled": True,
        }
        for index, (worker_id, port) in enumerate(slots)
    ]
    capabilities = catalog.get("capabilities", {})
    source_runtime = source.get("runtime", {})
    public_ids = [str(item) for item in source.get("public_model_ids", [])]
    if not public_ids:
        raise ValueError("来源部署没有可保留的公开模型 ID")
    served_name = re.sub(
        r"[^A-Za-z0-9._-]+", "-", target["model_id"].split("/")[-1]
    ).lower()
    aliases = registry.get("legacy_aliases", {})
    preserve_legacy_alias = str(aliases.get("gdn-inside", "")) in public_ids
    profile = next(
        (
            item
            for item in upgrade_profiles()
            if item["model_id"] == target["model_id"]
        ),
        None,
    )
    request = {
        "deployment_id": str(source["id"]),
        "hub": "huggingface",
        "model_id": target["model_id"],
        "revision": revision,
        "public_model_id": public_ids[0],
        "additional_public_ids": public_ids[1:],
        "served_model_name": served_name,
        "display_name": str(profile["label"] if profile else served_name),
        "publish_requested": bool(source.get("publish_requested", True)),
        "preserve_legacy_alias": preserve_legacy_alias,
        "instances": instances,
        "image": str(source_runtime.get("image", "vllm/vllm-openai:v0.22.1")),
        "tensor_parallel_size": tp_size,
        "max_model_len": int(plan["max_model_len"]),
        "gpu_memory_utilization": float(target["gpu_memory_utilization"]),
        "max_num_seqs": int(plan["max_num_seqs"]),
        "max_num_batched_tokens": int(
            source_runtime.get("max_num_batched_tokens", 8192)
        ),
        "trust_remote_code": bool(catalog.get("trust_remote_code", False)),
        "supports_image_input": bool(capabilities.get("image_input", False)),
        "supports_ocr": bool(capabilities.get("ocr_optimized", False)),
        "supports_tool_calling": bool(capabilities.get("tool_parser")),
        "supports_reasoning": bool(capabilities.get("reasoning_parser")),
        "supports_thinking_toggle": bool(capabilities.get("thinking_toggle", False)),
        "tool_call_parser": str(capabilities.get("tool_parser", "")),
        "reasoning_parser": str(capabilities.get("reasoning_parser", "")),
        "mm_limit": str(source_runtime.get("mm_limit", '{"image":4}')),
    }
    source_artifact = registry.get("artifacts", {}).get(source.get("artifact_id"), {})
    summary = {
        "family": "ornith",
        "source_deployment_id": str(source["id"]),
        "current_model_id": str(source.get("model_id", "")),
        "current_revision": str(source_artifact.get("revision", "")),
        "current_artifact_path": str(source_artifact.get("path", "")),
        "target_model_id": target["model_id"],
        "target_revision": revision,
        "target_weight_bytes": int(catalog.get("weight_bytes") or 0),
        "target_architectures": list(catalog.get("supported_architectures") or []),
        "target_tp_size": tp_size,
        "target_instance_count": instance_count,
        "target_max_model_len": int(plan["max_model_len"]),
        "target_max_num_seqs": int(plan["max_num_seqs"]),
        "source_worker_ids": sorted(int(item["worker_id"]) for item in source_instances),
        "target_worker_ids": [worker_id for worker_id, _port in slots],
        "retains_current_artifact": True,
        "rollback_requires_worker_reload": True,
    }
    return request, summary
