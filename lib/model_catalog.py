#!/usr/bin/env python3
"""Model discovery and conservative hardware planning for llm-cluster-deploy.

The helper intentionally uses only Python's standard library so discovery works
before any Python virtual environment is created.  It treats Hub metadata as a
preflight signal, never as proof that a model has completed a real vLLM load.
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import dataclasses
import json
import math
import os
import platform
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterable


_CATALOG_DIRECTORY = os.path.dirname(os.path.abspath(__file__))
if _CATALOG_DIRECTORY not in sys.path:
    sys.path.insert(0, _CATALOG_DIRECTORY)

from model_profiles import (
    QWEN38_ARCHITECTURE,
    QWEN38_IMAGE,
    qwen38_runtime_defaults,
)


CATALOG_VERSION = "2.3.0"
VLLM_COMPAT_VERSION = "0.22.1"
GIB = 1024**3
HF_ENDPOINT = os.environ.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/")
MS_ENDPOINT = os.environ.get("MODELSCOPE_ENDPOINT", "https://modelscope.cn").rstrip("/")
USER_AGENT = f"llm-cluster-deploy/{CATALOG_VERSION}"
LANGUAGE = os.environ.get("LLMCTL_LANG", "zh").lower()

# 主清单来自 vLLM 0.22.1 官方支持页；Qwen4Exp 是由专用预览镜像提供的
# 显式例外。安装仍会在下载大权重前读取目标镜像的 ModelRegistry，因此这里
# 只承担目录发现门禁，不替代真实运行时核验。
SUPPORTED_ARCHITECTURES = frozenset(
    """
AXK1ForCausalLM AfmoeForCausalLM ApertusForCausalLM AquilaForCausalLM
ArceeForCausalLM ArcticForCausalLM AriaForConditionalGeneration
AudioFlamingo3ForConditionalGeneration AyaVisionForConditionalGeneration
BagelForConditionalGeneration BaiChuanForCausalLM BailingMoeForCausalLM
BailingMoeV2ForCausalLM BailingMoeV2_5ForCausalLM BambaForCausalLM
BartForConditionalGeneration BeeForConditionalGeneration
Blip2ForConditionalGeneration BloomForCausalLM ChameleonForConditionalGeneration
ChatGLMForConditionalGeneration ChatGLMModel CheersForConditionalGeneration
Cohere2ForCausalLM Cohere2MoeForCausalLM Cohere2VisionForConditionalGeneration
CohereAsrForConditionalGeneration CohereForCausalLM CwmForCausalLM
DbrxForCausalLM DeciLMForCausalLM DeepseekForCausalLM
DeepseekOCR2ForCausalLM DeepseekOCRForCausalLM DeepseekV2ForCausalLM
DeepseekV3ForCausalLM DeepseekV4ForCausalLM DeepseekVLV2ForCausalLM
Dots1ForCausalLM DotsOCRForCausalLM Eagle2_5_VLForConditionalGeneration
Emu3ForConditionalGeneration Ernie4_5ForCausalLM Ernie4_5_MoeForCausalLM
Ernie4_5_VLMoeForConditionalGeneration Exaone4ForCausalLM
Exaone4_5_ForConditionalGeneration ExaoneForCausalLM ExaoneMoEForCausalLM
Fairseq2LlamaForCausalLM FalconForCausalLM FalconH1ForCausalLM
FalconMambaForCausalLM FireRedASR2ForConditionalGeneration
FireRedLIDForConditionalGeneration FlexOlmoForCausalLM
Florence2ForConditionalGeneration FunASRForConditionalGeneration FuyuForCausalLM
GLM4VForCausalLM GPT2LMHeadModel GPTBigCodeForCausalLM GPTJForCausalLM
GPTNeoXForCausalLM Gemma2ForCausalLM Gemma3ForCausalLM
Gemma3ForConditionalGeneration Gemma3nForCausalLM
Gemma3nForConditionalGeneration Gemma4ForCausalLM
Gemma4ForConditionalGeneration GemmaForCausalLM Glm4ForCausalLM
Glm4MoeForCausalLM Glm4MoeLiteForCausalLM Glm4vForConditionalGeneration
Glm4vMoeForConditionalGeneration GlmAsrForConditionalGeneration GlmForCausalLM
GlmOcrForConditionalGeneration GptOssForCausalLM
Granite4VisionForConditionalGeneration GraniteForCausalLM GraniteMoeForCausalLM
GraniteMoeHybridForCausalLM GraniteMoeSharedForCausalLM
GraniteSpeechForConditionalGeneration Grok1ForCausalLM Grok1ModelForCausalLM
H2OVLChatModel HCXVisionForCausalLM HCXVisionV2ForCausalLM HYV3ForCausalLM
HunYuanDenseV1ForCausalLM HunYuanMoEV1ForCausalLM
HunYuanVLForConditionalGeneration HyperCLOVAXForCausalLM IQuestCoderForCausalLM
IQuestLoopCoderForCausalLM Idefics3ForConditionalGeneration InternLM2ForCausalLM
InternLM3ForCausalLM InternLMForCausalLM InternS1ForConditionalGeneration
InternS1ProForConditionalGeneration InternS2PreviewForConditionalGeneration
InternVLChatModel InternVLForConditionalGeneration IsaacForConditionalGeneration
JAISLMHeadModel Jais2ForCausalLM JambaForCausalLM
KananaVForConditionalGeneration KeyeForConditionalGeneration
KeyeVL1_5ForConditionalGeneration KimiAudioForConditionalGeneration
KimiK25ForConditionalGeneration KimiLinearForCausalLM
KimiVLForConditionalGeneration Lfm2ForCausalLM Lfm2MoeForCausalLM
Lfm2VlForConditionalGeneration LightOnOCRForConditionalGeneration
Llama4ForConditionalGeneration LlamaForCausalLM LlavaForConditionalGeneration
LlavaNextForConditionalGeneration LlavaNextVideoForConditionalGeneration
LlavaOnevisionForConditionalGeneration LongcatFlashForCausalLM MPTForCausalLM
Mamba2ForCausalLM MambaForCausalLM MellumForCausalLM MiDashengLMModel
MiMoForCausalLM MiMoV2FlashForCausalLM MiMoV2ForCausalLM
MiMoV2OmniForCausalLM MiniCPM3ForCausalLM MiniCPMForCausalLM
MiniMaxForCausalLM MiniMaxM1ForCausalLM MiniMaxM2ForCausalLM
MiniMaxText01ForCausalLM MiniMaxVL01ForConditionalGeneration
Mistral3ForConditionalGeneration MistralForCausalLM
MistralLarge3ForCausalLM MixtralForCausalLM Molmo2ForConditionalGeneration
MolmoForCausalLM Moondream3ForCausalLM MusicFlamingoForConditionalGeneration
NVLM_D_Model NemotronForCausalLM NemotronHForCausalLM OPTForCausalLM
Olmo2ForCausalLM Olmo3ForCausalLM OlmoForCausalLM OlmoHybridForCausalLM
OlmoeForCausalLM OpenCUAForConditionalGeneration
OpenPanguVLForConditionalGeneration OrionForCausalLM OuroForCausalLM
Ovis2_6ForCausalLM Ovis2_6_MoeForCausalLM
PaddleOCRVLForConditionalGeneration PaliGemmaForConditionalGeneration
PanguEmbeddedForCausalLM PanguProMoEV2ForCausalLM PanguUltraMoEForCausalLM
Param2MoEForCausalLM PersimmonForCausalLM Phi3ForCausalLM Phi3VForCausalLM
Phi4MMForCausalLM PhiForCausalLM PhiMoEForCausalLM
PixtralForConditionalGeneration Plamo2ForCausalLM Plamo3ForCausalLM
QWenLMHeadModel QianfanOCRForConditionalGeneration
Qwen2AudioForConditionalGeneration Qwen2ForCausalLM Qwen2MoeForCausalLM
Qwen2VLForConditionalGeneration Qwen2_5OmniThinkerForConditionalGeneration
Qwen2_5_VLForConditionalGeneration Qwen3ASRForConditionalGeneration
Qwen3ForCausalLM Qwen3MoeForCausalLM Qwen3NextForCausalLM
Qwen3OmniMoeThinkerForConditionalGeneration Qwen3VLForConditionalGeneration
Qwen3VLMoeForConditionalGeneration Qwen3_5ForConditionalGeneration
Qwen3_5MoeForConditionalGeneration Qwen4ExpForConditionalGeneration
QwenVLForConditionalGeneration
RWForCausalLM Rnj1ForCausalLM SarvamMLAForCausalLM SarvamMoEForCausalLM
SeedOssForCausalLM SkyworkR1VChatModel SmolLM3ForCausalLM
SmolVLMForConditionalGeneration SolarForCausalLM StableLMEpochForCausalLM
StableLmForCausalLM Starcoder2ForCausalLM Step1ForCausalLM
Step3VLForConditionalGeneration Step3p5ForCausalLM
StepVLForConditionalGeneration Tarsier2ForConditionalGeneration
TarsierForConditionalGeneration TeleChat2ForCausalLM TeleChat3ForCausalLM
TeleChatForCausalLM TeleFLMForCausalLM UltravoxModel
VoxtralForConditionalGeneration WhisperForConditionalGeneration
XverseForCausalLM Zamba2ForCausalLM
""".split()
)

GENERATIVE_TASKS = {
    "text-generation",
    "image-text-to-text",
    "visual-question-answering",
    "image-to-text",
    "video-text-to-text",
    "audio-text-to-text",
    "automatic-speech-recognition",
}


class CatalogError(RuntimeError):
    pass


def tr(zh: str, en: str) -> str:
    return en if LANGUAGE == "en" else zh


@dataclasses.dataclass
class GPU:
    index: int
    name: str
    memory_mib: int
    compute_capability: str = "unknown"


@dataclasses.dataclass
class Hardware:
    gpus: list[GPU]
    driver_version: str = "unknown"
    os_id: str = "unknown"
    os_version: str = "unknown"
    os_pretty: str = "unknown"
    machine: str = "unknown"
    cpu_model: str = "unknown"
    cpu_sockets: int = 0
    cpu_cores: int = 0
    cpu_threads: int = 0
    numa_nodes: int = 0
    memory_total_bytes: int = 0
    memory_available_bytes: int = 0
    swap_total_bytes: int = 0
    pcie_current_gen_min: int = 0
    pcie_current_width_min: int = 0
    pcie_max_gen_min: int = 0
    pcie_max_width_min: int = 0
    topology_matrix: str = ""
    topology_worst_path: str = "unknown"
    nvlink_pairs: int = 0
    disk_path: str = ""
    disk_total_bytes: int = 0
    disk_free_bytes: int = 0

    @property
    def count(self) -> int:
        return len(self.gpus)

    @property
    def min_memory_bytes(self) -> int:
        return min((g.memory_mib for g in self.gpus), default=0) * 1024**2

    @property
    def total_memory_bytes(self) -> int:
        return sum(g.memory_mib for g in self.gpus) * 1024**2


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)


def request_json(url: str, token: str | None = None, timeout: int = 20) -> Any:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        body = exc.read(512).decode("utf-8", "replace")
        raise CatalogError(f"HTTP {exc.code}: {url}: {body}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise CatalogError(tr(f"请求失败: {url}: {exc}", f"Request failed: {url}: {exc}")) from exc


def request_optional_json(url: str, token: str | None = None) -> dict[str, Any]:
    try:
        data = request_json(url, token=token)
        return data if isinstance(data, dict) else {}
    except CatalogError:
        return {}


def read_os_release() -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        with open("/etc/os-release", encoding="utf-8") as handle:
            for line in handle:
                if "=" not in line:
                    continue
                key, value = line.rstrip().split("=", 1)
                values[key] = value.strip().strip('"')
    except OSError:
        pass
    return values


def read_meminfo() -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                key, _, raw = line.partition(":")
                match = re.search(r"\d+", raw)
                if match:
                    values[key] = int(match.group()) * 1024
    except OSError:
        pass
    return values


def run_host_command(command: list[str], timeout: int = 10) -> str:
    try:
        completed = subprocess.run(
            command,
            check=True,
            text=True,
            capture_output=True,
            timeout=timeout,
            env={**os.environ, "LC_ALL": "C"},
        )
        return completed.stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return ""


def detect_cpu() -> tuple[str, int, int, int, int]:
    cpu_model = "unknown"
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as handle:
            for line in handle:
                if line.lower().startswith(("model name", "hardware")) and ":" in line:
                    cpu_model = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    threads = os.cpu_count() or 0
    sockets: set[str] = set()
    cores: set[tuple[str, str]] = set()
    nodes: set[str] = set()
    topology = run_host_command(["lscpu", "-p=SOCKET,CORE,NODE"])
    for line in topology.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split(",")]
        if len(parts) >= 3:
            socket, core, node = parts[:3]
            sockets.add(socket)
            cores.add((socket, core))
            if node not in {"", "-"}:
                nodes.add(node)
    return cpu_model, len(sockets), len(cores), threads, len(nodes)


def parse_topology(matrix: str, gpu_count: int) -> tuple[str, int]:
    paths: list[str] = []
    for line in matrix.splitlines():
        parts = line.split()
        if not parts or not re.fullmatch(r"GPU\d+", parts[0]):
            continue
        for value in parts[1 : 1 + gpu_count]:
            if value != "X":
                paths.append(value)
    if not paths:
        return "unknown", 0
    order = {"PIX": 1, "PXB": 2, "PHB": 3, "NODE": 4, "SYS": 5}

    def rank(value: str) -> int:
        if value.startswith("NV"):
            return 0
        return order.get(value, 6)

    worst = max(paths, key=rank)
    nvlink_pairs = sum(1 for value in paths if value.startswith("NV")) // 2
    return worst, nvlink_pairs


def topology_paths_for_tp(matrix: str, gpu_count: int, tp: int) -> list[str]:
    if tp <= 1:
        return []
    rows: dict[int, list[str]] = {}
    for line in matrix.splitlines():
        parts = line.split()
        if not parts or not re.fullmatch(r"GPU\d+", parts[0]):
            continue
        rows[int(parts[0][3:])] = parts[1 : 1 + gpu_count]
    paths: list[str] = []
    for start in range(0, gpu_count, tp):
        for left in range(start, start + tp):
            for right in range(left + 1, start + tp):
                if left in rows and right < len(rows[left]):
                    value = rows[left][right]
                    if value != "X":
                        paths.append(value)
    return paths


def worst_topology_path(paths: Iterable[str]) -> str:
    values = list(paths)
    if not values:
        return "not-required"
    order = {"PIX": 1, "PXB": 2, "PHB": 3, "NODE": 4, "SYS": 5}

    def rank(value: str) -> int:
        if value.startswith("NV"):
            return 0
        return order.get(value, 6)

    return max(values, key=rank)


def disk_capacity(path: str | None) -> tuple[str, int, int]:
    probe = os.path.abspath(path or "/data/llm-cluster/models")
    while not os.path.exists(probe) and probe != "/":
        probe = os.path.dirname(probe)
    try:
        stat = os.statvfs(probe)
        return probe, stat.f_blocks * stat.f_frsize, stat.f_bavail * stat.f_frsize
    except OSError:
        return probe, 0, 0


def detect_hardware(override: str | None = None, model_root: str | None = None) -> Hardware:
    if override:
        raw = json.loads(override)
        values = raw.get("gpus", []) if isinstance(raw, dict) else raw
        if not isinstance(values, list):
            raise CatalogError(tr("hardware-json 中的 gpus 必须是列表", "gpus in hardware-json must be a list"))
        gpus = [
            GPU(
                int(item.get("index", i)),
                str(item.get("name", "GPU")),
                int(item["memory_mib"]),
                str(item.get("compute_capability", "unknown")),
            )
            for i, item in enumerate(values)
        ]
        metadata = raw if isinstance(raw, dict) else {}
        field_names = {field.name for field in dataclasses.fields(Hardware)} - {"gpus"}
        extras = {name: metadata[name] for name in field_names if name in metadata}
        return Hardware(gpus, **extras)
    query = ",".join(
        (
            "index",
            "name",
            "memory.total",
            "compute_cap",
            "driver_version",
            "pcie.link.gen.current",
            "pcie.link.width.current",
            "pcie.link.gen.max",
            "pcie.link.width.max",
        )
    )
    output = run_host_command(["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits"])
    has_link_fields = bool(output)
    if not output:
        # Older nvidia-smi builds may reject one of the PCIe query fields.  A
        # failed optional link query must not turn an otherwise healthy host
        # into a false "no GPU" result.
        base_query = "index,name,memory.total,compute_cap,driver_version"
        output = run_host_command(
            ["nvidia-smi", f"--query-gpu={base_query}", "--format=csv,noheader,nounits"]
        )
    gpus: list[GPU] = []
    drivers: list[str] = []
    current_gens: list[int] = []
    current_widths: list[int] = []
    max_gens: list[int] = []
    max_widths: list[int] = []
    for line in output.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) >= 5:
            gpus.append(GPU(int(parts[0]), parts[1], int(parts[2]), parts[3]))
            drivers.append(parts[4])
            if has_link_fields and len(parts) >= 9:
                for target, value in (
                    (current_gens, parts[5]),
                    (current_widths, parts[6]),
                    (max_gens, parts[7]),
                    (max_widths, parts[8]),
                ):
                    try:
                        target.append(int(value))
                    except ValueError:
                        pass
    os_release = read_os_release()
    cpu_model, cpu_sockets, cpu_cores, cpu_threads, numa_nodes = detect_cpu()
    memory = read_meminfo()
    topology = run_host_command(["nvidia-smi", "topo", "-m"])
    worst_path, nvlink_pairs = parse_topology(topology, len(gpus))
    disk_path, disk_total, disk_free = disk_capacity(model_root)
    return Hardware(
        gpus=gpus,
        driver_version=drivers[0] if drivers else "unknown",
        os_id=os_release.get("ID", "unknown"),
        os_version=os_release.get("VERSION_ID", "unknown"),
        os_pretty=os_release.get("PRETTY_NAME", "unknown"),
        machine=platform.machine() or "unknown",
        cpu_model=cpu_model,
        cpu_sockets=cpu_sockets,
        cpu_cores=cpu_cores,
        cpu_threads=cpu_threads,
        numa_nodes=numa_nodes,
        memory_total_bytes=memory.get("MemTotal", 0),
        memory_available_bytes=memory.get("MemAvailable", 0),
        swap_total_bytes=memory.get("SwapTotal", 0),
        pcie_current_gen_min=min(current_gens, default=0),
        pcie_current_width_min=min(current_widths, default=0),
        pcie_max_gen_min=min(max_gens, default=0),
        pcie_max_width_min=min(max_widths, default=0),
        topology_matrix=topology,
        topology_worst_path=worst_path,
        nvlink_pairs=nvlink_pairs,
        disk_path=disk_path,
        disk_total_bytes=disk_total,
        disk_free_bytes=disk_free,
    )


def nested_config(config: dict[str, Any]) -> dict[str, Any]:
    for key in ("text_config", "language_config", "llm_config"):
        value = config.get(key)
        if isinstance(value, dict) and value:
            return value
    return config


def native_context(config: dict[str, Any]) -> int:
    cfg = nested_config(config)
    candidates = [
        cfg.get("max_position_embeddings"),
        cfg.get("seq_length"),
        cfg.get("model_max_length"),
        config.get("max_position_embeddings"),
    ]
    valid = [int(x) for x in candidates if isinstance(x, (int, float)) and x > 0]
    return max(valid, default=32768)


def kv_bytes_per_token(config: dict[str, Any]) -> int | None:
    """估算单个 Token 的全量 KV Cache 字节数。

    混合 GDN/全注意力模型只让 ``full_attention`` 层按上下文长度增长；
    线性注意力状态按请求固定分配，由运行时预留承担，不能按全部层重复计算。
    """

    cfg = nested_config(config)
    try:
        layer_types = cfg.get("layer_types")
        if isinstance(layer_types, list) and layer_types:
            layers = sum(str(item) == "full_attention" for item in layer_types)
            if layers <= 0:
                return None
        else:
            layers = int(cfg.get("num_hidden_layers") or cfg.get("n_layer"))
        kv_heads = int(
            cfg.get("num_key_value_heads")
            or cfg.get("multi_query_group_num")
            or cfg.get("num_attention_heads")
            or cfg.get("n_head")
        )
        head_dim = int(
            cfg.get("head_dim")
            or int(cfg.get("hidden_size") or cfg.get("n_embd"))
            // int(cfg.get("num_attention_heads") or cfg.get("n_head"))
        )
        # vLLM defaults to an fp16/bf16 KV cache even for most weight-only
        # quantized checkpoints. K and V each contribute one tensor.
        return 2 * layers * kv_heads * head_dim * 2
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def normalize_precision(config: dict[str, Any], model_id: str, tags: Iterable[str]) -> str:
    """把不同量化配置规范为管理员可识别的精度名称。"""

    qcfg = config.get("quantization_config")
    if isinstance(qcfg, dict):
        method = str(qcfg.get("quant_method") or qcfg.get("quantization_method") or "").lower()
        algorithm = str(qcfg.get("quant_algo") or "").lower()
        if "nvfp4" in algorithm or "nvfp4" in method:
            return "nvfp4"
        if method:
            bits = qcfg.get("bits")
            return f"{method}{bits or ''}"
    haystack = " ".join([model_id, *tags]).lower()
    for needle, label in (
        ("nvfp4", "nvfp4"),
        ("fp8", "fp8"),
        ("awq", "awq4"),
        ("gptq", "gptq4"),
        ("int4", "int4"),
        ("4bit", "int4"),
        ("gguf", "gguf"),
    ):
        if needle in haystack:
            return label
    dtype = str(config.get("torch_dtype") or nested_config(config).get("torch_dtype") or "unknown")
    return dtype.replace("torch.", "")


def capability_profile(model_id: str, task: str, config: dict[str, Any], tokenizer: dict[str, Any]) -> dict[str, Any]:
    lower = f"{model_id} {config.get('model_type', '')} {' '.join(config.get('architectures') or [])}".lower()
    chat_template = str(tokenizer.get("chat_template") or config.get("tokenizer_config", {}).get("chat_template") or "")
    image = bool(
        task in {"image-text-to-text", "visual-question-answering", "image-to-text", "video-text-to-text"}
        or config.get("vision_config")
        or re.search(r"(^|[-_/])(vl|vision|ocr|llava|pixtral|internvl|molmo)([-_/]|$)", lower)
    )
    ocr = image and bool(re.search(r"ocr|ornith|qwen.*vl|internvl|glm.*v|deepseek.*vl|pixtral", lower))

    tool_parser = ""
    if "ornith" in lower:
        tool_parser = "qwen3_xml"
    elif "qwen3" in lower and ("tools" in chat_template or "tool_call" in chat_template):
        tool_parser = "qwen3_xml"
    elif "hermes" in lower:
        tool_parser = "hermes"
    elif "mistral" in lower and ("instruct" in lower or "tools" in chat_template):
        tool_parser = "mistral"
    elif "deepseek-v3" in lower or "deepseek_v3" in lower:
        tool_parser = "deepseek_v3"
    elif "llama-4" in lower or "llama4" in lower:
        tool_parser = "llama4_json"

    reasoning_parser = ""
    if "qwen3" in lower:
        reasoning_parser = "qwen3"
    elif "deepseek-r1" in lower or "deepseek_r1" in lower:
        reasoning_parser = "deepseek_r1"
    elif "deepseek-v3" in lower or "deepseek_v3" in lower:
        reasoning_parser = "deepseek_v3"
    elif "gpt-oss" in lower or "gptoss" in lower:
        reasoning_parser = "gptoss"
    elif "kimi-k2" in lower or "kimi_k2" in lower:
        reasoning_parser = "kimi_k2"
    elif "olmo3" in lower:
        reasoning_parser = "olmo3"
    elif "step3" in lower:
        reasoning_parser = "step3"

    thinking_toggle = bool(
        reasoning_parser == "qwen3"
        and ("enable_thinking" in chat_template or "qwen3" in lower)
    )
    return {
        "image_input": image,
        "ocr_optimized": ocr,
        "tool_parser": tool_parser,
        "reasoning_parser": reasoning_parser,
        "thinking_toggle": thinking_toggle,
    }


def architectures(config: dict[str, Any]) -> list[str]:
    values = config.get("architectures")
    if not values:
        values = nested_config(config).get("architectures")
    return [str(x) for x in (values or []) if isinstance(x, str)]


def qwen38_ple_weight_bytes(config: dict[str, Any]) -> int:
    """返回 Qwen3.8 Flash Next 可放到主内存的 PLE 表大小。

    参数：
        config: Hugging Face 模型配置。

    返回：
        按检查点声明精度计算的 PLE 字节数；缺少必要字段时返回 0。
    """

    cfg = nested_config(config)
    try:
        base_rows = int(cfg.get("ngram_vocab_size_base"))
        embedding_dim = int(cfg.get("ple_embed_dim"))
        layer_count = max(1, len(cfg.get("ple_layer_ids") or []))
    except (TypeError, ValueError):
        return 0
    dtype = str(cfg.get("ple_embedding_dtype") or cfg.get("dtype") or "").lower()
    bytes_per_value = 1 if "float8" in dtype or dtype in {"fp8", "f8_e4m3"} else 2
    return base_rows * embedding_dim * layer_count * bytes_per_value


def model_runtime_profile(
    model_id: str, config: dict[str, Any], precision: str
) -> dict[str, Any]:
    """为目录结果生成可由安装器和多模型控制器共同消费的运行时建议。"""

    archs = architectures(config)
    if QWEN38_ARCHITECTURE in archs:
        defaults = qwen38_runtime_defaults()
        return {
            "kind": "qwen38-flash-next-preview",
            "image": QWEN38_IMAGE,
            "minimum_tp": 2,
            "max_num_seqs": defaults["max_num_seqs"],
            "runtime_extra_bytes_per_gpu": 4 * GIB,
            "ple_cpu_offload": defaults["ple_cpu_offload"],
            "ple_offload_bytes": qwen38_ple_weight_bytes(config),
            "enable_expert_parallel": defaults["enable_expert_parallel"],
            "enable_prefix_caching": defaults["enable_prefix_caching"],
            "enable_flashinfer_autotune": defaults["enable_flashinfer_autotune"],
            "disable_custom_all_reduce": defaults["disable_custom_all_reduce"],
            "mtp_speculative_tokens": defaults["mtp_speculative_tokens"],
            "kv_cache_dtype": defaults["kv_cache_dtype"],
            "startup_parallelism_cap": 1,
            "preview": True,
            "precision": precision,
            "model_id": model_id,
        }
    return {
        "kind": "standard",
        "image": f"vllm/vllm-openai:v{VLLM_COMPAT_VERSION}",
        "minimum_tp": 1,
        "max_num_seqs": 7,
        "runtime_extra_bytes_per_gpu": 0,
        "ple_cpu_offload": False,
        "ple_offload_bytes": 0,
        "enable_expert_parallel": False,
        "enable_prefix_caching": True,
        "enable_flashinfer_autotune": True,
        "disable_custom_all_reduce": False,
        "mtp_speculative_tokens": 0,
        "kv_cache_dtype": "auto",
        "startup_parallelism_cap": 8,
        "preview": False,
        "precision": precision,
        "model_id": model_id,
    }


def yarn_factor_for_context(native_limit: int, requested_limit: int) -> float:
    """为 Qwen3.8 Flash Next 选择受支持的静态 YaRN 档位。

    原生范围返回 1；超过原生范围后只使用官方说明中的 2× 或 4× 档位。
    超过 4× 原生范围会失败，避免把未经支持的外推比例写入 Worker。
    """

    if requested_limit <= native_limit:
        return 1.0
    if requested_limit <= native_limit * 2:
        return 2.0
    if requested_limit <= native_limit * 4:
        return 4.0
    raise ValueError("Qwen3.8 Flash Next 最多支持静态 YaRN 4× 上下文")


def is_mlx_weight_repository(model: dict[str, Any]) -> bool:
    model_id = str(model.get("id") or "").lower()
    owner = model_id.split("/", 1)[0]
    tags = {str(tag).lower() for tag in (model.get("tags") or [])}
    return bool(
        owner == "mlx-community"
        or tags.intersection({"mlx", "mlx-lm", "mlx-vlm"})
        or re.search(r"(?:^|[-_/])mlx(?:[-_/]|$)", model_id)
    )


def weight_size_from_siblings(siblings: Iterable[dict[str, Any]]) -> int:
    files = []
    for item in siblings:
        name = str(item.get("rfilename") or item.get("name") or "")
        size = int(item.get("size") or (item.get("lfs") or {}).get("size") or 0)
        files.append((name, size))
    safe = sum(size for name, size in files if name.endswith(".safetensors") and "adapter" not in name.lower())
    if safe:
        return safe
    return sum(
        size
        for name, size in files
        if re.search(r"(?:pytorch_model|model).*\.bin$", name) and "adapter" not in name.lower()
    )


def hf_raw_file(model_id: str, revision: str, filename: str, token: str | None) -> dict[str, Any]:
    url = (
        f"{HF_ENDPOINT}/{urllib.parse.quote(model_id, safe='/')}/raw/"
        f"{urllib.parse.quote(revision, safe='')}/{filename}"
    )
    return request_optional_json(url, token=token)


def hf_search(query: str, task: str, limit: int, token: str | None) -> list[dict[str, Any]]:
    pipeline_tags = ["text-generation", "image-text-to-text"] if task == "auto" else [
        "image-text-to-text" if task == "vision" else "text-generation"
    ]
    found: dict[str, dict[str, Any]] = {}
    for pipeline_tag in pipeline_tags:
        params = {
            "search": query,
            "pipeline_tag": pipeline_tag,
            "apps": "vllm",
            "sort": "downloads",
            "direction": "-1",
            "limit": str(max(limit * 2, 10)),
            "full": "true",
            "config": "true",
        }
        url = f"{HF_ENDPOINT}/api/models?{urllib.parse.urlencode(params)}"
        for item in request_json(url, token=token):
            found[str(item["id"])] = item

    def enrich(item: dict[str, Any]) -> dict[str, Any]:
        model_id = str(item["id"])
        detail = request_json(
            f"{HF_ENDPOINT}/api/models/{urllib.parse.quote(model_id, safe='/')}?blobs=true",
            token=token,
        )
        revision = str(detail.get("sha") or item.get("sha") or "main")
        config = hf_raw_file(model_id, revision, "config.json", token) or detail.get("config") or item.get("config") or {}
        tokenizer = hf_raw_file(model_id, revision, "tokenizer_config.json", token) or config.get("tokenizer_config") or {}
        return {
            "source": "huggingface",
            "id": model_id,
            "revision": revision,
            "task": str(detail.get("pipeline_tag") or item.get("pipeline_tag") or "unknown"),
            "downloads": int(detail.get("downloads") or item.get("downloads") or 0),
            "likes": int(detail.get("likes") or item.get("likes") or 0),
            "license": str((detail.get("cardData") or {}).get("license") or "unknown"),
            "gated": bool(detail.get("gated")),
            "private": bool(detail.get("private")),
            "tags": list(detail.get("tags") or []),
            "params": int((detail.get("safetensors") or {}).get("total") or 0),
            "weight_bytes": weight_size_from_siblings(detail.get("siblings") or []),
            "config": config,
            "tokenizer": tokenizer,
        }

    results: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        future_map = {pool.submit(enrich, item): item for item in found.values()}
        for future in concurrent.futures.as_completed(future_map):
            try:
                results.append(future.result())
            except CatalogError as exc:
                eprint(tr(f"[catalog] Hugging Face 条目跳过: {exc}", f"[catalog] Skipped Hugging Face entry: {exc}"))
    return results


def ms_raw_file(model_id: str, revision: str, filename: str, token: str | None) -> dict[str, Any]:
    url = f"{MS_ENDPOINT}/models/{urllib.parse.quote(model_id, safe='/')}/resolve/{urllib.parse.quote(revision, safe='')}/{filename}"
    return request_optional_json(url, token=token)


def ms_resolve_revision(
    model_id: str, revision: str | None, token: str | None
) -> str:
    """把 ModelScope 分支或标签解析为仓库当前不可变提交 SHA。"""

    requested = str(revision or "master").strip()
    if re.fullmatch(r"[0-9a-fA-F]{40,64}", requested):
        return requested.lower()
    params = urllib.parse.urlencode(
        {"Revision": requested, "Recursive": "true"}
    )
    payload = request_json(
        f"{MS_ENDPOINT}/api/v1/models/"
        f"{urllib.parse.quote(model_id, safe='/')}/repo/files?{params}",
        token=token,
    )
    files = (payload.get("Data") or {}).get("Files") or []
    commits = [
        (int(item.get("CommittedDate") or 0), str(item.get("Revision") or ""))
        for item in files
        if isinstance(item, dict)
        and re.fullmatch(r"[0-9a-fA-F]{40,64}", str(item.get("Revision") or ""))
    ]
    if not commits:
        raise CatalogError(
            tr(
                "ModelScope 没有返回可固定的仓库提交 SHA",
                "ModelScope did not return an immutable repository commit SHA",
            )
        )
    return max(commits)[1].lower()


def ms_search(query: str, task: str, limit: int, token: str | None) -> list[dict[str, Any]]:
    tasks = ["text-generation", "image-text-to-text"] if task == "auto" else [
        "image-text-to-text" if task == "vision" else "text-generation"
    ]
    found: dict[str, dict[str, Any]] = {}
    for model_task in tasks:
        params = {
            "search": query,
            "sort": "downloads",
            "page_size": str(min(max(limit * 2, 10), 50)),
            "filter.task": model_task,
            "filter.library": "safetensors",
        }
        payload = request_json(f"{MS_ENDPOINT}/openapi/v1/models?{urllib.parse.urlencode(params)}", token=token)
        for item in (payload.get("data") or {}).get("models") or []:
            found[str(item["id"])] = item

    def enrich(item: dict[str, Any]) -> dict[str, Any]:
        model_id = str(item["id"])
        revision = "master"
        config = ms_raw_file(model_id, revision, "config.json", token)
        tokenizer = ms_raw_file(model_id, revision, "tokenizer_config.json", token)
        return {
            "source": "modelscope",
            "id": model_id,
            "revision": revision,
            "task": str(((item.get("tasks") or ["unknown"])[0])),
            "downloads": int(item.get("downloads") or 0),
            "likes": int(item.get("likes") or 0),
            "license": str(item.get("license") or "unknown"),
            "gated": bool(item.get("gated")),
            "private": bool(item.get("private")),
            "tags": list(item.get("tags") or []),
            "params": int(item.get("params") or 0),
            "weight_bytes": int(item.get("file_size") or 0),
            "config": config,
            "tokenizer": tokenizer,
        }

    results: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        future_map = {pool.submit(enrich, item): item for item in found.values()}
        for future in concurrent.futures.as_completed(future_map):
            try:
                results.append(future.result())
            except CatalogError as exc:
                eprint(tr(f"[catalog] ModelScope 条目跳过: {exc}", f"[catalog] Skipped ModelScope entry: {exc}"))
    return results


def inspect_hf(model_id: str, revision: str | None, token: str | None) -> dict[str, Any]:
    detail = request_json(
        f"{HF_ENDPOINT}/api/models/{urllib.parse.quote(model_id, safe='/')}?blobs=true",
        token=token,
    )
    rev = revision or str(detail.get("sha") or "main")
    config = hf_raw_file(model_id, rev, "config.json", token) or detail.get("config") or {}
    tokenizer = hf_raw_file(model_id, rev, "tokenizer_config.json", token) or config.get("tokenizer_config") or {}
    return {
        "source": "huggingface",
        "id": model_id,
        "revision": rev,
        "task": str(detail.get("pipeline_tag") or "unknown"),
        "downloads": int(detail.get("downloads") or 0),
        "likes": int(detail.get("likes") or 0),
        "license": str((detail.get("cardData") or {}).get("license") or "unknown"),
        "gated": bool(detail.get("gated")),
        "private": bool(detail.get("private")),
        "tags": list(detail.get("tags") or []),
        "params": int((detail.get("safetensors") or {}).get("total") or 0),
        "weight_bytes": weight_size_from_siblings(detail.get("siblings") or []),
        "config": config,
        "tokenizer": tokenizer,
    }


def inspect_ms(model_id: str, revision: str | None, token: str | None) -> dict[str, Any]:
    payload = request_json(
        f"{MS_ENDPOINT}/openapi/v1/models/{urllib.parse.quote(model_id, safe='/')}",
        token=token,
    )
    detail = payload.get("data") or {}
    rev = ms_resolve_revision(model_id, revision, token)
    config = ms_raw_file(model_id, rev, "config.json", token)
    tokenizer = ms_raw_file(model_id, rev, "tokenizer_config.json", token)
    return {
        "source": "modelscope",
        "id": model_id,
        "revision": rev,
        "task": str(((detail.get("tasks") or ["unknown"])[0])),
        "downloads": int(detail.get("downloads") or 0),
        "likes": int(detail.get("likes") or 0),
        "license": str(detail.get("license") or "unknown"),
        "gated": bool(detail.get("gated")),
        "private": bool(detail.get("private")),
        "tags": list(detail.get("tags") or []),
        "params": int(detail.get("params") or 0),
        "weight_bytes": int(detail.get("file_size") or 0),
        "config": config,
        "tokenizer": tokenizer,
    }


def evaluate(model: dict[str, Any], hardware: Hardware, utilization: float, requested_context: int) -> dict[str, Any]:
    """核验模型元数据并为当前硬件生成保守部署计划。"""

    config = model.get("config") or {}
    archs = architectures(config)
    supported = [arch for arch in archs if arch in SUPPORTED_ARCHITECTURES]
    tags = model.get("tags") or []
    model["architectures"] = archs
    model["supported_architectures"] = supported
    model["precision"] = normalize_precision(config, model["id"], tags)
    runtime_profile = model_runtime_profile(
        model["id"], config, model["precision"]
    )
    model["runtime_profile"] = runtime_profile
    model["native_context"] = native_context(config)
    model["capabilities"] = capability_profile(model["id"], model["task"], config, model.get("tokenizer") or {})
    model["trust_remote_code"] = bool(config.get("auto_map"))

    reasons: list[str] = []
    if model["task"] not in GENERATIVE_TASKS:
        reasons.append(tr(f"任务 {model['task']} 不是生成式 LLM/VLM", f"Task {model['task']} is not a generative LLM/VLM task"))
    if not supported:
        arch_text = ",".join(archs) or tr("未知", "unknown")
        reasons.append(tr(f"架构 {arch_text} 不在 vLLM {VLLM_COMPAT_VERSION} 保守清单", f"Architecture {arch_text} is not in the conservative vLLM {VLLM_COMPAT_VERSION} allowlist"))
    if is_mlx_weight_repository(model):
        reasons.append(
            tr(
                "MLX 转换权重面向 Apple MLX，不能作为 vLLM/CUDA 权重部署",
                "MLX-converted weights target Apple MLX and cannot be deployed as vLLM/CUDA weights",
            )
        )
    source_token = (
        os.environ.get("HF_TOKEN")
        if model.get("source") == "huggingface"
        else os.environ.get("MODELSCOPE_API_TOKEN") or os.environ.get("MODELSCOPE_API_KEY")
    )
    if model.get("private") and not source_token:
        reasons.append(tr("私有模型需要有效访问令牌", "A private model requires a valid access token"))
    if model.get("gated") and not os.environ.get("HF_TOKEN"):
        reasons.append(tr("受限模型需要先设置 Hugging Face Token 并接受许可", "A gated model requires an HF token and prior license acceptance"))
    weight_bytes = int(model.get("weight_bytes") or 0)
    params = int(model.get("params") or 0)
    if weight_bytes <= 0:
        reasons.append(tr("无法取得权重大小，不能安全规划显存", "Weight size is unavailable, so VRAM cannot be planned safely"))
    if params and weight_bytes and weight_bytes < params * 0.40:
        reasons.append(tr("仓库体积显著小于参数量，可能是适配器或非完整权重", "Repository size is too small for its parameter count and may contain an adapter or incomplete weights"))
    if not hardware.count:
        reasons.append(tr("未检测到 NVIDIA GPU", "No NVIDIA GPU was detected"))
    if runtime_profile["kind"] == "qwen38-flash-next-preview" and model["precision"] == "nvfp4":
        known_compute_capabilities = []
        for gpu in hardware.gpus:
            try:
                known_compute_capabilities.append(int(str(gpu.compute_capability).split(".", 1)[0]))
            except ValueError:
                continue
        if known_compute_capabilities and min(known_compute_capabilities) < 10:
            reasons.append(
                tr(
                    "NVFP4 检查点需要 NVIDIA Blackwell GPU；当前检测到较早的计算能力",
                    "The NVFP4 checkpoint requires NVIDIA Blackwell GPUs; an older compute capability was detected",
                )
            )

    # 默认保留模型原生长上下文。Qwen3.8 Flash Next 只有在用户明确请求超过
    # 262K 时才启用静态 YaRN，避免短上下文也承受位置外推的质量代价。
    native_limit = int(model["native_context"])
    if runtime_profile["kind"] == "qwen38-flash-next-preview":
        desired = requested_context or native_limit
        desired = max(8192, min(desired, native_limit * 4))
        yarn_factor = yarn_factor_for_context(native_limit, desired)
    else:
        desired = requested_context or min(native_limit, 262144)
        desired = max(8192, min(desired, native_limit, 262144))
        yarn_factor = 1.0
    kv_bpt = kv_bytes_per_token(config)
    if kv_bpt is None:
        desired = min(desired, 32768)
    plan: dict[str, Any] | None = None
    if not reasons:
        min_mem = hardware.min_memory_bytes
        minimum_tp = int(runtime_profile["minimum_tp"])
        host_memory_rejected = False
        for tp in (1, 2, 4, 8):
            if tp < minimum_tp:
                continue
            if tp > hardware.count or hardware.count % tp:
                continue
            usable = min_mem * utilization
            ple_offload_bytes = (
                int(runtime_profile["ple_offload_bytes"])
                if runtime_profile["ple_cpu_offload"]
                else 0
            )
            resident_weight_bytes = max(0, weight_bytes - ple_offload_bytes)
            weight_per_gpu = resident_weight_bytes * 1.08 / tp
            runtime = max(4 * GIB, min_mem * 0.06) + int(
                runtime_profile["runtime_extra_bytes_per_gpu"]
            )
            if model["capabilities"]["image_input"]:
                runtime += 3 * GIB
            context = desired
            while context >= 8192:
                kv_one = (kv_bpt * context / tp * 1.10) if kv_bpt else 4 * GIB
                free_for_kv = usable - weight_per_gpu - runtime
                if free_for_kv >= kv_one:
                    reference_context = min(context, 32768)
                    kv_reference = (kv_bpt * reference_context / tp * 1.10) if kv_bpt else 4 * GIB
                    estimated_seq_capacity = (
                        max(1, min(16, int(free_for_kv // kv_reference))) if kv_bpt else 1
                    )
                    full_context_capacity = (
                        max(1, int(free_for_kv // kv_one)) if kv_bpt else 1
                    )
                    # 调度序列数按典型 32K 请求估算，不代表每个序列都能同时占满
                    # 最大窗口；完整窗口容量单独显示，避免把 8 个槽误读成 8×1M。
                    seqs = min(
                        int(runtime_profile["max_num_seqs"]),
                        estimated_seq_capacity,
                    )
                    replicas = hardware.count // tp
                    host_reserve = max(16 * GIB, int(hardware.memory_total_bytes * 0.05))
                    host_offload_per_instance = int(ple_offload_bytes * 1.10)
                    host_running_required = host_reserve + replicas * host_offload_per_instance
                    if (
                        hardware.memory_total_bytes
                        and host_running_required > hardware.memory_total_bytes
                    ):
                        host_memory_rejected = True
                        break
                    plan = {
                        "tp": tp,
                        "replicas": replicas,
                        "max_model_len": context,
                        "max_num_seqs": seqs,
                        "estimated_seq_capacity": estimated_seq_capacity,
                        "estimated_full_context_sequences": full_context_capacity,
                        "sequence_reference_context": reference_context,
                        "estimated_weight_per_gpu": int(weight_per_gpu),
                        "estimated_gpu_resident_weight_total": resident_weight_bytes,
                        "estimated_runtime_reserve_per_gpu": int(runtime),
                        "estimated_kv_bytes_per_token_total": kv_bpt or 0,
                        "usable_per_gpu": int(usable),
                        "yarn_factor": yarn_factor,
                        "ple_cpu_offload": bool(runtime_profile["ple_cpu_offload"]),
                        "ple_offload_bytes_per_instance": ple_offload_bytes,
                        "host_memory_per_running_instance": host_offload_per_instance,
                        "host_memory_running_required": host_running_required,
                    }
                    tp_paths = topology_paths_for_tp(hardware.topology_matrix, hardware.count, tp)
                    tp_worst_path = worst_topology_path(tp_paths)
                    memory_per_start = int(weight_bytes * 1.15 + 4 * GIB)
                    if hardware.memory_available_bytes:
                        memory_budget = max(0, hardware.memory_available_bytes - host_reserve)
                        memory_parallelism = max(1, memory_budget // max(1, memory_per_start))
                    else:
                        memory_parallelism = min(2, plan["replicas"])
                    cpu_parallelism = (
                        max(1, hardware.cpu_threads // 8)
                        if hardware.cpu_threads
                        else min(2, plan["replicas"])
                    )
                    startup_parallelism = max(
                        1,
                        min(
                            plan["replicas"],
                            memory_parallelism,
                            cpu_parallelism,
                            int(runtime_profile["startup_parallelism_cap"]),
                        ),
                    )
                    disk_required = weight_bytes + weight_bytes // 3 + 10 * GIB
                    warnings: list[str] = []
                    if tp > 1 and not all(path.startswith("NV") for path in tp_paths):
                        warnings.append(
                            tr(
                                f"TP{tp} 组最慢链路为 {tp_worst_path}，无全组 NVLink；功能可用但跨卡通信可能限制吞吐",
                                f"The slowest TP{tp} group link is {tp_worst_path} without full-group NVLink; it is functional, but inter-GPU communication may limit throughput",
                            )
                        )
                    if tp > 1 and hardware.pcie_max_width_min and hardware.pcie_max_width_min < 16:
                        warnings.append(
                            tr(
                                f"至少一张 GPU 的最大 PCIe 链路仅 x{hardware.pcie_max_width_min}",
                                f"At least one GPU has a maximum PCIe link width of only x{hardware.pcie_max_width_min}",
                            )
                        )
                    if hardware.memory_available_bytes and hardware.memory_available_bytes < memory_per_start + host_reserve:
                        warnings.append(
                            tr(
                                "当前可用系统内存低于单实例保守加载预算；启动前应释放内存",
                                "Available system memory is below the conservative single-replica loading budget; free memory before startup",
                            )
                        )
                    if hardware.disk_free_bytes and hardware.disk_free_bytes < disk_required:
                        warnings.append(
                            tr(
                                f"默认模型盘可用 {human_size(hardware.disk_free_bytes)}，低于下载与临时空间预算 {human_size(disk_required)}；需选择其他目录或复用本地权重",
                                f"The default model disk has {human_size(hardware.disk_free_bytes)} free, below the {human_size(disk_required)} download and temporary-space budget; choose another directory or reuse local weights",
                            )
                        )
                    if runtime_profile["kind"] == "qwen38-flash-next-preview":
                        warnings.append(
                            tr(
                                "Qwen3.8 Flash Next 仍使用专用预览镜像；默认关闭 MTP 和前缀缓存，先完成单个 TP2 金丝雀验收",
                                "Qwen3.8 Flash Next still uses a dedicated preview image; MTP and prefix caching stay disabled until a single TP2 canary passes",
                            )
                        )
                        if yarn_factor > 1:
                            warnings.append(
                                tr(
                                    f"当前部署启用静态 YaRN {yarn_factor:g}×；短上下文质量也会受该缩放影响，全部实例必须保持一致并重新做质量验收",
                                    f"This deployment uses static YaRN {yarn_factor:g}x; short-context quality is affected too, so every replica must remain aligned and pass a new quality acceptance",
                                )
                            )
                    plan.update(
                        {
                            "startup_parallelism": startup_parallelism,
                            "host_memory_per_starting_instance": memory_per_start,
                            "host_memory_reserve": host_reserve,
                            "memory_parallelism_limit": int(memory_parallelism),
                            "cpu_parallelism_limit": int(cpu_parallelism),
                            "tp_topology_worst_path": tp_worst_path,
                            "disk_required": disk_required,
                            "disk_free": hardware.disk_free_bytes,
                            "disk_path": hardware.disk_path,
                            "warnings": warnings,
                        }
                    )
                    break
                context //= 2
            if plan:
                break
        if not plan:
            if host_memory_rejected:
                reasons.append(
                    tr(
                        "系统内存不足以同时保留全部实例的 PLE offload 与操作系统余量",
                        "System memory cannot hold PLE offload for all replicas plus operating-system headroom",
                    )
                )
            else:
                reasons.append(tr("在当前 GPU 数量/显存及至少 8K 上下文下无法保守部署", "The model cannot be conservatively deployed on the available GPUs/VRAM with at least an 8K context"))

    model["plan"] = plan
    model["installable"] = not reasons and plan is not None
    model["rejection_reasons"] = reasons
    score = math.log10(max(1, int(model.get("downloads") or 0))) * 10
    score += math.log10(max(1, int(model.get("likes") or 0))) * 3
    lower_id = model["id"].lower()
    if re.search(r"instruct|chat|it(?:-|$)", lower_id):
        score += 12
    if model["capabilities"]["tool_parser"]:
        score += 4
    if model["capabilities"]["thinking_toggle"]:
        score += 3
    if plan:
        score += min(8, plan["replicas"]) * 2 - plan["tp"]
        if plan["tp"] > 1 and not str(plan["tp_topology_worst_path"]).startswith("NV"):
            score -= 8
        score -= len(plan.get("warnings") or []) * 2
    model["recommendation_score"] = round(score, 2)
    return model


def strip_internal(model: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in model.items() if k not in {"config", "tokenizer"}}


def human_size(value: int) -> str:
    if not value:
        return "?"
    return f"{value / GIB:.1f}G"


def render_hardware_summary(hardware: Hardware) -> None:
    print()
    print(tr("本机只读体检", "Read-only host preflight"))
    print("=" * 72)
    print(f"{tr('操作系统', 'Operating system')}: {hardware.os_pretty} ({hardware.machine})")
    print(
        f"CPU: {hardware.cpu_model}; "
        f"{hardware.cpu_sockets} {tr('路', 'socket(s)')}, "
        f"{hardware.cpu_cores} {tr('核', 'core(s)')}, "
        f"{hardware.cpu_threads} {tr('线程', 'thread(s)')}, "
        f"NUMA={hardware.numa_nodes}"
    )
    print(
        f"{tr('内存', 'Memory')}: {tr('总计', 'total')} {human_size(hardware.memory_total_bytes)}, "
        f"{tr('可用', 'available')} {human_size(hardware.memory_available_bytes)}, "
        f"Swap {human_size(hardware.swap_total_bytes)}"
    )
    print(f"NVIDIA {tr('驱动', 'driver')}: {hardware.driver_version}")
    print(f"GPU: {hardware.count}")
    for gpu in hardware.gpus:
        print(f"  GPU {gpu.index}: {gpu.name}, {gpu.memory_mib} MiB, CC {gpu.compute_capability}")
    print(
        f"PCIe: {tr('当前最低', 'current minimum')} Gen{hardware.pcie_current_gen_min} x{hardware.pcie_current_width_min}; "
        f"{tr('最大最低', 'minimum maximum capability')} Gen{hardware.pcie_max_gen_min} x{hardware.pcie_max_width_min}"
    )
    print(
        f"{tr('GPU 拓扑最慢路径', 'Slowest GPU topology path')}: {hardware.topology_worst_path}; "
        f"NVLink {tr('配对数', 'pair count')}={hardware.nvlink_pairs}"
    )
    print(
        f"{tr('模型目录所在磁盘', 'Model-directory filesystem')}: {hardware.disk_path or '?'}; "
        f"{tr('可用', 'free')} {human_size(hardware.disk_free_bytes)} / "
        f"{tr('总计', 'total')} {human_size(hardware.disk_total_bytes)}"
    )
    if hardware.topology_matrix:
        print()
        print(tr("nvidia-smi GPU/NUMA 拓扑：", "nvidia-smi GPU/NUMA topology:"))
        print(hardware.topology_matrix)
    print()
    print(
        tr(
            "说明：拓扑与 PCIe 是只读能力快照，不等于实际 NCCL 带宽测试；真实吞吐仍需部署后的业务压测。",
            "Note: topology and PCIe data are a read-only capability snapshot, not an active NCCL bandwidth test. Real throughput still requires post-deployment workload benchmarks.",
        )
    )


def capability_text(model: dict[str, Any]) -> str:
    caps = model["capabilities"]
    values = [tr("文本", "text")]
    if caps["image_input"]:
        values.append(tr("图片", "image"))
    if caps["ocr_optimized"]:
        values.append("OCR")
    if caps["tool_parser"]:
        values.append(tr("工具", "tools"))
    if caps["reasoning_parser"]:
        values.append(tr("思考", "reasoning"))
    if caps["thinking_toggle"]:
        values.append(tr("可关闭思考", "reasoning-toggle"))
    return "/".join(values)


def render_table(models: list[dict[str, Any]], show_rejected: bool = False) -> None:
    visible = [m for m in models if m["installable"] or show_rejected]
    if not visible:
        print(tr("没有找到同时满足 vLLM 兼容与当前硬件容量要求的模型。", "No model satisfies both vLLM compatibility and this host's capacity requirements."))
        return
    headers = ["#", tr("来源", "source"), tr("模型", "model"), tr("权重", "weights"), tr("精度", "precision"), tr("计划", "plan"), tr("能力", "capabilities"), tr("结论", "result")]
    rows: list[list[str]] = []
    for i, model in enumerate(visible, 1):
        plan = model.get("plan")
        plan_text = (
            f"TP{plan['tp']}×{plan['replicas']} ctx={plan['max_model_len']} seq={plan['max_num_seqs']} start={plan.get('startup_parallelism', '?')}"
            if plan
            else tr("不可部署", "rejected")
        )
        conclusion = tr("推荐", "recommended") if model["installable"] else "; ".join(model["rejection_reasons"][:2])
        rows.append(
            [
                str(i),
                "HF" if model["source"] == "huggingface" else "MS",
                model["id"],
                human_size(int(model.get("weight_bytes") or 0)),
                model["precision"],
                plan_text,
                capability_text(model),
                conclusion,
            ]
        )
    widths = [min(44, max(len(str(row[i])) for row in [headers, *rows])) for i in range(len(headers))]
    print("  ".join(headers[i].ljust(widths[i]) for i in range(len(headers))))
    print("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        clipped = [value if len(value) <= widths[i] else value[: widths[i] - 1] + "…" for i, value in enumerate(row)]
        print("  ".join(clipped[i].ljust(widths[i]) for i in range(len(headers))))
    print()
    print(
        tr(
            "“推荐”表示候选已通过任务、完整权重、非平台专用格式、vLLM 架构、NVIDIA GPU 显存与至少 8K 上下文门禁。",
            '“Recommended” means the candidate passed task, complete-weight, non-platform-specific format, vLLM architecture, NVIDIA GPU memory, and minimum 8K-context gates.',
        )
    )
    print(
        tr(
            "排序综合下载量、点赞、Instruct/Chat、已知工具/思考协议、可形成的副本数和 TP 成本；选择后会显示本机详细计划与逐项原因。",
            "Ranking combines downloads, likes, Instruct/Chat signals, known tool/reasoning protocols, replica count, and TP cost. Select a model to review the host-specific plan and reasons.",
        )
    )


def render_selection_summary(model: dict[str, Any]) -> None:
    if not model.get("installable") or not model.get("plan"):
        raise CatalogError(tr("所选模型未通过兼容/硬件门禁", "The selected model did not pass compatibility or hardware gates"))
    plan = model["plan"]
    caps = model["capabilities"]
    arch = ",".join(model.get("supported_architectures") or model.get("architectures") or [])
    gpu_count = int(plan["tp"]) * int(plan["replicas"])
    kv_budget = max(
        0,
        int(plan["usable_per_gpu"])
        - int(plan["estimated_weight_per_gpu"])
        - int(plan["estimated_runtime_reserve_per_gpu"]),
    )
    score = model.get("recommendation_score", 0)
    print()
    print(tr("所选模型详细计划", "Selected model: detailed plan"))
    print("=" * 72)
    print(f"{tr('来源', 'Source')}:        {model['source']}")
    print(f"{tr('模型', 'Model')}:        {model['id']}@{model['revision']}")
    print(f"{tr('架构', 'Architecture')}: {arch}")
    print(f"{tr('权重/精度', 'Weights/precision')}: {human_size(int(model.get('weight_bytes') or 0))} / {model['precision']}")
    print(f"{tr('能力', 'Capabilities')}: {capability_text(model)}")
    print(f"{tr('硬件拓扑', 'Hardware topology')}: {gpu_count} GPU -> TP{plan['tp']} × {plan['replicas']} {tr('个独立实例', 'independent replicas')}")
    print(f"{tr('TP 组最慢链路', 'Slowest TP-group link')}: {plan.get('tp_topology_worst_path', 'unknown')}")
    print(f"{tr('上下文', 'Context')}: {plan['max_model_len']} tokens ({tr('模型原生上限', 'model-native limit')} {model['native_context']})")
    print(f"max-num-seqs: {plan['max_num_seqs']} ({tr('按参考上下文估算容量', 'estimated capacity at reference context')} {plan['estimated_seq_capacity']} @ {plan['sequence_reference_context']} tokens)")
    print(f"{tr('每卡可用预算', 'Usable budget per GPU')}: {human_size(int(plan['usable_per_gpu']))}")
    print(f"{tr('每卡权重估算', 'Estimated weights per GPU')}: {human_size(int(plan['estimated_weight_per_gpu']))}")
    print(f"{tr('每卡运行时预留', 'Runtime reserve per GPU')}: {human_size(int(plan['estimated_runtime_reserve_per_gpu']))}")
    print(f"{tr('完整最大窗口容量估算', 'Estimated full-window capacity')}: {plan.get('estimated_full_context_sequences', 1)} {tr('路/实例', 'sequence(s) per replica')}")
    if plan.get("ple_cpu_offload"):
        print(f"PLE CPU offload: {human_size(int(plan.get('ple_offload_bytes_per_instance') or 0))} / {tr('实例', 'replica')}")
        print(f"{tr('全池主内存常驻预算', 'Whole-pool resident host-memory budget')}: {human_size(int(plan.get('host_memory_running_required') or 0))}")
    if float(plan.get("yarn_factor") or 1) > 1:
        print(f"Static YaRN: {float(plan['yarn_factor']):g}x")
    print(f"vLLM: {model.get('runtime_profile', {}).get('image', '?')}")
    print(f"{tr('每卡剩余 KV 预算', 'Remaining KV budget per GPU')}: {human_size(kv_budget)}")
    print(f"{tr('推荐启动并行度', 'Recommended startup parallelism')}: {plan.get('startup_parallelism', 1)}")
    print(f"{tr('单实例主机内存加载预算', 'Host-memory loading budget per replica')}: {human_size(int(plan.get('host_memory_per_starting_instance') or 0))}")
    print(f"{tr('模型盘空间', 'Model disk')}: {plan.get('disk_path') or '?'} {tr('可用', 'free')} {human_size(int(plan.get('disk_free') or 0))} / {tr('所需预算', 'required budget')} {human_size(int(plan.get('disk_required') or 0))}")
    print(f"{tr('推荐分', 'Recommendation score')}: {score}")
    print()
    print(tr("推荐原因：", "Why this is recommended:"))
    runtime_kind = model.get("runtime_profile", {}).get("kind")
    runtime_reason = (
        tr(
            f"架构 {arch} 由 Qwen3.8 Flash Next 专用 vLLM 预览镜像提供。",
            f"Architecture {arch} is provided by the dedicated Qwen3.8 Flash Next vLLM preview image.",
        )
        if runtime_kind == "qwen38-flash-next-preview"
        else tr(f"架构 {arch} 位于 vLLM {VLLM_COMPAT_VERSION} 保守兼容清单中。", f"Architecture {arch} is in the conservative vLLM {VLLM_COMPAT_VERSION} compatibility list.")
    )
    context_reason = (
        tr(
            f"计划上下文 {plan['max_model_len']} 使用静态 YaRN {float(plan['yarn_factor']):g}×，并通过当前显存估算。",
            f"The planned {plan['max_model_len']}-token context uses static YaRN {float(plan['yarn_factor']):g}x and passes the current VRAM estimate.",
        )
        if float(plan.get("yarn_factor") or 1) > 1
        else tr(f"计划上下文 {plan['max_model_len']} 不超过模型原生上限，并通过当前显存估算。", f"The planned {plan['max_model_len']}-token context does not exceed the model-native limit and passes the current VRAM estimate.")
    )
    reasons = [
        runtime_reason,
        tr(f"仓库提供约 {human_size(int(model.get('weight_bytes') or 0))} 完整权重，且未命中 MLX 等平台专用格式。", f"The repository provides about {human_size(int(model.get('weight_bytes') or 0))} of complete weights and is not tagged as a platform-specific format such as MLX."),
        tr(f"TP{plan['tp']} 可在每卡保留运行时与 KV Cache 余量，并形成 {plan['replicas']} 个独立服务实例。", f"TP{plan['tp']} leaves runtime and KV-cache headroom per GPU while forming {plan['replicas']} independent serving replicas."),
        context_reason,
        tr(f"排名信号：downloads={model.get('downloads', 0)}、likes={model.get('likes', 0)}、能力={capability_text(model)}、副本数={plan['replicas']}、TP 成本={plan['tp']}。", f"Ranking signals: downloads={model.get('downloads', 0)}, likes={model.get('likes', 0)}, capabilities={capability_text(model)}, replicas={plan['replicas']}, TP cost={plan['tp']}.")
    ]
    for number, reason in enumerate(reasons, 1):
        print(f"  {number}. {reason}")
    warnings = plan.get("warnings") or []
    if warnings:
        print()
        print(tr("计划警告：", "Plan warnings:"))
        for warning in warnings:
            print(f"  - {warning}")
    print()
    print(tr("重要边界：", "Important boundaries:"))
    print(tr("  - 262K 等上下文值是单请求上限，不表示每个调度序列都能同时占满该长度。", "  - A context such as 262K is a per-request ceiling; it does not mean every scheduling sequence can use that length simultaneously."))
    print(tr("  - max-num-seqs 使用最多 32K 的参考上下文估算；真实并发取决于请求长度、图片数量和输出长度。", "  - max-num-seqs is estimated with a reference context capped at 32K; real concurrency depends on request length, image count, and output length."))
    print(tr("  - 此计划仍须通过固定 vLLM 镜像核验、完整模型加载和能力感知 API 冒烟测试。", "  - This plan must still pass pinned-image verification, a complete model load, and capability-aware API smoke tests."))


def shell_assignments(model: dict[str, Any]) -> str:
    """把已通过门禁的目录结果输出为安全转义的 Shell 赋值。"""

    if not model.get("installable") or not model.get("plan"):
        raise CatalogError(tr("所选模型未通过兼容/硬件门禁", "The selected model did not pass compatibility or hardware gates"))
    plan = model["plan"]
    caps = model["capabilities"]
    runtime = model.get("runtime_profile") or {}
    served = re.sub(r"[^A-Za-z0-9._-]+", "-", model["id"].split("/")[-1]).lower()
    values = {
        "MODEL_HUB": model["source"],
        "MODEL_ID": model["id"],
        "MODEL_REVISION": model["revision"],
        "MODEL_ARCHITECTURE": model["supported_architectures"][0],
        "MODEL_TASK": "vision" if caps["image_input"] else "text",
        "MODEL_PRECISION": model["precision"],
        "MODEL_WEIGHT_BYTES": int(model.get("weight_bytes") or 0),
        "MODEL_PARAMS": int(model.get("params") or 0),
        "MODEL_NATIVE_CONTEXT": model["native_context"],
        "SERVED_MODEL_NAME": served,
        "VLLM_IMAGE": runtime.get(
            "image", f"vllm/vllm-openai:v{VLLM_COMPAT_VERSION}"
        ),
        "TP_SIZE": plan["tp"],
        "MAX_MODEL_LEN": plan["max_model_len"],
        "MAX_NUM_SEQS": plan["max_num_seqs"],
        "ESTIMATED_MAX_NUM_SEQS": plan["estimated_seq_capacity"],
        "STARTUP_PARALLELISM": plan.get("startup_parallelism", 1),
        "PLE_CPU_OFFLOAD": int(bool(runtime.get("ple_cpu_offload"))),
        "ENABLE_EXPERT_PARALLEL": int(bool(runtime.get("enable_expert_parallel"))),
        "ENABLE_PREFIX_CACHING": int(bool(runtime.get("enable_prefix_caching", True))),
        "ENABLE_FLASHINFER_AUTOTUNE": int(bool(runtime.get("enable_flashinfer_autotune", True))),
        "DISABLE_CUSTOM_ALL_REDUCE": int(bool(runtime.get("disable_custom_all_reduce"))),
        "MTP_SPECULATIVE_TOKENS": int(runtime.get("mtp_speculative_tokens") or 0),
        "KV_CACHE_DTYPE": runtime.get("kv_cache_dtype", "auto"),
        "YARN_FACTOR": plan.get("yarn_factor", 1),
        "TOOL_CALL_PARSER": caps["tool_parser"],
        "REASONING_PARSER": caps["reasoning_parser"],
        "SUPPORTS_IMAGE_INPUT": int(caps["image_input"]),
        "SUPPORTS_OCR": int(caps["ocr_optimized"]),
        "SUPPORTS_TOOL_CALLING": int(bool(caps["tool_parser"])),
        "SUPPORTS_REASONING": int(bool(caps["reasoning_parser"])),
        "SUPPORTS_THINKING_TOGGLE": int(caps["thinking_toggle"]),
        "TRUST_REMOTE_CODE": int(bool(model.get("trust_remote_code"))),
    }
    return "\n".join(f"{key}={shlex.quote(str(value))}" for key, value in values.items())


def search_models(args: argparse.Namespace, hardware: Hardware) -> list[dict[str, Any]]:
    tokens = {
        "huggingface": os.environ.get("HF_TOKEN"),
        "modelscope": os.environ.get("MODELSCOPE_API_TOKEN") or os.environ.get("MODELSCOPE_API_KEY"),
    }
    raw: list[dict[str, Any]] = []
    errors: list[str] = []
    sources = [args.source] if args.source != "all" else ["modelscope", "huggingface"]
    for source in sources:
        try:
            if source == "huggingface":
                raw.extend(hf_search(args.query, args.task, args.limit, tokens[source]))
            else:
                raw.extend(ms_search(args.query, args.task, args.limit, tokens[source]))
        except CatalogError as exc:
            errors.append(f"{source}: {exc}")
    if not raw and errors:
        raise CatalogError(("；" if LANGUAGE == "zh" else "; ").join(errors))
    for error in errors:
        eprint(tr(f"[catalog] 警告：{error}", f"[catalog] WARNING: {error}"))
    evaluated = [evaluate(item, hardware, args.gpu_memory_utilization, args.max_model_len) for item in raw]
    # Prefer installable, then the hardware-aware recommendation score.
    evaluated.sort(key=lambda item: (bool(item["installable"]), item["recommendation_score"]), reverse=True)
    installable = [item for item in evaluated if item["installable"]]
    rejected = [item for item in evaluated if not item["installable"]]
    if rejected and not args.show_rejected:
        counts: collections.Counter[str] = collections.Counter(
            reason
            for item in rejected
            for reason in item.get("rejection_reasons") or []
        )
        eprint(tr("[catalog] 已排除的候选：", "[catalog] Excluded candidates:"))
        for reason, count in counts.most_common(5):
            eprint(f"  - {count}× {reason}")
    return (installable[: args.limit] + rejected[: args.limit]) if args.show_rejected else installable[: args.limit]


def add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--hardware-json", help=tr("测试/离线规划用硬件 JSON", "hardware JSON for tests/offline planning"))
    parser.add_argument("--model-root", default="/data/llm-cluster/models")
    parser.add_argument("--gpu-memory-utilization", type=float, default=0.92)
    parser.add_argument("--max-model-len", type=int, default=0, help=tr("0=按模型和硬件自动", "0=plan from the model and hardware"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=tr("LLM 模型目录、兼容检查与硬件规划", "LLM catalog, compatibility checks, and hardware planning"))
    parser.add_argument("--version", action="version", version=CATALOG_VERSION)
    parser.add_argument("--lang", choices=("zh", "en"), default=LANGUAGE)
    sub = parser.add_subparsers(dest="command", required=True)

    hardware = sub.add_parser("hardware", help=tr("显示本机只读硬件体检", "show the read-only host hardware preflight"))
    hardware.add_argument("--hardware-json")
    hardware.add_argument("--model-root", default="/data/llm-cluster/models")
    hardware.add_argument("--json", action="store_true")
    hardware.add_argument("--summary", action="store_true")

    search = sub.add_parser("search", help=tr("搜索并只列出当前硬件可部署的模型", "search for models deployable on this host"))
    search.add_argument("query", nargs="?", default="")
    search.add_argument("--source", choices=("all", "huggingface", "modelscope"), default="all")
    search.add_argument("--task", choices=("auto", "text", "vision"), default="auto")
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--show-rejected", action="store_true")
    search.add_argument("--json", action="store_true")
    search.add_argument("--output-json")
    add_common_options(search)

    inspect = sub.add_parser("inspect", help=tr("检查指定模型并生成部署计划", "inspect a model and build a deployment plan"))
    inspect.add_argument("source", choices=("huggingface", "modelscope"))
    inspect.add_argument("model_id")
    inspect.add_argument("revision", nargs="?")
    inspect.add_argument("--json", action="store_true")
    inspect.add_argument("--shell", action="store_true")
    add_common_options(inspect)

    select = sub.add_parser("select", help=tr("从 search 保存的 JSON 中选取模型", "select a model from search JSON"))
    select.add_argument("input")
    select.add_argument("index", type=int, help=tr("从 1 开始", "starts at 1"))
    select.add_argument("--shell", action="store_true")
    select.add_argument("--json", action="store_true")
    select.add_argument("--summary", action="store_true")
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if hasattr(args, "limit") and not 1 <= args.limit <= 50:
        raise CatalogError(tr("limit 必须在 1-50", "limit must be between 1 and 50"))
    if hasattr(args, "gpu_memory_utilization") and not 0.70 <= args.gpu_memory_utilization <= 0.96:
        raise CatalogError(tr("gpu-memory-utilization 必须在 0.70-0.96", "gpu-memory-utilization must be between 0.70 and 0.96"))
    if hasattr(args, "max_model_len") and args.max_model_len not in range(0, 262145):
        raise CatalogError(tr("max-model-len 必须在 0-262144", "max-model-len must be between 0 and 262144"))


def main() -> int:
    global LANGUAGE
    for position, token in enumerate(sys.argv[1:]):
        if token in {"--lang", "--language"} and position + 2 <= len(sys.argv[1:]):
            requested = sys.argv[1:][position + 1]
            if requested in {"zh", "en"}:
                LANGUAGE = requested
            break
    args = build_parser().parse_args()
    LANGUAGE = args.lang
    try:
        validate_args(args)
        if args.command == "hardware":
            hardware = detect_hardware(args.hardware_json, args.model_root)
            payload = dataclasses.asdict(hardware)
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            elif args.summary:
                render_hardware_summary(hardware)
            else:
                if not hardware.gpus:
                    print(tr("未检测到 NVIDIA GPU", "No NVIDIA GPU detected"))
                for gpu in hardware.gpus:
                    print(f"GPU {gpu.index}: {gpu.name}, {gpu.memory_mib} MiB, CC {gpu.compute_capability}")
            return 0

        if args.command == "select":
            with open(args.input, "r", encoding="utf-8") as handle:
                models = json.load(handle)
            if isinstance(models, dict):
                models = [models]
            if not isinstance(models, list):
                raise CatalogError(tr("选择文件必须包含模型对象或列表", "Selection input must contain a model object or list"))
            if not 1 <= args.index <= len(models):
                raise CatalogError(tr(f"选择范围必须是 1-{len(models)}", f"Selection must be between 1 and {len(models)}"))
            model = models[args.index - 1]
            if args.summary:
                render_selection_summary(model)
            elif args.shell:
                print(shell_assignments(model))
            else:
                print(json.dumps(model, ensure_ascii=False, indent=2))
            return 0

        hardware = detect_hardware(args.hardware_json, args.model_root)
        if args.command == "search":
            models = search_models(args, hardware)
            serializable = [strip_internal(model) for model in models]
            if args.output_json:
                with open(args.output_json, "w", encoding="utf-8") as handle:
                    json.dump(serializable, handle, ensure_ascii=False, indent=2)
            if args.json:
                print(json.dumps(serializable, ensure_ascii=False, indent=2))
            else:
                render_table(models, args.show_rejected)
            return 0 if any(model["installable"] for model in models) else 2

        token = (
            os.environ.get("HF_TOKEN")
            if args.source == "huggingface"
            else os.environ.get("MODELSCOPE_API_TOKEN") or os.environ.get("MODELSCOPE_API_KEY")
        )
        raw = (
            inspect_hf(args.model_id, args.revision, token)
            if args.source == "huggingface"
            else inspect_ms(args.model_id, args.revision, token)
        )
        model = evaluate(raw, hardware, args.gpu_memory_utilization, args.max_model_len)
        if args.shell:
            print(shell_assignments(model))
        elif args.json:
            print(json.dumps(strip_internal(model), ensure_ascii=False, indent=2))
        else:
            render_table([model], show_rejected=True)
        return 0 if model["installable"] else 2
    except (CatalogError, json.JSONDecodeError, OSError) as exc:
        eprint(f"[catalog] ERROR: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
