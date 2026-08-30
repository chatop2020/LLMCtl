#!/usr/bin/env python3
"""Qwen3.8 一键部署使用的主机资源读取与 TP2 拓扑分组。"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import tempfile
from typing import Any

from model_profiles import QWEN38_ARCHITECTURE


QWEN38_PLE_MODULE = "vllm.models.qwen3_8_flash_next.nvidia.ple_layer"


def host_resource_snapshot(model_root: pathlib.Path) -> dict[str, int]:
    """读取部署前可用的主内存与模型盘空间，不修改系统状态。"""

    memory: dict[str, int] = {}
    try:
        for line in pathlib.Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition(":")
            match = re.search(r"\d+", value)
            if match:
                memory[name] = int(match.group()) * 1024
    except OSError:
        pass
    probe = model_root
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        disk_free = int(shutil.disk_usage(probe).free)
    except OSError:
        disk_free = 0
    return {
        "memory_total_bytes": int(memory.get("MemTotal", 0)),
        "memory_available_bytes": int(memory.get("MemAvailable", 0)),
        "disk_free_bytes": disk_free,
    }


def verify_qwen38_artifact(directory: pathlib.Path) -> None:
    """核验固定 NVFP4 制品包含语言、PLE 和视觉所需的完整权重。

    参数：
        directory: ModelScope 下载完成后、即将挂载给 vLLM 的本地目录。

    异常：
        ValueError: 配置不是目标架构、量化契约不符、缺少视觉/PLE 映射，
        或权重索引引用了未下载的分片。
    """

    try:
        config = json.loads((directory / "config.json").read_text(encoding="utf-8"))
        index = json.loads(
            (directory / "model.safetensors.index.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Qwen3.8 配置或权重索引不可读：{error}") from error
    if not isinstance(config, dict):
        raise ValueError("Qwen3.8 config.json 必须是 JSON 对象")
    text_config = config.get("text_config")
    quantization = config.get("quantization_config")
    try:
        native_context = int(text_config.get("max_position_embeddings", 0))
    except (AttributeError, TypeError, ValueError):
        native_context = 0
    if (
        QWEN38_ARCHITECTURE not in config.get("architectures", [])
        or config.get("language_model_only") is not False
        or not isinstance(config.get("vision_config"), dict)
        or not isinstance(text_config, dict)
        or native_context < 262_144
        or str(text_config.get("ple_embedding_dtype", "")).lower()
        != "float8_e4m3fn"
        or not isinstance(quantization, dict)
        or str(quantization.get("quant_method", "")).lower() != "modelopt"
        or str(quantization.get("quant_algo", "")).upper() != "NVFP4"
    ):
        raise ValueError("Qwen3.8 制品不满足 NVFP4、FP8 PLE、原生 262K 和视觉模型契约")
    weight_map = index.get("weight_map") if isinstance(index, dict) else None
    if not isinstance(weight_map, dict) or not weight_map:
        raise ValueError("Qwen3.8 权重索引缺少 weight_map")
    names = tuple(str(name) for name in weight_map)
    if not any(name.startswith("model.visual.") for name in names):
        raise ValueError("Qwen3.8 权重索引缺少视觉编码器")
    if not any(".ple." in name for name in names):
        raise ValueError("Qwen3.8 权重索引缺少 PLE n-gram 权重")
    missing = sorted(
        {
            str(shard)
            for shard in weight_map.values()
            if not isinstance(shard, str) or not (directory / shard).is_file()
        }
    )
    if missing:
        raise ValueError(f"Qwen3.8 权重分片不完整：{missing[:3]}")


def _atomic_private_write(path: pathlib.Path, content: str) -> None:
    """在受控状态目录原子写入只允许 root 修改的构建输入。"""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def ensure_radixark_ple_runtime_image(
    runner: Any, base_image: str, state_dir: pathlib.Path
) -> str:
    """从本地专用镜像构建带 FP8-PLE resolver 门禁的可追溯派生镜像。

    RadixArk checkpoint 的主体使用 ModelOpt NVFP4，PLE 表却是带全局 scale
    的 FP8 分片。目标 vLLM 已包含对应 loader，但只按外层 ``Fp8Config``
    选择它。本函数在构建时验证唯一补丁锚点，生成以基础镜像 ID 命名的小
    派生层；不修改原镜像、模型权重或宿主 Python 环境。
    """

    inspected = runner.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", base_image],
        timeout=30,
    )
    image_id = str(inspected.stdout or "").strip()
    match = re.fullmatch(r"sha256:([0-9a-f]{12,64})", image_id)
    if not match:
        raise RuntimeError("无法读取 Qwen3.8 基础镜像 ID，拒绝构建 PLE 兼容层")
    derived_image = f"llmctl/qwen38-flash-next:radixark-ple-{match.group(1)[:12]}"
    existing = runner.run(
        ["docker", "image", "inspect", derived_image], check=False, timeout=10
    )
    if existing.returncode == 0:
        return derived_image

    context = state_dir / "runtime-overrides" / "qwen38-radixark-ple"
    patch_script = f'''from importlib import import_module
from pathlib import Path

module = import_module("{QWEN38_PLE_MODULE}")
target = Path(module.__file__).resolve()
source = target.read_text(encoding="utf-8")
needle = "    if not isinstance(quant_config, Fp8Config):\\n"
replacement = (
    "    import os\\n"
    "    if os.environ.get(\\\"PLE_FORCE_FP8\\\") == \\\"1\\\":\\n"
    "        return Qwen3_8FlashNextPLEFp8EmbeddingMethod()\\n"
    + needle
)
if source.count(needle) != 1:
    raise SystemExit("Qwen3.8 PLE resolver patch anchor is missing or ambiguous")
patched = source.replace(needle, replacement, 1)
compile(patched, str(target), "exec")
target.write_text(patched, encoding="utf-8")
print(target)
'''
    dockerfile = '''ARG BASE_IMAGE
FROM ${BASE_IMAGE}
COPY patch_ple.py /tmp/llmctl-patch-ple.py
RUN python3 /tmp/llmctl-patch-ple.py && rm -f /tmp/llmctl-patch-ple.py
ENV PLE_FORCE_FP8=1
LABEL org.llmctl.runtime="qwen38-radixark-fp8-ple-v1"
'''
    _atomic_private_write(context / "patch_ple.py", patch_script)
    _atomic_private_write(context / "Dockerfile", dockerfile)
    runner.run(
        [
            "docker", "build", "--pull=false",
            "--build-arg", f"BASE_IMAGE={base_image}",
            "--tag", derived_image, str(context),
        ],
        timeout=30 * 60,
    )
    runner.run(
        ["docker", "image", "inspect", derived_image], timeout=30
    )
    return derived_image


def _topology_link_cost(value: str) -> int:
    """把 NVIDIA 拓扑链路转换为仅用于两卡配对的相对成本。"""

    label = str(value or "").upper()
    if label.startswith("NV"):
        return 0
    return {"PIX": 1, "PXB": 2, "PHB": 3, "NODE": 4, "SYS": 5}.get(
        label, 6
    )


def _best_gpu_pairs(
    gpu_ids: tuple[int, ...], links: dict[tuple[int, int], str]
) -> list[list[int]]:
    """穷举最多八张 GPU 的完美匹配，优先降低最慢链路和总通信成本。"""

    if not gpu_ids:
        return []
    first = gpu_ids[0]
    best_score: tuple[int, int, tuple[tuple[int, int], ...]] | None = None
    best_groups: list[list[int]] = []
    for offset in range(1, len(gpu_ids)):
        second = gpu_ids[offset]
        remaining = gpu_ids[1:offset] + gpu_ids[offset + 1 :]
        tail = _best_gpu_pairs(remaining, links)
        groups = [[first, second], *tail]
        costs = [
            _topology_link_cost(links.get(tuple(sorted(group)), "unknown"))
            for group in groups
        ]
        normalized = tuple(tuple(sorted(group)) for group in groups)
        score = (max(costs, default=0), sum(costs), normalized)
        if best_score is None or score < best_score:
            best_score = score
            best_groups = [list(group) for group in normalized]
    return best_groups


def qwen38_gpu_groups(
    runner: Any, inventory: list[dict[str, Any]]
) -> dict[str, Any]:
    """根据 `nvidia-smi topo -m` 为 Qwen TP2 自动选择四组 GPU。"""

    gpu_ids = tuple(sorted(int(item["id"]) for item in inventory))
    fallback = [list(gpu_ids[index : index + 2]) for index in range(0, len(gpu_ids), 2)]
    if len(gpu_ids) % 2:
        return {"groups": fallback, "links": [], "source": "index-fallback"}
    try:
        result = runner.run(["nvidia-smi", "topo", "-m"], timeout=5)
    except Exception:
        return {"groups": fallback, "links": [], "source": "index-fallback"}
    rows: dict[int, list[str]] = {}
    for line in (result.stdout or "").splitlines():
        parts = line.split()
        if not parts or not re.fullmatch(r"GPU\d+", parts[0]):
            continue
        rows[int(parts[0][3:])] = parts[1 : 1 + len(gpu_ids)]
    if any(gpu_id not in rows or len(rows[gpu_id]) < len(gpu_ids) for gpu_id in gpu_ids):
        return {"groups": fallback, "links": [], "source": "index-fallback"}
    position = {gpu_id: index for index, gpu_id in enumerate(gpu_ids)}
    links = {
        (left, right): rows[left][position[right]]
        for left in gpu_ids
        for right in gpu_ids
        if left < right
    }
    groups = _best_gpu_pairs(gpu_ids, links)
    return {
        "groups": groups,
        "links": [
            {"gpu_devices": group, "link": links.get(tuple(group), "unknown")}
            for group in groups
        ],
        "source": "nvidia-smi",
    }
