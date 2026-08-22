#!/usr/bin/env python3
"""LLMCtl 多模型部署注册表与受限控制服务。

该模块只在本机 Unix Socket 上提供经过严格校验的模型部署操作。普通推理
流量不会经过本进程；账户门户仅使用它读取状态、生成计划和提交后台任务。
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as dt
import fcntl
import grp
import hashlib
import json
import os
import pathlib
import re
import shlex
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Callable, Iterable


# 测试和安装后的守护进程都可能按文件路径加载本模块。把同级目录作为领域
# 模块解析根，确保升级规则不会依赖调用者当前工作目录。
_MODEL_CONTROL_DIRECTORY = str(pathlib.Path(__file__).resolve().parent)
if _MODEL_CONTROL_DIRECTORY not in sys.path:
    sys.path.insert(0, _MODEL_CONTROL_DIRECTORY)

from model_upgrade import (
    build_upgrade_request,
    requested_upgrade_target,
    select_source_deployment,
    upgrade_profiles,
)
from omniroute_maintenance import (
    OmniRouteMaintenanceManager,
    OmniRouteMaintenancePaths,
)


APP_VERSION = "3.5.0"
SCHEMA_VERSION = 1
MAX_REQUEST_BYTES = 2 * 1024 * 1024
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
PUBLIC_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
DEPLOYMENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,299}$")
SAFE_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
TERMINAL_JOB_STATES = {"succeeded", "failed", "cancelled", "rolled_back"}
DEFAULT_MAINTENANCE_NO_PROXY = "127.0.0.1,localhost,::1"
MODELSCOPE_DOWNLOADER_VERSION = "0.1.8"


def utc_now() -> str:
    """返回便于审计和排序的 UTC 时间戳。"""

    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def atomic_write(path: pathlib.Path, content: str, mode: int = 0o600) -> None:
    """原子写入文本文件，避免掉电或进程退出留下半份配置。

    参数：
        path: 目标文件。
        content: 要写入的 UTF-8 文本。
        mode: 最终文件权限。
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def parse_env_file(path: pathlib.Path) -> dict[str, str]:
    """读取 LLMCtl 生成的简单 Shell 环境文件。

    只接受 NAME=VALUE 形式，不执行命令替换或其他 Shell 语法，因此可安全用于
    控制服务的迁移与快照逻辑。
    """

    result: dict[str, str] = {}
    if not path.is_file():
        return result
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if not SAFE_ENV_NAME_RE.fullmatch(name):
            continue
        try:
            parts = shlex.split(value, comments=False, posix=True)
        except ValueError:
            continue
        result[name] = parts[0] if len(parts) == 1 else value.strip()
    return result


def validate_maintenance_proxy(
    proxy_url: str, no_proxy: str = DEFAULT_MAINTENANCE_NO_PROXY
) -> tuple[str, str]:
    """校验只用于模型目录和权重下载的维护代理配置。

    参数：
        proxy_url: 完整的 HTTP(S) 代理地址，不允许凭据、路径或查询参数。
        no_proxy: 不经过代理的逗号分隔主机、IP 或 CIDR 清单。

    返回：
        规范化后的代理地址与直连清单。

    异常：
        ValueError: 地址、端口或直连清单不满足安全边界。
    """

    value = str(proxy_url or "").strip()
    bypass = str(no_proxy or DEFAULT_MAINTENANCE_NO_PROXY).strip()
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError("维护代理地址或端口无效") from error
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or port is None
        or not 1 <= port <= 65535
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("维护代理必须是无凭据、无路径的 http(s)://主机:端口")
    if (
        not bypass
        or len(bypass) > 2048
        or any(character.isspace() or ord(character) < 32 for character in bypass)
        or not re.fullmatch(r"[A-Za-z0-9._:/,\[\]-]+", bypass)
    ):
        raise ValueError("NO_PROXY 必须是无空格的逗号分隔主机、IP 或 CIDR")
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    return f"{parsed.scheme}://{host}:{port}", bypass


def maintenance_environment(proxy_env: pathlib.Path) -> dict[str, str]:
    """构造显式维护操作环境，并把保存字段转换成标准代理变量。

    参数：
        proxy_env: root 管理的维护代理环境文件。

    返回：
        当前进程环境副本；配置有效时同时包含大小写两组代理变量。
    """

    environment = os.environ.copy()
    saved = parse_env_file(proxy_env)
    proxy_url = str(saved.get("MAINTENANCE_PROXY") or "").strip()
    no_proxy = str(
        saved.get("MAINTENANCE_NO_PROXY") or DEFAULT_MAINTENANCE_NO_PROXY
    ).strip()
    if proxy_url:
        proxy_url, no_proxy = validate_maintenance_proxy(proxy_url, no_proxy)
        environment.update(
            {
                "HTTP_PROXY": proxy_url,
                "HTTPS_PROXY": proxy_url,
                "http_proxy": proxy_url,
                "https_proxy": proxy_url,
                "NO_PROXY": no_proxy,
                "no_proxy": no_proxy,
            }
        )
    return environment


def render_env(values: dict[str, Any]) -> str:
    """把受校验的 Worker 配置渲染为不可执行注入的 Shell 环境文件。"""

    lines = ["# 由 LLMCtl 多模型控制器生成；请通过 llmctl model 修改。"]
    for name in sorted(values):
        if not SAFE_ENV_NAME_RE.fullmatch(name):
            raise ValueError(f"环境变量名非法：{name}")
        value = str(values[name])
        if "\n" in value or "\r" in value or "\x00" in value:
            raise ValueError(f"环境变量值包含非法字符：{name}")
        lines.append(f"{name}={shlex.quote(value)}")
    return "\n".join(lines) + "\n"


@dataclasses.dataclass(frozen=True)
class Paths:
    """集中描述多模型控制面使用的文件位置。"""

    config_dir: pathlib.Path
    state_dir: pathlib.Path
    model_root: pathlib.Path
    registry: pathlib.Path
    socket: pathlib.Path
    jobs_dir: pathlib.Path
    backups_dir: pathlib.Path
    cluster_env: pathlib.Path
    secrets_env: pathlib.Path
    workers_dir: pathlib.Path
    proxy_env: pathlib.Path
    gateway_helper: pathlib.Path
    catalog_helper: pathlib.Path = pathlib.Path(
        "/usr/local/lib/llm-cluster/model_catalog.py"
    )
    modelscope_downloader: pathlib.Path = pathlib.Path(
        "/opt/llm-cluster/hub-venv/bin/ms"
    )

    @classmethod
    def from_environment(cls) -> "Paths":
        """根据环境变量和稳定默认值构造路径集合。"""

        config = pathlib.Path(os.environ.get("LLM_CLUSTER_CONFIG_DIR", "/etc/llm-cluster"))
        state = pathlib.Path(os.environ.get("LLM_CLUSTER_STATE_DIR", "/var/lib/llm-cluster"))
        model_root = pathlib.Path(os.environ.get("LLM_MODEL_ROOT", "/data/llm-cluster/models"))
        return cls(
            config_dir=config,
            state_dir=state,
            model_root=model_root,
            registry=pathlib.Path(
                os.environ.get("LLM_DEPLOYMENT_REGISTRY", str(config / "deployments.json"))
            ),
            socket=pathlib.Path(
                os.environ.get(
                    "LLM_MODEL_CONTROL_SOCKET", "/run/llm-cluster/model-control.sock"
                )
            ),
            jobs_dir=pathlib.Path(
                os.environ.get("LLM_MODEL_JOBS_DIR", str(state / "model-control/jobs"))
            ),
            backups_dir=pathlib.Path(
                os.environ.get("LLM_MODEL_BACKUPS_DIR", "/var/backups/llmctl/model-deployments")
            ),
            cluster_env=config / "cluster.env",
            secrets_env=config / "secrets.env",
            workers_dir=config / "workers",
            proxy_env=config / "proxy.env",
            gateway_helper=pathlib.Path(
                os.environ.get(
                    "LLM_GATEWAY_HELPER", "/usr/local/lib/llm-cluster/gateway_config.py"
                )
            ),
            catalog_helper=pathlib.Path(
                os.environ.get(
                    "LLM_CATALOG_HELPER",
                    "/usr/local/lib/llm-cluster/model_catalog.py",
                )
            ),
            modelscope_downloader=pathlib.Path(
                os.environ.get(
                    "LLM_MODELSCOPE_DOWNLOADER",
                    "/opt/llm-cluster/hub-venv/bin/ms",
                )
            ),
        )


def gateway_capabilities(paths: Paths) -> dict[str, Any]:
    """返回当前 AI 接入层对多模型发布和注册表同步的支持情况。"""

    cluster = parse_env_file(paths.cluster_env)
    kind = str(cluster.get("GATEWAY_KIND", "litellm")).strip().lower() or "litellm"
    registry_publish = kind == "omniroute"
    return {
        "kind": kind,
        "registry_publish": registry_publish,
        "message": (
            "OmniRoute 支持由 LLMCtl 原子同步多模型实例、公开 ID 和兼容别名"
            if registry_publish
            else "当前 AI 接入层不支持 LLMCtl 多模型注册表自动发布；仅允许不占用现有 Worker 的未发布实例"
        ),
    }


class RegistryStore:
    """提供带文件锁、校验和原子提交的部署注册表。"""

    def __init__(self, paths: Paths):
        """保存路径配置，实际文件在首次读取或提交时访问。"""

        self.paths = paths
        self.lock_path = paths.registry.with_suffix(paths.registry.suffix + ".lock")

    @contextlib.contextmanager
    def locked(self) -> Iterable[None]:
        """取得跨进程排他锁，序列化注册表和 Worker 配置变更。"""

        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def read(self, persist_legacy: bool = False) -> dict[str, Any]:
        """读取并校验注册表；旧部署可按需合成为兼容快照。"""

        if self.paths.registry.is_file():
            payload = json.loads(self.paths.registry.read_text(encoding="utf-8"))
            validate_registry(payload, self.paths)
            return payload
        payload = self.synthesize_legacy()
        if persist_legacy:
            self.write(payload)
        return payload

    def write(self, payload: dict[str, Any]) -> None:
        """校验并原子提交完整注册表。"""

        validate_registry(payload, self.paths)
        payload = json.loads(json.dumps(payload))
        payload["updated_at"] = utc_now()
        atomic_write(
            self.paths.registry,
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )

    def synthesize_legacy(self) -> dict[str, Any]:
        """把现有单模型配置只读映射成 schema v1，不重启任何服务。"""

        cluster = parse_env_file(self.paths.cluster_env)
        if not cluster:
            return empty_registry()
        model_root = pathlib.Path(cluster.get("MODEL_ROOT", str(self.paths.model_root)))
        active_workers = parse_worker_set(cluster.get("ACTIVE_WORKERS", ""))
        if not active_workers:
            count = bounded_int(cluster.get("INSTANCE_COUNT", "0"), "INSTANCE_COUNT", 0, 256)
            active_workers = list(range(count))
        instances: list[dict[str, Any]] = []
        for worker_id in active_workers:
            worker = parse_env_file(self.paths.workers_dir / f"{worker_id}.env")
            instances.append(
                {
                    "id": f"legacy-worker-{worker_id}",
                    "kind": "local",
                    "worker_id": worker_id,
                    "gpu_devices": parse_gpu_devices(
                        worker.get("GPU_DEVICES", legacy_gpu_devices(cluster, worker_id))
                    ),
                    "port": bounded_int(
                        worker.get(
                            "WORKER_PORT",
                            str(int(cluster.get("WORKER_BASE_PORT", "8100")) + worker_id),
                        ),
                        "WORKER_PORT",
                        1024,
                        65535,
                    ),
                    "enabled": True,
                }
            )
        model_id = cluster.get("MODEL_ID", cluster.get("SERVED_MODEL_NAME", "legacy-model"))
        served = cluster.get("SERVED_MODEL_NAME", model_id)
        registry = empty_registry()
        registry["migrated_from_legacy"] = True
        registry["artifacts"]["legacy-current"] = {
            "id": "legacy-current",
            "hub": cluster.get("MODEL_HUB", "local"),
            "model_id": model_id,
            "revision": cluster.get("MODEL_REVISION", "current"),
            "path": str(model_root / "current"),
            "status": "ready",
            "immutable": False,
            "created_at": utc_now(),
        }
        registry["deployments"]["legacy"] = {
            "id": "legacy",
            "display_name": served,
            "artifact_id": "legacy-current",
                "model_id": model_id,
                "served_model_name": served,
                "served_model_aliases": [],
            "public_model_ids": [served],
            "status": "running",
            "enabled": True,
            "publish_requested": gateway_capabilities(self.paths)["registry_publish"],
            "instances": instances,
            "runtime": runtime_from_environment(cluster),
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }
        if served != "gdn-inside":
            registry["legacy_aliases"]["gdn-inside"] = served
        validate_registry(registry, self.paths)
        return registry


def empty_registry() -> dict[str, Any]:
    """返回不含模型但具备完整版本信息的注册表。"""

    stamp = utc_now()
    return {
        "schema_version": SCHEMA_VERSION,
        "revision": 0,
        "migrated_from_legacy": False,
        "artifacts": {},
        "deployments": {},
        "legacy_aliases": {},
        "created_at": stamp,
        "updated_at": stamp,
    }


def parse_worker_set(value: str) -> list[int]:
    """解析逗号分隔的 Worker ID，并返回去重排序结果。"""

    if not value.strip():
        return []
    result: set[int] = set()
    for item in value.split(","):
        result.add(bounded_int(item.strip(), "Worker ID", 0, 255))
    return sorted(result)


def parse_gpu_devices(value: str | list[Any]) -> list[int]:
    """解析 GPU 设备列表，拒绝重复、负数和过大编号。"""

    items = value if isinstance(value, list) else str(value).split(",")
    result = [bounded_int(str(item).strip(), "GPU ID", 0, 255) for item in items if str(item).strip()]
    if not result or len(result) != len(set(result)):
        raise ValueError("GPU 列表不能为空或包含重复编号")
    return result


def bounded_int(value: Any, name: str, minimum: int, maximum: int) -> int:
    """把输入转换为有界整数，并生成可读错误。"""

    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} 必须是整数") from error
    if result < minimum or result > maximum:
        raise ValueError(f"{name} 必须位于 {minimum}-{maximum}")
    return result


def bounded_float(value: Any, name: str, minimum: float, maximum: float) -> float:
    """把输入转换为有界浮点数，并拒绝无穷或异常范围。"""

    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} 必须是数字") from error
    if not minimum <= result <= maximum:
        raise ValueError(f"{name} 必须位于 {minimum}-{maximum}")
    return result


def legacy_gpu_devices(cluster: dict[str, str], worker_id: int) -> str:
    """按旧版 TP_SIZE 推导缺失的 Worker GPU 列表。"""

    tp_size = bounded_int(cluster.get("TP_SIZE", "1"), "TP_SIZE", 1, 16)
    return ",".join(str(worker_id * tp_size + offset) for offset in range(tp_size))


def runtime_from_environment(values: dict[str, str]) -> dict[str, Any]:
    """从旧版全局配置提取单个部署可独立覆盖的运行参数。"""

    return {
        "image": values.get("VLLM_IMAGE", "vllm/vllm-openai:v0.22.1"),
        "tensor_parallel_size": bounded_int(values.get("TP_SIZE", "1"), "TP_SIZE", 1, 16),
        "max_model_len": bounded_int(values.get("MAX_MODEL_LEN", "32768"), "MAX_MODEL_LEN", 256, 10_000_000),
        "gpu_memory_utilization": bounded_float(values.get("GPU_MEMORY_UTILIZATION", "0.9"), "GPU_MEMORY_UTILIZATION", 0.1, 1.0),
        "max_num_seqs": bounded_int(values.get("MAX_NUM_SEQS", "1"), "MAX_NUM_SEQS", 1, 65536),
        "max_num_batched_tokens": bounded_int(values.get("MAX_NUM_BATCHED_TOKENS", "8192"), "MAX_NUM_BATCHED_TOKENS", 256, 10_000_000),
        "trust_remote_code": values.get("TRUST_REMOTE_CODE", "0") == "1",
        "supports_image_input": values.get("SUPPORTS_IMAGE_INPUT", "0") == "1",
        "supports_ocr": values.get("SUPPORTS_OCR", "0") == "1",
        "supports_tool_calling": values.get("SUPPORTS_TOOL_CALLING", "0") == "1",
        "supports_reasoning": values.get("SUPPORTS_REASONING", "0") == "1",
        "supports_thinking_toggle": values.get("SUPPORTS_THINKING_TOGGLE", "0") == "1",
        "tool_call_parser": values.get("TOOL_CALL_PARSER", ""),
        "reasoning_parser": values.get("REASONING_PARSER", ""),
        "mm_limit": values.get("MM_LIMIT", '{"image":4}'),
    }


def validate_runtime(runtime: dict[str, Any]) -> None:
    """校验单个部署的 vLLM 运行参数和能力开关。"""

    if not isinstance(runtime, dict) or not IMAGE_RE.fullmatch(str(runtime.get("image", ""))):
        raise ValueError("vLLM 镜像名称非法")
    bounded_int(runtime.get("tensor_parallel_size"), "TP_SIZE", 1, 16)
    bounded_int(runtime.get("max_model_len"), "最大上下文", 256, 10_000_000)
    bounded_float(runtime.get("gpu_memory_utilization"), "显存利用率", 0.1, 1.0)
    bounded_int(runtime.get("max_num_seqs"), "最大并发序列", 1, 65536)
    bounded_int(runtime.get("max_num_batched_tokens"), "批处理 Token", 256, 10_000_000)
    for name in ("tool_call_parser", "reasoning_parser"):
        value = str(runtime.get(name, ""))
        if len(value) > 100 or any(character.isspace() for character in value):
            raise ValueError(f"{name} 非法")


def validate_registry(payload: dict[str, Any], paths: Paths) -> None:
    """校验注册表跨对象约束、端口冲突和 GPU 独占关系。"""

    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("部署注册表版本不受支持")
    artifacts = payload.get("artifacts")
    deployments = payload.get("deployments")
    aliases = payload.get("legacy_aliases")
    if not isinstance(artifacts, dict) or not isinstance(deployments, dict) or not isinstance(aliases, dict):
        raise ValueError("部署注册表结构不完整")
    allowed_roots = [paths.model_root.resolve(strict=False)]
    cluster_model_root = parse_env_file(paths.cluster_env).get("MODEL_ROOT")
    if cluster_model_root:
        allowed_roots.append(pathlib.Path(cluster_model_root).resolve(strict=False))
    for artifact_id, artifact in artifacts.items():
        if not DEPLOYMENT_ID_RE.fullmatch(str(artifact_id)) or not isinstance(artifact, dict):
            raise ValueError("模型制品 ID 非法")
        if not MODEL_ID_RE.fullmatch(str(artifact.get("model_id", ""))):
            raise ValueError(f"模型 ID 非法：{artifact_id}")
        artifact_path = pathlib.Path(str(artifact.get("path", ""))).resolve(strict=False)
        if not artifact_path.is_absolute() or not any(
            artifact_path == root or root in artifact_path.parents for root in allowed_roots
        ):
            raise ValueError(f"模型制品路径必须位于模型根目录：{artifact_id}")
    used_workers: dict[int, str] = {}
    used_gpus: dict[int, str] = {}
    used_ports: dict[int, str] = {}
    used_public_ids: dict[str, str] = {}
    for deployment_id, deployment in deployments.items():
        if not DEPLOYMENT_ID_RE.fullmatch(str(deployment_id)) or not isinstance(deployment, dict):
            raise ValueError("部署 ID 非法")
        if deployment.get("artifact_id") not in artifacts:
            raise ValueError(f"部署引用了不存在的模型制品：{deployment_id}")
        if not MODEL_ID_RE.fullmatch(str(deployment.get("model_id", ""))):
            raise ValueError(f"部署模型 ID 非法：{deployment_id}")
        if not PUBLIC_ID_RE.fullmatch(str(deployment.get("served_model_name", ""))):
            raise ValueError(f"服务模型 ID 非法：{deployment_id}")
        served_aliases = deployment.get("served_model_aliases", [])
        if (
            not isinstance(served_aliases, list)
            or len(served_aliases) > 16
            or any(
                not PUBLIC_ID_RE.fullmatch(str(item))
                for item in served_aliases
            )
            or len(set(str(item) for item in served_aliases)) != len(served_aliases)
            or str(deployment.get("served_model_name")) in served_aliases
        ):
            raise ValueError(f"服务模型兼容别名非法：{deployment_id}")
        public_ids = deployment.get("public_model_ids", [])
        if not isinstance(public_ids, list) or not public_ids or any(
            not PUBLIC_ID_RE.fullmatch(str(item)) for item in public_ids
        ):
            raise ValueError(f"公开模型 ID 非法：{deployment_id}")
        if deployment.get("enabled", True):
            for public_id in public_ids:
                owner = used_public_ids.get(str(public_id))
                if owner and owner != deployment_id:
                    raise ValueError(f"公开模型 ID {public_id} 同时属于多个部署")
                used_public_ids[str(public_id)] = str(deployment_id)
        validate_runtime(deployment.get("runtime", {}))
        instances = deployment.get("instances")
        if not isinstance(instances, list) or not instances:
            raise ValueError(f"部署至少需要一个实例：{deployment_id}")
        tp_size = int(deployment["runtime"]["tensor_parallel_size"])
        for instance in instances:
            if not isinstance(instance, dict) or instance.get("kind") not in {"local", "remote"}:
                raise ValueError(f"实例类型非法：{deployment_id}")
            if instance.get("kind") == "remote":
                base_url = str(instance.get("base_url", ""))
                if not re.fullmatch(r"https?://[^\s]{1,500}", base_url):
                    raise ValueError(f"远程 Worker URL 非法：{deployment_id}")
                continue
            worker_id = bounded_int(instance.get("worker_id"), "Worker ID", 0, 255)
            port = bounded_int(instance.get("port"), "Worker 端口", 1024, 65535)
            gpus = parse_gpu_devices(instance.get("gpu_devices", []))
            if len(gpus) != tp_size:
                raise ValueError(f"实例 GPU 数必须等于 TP_SIZE：{deployment_id}")
            if not deployment.get("enabled", True) or not instance.get("enabled", True):
                continue
            if worker_id in used_workers:
                raise ValueError(f"Worker {worker_id} 同时属于多个部署")
            if port in used_ports:
                raise ValueError(f"端口 {port} 同时属于多个部署")
            used_workers[worker_id] = deployment_id
            used_ports[port] = deployment_id
            for gpu in gpus:
                if gpu in used_gpus:
                    raise ValueError(f"GPU {gpu} 同时属于多个部署")
                used_gpus[gpu] = deployment_id
    for alias, target in aliases.items():
        if not PUBLIC_ID_RE.fullmatch(str(alias)) or not PUBLIC_ID_RE.fullmatch(str(target)):
            raise ValueError("兼容别名非法")


class CommandRunner:
    """封装外部命令，便于测试故障路径且统一日志脱敏边界。"""

    def __init__(self, logger: Callable[[str], None] | None = None):
        """接收可选日志回调；命令参数不会自动写入日志。"""

        self.logger = logger or (lambda _message: None)

    def run(
        self,
        command: list[str],
        *,
        check: bool = True,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
        capture_output: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """执行参数数组形式的命令，并按需把输出逐行写入任务日志。"""

        result = subprocess.run(
            command,
            check=False,
            text=True,
            stdout=subprocess.PIPE if capture_output else None,
            stderr=subprocess.STDOUT if capture_output else None,
            timeout=timeout,
            env=env,
        )
        if result.stdout:
            for line in result.stdout.splitlines():
                self.logger(line[:2000])
        if check and result.returncode:
            lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
            detail = "；".join(lines[-3:])[-2000:]
            raise RuntimeError(
                f"命令执行失败，退出码 {result.returncode}"
                f"{f'：{detail}' if detail else ''}"
            )
        return result


class JobStore:
    """持久化后台部署任务、阶段、日志摘要和取消标志。"""

    def __init__(self, directory: pathlib.Path):
        """创建任务目录，任务文件默认仅 root 可读写。"""

        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = threading.RLock()

    def create(self, request: dict[str, Any], kind: str = "deploy") -> dict[str, Any]:
        """创建 waiting 状态任务并返回完整任务对象。"""

        job_id = str(uuid.uuid4())
        job = {
            "id": job_id,
            "state": "waiting",
            "phase": "waiting",
            "progress": 0,
            "message": "等待执行",
            "kind": kind,
            "request": request,
            "cancel_requested": False,
            "logs": [],
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }
        self.save(job)
        return job

    def save(self, job: dict[str, Any]) -> None:
        """原子保存任务状态并限制日志条数，防止状态文件无限增长。"""

        with self._lock:
            job["updated_at"] = utc_now()
            job["logs"] = list(job.get("logs", []))[-500:]
            atomic_write(
                self.directory / f"{job['id']}.json",
                json.dumps(job, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            )

    def get(self, job_id: str) -> dict[str, Any]:
        """读取指定任务；不存在时返回明确错误。"""

        try:
            return json.loads((self.directory / f"{uuid.UUID(job_id)}.json").read_text(encoding="utf-8"))
        except (ValueError, FileNotFoundError) as error:
            raise ValueError("部署任务不存在") from error

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        """按更新时间倒序返回最近任务。"""

        jobs: list[dict[str, Any]] = []
        for path in self.directory.glob("*.json"):
            with contextlib.suppress(OSError, ValueError):
                jobs.append(json.loads(path.read_text(encoding="utf-8")))
        return sorted(jobs, key=lambda item: item.get("updated_at", ""), reverse=True)[:limit]


class DeploymentManager:
    """执行计划、模型下载、局部 Worker 变更和失败回滚。"""

    def __init__(
        self,
        paths: Paths,
        runner: CommandRunner | None = None,
        upgrade_inspector: Callable[[str, str, str, int, float], dict[str, Any]]
        | None = None,
    ):
        """初始化注册表、任务存储、命令执行器和升级目录检查器。

        参数：
            paths: 控制服务允许访问的受管路径。
            runner: 可替换的外部命令执行器，用于故障测试和日志收集。
            upgrade_inspector: 可选的目录检查函数；生产默认调用固定的
                `model_catalog.py`，测试可注入无网络实现。
        """

        self.paths = paths
        self.registry = RegistryStore(paths)
        self.jobs = JobStore(paths.jobs_dir)
        self.runner = runner or CommandRunner()
        maintenance_runner = runner or CommandRunner()
        self.upgrade_inspector = upgrade_inspector or self._inspect_upgrade_target
        self._mutation_lock = threading.Lock()
        self._submission_lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}
        self.omniroute = OmniRouteMaintenanceManager(
            OmniRouteMaintenancePaths.from_control_paths(paths),
            maintenance_runner,
            active_model_job=self._has_active_model_job,
            submission_lock=self._submission_lock,
        )

    def _has_active_model_job(self) -> bool:
        """返回模型部署、升级、发布或回滚任务是否仍在执行。"""

        return any(
            item.get("state") not in TERMINAL_JOB_STATES
            for item in self.jobs.list(limit=200)
        )

    def _reject_omniroute_conflict(self) -> None:
        """阻止模型路由变更与 OmniRoute 维护同时提交。"""

        if self.omniroute.has_active_job():
            raise RuntimeError("OmniRoute 升级或 SQLite 维护任务正在运行")

    def recover_interrupted_jobs(self) -> None:
        """把守护进程重启前遗留的非终态任务标记为中断，避免永久阻塞新任务。"""

        for job in self.jobs.list(limit=200):
            if job.get("state") in TERMINAL_JOB_STATES:
                continue
            job.update(
                {
                    "state": "failed",
                    "phase": "interrupted",
                    "message": "模型控制服务在任务执行期间重启；请核对 Worker 与路由状态后重新提交",
                    "error": "controller_restarted",
                }
            )
            self.jobs.save(job)

    def migrate(self) -> dict[str, Any]:
        """持久化旧版单模型快照，但不改动任何服务或 Worker 文件。"""

        with self.registry.locked():
            payload = self.registry.read(persist_legacy=False)
            if not self.paths.registry.exists():
                self.registry.write(payload)
        return {"registry": str(self.paths.registry), "revision": payload["revision"]}

    def snapshot(self) -> dict[str, Any]:
        """返回注册表、任务和只读 GPU 清单，供 CLI 与管理后台展示。"""

        return {
            "version": APP_VERSION,
            "available": True,
            "registry": self.registry.read(),
            "jobs": self.jobs.list(),
            "gpus": gpu_inventory(self.runner),
            "gateway": gateway_capabilities(self.paths),
            "upgrade_profiles": upgrade_profiles(),
            "download_environment": self.download_environment(),
            "socket": str(self.paths.socket),
        }

    def download_environment(self) -> dict[str, Any]:
        """返回维护代理和 ModelScope 下载器的只读准备状态。

        返回：
            页面可安全展示的下载环境；代理地址不支持内嵌凭据。
        """

        saved = parse_env_file(self.paths.proxy_env)
        proxy_url = str(saved.get("MAINTENANCE_PROXY") or "").strip()
        no_proxy = str(
            saved.get("MAINTENANCE_NO_PROXY")
            or DEFAULT_MAINTENANCE_NO_PROXY
        ).strip()
        error = ""
        if proxy_url:
            try:
                proxy_url, no_proxy = validate_maintenance_proxy(
                    proxy_url, no_proxy
                )
            except ValueError as invalid:
                error = str(invalid)
                proxy_url = ""
        return {
            "maintenance_proxy": {
                "configured": bool(proxy_url),
                "proxy_url": proxy_url,
                "no_proxy": no_proxy,
                "error": error,
                "scope": "仅模型目录、依赖和权重下载；不注入 Router 或 Worker",
            },
            "modelscope": {
                "downloader_ready": self.paths.modelscope_downloader.is_file()
                and os.access(self.paths.modelscope_downloader, os.X_OK),
                "downloader_path": str(self.paths.modelscope_downloader),
                "auto_prepare": True,
                "version": MODELSCOPE_DOWNLOADER_VERSION,
            },
        }

    def _probe_download_proxy(self, proxy_url: str, hub: str) -> dict[str, Any]:
        """通过候选代理访问指定 Hub 的轻量元数据端点。

        参数：
            proxy_url: 已通过结构校验但尚未持久化的代理地址。
            hub: `huggingface` 或 `modelscope`，决定实际探测目标。

        返回：
            包含 Hub、代理地址和成功状态的页面结果。

        异常：
            ValueError: Hub 不受支持。
            RuntimeError: curl 不存在、超时或代理无法访问目标 Hub。
        """

        targets = {
            "huggingface": "https://huggingface.co/api/models?limit=1",
            "modelscope": "https://modelscope.cn/openapi/v1/models?page_size=1",
        }
        if hub not in targets:
            raise ValueError("代理测试目标只能是 Hugging Face 或 ModelScope")
        command = [
            "curl",
            "--proxy",
            proxy_url,
            "--noproxy",
            "",
            "--fail",
            "--silent",
            "--show-error",
            "--connect-timeout",
            "8",
            "--max-time",
            "20",
            "--output",
            "/dev/null",
            targets[hub],
        ]
        try:
            result = self.runner.run(command, check=False, timeout=25)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeError(f"代理测试无法执行：{error}") from error
        if result.returncode:
            detail = "；".join((result.stdout or "").strip().splitlines()[-3:])
            raise RuntimeError(
                f"代理无法访问 {hub}：{detail or f'curl 退出码 {result.returncode}'}"
            )
        return {"ok": True, "hub": hub, "proxy_url": proxy_url}

    def test_download_proxy(self, payload: dict[str, Any]) -> dict[str, Any]:
        """校验但不保存页面提交的维护代理，并执行真实 Hub 请求。"""

        proxy_url, no_proxy = validate_maintenance_proxy(
            str(payload.get("proxy_url") or ""),
            str(payload.get("no_proxy") or DEFAULT_MAINTENANCE_NO_PROXY),
        )
        result = self._probe_download_proxy(
            proxy_url, str(payload.get("hub") or "huggingface").lower()
        )
        return {**result, "no_proxy": no_proxy}

    def save_download_proxy(self, payload: dict[str, Any]) -> dict[str, Any]:
        """真实测试成功后原子保存维护代理，供后续目录和下载操作使用。"""

        tested = self.test_download_proxy(payload)
        atomic_write(
            self.paths.proxy_env,
            "MAINTENANCE_PROXY="
            f"{tested['proxy_url']}\nMAINTENANCE_NO_PROXY={tested['no_proxy']}\n",
            mode=0o600,
        )
        return self.download_environment()

    def clear_download_proxy(self) -> dict[str, Any]:
        """清除维护代理；推理服务和正在运行的 Worker 不会被修改。"""

        with contextlib.suppress(FileNotFoundError):
            self.paths.proxy_env.unlink()
        return self.download_environment()

    def _inspect_upgrade_target(
        self,
        hub: str,
        model_id: str,
        revision: str,
        max_model_len: int,
        gpu_memory_utilization: float,
    ) -> dict[str, Any]:
        """通过固定模型目录助手解析 SHA、能力和真实主机拓扑计划。

        参数：
            hub: 目标模型所在的 ModelScope 或 Hugging Face。
            model_id: Hub 上的目标模型身份。
            revision: 可选的不可变提交 SHA；空值由目录解析当前提交。
            max_model_len: 管理员希望保留的单请求上下文上限。
            gpu_memory_utilization: 来源部署已经使用的显存比例。

        返回：
            `model_catalog inspect --json` 的结构化结果。
        """

        command = [
            sys.executable,
            str(self.paths.catalog_helper),
            "--lang",
            "zh",
            "inspect",
            hub,
            model_id,
        ]
        if revision:
            command.append(revision)
        command.extend(
            [
                "--json",
                "--model-root",
                str(self.paths.model_root),
                "--gpu-memory-utilization",
                str(gpu_memory_utilization),
                "--max-model-len",
                str(max_model_len),
            ]
        )
        environment = maintenance_environment(self.paths.proxy_env)
        try:
            result = self.runner.run(
                command, check=False, timeout=90, env=environment
            )
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
            raise RuntimeError(f"无法完成目标模型目录检查：{error}") from error
        output = (result.stdout or "").strip()
        try:
            payload = json.loads(output)
        except json.JSONDecodeError as error:
            if result.returncode:
                detail = "；".join(output.splitlines()[-4:])[-2000:]
                raise RuntimeError(
                    "无法完成目标模型目录检查："
                    f"{detail or f'目录命令退出码 {result.returncode}'}"
                ) from error
            raise RuntimeError("目标模型目录返回了无效 JSON") from error
        if not isinstance(payload, dict):
            raise RuntimeError("目标模型目录返回结构无效")
        return payload

    def plan(self, request: dict[str, Any]) -> dict[str, Any]:
        """校验部署请求并返回受影响 Worker、GPU 和兼容别名计划。"""

        normalized = normalize_request(request, self.paths)
        current = self.registry.read()
        candidate, affected = merge_deployment(current, normalized)
        validate_registry(candidate, self.paths)
        gateway = gateway_capabilities(self.paths)
        publish_requested = bool(normalized["deployment"].get("publish_requested", True))
        if publish_requested and not gateway["registry_publish"]:
            raise ValueError(
                f"当前 AI 接入层 {gateway['kind']} 不支持 LLMCtl 多模型自动发布；"
                "请改用 OmniRoute，或取消“部署后同步”并仅使用空闲 Worker"
            )
        occupied_workers = set(local_instances(current))
        if (
            not gateway["registry_publish"]
            and not publish_requested
            and bool(occupied_workers & affected)
        ):
            raise ValueError(
                f"当前 AI 接入层 {gateway['kind']} 无法安全同步现有 Worker 的重新分配；"
                "未发布部署只能使用尚未登记的 Worker，或接入远程实例"
            )
        inventory = gpu_inventory(self.runner)
        known_gpu_ids = {int(item["id"]) for item in inventory}
        requested_gpu_ids = {
            gpu
            for item in normalized["deployment"]["instances"]
            if item["kind"] == "local"
            for gpu in item["gpu_devices"]
        }
        if known_gpu_ids and not requested_gpu_ids.issubset(known_gpu_ids):
            missing = sorted(requested_gpu_ids - known_gpu_ids)
            raise ValueError(f"本机不存在 GPU：{missing}")
        return {
            "normalized_request": normalized,
            "affected_worker_ids": sorted(affected),
            "requested_gpu_ids": sorted(requested_gpu_ids),
            "public_model_ids": normalized["deployment"]["public_model_ids"],
            "legacy_aliases": candidate.get("legacy_aliases", {}),
            "download_required": not pathlib.Path(normalized["artifact"]["path"]).is_dir(),
            "warnings": plan_warnings(current, candidate, normalized),
            "gateway": gateway,
        }

    def plan_upgrade(self, payload: dict[str, Any]) -> dict[str, Any]:
        """为 Ornith 原地升级生成固定 revision、目标拓扑和回退说明。

        参数：
            payload: 来源部署、目标模型、可选目标 SHA 和上下文上限。

        返回：
            标准部署计划，以及当前/目标版本、资源变化和回退条件。

        该方法只读访问注册表、GPU 和模型目录，不下载权重或修改服务。
        """

        if not isinstance(payload, dict):
            raise ValueError("升级请求必须是 JSON 对象")
        current = self.registry.read()
        source = select_source_deployment(
            current, str(payload.get("source_deployment_id") or "").strip()
        )
        normalized_payload = dict(payload)
        if not str(normalized_payload.get("target_hub") or "").strip():
            source_artifact = current.get("artifacts", {}).get(
                source.get("artifact_id"), {}
            )
            source_hub = str(source_artifact.get("hub") or "").strip().lower()
            normalized_payload["target_hub"] = (
                source_hub
                if source_hub in {"huggingface", "modelscope"}
                else "modelscope"
            )
        target = requested_upgrade_target(source, normalized_payload)
        catalog = self.upgrade_inspector(
            target["hub"],
            target["model_id"],
            target["revision"],
            target["max_model_len"],
            target["gpu_memory_utilization"],
        )
        request, summary = build_upgrade_request(
            current, source, normalized_payload, catalog
        )
        plan = self.plan(request)
        if int(self.registry.read().get("revision", 0)) != int(
            current.get("revision", 0)
        ):
            raise ValueError("升级检查期间部署注册表发生变化，请重新生成计划")
        plan["source_registry_revision"] = int(current.get("revision", 0))
        plan["upgrade"] = summary
        plan["catalog"] = {
            "id": catalog.get("id"),
            "revision": catalog.get("revision"),
            "weight_bytes": int(catalog.get("weight_bytes") or 0),
            "architectures": list(catalog.get("supported_architectures") or []),
            "capabilities": dict(catalog.get("capabilities") or {}),
            "plan": dict(catalog.get("plan") or {}),
        }
        plan["warnings"] = [
            "升级会重新加载全部受影响 Worker；提交前请安排维护窗口。",
            "旧模型权重会保留，成功任务可从页面或 llmctl 回退到升级前快照。",
            *plan.get("warnings", []),
        ]
        return plan

    def submit_upgrade(self, payload: dict[str, Any]) -> dict[str, Any]:
        """提交已经确认且注册表版本未变化的 Ornith 升级任务。

        参数：
            payload: 与计划阶段相同的目标参数，并包含
                `expected_registry_revision`。

        返回：
            已持久化的 waiting 状态升级任务。
        """

        with self._submission_lock:
            self._reject_omniroute_conflict()
            if any(
                item.get("state") not in TERMINAL_JOB_STATES
                for item in self.jobs.list(limit=200)
            ):
                raise RuntimeError("已有模型部署或升级任务正在运行")
            try:
                expected_revision = int(payload.get("expected_registry_revision"))
            except (TypeError, ValueError) as error:
                raise ValueError("升级提交缺少计划返回的注册表版本") from error
            current_revision = int(self.registry.read().get("revision", 0))
            if expected_revision != current_revision:
                raise ValueError("部署注册表已变化，请重新生成升级计划")
            plan = self.plan_upgrade(payload)
            if int(plan["source_registry_revision"]) != expected_revision:
                raise ValueError("升级检查期间部署注册表发生变化，请重新生成计划")
            job = self.jobs.create(plan["normalized_request"], kind="upgrade")
            job["upgrade"] = plan["upgrade"]
            job["source_registry_revision"] = expected_revision
            self.jobs.save(job)
            thread = threading.Thread(
                target=self._run_job, args=(job["id"],), daemon=True
            )
            self._threads[job["id"]] = thread
            thread.start()
        return job

    def submit(self, request: dict[str, Any]) -> dict[str, Any]:
        """提交后台部署任务；同一时间只允许一个 GPU 变更任务。"""

        with self._submission_lock:
            self._reject_omniroute_conflict()
            plan = self.plan(request)
            if any(
                item.get("state") not in TERMINAL_JOB_STATES
                for item in self.jobs.list(limit=200)
            ):
                raise RuntimeError("已有模型部署任务正在运行")
            job = self.jobs.create(plan["normalized_request"])
            thread = threading.Thread(target=self._run_job, args=(job["id"],), daemon=True)
            self._threads[job["id"]] = thread
            thread.start()
        return job

    def publish(self) -> dict[str, Any]:
        """创建仅同步当前注册表到 AI 接入层的后台恢复任务。

        返回：
            已持久化的 waiting 任务。该任务不下载权重、不修改注册表，也不
            停止 Worker；失败后可安全重试。

        异常：
            RuntimeError: 已有非终态模型任务。
            ValueError: 当前注册表没有可发布部署或接入层不支持注册表同步。
        """

        with self._submission_lock:
            self._reject_omniroute_conflict()
            if any(
                item.get("state") not in TERMINAL_JOB_STATES
                for item in self.jobs.list(limit=200)
            ):
                raise RuntimeError("已有模型部署、升级或发布任务正在运行")
            registry = self.registry.read()
            if not any(
                deployment.get("enabled", True)
                and deployment.get("publish_requested", True)
                for deployment in registry.get("deployments", {}).values()
            ):
                raise ValueError("当前注册表没有可发布部署")
            if not gateway_capabilities(self.paths)["registry_publish"]:
                raise ValueError("当前 AI 接入层不支持注册表发布")
            job = self.jobs.create(
                {"registry_revision": int(registry.get("revision", 0))},
                kind="publish",
            )
            thread = threading.Thread(
                target=self._run_publish_job, args=(job["id"],), daemon=True
            )
            self._threads[job["id"]] = thread
            thread.start()
        return job

    def cancel(self, job_id: str) -> dict[str, Any]:
        """请求取消任务；已进入配置提交阶段时会先完成安全回滚。"""

        job = self.jobs.get(job_id)
        if job["state"] in TERMINAL_JOB_STATES:
            return job
        job["cancel_requested"] = True
        job["message"] = "已请求取消；将在安全检查点停止"
        self.jobs.save(job)
        return job

    def rollback(self, source_job_id: str) -> dict[str, Any]:
        """把运行配置恢复到指定成功任务执行前的快照。"""

        with self._submission_lock:
            self._reject_omniroute_conflict()
            if any(
                item.get("state") not in TERMINAL_JOB_STATES
                for item in self.jobs.list(limit=200)
            ):
                raise RuntimeError("已有模型部署或回滚任务正在运行")
            source_job = self.jobs.get(source_job_id)
            if source_job.get("state") != "succeeded" or not source_job.get("backup"):
                raise ValueError("只能回滚已成功且包含运行快照的部署任务")
            source_backup = self._validated_backup(pathlib.Path(str(source_job["backup"])))
            job = self.jobs.create(
                {
                    "source_job_id": source_job_id,
                    "source_backup": str(source_backup),
                },
                kind="rollback",
            )
            thread = threading.Thread(
                target=self._run_rollback_job, args=(job["id"],), daemon=True
            )
            self._threads[job["id"]] = thread
            thread.start()
        return job

    def _update_job(
        self, job: dict[str, Any], phase: str, progress: int, message: str, log: str = ""
    ) -> None:
        """更新阶段、百分比和审计日志，并在安全检查点处理取消。"""

        latest = self.jobs.get(job["id"])
        job["cancel_requested"] = bool(latest.get("cancel_requested"))
        if job["cancel_requested"]:
            raise InterruptedError("任务已由管理员取消")
        job.update({"state": "running", "phase": phase, "progress": progress, "message": message})
        if log:
            job.setdefault("logs", []).append({"time": utc_now(), "message": log[:2000]})
        self.jobs.save(job)

    def _run_job(self, job_id: str) -> None:
        """在后台线程中依次完成下载、验证、局部部署、测试和发布。"""

        job = self.jobs.get(job_id)
        backup: pathlib.Path | None = None
        acquired = False
        try:
            acquired = self._mutation_lock.acquire(blocking=False)
            if not acquired:
                raise RuntimeError("另一个部署任务持有变更锁")
            request = job["request"]
            self._update_job(job, "preflight", 5, "检查硬件、端口和当前部署")
            artifact = request["artifact"]
            if not pathlib.Path(artifact["path"]).is_dir():
                self._update_job(job, "downloading", 15, "下载模型权重；支持复用已完成目录")
                self._download_artifact(artifact, request["deployment"]["runtime"], job)
            self._update_job(job, "verifying", 45, "验证模型目录、配置和权重文件")
            verify_artifact(pathlib.Path(artifact["path"]))
            artifact["status"] = "ready"
            self._update_job(job, "backing_up", 52, "备份注册表和受影响 Worker 配置")
            current = self.registry.read()
            candidate, affected = merge_deployment(current, request)
            backup = self._backup_runtime(job_id, affected)
            self._update_job(job, "deploying", 60, "写入每实例配置并仅停止受影响 Worker")
            self._apply_candidate(candidate, affected, request["deployment"]["id"])
            self._update_job(job, "starting", 72, "启动受影响 Worker 并等待健康")
            self._start_and_wait(candidate, affected)
            is_upgrade = job.get("kind") == "upgrade"
            if is_upgrade:
                # 公开别名切换前必须让每个目标实例完成一次真实生成；仅有健康
                # 端点不能证明量化内核、模板和推理路径可用。
                self._update_job(
                    job,
                    "testing",
                    84,
                    "公开切换前逐实例执行真实文本生成；每个实例最多等待 60 秒",
                )
                self._verify_instances(
                    candidate,
                    request["deployment"]["id"],
                    inference=True,
                    job=job,
                )
            gateway = gateway_capabilities(self.paths)
            if gateway["registry_publish"]:
                self._update_job(job, "publishing", 92, "同步多模型路由到当前 AI 接入层")
                self._reconcile_gateway()
            else:
                self._update_job(
                    job,
                    "publishing",
                    88,
                    f"当前 AI 接入层 {gateway['kind']} 不支持注册表同步；已按计划跳过发布",
                )
            if not is_upgrade:
                self._update_job(job, "testing", 95, "逐实例执行模型列表和健康检查")
                self._verify_instances(candidate, request["deployment"]["id"])
            job.update(
                {
                    "state": "succeeded",
                    "phase": "succeeded",
                    "progress": 100,
                    "message": (
                        "升级完成；旧模型权重和升级前回退点均已保留"
                        if is_upgrade
                        else "部署完成；公开模型可在门户中配置定价和授权"
                    ),
                    "backup": str(backup),
                }
            )
            self.jobs.save(job)
        except InterruptedError as error:
            if backup:
                self._restore_runtime(backup, wait_for_health=True)
            job.update(
                {
                    "state": "cancelled" if not backup else "rolled_back",
                    "phase": "cancelled",
                    "message": str(error),
                }
            )
            self.jobs.save(job)
        except Exception as error:
            rollback_error = ""
            if backup:
                try:
                    self._restore_runtime(backup, wait_for_health=True)
                except Exception as rollback_exception:
                    rollback_error = f"；自动回滚失败：{rollback_exception}"
            job.update(
                {
                    "state": "failed" if not backup else "rolled_back",
                    "phase": "failed",
                    "message": (
                        f"升级失败：{error}{rollback_error}"
                        if job.get("kind") == "upgrade"
                        else f"部署失败：{error}{rollback_error}"
                    ),
                    "error": str(error)[:4000],
                }
            )
            self.jobs.save(job)
        finally:
            if acquired:
                self._mutation_lock.release()
            self._threads.pop(job_id, None)

    def _run_rollback_job(self, job_id: str) -> None:
        """执行显式回滚，并在回滚失败时恢复回滚前的安全快照。"""

        job = self.jobs.get(job_id)
        safety_backup: pathlib.Path | None = None
        acquired = False
        try:
            acquired = self._mutation_lock.acquire(blocking=False)
            if not acquired:
                raise RuntimeError("另一个部署任务持有变更锁")
            source_backup = self._validated_backup(
                pathlib.Path(str(job["request"]["source_backup"]))
            )
            manifest = json.loads(
                (source_backup / "manifest.json").read_text(encoding="utf-8")
            )
            affected = {int(item) for item in manifest.get("affected_worker_ids", [])}
            if not affected:
                raise ValueError("回滚快照没有受影响 Worker，拒绝执行")
            self._update_job(job, "backing_up", 15, "备份当前运行配置作为回滚保护点")
            safety_backup = self._backup_runtime(job_id, affected)
            self._update_job(job, "restoring", 40, "恢复目标注册表和受影响 Worker 配置")
            self._restore_runtime(source_backup, wait_for_health=True)
            self._update_job(job, "testing", 85, "检查回滚后的 Worker 和 AI 接入层")
            self._wait_worker_ids(affected)
            job.update(
                {
                    "state": "succeeded",
                    "phase": "succeeded",
                    "progress": 100,
                    "message": "回滚完成；仅目标快照涉及的 Worker 被重启",
                    "backup": str(safety_backup),
                    "restored_backup": str(source_backup),
                }
            )
            self.jobs.save(job)
        except Exception as error:
            recovery_error = ""
            if safety_backup:
                try:
                    self._restore_runtime(safety_backup, wait_for_health=True)
                except Exception as recovery_exception:
                    recovery_error = f"；恢复回滚保护点失败：{recovery_exception}"
            job.update(
                {
                    "state": "failed" if not safety_backup else "rolled_back",
                    "phase": "failed",
                    "message": f"回滚失败：{error}{recovery_error}",
                    "error": str(error)[:4000],
                }
            )
            self.jobs.save(job)
        finally:
            if acquired:
                self._mutation_lock.release()
            self._threads.pop(job_id, None)

    def _run_publish_job(self, job_id: str) -> None:
        """幂等重试当前注册表发布，不触碰模型和 Worker 生命周期。"""

        job = self.jobs.get(job_id)
        acquired = False
        try:
            acquired = self._mutation_lock.acquire(blocking=False)
            if not acquired:
                raise RuntimeError("另一个模型任务持有变更锁")
            self._update_job(
                job, "publishing", 50, "根据当前部署注册表重新同步 OmniRoute"
            )
            self._reconcile_gateway()
            job.update(
                {
                    "state": "succeeded",
                    "phase": "succeeded",
                    "progress": 100,
                    "message": "OmniRoute 已与当前部署注册表同步；Worker 未重启",
                }
            )
            self.jobs.save(job)
        except Exception as error:
            job.update(
                {
                    "state": "failed",
                    "phase": "failed",
                    "message": f"OmniRoute 发布重试失败：{error}",
                    "error": str(error)[:4000],
                }
            )
            self.jobs.save(job)
        finally:
            if acquired:
                self._mutation_lock.release()
            self._threads.pop(job_id, None)

    def _ensure_modelscope_downloader(
        self, runner: CommandRunner, environment: dict[str, str]
    ) -> pathlib.Path:
        """核验并按需准备固定版本的 ModelScope 独立下载器。

        参数：
            runner: 把安装输出写入当前后台任务的受控命令执行器。
            environment: 包含可选维护代理的显式下载环境。

        返回：
            已核验且支持 `download` 子命令的固定 CLI 路径。

        异常：
            RuntimeError: Python venv、固定依赖安装或 CLI 验证失败。
        """

        downloader = self.paths.modelscope_downloader
        venv = downloader.parent.parent
        python = venv / "bin/python"
        pip = venv / "bin/pip"
        version_check = [
            str(python),
            "-c",
            "import importlib.metadata;"
            "raise SystemExit(0 if importlib.metadata.version('modelscope-hub')"
            f" == '{MODELSCOPE_DOWNLOADER_VERSION}' else 1)",
        ]
        if (
            python.is_file()
            and os.access(python, os.X_OK)
            and downloader.is_file()
            and os.access(downloader, os.X_OK)
        ):
            version = runner.run(
                version_check, check=False, timeout=30, env=environment
            )
            help_result = runner.run(
                [str(downloader), "download", "--help"],
                check=False,
                timeout=30,
                env=environment,
            )
            if version.returncode == 0 and help_result.returncode == 0:
                return downloader
        try:
            runner.run(
                [sys.executable, "-m", "venv", str(venv)],
                timeout=120,
                env=environment,
            )
            runner.run(
                [
                    str(pip),
                    "install",
                    "--disable-pip-version-check",
                    "--upgrade",
                    "--force-reinstall",
                    f"modelscope-hub=={MODELSCOPE_DOWNLOADER_VERSION}",
                ],
                timeout=600,
                env=environment,
            )
            runner.run(version_check, timeout=30, env=environment)
            runner.run(
                [str(downloader), "download", "--help"],
                timeout=30,
                env=environment,
            )
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
            raise RuntimeError(
                "ModelScope 下载器自动准备失败："
                f"{error}；请确认已安装 python3-venv，并检查维护代理"
            ) from error
        return downloader

    def _download_artifact(
        self, artifact: dict[str, Any], runtime: dict[str, Any], job: dict[str, Any]
    ) -> None:
        """下载 Hugging Face 或 ModelScope 模型到不可变目录。"""

        destination = pathlib.Path(artifact["path"])
        partial = destination.with_name(destination.name + ".partial")
        partial.mkdir(parents=True, exist_ok=True)
        environment = maintenance_environment(self.paths.proxy_env)
        hub = artifact["hub"]
        model_id = artifact["model_id"]
        revision = artifact["revision"]
        logger = lambda line: self._append_job_log(job, line)
        runner = CommandRunner(logger)
        if hub == "modelscope":
            downloader = self._ensure_modelscope_downloader(runner, environment)
            command = [
                str(downloader),
                "download",
                model_id,
                "--revision",
                revision,
                "--local-dir",
                str(partial),
                "--max-workers",
                "8",
            ]
        elif hub == "huggingface":
            script = (
                "from huggingface_hub import snapshot_download;"
                "import sys;"
                "snapshot_download(repo_id=sys.argv[1],revision=sys.argv[2],"
                "local_dir=sys.argv[3],resume_download=True)"
            )
            command = [
                "/usr/bin/docker",
                "run",
                "--rm",
            ]
            for name in (
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "NO_PROXY",
                "http_proxy",
                "https_proxy",
                "no_proxy",
            ):
                if environment.get(name):
                    command.extend(["-e", name])
            command.extend([
                "--entrypoint",
                "python3",
                "-v",
                f"{partial}:/download",
                str(runtime["image"]),
                "-c",
                script,
                model_id,
                revision,
                "/download",
            ])
        else:
            raise ValueError("远程下载只支持 huggingface 或 modelscope")
        runner.run(command, timeout=24 * 60 * 60, env=environment)
        verify_artifact(partial)
        if destination.exists():
            shutil.rmtree(partial)
            return
        os.replace(partial, destination)

    def _append_job_log(self, job: dict[str, Any], line: str) -> None:
        """把下载器输出追加到任务日志，但不记录命令和环境密钥。"""

        latest = self.jobs.get(job["id"])
        latest.setdefault("logs", []).append({"time": utc_now(), "message": line[:2000]})
        self.jobs.save(latest)

    def _backup_runtime(self, job_id: str, affected: set[int]) -> pathlib.Path:
        """创建可独立恢复的注册表、全局环境和 Worker 文件快照。"""

        timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = self.paths.backups_dir / f"{timestamp}-{job_id}"
        backup.mkdir(parents=True, mode=0o700)
        manifest = {"affected_worker_ids": sorted(affected), "active_before": active_systemd_workers()}
        for source, name in (
            (self.paths.registry, "deployments.json"),
            (self.paths.cluster_env, "cluster.env"),
        ):
            if source.exists():
                shutil.copy2(source, backup / name)
        workers = backup / "workers"
        workers.mkdir(mode=0o700)
        for worker_id in affected:
            source = self.paths.workers_dir / f"{worker_id}.env"
            if source.exists():
                shutil.copy2(source, workers / source.name)
        atomic_write(
            backup / "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        )
        return backup

    def _apply_candidate(
        self, candidate: dict[str, Any], affected: set[int], deployment_id: str
    ) -> None:
        """停止受影响槽位，提交注册表并生成每实例独立运行参数。"""

        for worker_id in sorted(affected):
            self.runner.run(
                ["systemctl", "stop", f"llm-worker@{worker_id}.service"], check=False
            )
        desired = local_instances(candidate)
        self.paths.workers_dir.mkdir(parents=True, exist_ok=True)
        for worker_id in affected:
            instance_record = desired.get(worker_id)
            destination = self.paths.workers_dir / f"{worker_id}.env"
            if instance_record is None:
                with contextlib.suppress(FileNotFoundError):
                    destination.unlink()
                self.runner.run(
                    ["systemctl", "disable", f"llm-worker@{worker_id}.service"], check=False
                )
                continue
            deployment, instance = instance_record
            artifact = candidate["artifacts"][deployment["artifact_id"]]
            atomic_write(destination, render_env(worker_environment(deployment, instance, artifact)))
        candidate["revision"] = int(candidate.get("revision", 0)) + 1
        candidate["deployments"][deployment_id]["status"] = "starting"
        self.registry.write(candidate)
        update_env_assignments(
            self.paths.cluster_env,
            {"ACTIVE_WORKERS": ",".join(str(item) for item in sorted(desired))},
        )

    def _start_and_wait(self, candidate: dict[str, Any], affected: set[int]) -> None:
        """启动候选配置中的受影响 Worker，并等待每个端点健康。"""

        desired = local_instances(candidate)
        for worker_id in sorted(affected):
            if worker_id not in desired:
                continue
            self.runner.run(["systemctl", "enable", f"llm-worker@{worker_id}.service"])
            self.runner.run(["systemctl", "start", f"llm-worker@{worker_id}.service"])
        deadline = time.monotonic() + bounded_int(
            os.environ.get("LLM_MODEL_START_TIMEOUT", "1800"), "启动超时", 30, 7200
        )
        pending = {worker_id for worker_id in affected if worker_id in desired}
        while pending and time.monotonic() < deadline:
            for worker_id in list(pending):
                _deployment, instance = desired[worker_id]
                if endpoint_healthy(f"http://127.0.0.1:{instance['port']}", self.paths.secrets_env):
                    pending.remove(worker_id)
            if pending:
                time.sleep(3)
        if pending:
            raise RuntimeError(f"Worker 健康检查超时：{sorted(pending)}")
        for deployment in candidate["deployments"].values():
            deployment["status"] = "running" if deployment.get("enabled") else "disabled"
            deployment["updated_at"] = utc_now()
        self.registry.write(candidate)

    def _reconcile_gateway(self) -> None:
        """调用受信任网关助手同步整个注册表，不暴露后台密钥。"""

        cluster = parse_env_file(self.paths.cluster_env)
        gateway = cluster.get("GATEWAY_KIND", "litellm")
        if gateway != "omniroute":
            raise RuntimeError("当前多模型在线发布首先支持 OmniRoute；其他网关不会被静默改写")
        environment = os.environ.copy()
        if not str(environment.get("GATEWAY_LOCAL_URL") or "").strip():
            # 交互式 llmctl 会临时导出此变量，但 systemd 模型控制服务只读取
            # cluster.env。根据同一配置中的内部端口构造回环地址，保证后台任务
            # 与命令行使用完全相同的 OmniRoute 管理入口。
            port = bounded_int(
                cluster.get("GATEWAY_INTERNAL_PORT", cluster.get("API_PORT", "8000")),
                "OmniRoute 内部端口",
                1,
                65535,
            )
            environment["GATEWAY_LOCAL_URL"] = f"http://127.0.0.1:{port}"
        self.runner.run(
            [
                str(self.paths.gateway_helper),
                "reconcile-omniroute-registry",
                "--registry",
                str(self.paths.registry),
                "--secrets-file",
                str(self.paths.secrets_env),
            ],
            timeout=180,
            env=environment,
        )

    def _verify_instances(
        self,
        candidate: dict[str, Any],
        deployment_id: str,
        inference: bool = False,
        job: dict[str, Any] | None = None,
    ) -> None:
        """验证目标部署的健康端点，并按需执行真实文本生成。

        参数：
            candidate: 已写入 Worker 配置的候选注册表。
            deployment_id: 本次需要验收的目标部署 ID。
            inference: 为真时要求每个实例完成一次 Chat Completions 生成。
            job: 可选的后台任务；提供时逐实例保存阶段进度和验收日志。
        """

        deployment = candidate["deployments"][deployment_id]
        failures: list[str] = []
        instances = [
            instance
            for instance in deployment["instances"]
            if instance.get("enabled", True)
        ]
        total = len(instances)
        inference_timeout = bounded_int(
            os.environ.get("LLM_MODEL_INFERENCE_PROBE_TIMEOUT", "60"),
            "真实生成探测超时",
            10,
            300,
        )
        for index, instance in enumerate(instances, start=1):
            instance_id = str(instance["id"])
            if inference and job:
                progress = 84 + ((index - 1) * 7 // max(total, 1))
                self._update_job(
                    job,
                    "testing",
                    progress,
                    f"真实生成验收 {index}/{total}：{instance_id}（最多等待 {inference_timeout} 秒）",
                )
            origin = (
                str(instance["base_url"]).removesuffix("/v1").rstrip("/")
                if instance["kind"] == "remote"
                else f"http://127.0.0.1:{instance['port']}"
            )
            if not endpoint_healthy(origin, self.paths.secrets_env):
                failures.append(instance_id)
                if inference and job:
                    self._update_job(
                        job,
                        "testing",
                        84 + (index * 7 // max(total, 1)),
                        f"真实生成验收 {index}/{total}：{instance_id} 健康检查失败",
                        log=f"实例 {instance_id} 健康检查失败",
                    )
                continue
            if inference and not endpoint_inference_ready(
                origin,
                self.paths.secrets_env,
                str(deployment["served_model_name"]),
                timeout=inference_timeout,
                detail=(probe_detail := []),
            ):
                reason = probe_detail[0] if probe_detail else "未知响应"
                failures.append(f"{instance_id}:inference:{reason}")
                if job:
                    self._update_job(
                        job,
                        "testing",
                        84 + (index * 7 // max(total, 1)),
                        f"真实生成验收 {index}/{total}：{instance_id} 失败（{reason}）",
                        log=f"实例 {instance_id} 真实生成失败：{reason}",
                    )
                continue
            if inference and job:
                self._update_job(
                    job,
                    "testing",
                    84 + (index * 7 // max(total, 1)),
                    f"真实生成验收 {index}/{total}：{instance_id} 已通过",
                    log=f"实例 {instance_id} 真实生成通过",
                )
        if failures:
            raise RuntimeError(f"实例验收失败：{failures}")

    def _validated_backup(self, backup: pathlib.Path) -> pathlib.Path:
        """确认快照位于受管备份目录内且包含完整清单。"""

        root = self.paths.backups_dir.resolve()
        resolved = backup.resolve()
        if resolved == root or root not in resolved.parents:
            raise ValueError("回滚快照不在 LLMCtl 受管备份目录内")
        if not resolved.is_dir() or not (resolved / "manifest.json").is_file():
            raise ValueError("回滚快照不存在或缺少 manifest.json")
        return resolved

    def _wait_worker_ids(self, worker_ids: set[int]) -> None:
        """等待指定本机 Worker 恢复健康，未配置的槽位会被忽略。"""

        current = self.registry.read()
        desired = local_instances(current)
        pending = {worker_id for worker_id in worker_ids if worker_id in desired}
        deadline = time.monotonic() + bounded_int(
            os.environ.get("LLM_MODEL_START_TIMEOUT", "1800"), "启动超时", 30, 7200
        )
        while pending and time.monotonic() < deadline:
            for worker_id in list(pending):
                _deployment, instance = desired[worker_id]
                if endpoint_healthy(
                    f"http://127.0.0.1:{instance['port']}", self.paths.secrets_env
                ):
                    pending.remove(worker_id)
            if pending:
                time.sleep(3)
        if pending:
            raise RuntimeError(f"回滚后 Worker 健康检查超时：{sorted(pending)}")

    def _restore_runtime(self, backup: pathlib.Path, wait_for_health: bool = False) -> None:
        """恢复失败任务前的注册表、环境和受影响 Worker 运行状态。"""

        backup = self._validated_backup(backup)
        manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
        affected = {int(item) for item in manifest["affected_worker_ids"]}
        for worker_id in sorted(affected):
            self.runner.run(
                ["systemctl", "stop", f"llm-worker@{worker_id}.service"], check=False
            )
        for source_name, destination in (
            ("deployments.json", self.paths.registry),
            ("cluster.env", self.paths.cluster_env),
        ):
            source = backup / source_name
            if source.exists():
                shutil.copy2(source, destination)
            elif destination.exists() and source_name == "deployments.json":
                destination.unlink()
        for worker_id in affected:
            source = backup / "workers" / f"{worker_id}.env"
            destination = self.paths.workers_dir / f"{worker_id}.env"
            if source.exists():
                shutil.copy2(source, destination)
            else:
                with contextlib.suppress(FileNotFoundError):
                    destination.unlink()
        active_before = {int(item) for item in manifest.get("active_before", [])}
        for worker_id in sorted(affected & active_before):
            self.runner.run(["systemctl", "start", f"llm-worker@{worker_id}.service"])
        if wait_for_health:
            # 旧模型真实恢复健康后再把公开路由指回去，避免回退窗口把流量发送
            # 到仍在加载权重的 Worker。
            self._wait_worker_ids(affected & active_before)
        if gateway_capabilities(self.paths)["registry_publish"]:
            self._reconcile_gateway()


def normalize_request(request: dict[str, Any], paths: Paths) -> dict[str, Any]:
    """把 API 输入转换为稳定部署对象，并计算不可变制品目录。"""

    if not isinstance(request, dict):
        raise ValueError("部署请求必须是 JSON 对象")
    deployment_id = str(request.get("deployment_id", "")).strip().lower()
    if not DEPLOYMENT_ID_RE.fullmatch(deployment_id):
        raise ValueError("部署 ID 只能使用小写字母、数字和短横线")
    hub = str(request.get("hub", "huggingface")).strip().lower()
    if hub not in {"huggingface", "modelscope", "local"}:
        raise ValueError("模型来源必须是 huggingface、modelscope 或 local")
    model_id = str(request.get("model_id", "")).strip()
    if not MODEL_ID_RE.fullmatch(model_id):
        raise ValueError("模型 ID 非法")
    revision = str(request.get("revision", "main" if hub == "huggingface" else "master")).strip()
    if not revision or len(revision) > 200 or any(character.isspace() for character in revision):
        raise ValueError("模型 revision 非法")
    public_id = str(request.get("public_model_id", deployment_id)).strip()
    if not PUBLIC_ID_RE.fullmatch(public_id):
        raise ValueError("公开模型 ID 非法")
    served = str(request.get("served_model_name", public_id)).strip()
    if not PUBLIC_ID_RE.fullmatch(served):
        raise ValueError("服务模型 ID 非法")
    raw_served_aliases = request.get("served_model_aliases", [])
    if not isinstance(raw_served_aliases, list):
        raise ValueError("服务模型兼容别名必须是列表")
    served_aliases = list(
        dict.fromkeys(str(item).strip() for item in raw_served_aliases)
    )
    if (
        len(served_aliases) > 16
        or any(not PUBLIC_ID_RE.fullmatch(item) for item in served_aliases)
        or served in served_aliases
    ):
        raise ValueError("服务模型兼容别名非法")
    artifact_hash = hashlib.sha256(f"{hub}\0{model_id}\0{revision}".encode()).hexdigest()[:12]
    artifact_id = f"artifact-{artifact_hash}"
    if hub == "local":
        artifact_path = pathlib.Path(str(request.get("artifact_path", ""))).resolve(strict=False)
    else:
        artifact_path = paths.model_root / "artifacts" / f"{model_id.replace('/', '--')}-{artifact_hash}"
    instances = normalize_instances(request.get("instances", []))
    runtime = {
        "image": str(request.get("image", "vllm/vllm-openai:v0.22.1")),
        "tensor_parallel_size": bounded_int(request.get("tensor_parallel_size", 1), "TP_SIZE", 1, 16),
        "max_model_len": bounded_int(request.get("max_model_len", 32768), "最大上下文", 256, 10_000_000),
        "gpu_memory_utilization": bounded_float(request.get("gpu_memory_utilization", 0.9), "显存利用率", 0.1, 1.0),
        "max_num_seqs": bounded_int(request.get("max_num_seqs", 1), "最大并发序列", 1, 65536),
        "max_num_batched_tokens": bounded_int(request.get("max_num_batched_tokens", 8192), "批处理 Token", 256, 10_000_000),
        "trust_remote_code": bool(request.get("trust_remote_code", False)),
        "supports_image_input": bool(request.get("supports_image_input", False)),
        "supports_ocr": bool(request.get("supports_ocr", False)),
        "supports_tool_calling": bool(request.get("supports_tool_calling", False)),
        "supports_reasoning": bool(request.get("supports_reasoning", False)),
        "supports_thinking_toggle": bool(request.get("supports_thinking_toggle", False)),
        "tool_call_parser": str(request.get("tool_call_parser", "")),
        "reasoning_parser": str(request.get("reasoning_parser", "")),
        "mm_limit": str(request.get("mm_limit", '{"image":4}')),
    }
    validate_runtime(runtime)
    if any(item["kind"] == "local" and len(item["gpu_devices"]) != runtime["tensor_parallel_size"] for item in instances):
        raise ValueError("每个本机实例的 GPU 数必须等于 TP_SIZE")
    stamp = utc_now()
    return {
        "artifact": {
            "id": artifact_id,
            "hub": hub,
            "model_id": model_id,
            "revision": revision,
            "path": str(artifact_path),
            "status": "ready" if artifact_path.is_dir() else "pending",
            "immutable": hub != "local",
            "created_at": stamp,
        },
        "deployment": {
            "id": deployment_id,
            "display_name": str(request.get("display_name", public_id)).strip()[:200],
            "artifact_id": artifact_id,
            "model_id": model_id,
            "served_model_name": served,
            "served_model_aliases": served_aliases,
            "public_model_ids": list(dict.fromkeys([public_id, *request.get("additional_public_ids", [])])),
            "status": "planned",
            "enabled": True,
            "instances": instances,
            "runtime": runtime,
            "publish_requested": bool(request.get("publish_requested", True)),
            "created_at": stamp,
            "updated_at": stamp,
        },
        "preserve_legacy_alias": bool(request.get("preserve_legacy_alias", deployment_id == "ornith")),
    }


def normalize_instances(raw_instances: Any) -> list[dict[str, Any]]:
    """校验本机或远程实例列表并生成稳定实例 ID。"""

    if not isinstance(raw_instances, list) or not raw_instances or len(raw_instances) > 256:
        raise ValueError("至少需要一个实例，且实例数不能超过 256")
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_instances):
        if not isinstance(raw, dict):
            raise ValueError("实例必须是 JSON 对象")
        kind = str(raw.get("kind", "local"))
        if kind == "remote":
            api_key_env = str(raw.get("api_key_env", "BACKEND_API_KEY"))
            if not SAFE_ENV_NAME_RE.fullmatch(api_key_env):
                raise ValueError("远程实例密钥环境变量名非法")
            result.append(
                {
                    "id": str(raw.get("id", f"remote-{index}"))[:100],
                    "kind": "remote",
                    "base_url": str(raw.get("base_url", "")).rstrip("/"),
                    "api_key_env": api_key_env,
                    "enabled": bool(raw.get("enabled", True)),
                }
            )
        elif kind == "local":
            worker_id = bounded_int(raw.get("worker_id"), "Worker ID", 0, 255)
            result.append(
                {
                    "id": str(raw.get("id", f"worker-{worker_id}"))[:100],
                    "kind": "local",
                    "worker_id": worker_id,
                    "gpu_devices": parse_gpu_devices(raw.get("gpu_devices", [])),
                    "port": bounded_int(raw.get("port", 8100 + worker_id), "Worker 端口", 1024, 65535),
                    "enabled": bool(raw.get("enabled", True)),
                }
            )
        else:
            raise ValueError("实例类型必须是 local 或 remote")
    return result


def merge_deployment(
    current: dict[str, Any], request: dict[str, Any]
) -> tuple[dict[str, Any], set[int]]:
    """合并新部署并从旧部署移除冲突槽位，返回候选注册表和影响集合。"""

    candidate = json.loads(json.dumps(current))
    deployment = request["deployment"]
    new_workers = {
        int(item["worker_id"])
        for item in deployment["instances"]
        if item["kind"] == "local" and item.get("enabled", True)
    }
    new_gpus = {
        int(gpu)
        for item in deployment["instances"]
        if item["kind"] == "local" and item.get("enabled", True)
        for gpu in item["gpu_devices"]
    }
    affected = set(new_workers)
    for deployment_id, existing in list(candidate["deployments"].items()):
        retained: list[dict[str, Any]] = []
        for instance in existing.get("instances", []):
            conflict = instance.get("kind") == "local" and (
                int(instance["worker_id"]) in new_workers
                or bool(set(instance.get("gpu_devices", [])) & new_gpus)
            )
            if conflict:
                affected.add(int(instance["worker_id"]))
            else:
                retained.append(instance)
        if deployment_id == deployment["id"]:
            affected.update(
                int(item["worker_id"])
                for item in existing.get("instances", [])
                if item.get("kind") == "local"
            )
            del candidate["deployments"][deployment_id]
        elif not retained:
            existing["instances"] = existing.get("instances", [])
            existing["enabled"] = False
            existing["status"] = "disabled"
        else:
            existing["instances"] = retained
            existing["updated_at"] = utc_now()
    candidate["artifacts"][request["artifact"]["id"]] = request["artifact"]
    candidate["deployments"][deployment["id"]] = deployment
    if request.get("preserve_legacy_alias"):
        candidate["legacy_aliases"]["gdn-inside"] = deployment["public_model_ids"][0]
    candidate["updated_at"] = utc_now()
    return candidate, affected


def plan_warnings(
    current: dict[str, Any], candidate: dict[str, Any], request: dict[str, Any]
) -> list[str]:
    """生成计划确认页需要突出显示的非阻断风险。"""

    warnings: list[str] = []
    disabled = [
        item["display_name"]
        for item in candidate["deployments"].values()
        if not item.get("enabled", True)
    ]
    if disabled:
        warnings.append("以下旧部署将因 GPU 被重新分配而停用：" + "、".join(disabled))
    reduced: list[str] = []
    for deployment_id, previous in current["deployments"].items():
        updated = candidate["deployments"].get(deployment_id)
        if not updated or not updated.get("enabled", True):
            continue
        previous_slots = {
            (int(item["worker_id"]), tuple(int(gpu) for gpu in item.get("gpu_devices", [])))
            for item in previous.get("instances", [])
            if item.get("kind") == "local" and item.get("enabled", True)
        }
        updated_slots = {
            (int(item["worker_id"]), tuple(int(gpu) for gpu in item.get("gpu_devices", [])))
            for item in updated.get("instances", [])
            if item.get("kind") == "local" and item.get("enabled", True)
        }
        released = previous_slots - updated_slots
        if released:
            slots = "、".join(
                f"Worker {worker_id}/GPU {','.join(str(gpu) for gpu in gpus)}"
                for worker_id, gpus in sorted(released)
            )
            reduced.append(f"{previous.get('display_name', deployment_id)}（{slots}）")
    if reduced:
        warnings.append("以下旧部署将释放部分 GPU/Worker，未被释放的实例继续服务：" + "；".join(reduced))
    if request["artifact"]["hub"] != "local" and not pathlib.Path(request["artifact"]["path"]).is_dir():
        warnings.append("需要下载模型权重；下载目录保留为可断点续传的 partial 目录")
    if current.get("migrated_from_legacy"):
        warnings.append("当前环境来自单模型配置；首次提交会进入多模型注册表管理")
    return warnings


def local_instances(registry: dict[str, Any]) -> dict[int, tuple[dict[str, Any], dict[str, Any]]]:
    """按 Worker ID 建立已启用本机实例索引。"""

    result: dict[int, tuple[dict[str, Any], dict[str, Any]]] = {}
    for deployment in registry["deployments"].values():
        if not deployment.get("enabled", True):
            continue
        for instance in deployment.get("instances", []):
            if instance.get("kind") == "local" and instance.get("enabled", True):
                result[int(instance["worker_id"])] = (deployment, instance)
    return result


def worker_environment(
    deployment: dict[str, Any], instance: dict[str, Any], artifact: dict[str, Any]
) -> dict[str, Any]:
    """构造 Worker 私有环境，覆盖旧版全局单模型参数。"""

    runtime = deployment["runtime"]
    return {
        "DEPLOYMENT_ID": deployment["id"],
        "GPU_DEVICES": ",".join(str(item) for item in instance["gpu_devices"]),
        "WORKER_PORT": instance["port"],
        "MODEL_LOCAL_DIR": artifact["path"],
        "MODEL_ID": deployment["model_id"],
        "SERVED_MODEL_NAME": deployment["served_model_name"],
        "SERVED_MODEL_ALIASES": ",".join(
            str(item) for item in deployment.get("served_model_aliases", [])
        ),
        "VLLM_IMAGE": runtime["image"],
        "TP_SIZE": runtime["tensor_parallel_size"],
        "MAX_MODEL_LEN": runtime["max_model_len"],
        "GPU_MEMORY_UTILIZATION": runtime["gpu_memory_utilization"],
        "MAX_NUM_SEQS": runtime["max_num_seqs"],
        "MAX_NUM_BATCHED_TOKENS": runtime["max_num_batched_tokens"],
        "TRUST_REMOTE_CODE": int(runtime["trust_remote_code"]),
        "SUPPORTS_IMAGE_INPUT": int(runtime["supports_image_input"]),
        "SUPPORTS_OCR": int(runtime["supports_ocr"]),
        "SUPPORTS_TOOL_CALLING": int(runtime["supports_tool_calling"]),
        "SUPPORTS_REASONING": int(runtime["supports_reasoning"]),
        "SUPPORTS_THINKING_TOGGLE": int(runtime["supports_thinking_toggle"]),
        "TOOL_CALL_PARSER": runtime["tool_call_parser"],
        "REASONING_PARSER": runtime["reasoning_parser"],
        "MM_LIMIT": runtime["mm_limit"],
    }


def verify_artifact(directory: pathlib.Path) -> None:
    """验证模型目录包含配置和权重，且不存在越界符号链接或特殊文件。"""

    if not directory.is_dir() or not (directory / "config.json").is_file():
        raise ValueError("模型目录缺少 config.json")
    weight_suffixes = {".safetensors", ".bin", ".pt", ".gguf"}
    has_weight = False
    root = directory.resolve()
    for path in directory.rglob("*"):
        if path.is_symlink():
            target = path.resolve(strict=False)
            if target != root and root not in target.parents:
                raise ValueError(f"模型目录包含越界符号链接：{path.name}")
        elif path.is_file():
            has_weight = has_weight or path.suffix.lower() in weight_suffixes
        elif not path.is_dir():
            raise ValueError(f"模型目录包含特殊文件：{path.name}")
    if not has_weight:
        raise ValueError("模型目录未发现可识别权重文件")


def update_env_assignments(path: pathlib.Path, assignments: dict[str, str]) -> None:
    """保留原文件顺序和注释，仅原子更新指定环境变量。"""

    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(assignments)
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        if "=" in stripped and not stripped.startswith("#"):
            name = stripped.split("=", 1)[0].strip()
            if name in remaining:
                output.append(f"{name}={shlex.quote(remaining.pop(name))}")
                continue
        output.append(line)
    for name, value in remaining.items():
        output.append(f"{name}={shlex.quote(value)}")
    atomic_write(path, "\n".join(output) + "\n")


def endpoint_healthy(origin: str, secrets_file: pathlib.Path) -> bool:
    """使用内部密钥检查 Worker 健康端点，不把密钥写入日志。"""

    key = parse_env_file(secrets_file).get("BACKEND_API_KEY", "")
    request = urllib.request.Request(
        f"{origin.rstrip('/')}/health",
        headers={"Authorization": f"Bearer {key}"} if key else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return 200 <= response.status < 300
    except (OSError, urllib.error.URLError):
        return False


def endpoint_inference_ready(
    origin: str,
    secrets_file: pathlib.Path,
    served_model_name: str,
    timeout: int = 60,
    detail: list[str] | None = None,
) -> bool:
    """向单个 Worker 发送有界文本生成，验证真实模型执行路径。

    参数：
        origin: 不含 `/v1` 的 Worker 来源地址。
        secrets_file: 保存内部 Worker API Key 的受保护环境文件。
        served_model_name: 候选部署写入 vLLM 的服务模型名。
        timeout: 单个真实生成请求的最大等待秒数。
        detail: 可选的诊断输出列表；失败时追加一条脱敏、截断的原因。

    返回：
        HTTP 成功且响应包含至少一个有效 assistant 消息时返回真。
    """

    key = parse_env_file(secrets_file).get("BACKEND_API_KEY", "")
    body = json.dumps(
        {
            "model": served_model_name,
            "messages": [{"role": "user", "content": "只回复 OK"}],
            "temperature": 0,
            "max_tokens": 16,
            "stream": False,
        },
        ensure_ascii=False,
    ).encode()
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(
        f"{origin.rstrip('/')}/v1/chat/completions",
        data=body,
        headers=headers,
        method="POST",
    )
    def fail(reason: str) -> bool:
        """记录不含密钥的单条失败摘要并返回假。"""

        if detail is not None:
            detail.append(" ".join(str(reason).split())[:800])
        return False

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if not 200 <= response.status < 300:
                return fail(f"HTTP {response.status}")
            payload = json.loads(response.read(2 << 20))
    except urllib.error.HTTPError as error:
        with contextlib.suppress(OSError):
            body = error.read(4096).decode("utf-8", errors="replace")
            return fail(f"HTTP {error.code}: {body}")
        return fail(f"HTTP {error.code}")
    except json.JSONDecodeError as error:
        return fail(f"响应不是有效 JSON：{error.msg}")
    except (OSError, urllib.error.URLError) as error:
        return fail(f"请求失败或超时：{error}")
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not isinstance(choices, list) or not choices:
        return fail("响应缺少 choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        return fail("响应缺少 assistant message")
    has_text = any(
        isinstance(message.get(field), str) and bool(message.get(field).strip())
        for field in (
            "content",
            "reasoning_content",
            "reasoning_text",
            "reasoning",
        )
    )
    if has_text or (
        isinstance(message.get("tool_calls"), list) and bool(message["tool_calls"])
    ):
        return True
    return fail("assistant message 没有文本、思考内容或工具调用")


def gpu_inventory(runner: CommandRunner) -> list[dict[str, Any]]:
    """读取 NVIDIA GPU 静态信息；无 GPU 或测试环境中返回空列表。"""

    try:
        result = runner.run(
            [
                "nvidia-smi",
                "--query-gpu=index,name,memory.total,uuid,pci.bus_id",
                "--format=csv,noheader,nounits",
            ],
            timeout=5,
        )
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        return []
    inventory: list[dict[str, Any]] = []
    for line in (result.stdout or "").splitlines():
        parts = [item.strip() for item in line.split(",", 4)]
        if len(parts) == 5:
            inventory.append(
                {
                    "id": int(parts[0]),
                    "name": parts[1],
                    "memory_mib": int(parts[2]),
                    "uuid": parts[3],
                    "pci_bus_id": parts[4],
                }
            )
    return inventory


def active_systemd_workers() -> list[int]:
    """返回当前运行中的 llm-worker 单元编号，用于失败恢复。"""

    result = subprocess.run(
        ["systemctl", "list-units", "llm-worker@*.service", "--state=running", "--no-legend"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    workers: list[int] = []
    for line in result.stdout.splitlines():
        match = re.search(r"llm-worker@(\d+)\.service", line)
        if match:
            workers.append(int(match.group(1)))
    return sorted(set(workers))


class ControlRequestHandler(socketserver.StreamRequestHandler):
    """处理一行一个 JSON 请求并返回一行 JSON 响应。"""

    def handle(self) -> None:
        """限制请求大小，分派操作并把内部异常转换为稳定错误结构。"""

        raw = self.rfile.readline(MAX_REQUEST_BYTES + 1)
        if not raw or len(raw) > MAX_REQUEST_BYTES:
            self._reply({"ok": False, "error": "请求为空或超过 2 MiB"})
            return
        try:
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise ValueError("请求必须是 JSON 对象")
            result = self.server.dispatch(request)  # type: ignore[attr-defined]
            self._reply({"ok": True, "result": result})
        except Exception as error:
            self._reply({"ok": False, "error": str(error)[:4000]})

    def _reply(self, payload: dict[str, Any]) -> None:
        """发送 UTF-8 JSON 响应并立即刷新。"""

        self.wfile.write(json.dumps(payload, ensure_ascii=False).encode() + b"\n")
        self.wfile.flush()


class ControlServer(socketserver.ThreadingUnixStreamServer):
    """只监听本机 Unix Socket 的多线程控制服务。"""

    daemon_threads = True

    def __init__(self, socket_path: pathlib.Path, manager: DeploymentManager):
        """清理陈旧 Socket，绑定新端点并收紧访问权限。"""

        self.socket_path = socket_path
        self.manager = manager
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            socket_path.unlink()
        super().__init__(str(socket_path), ControlRequestHandler)
        os.chmod(socket_path, 0o660)
        group_name = os.environ.get("LLM_MODEL_CONTROL_GROUP", "llm-account")
        with contextlib.suppress(KeyError, PermissionError):
            os.chown(socket_path, 0, grp.getgrnam(group_name).gr_gid)

    def dispatch(self, request: dict[str, Any]) -> Any:
        """按白名单分派操作，绝不接受任意命令或路径写入。"""

        operation = str(request.get("operation", ""))
        payload = request.get("payload", {})
        if operation == "snapshot":
            return self.manager.snapshot()
        if operation == "migrate":
            return self.manager.migrate()
        if operation == "plan":
            return self.manager.plan(payload)
        if operation == "submit":
            return self.manager.submit(payload)
        if operation == "upgrade-plan":
            return self.manager.plan_upgrade(payload)
        if operation == "upgrade-submit":
            return self.manager.submit_upgrade(payload)
        if operation == "publish":
            return self.manager.publish()
        if operation == "download-proxy-test":
            return self.manager.test_download_proxy(payload)
        if operation == "download-proxy-save":
            return self.manager.save_download_proxy(payload)
        if operation == "download-proxy-clear":
            return self.manager.clear_download_proxy()
        if operation == "job":
            return self.manager.jobs.get(str(payload.get("id", "")))
        if operation == "cancel":
            return self.manager.cancel(str(payload.get("id", "")))
        if operation == "rollback":
            return self.manager.rollback(str(payload.get("id", "")))
        if operation == "omniroute-status":
            return self.manager.omniroute.status()
        if operation == "omniroute-assess":
            return self.manager.omniroute.assess(bool(payload.get("deep", False)))
        if operation == "omniroute-submit":
            return self.manager.omniroute.submit(payload)
        if operation == "omniroute-job":
            return self.manager.omniroute.jobs.get(str(payload.get("id", "")))
        if operation == "omniroute-cancel":
            return self.manager.omniroute.cancel(str(payload.get("id", "")))
        raise ValueError("未知模型控制操作")

    def server_close(self) -> None:
        """关闭监听器并删除 Socket 文件，避免下次启动误判。"""

        super().server_close()
        with contextlib.suppress(FileNotFoundError):
            self.socket_path.unlink()


def rpc_call(socket_path: pathlib.Path, operation: str, payload: dict[str, Any] | None = None) -> Any:
    """向本机控制服务发送单次请求并返回 result 字段。"""

    request = json.dumps({"operation": operation, "payload": payload or {}}, ensure_ascii=False).encode() + b"\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        # Hub 元数据和硬件目录检查可能跨越维护代理，升级计划使用独立的有界
        # 等待；其他本机控制操作继续保持快速失败。
        client.settimeout(
            300
            if operation == "omniroute-assess" and bool((payload or {}).get("deep"))
            else 120
            if operation in {"upgrade-plan", "omniroute-assess"}
            else 30
        )
        client.connect(str(socket_path))
        client.sendall(request)
        response = b""
        while not response.endswith(b"\n"):
            chunk = client.recv(65536)
            if not chunk:
                break
            response += chunk
            if len(response) > MAX_REQUEST_BYTES:
                raise RuntimeError("模型控制响应超过 2 MiB")
    parsed = json.loads(response)
    if not parsed.get("ok"):
        raise RuntimeError(str(parsed.get("error", "模型控制请求失败")))
    return parsed.get("result")


def serve(paths: Paths) -> None:
    """启动控制服务并在启动时无侵入持久化旧部署快照。"""

    manager = DeploymentManager(paths)
    manager.recover_interrupted_jobs()
    manager.migrate()
    with ControlServer(paths.socket, manager) as server:
        server.serve_forever(poll_interval=0.5)


def main() -> int:
    """解析守护进程和 CLI 调试子命令。"""

    parser = argparse.ArgumentParser(description="LLMCtl 多模型部署控制服务")
    parser.add_argument("--socket", default="")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("serve")
    subparsers.add_parser("snapshot")
    subparsers.add_parser("migrate")
    request_parser = subparsers.add_parser("request")
    request_parser.add_argument(
        "operation",
        choices=[
            "plan",
            "submit",
            "upgrade-plan",
            "upgrade-submit",
            "publish",
            "download-proxy-test",
            "download-proxy-save",
            "download-proxy-clear",
            "job",
            "cancel",
            "rollback",
            "omniroute-status",
            "omniroute-assess",
            "omniroute-submit",
            "omniroute-job",
            "omniroute-cancel",
        ],
    )
    request_parser.add_argument("json_file", type=pathlib.Path)
    args = parser.parse_args()
    paths = Paths.from_environment()
    if args.socket:
        paths = dataclasses.replace(paths, socket=pathlib.Path(args.socket))
    if args.command == "serve":
        serve(paths)
        return 0
    if args.command in {"snapshot", "migrate"}:
        result = rpc_call(paths.socket, args.command)
    else:
        payload = json.loads(args.json_file.read_text(encoding="utf-8"))
        result = rpc_call(paths.socket, args.operation, payload)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
