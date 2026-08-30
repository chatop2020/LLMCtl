#!/usr/bin/env python3
"""Qwen3.8 一键部署使用的主机资源读取与 TP2 拓扑分组。"""

from __future__ import annotations

import pathlib
import re
import shutil
from typing import Any


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
