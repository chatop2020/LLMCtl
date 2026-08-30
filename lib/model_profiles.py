#!/usr/bin/env python3
"""集中保存需要跨目录规划、控制服务和门户共享的模型运行预设。"""

from __future__ import annotations

from typing import Any


GIB = 1024**3
QWEN38_DEPLOYMENT_ID = "qwen38-flash-next"
QWEN38_DISPLAY_NAME = "Qwen3.8 Flash Next NVFP4"
QWEN38_MODEL_HUB = "modelscope"
QWEN38_MODEL_ID = "RadixArk/Qwen3.8-Flash-Next-NVFP4"
QWEN38_MODEL_REVISION = "a6cc3dfc4d4d4617b6ede29f53e751215510e681"
QWEN38_ARCHITECTURE = "Qwen4ExpForConditionalGeneration"
QWEN38_IMAGE = "vllm/vllm-openai:qwen38-flash-next"
QWEN38_PUBLIC_MODEL_ID = "gdn-inside"
QWEN38_DEFAULT_MAX_IMAGES = 4
QWEN38_EDITABLE_FIELDS = frozenset(
    {
        "max_model_len",
        "gpu_memory_utilization",
        "max_num_seqs",
        "max_num_batched_tokens",
        "mtp_speculative_tokens",
        "kv_cache_dtype",
        "enable_prefix_caching",
        "max_images_per_request",
        "expected_registry_revision",
    }
)


def qwen38_runtime_defaults() -> dict[str, Any]:
    """返回 Qwen3.8 一键部署和目录规划共同使用的保守生产预设。"""

    return {
        "image": QWEN38_IMAGE,
        "tensor_parallel_size": 2,
        "max_model_len": 262_144,
        "gpu_memory_utilization": 0.90,
        "max_num_seqs": 8,
        "max_num_batched_tokens": 8192,
        "ple_cpu_offload": True,
        "enable_expert_parallel": True,
        "enable_prefix_caching": False,
        "enable_flashinfer_autotune": False,
        "disable_custom_all_reduce": False,
        "mtp_speculative_tokens": 0,
        "kv_cache_dtype": "auto",
        "yarn_factor": 1.0,
        "trust_remote_code": False,
        "supports_image_input": True,
        "supports_ocr": False,
        "supports_tool_calling": True,
        "supports_reasoning": True,
        "supports_thinking_toggle": True,
        "tool_call_parser": "qwen3_xml",
        "reasoning_parser": "qwen3",
        "mm_limit": f'{{"image":{QWEN38_DEFAULT_MAX_IMAGES},"video":0}}',
    }


def qwen38_capacity_requirements() -> dict[str, int]:
    """返回全八卡部署在下载前必须满足的保守主机资源下限。"""

    return {
        "gpu_count": 8,
        "minimum_gpu_memory_mib": 80 * 1024,
        "minimum_host_memory_bytes": 480 * GIB,
        "recommended_available_memory_bytes": 420 * GIB,
        "download_disk_bytes": 250 * GIB,
    }
