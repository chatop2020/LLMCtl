#!/usr/bin/env python3
"""Model discovery and conservative hardware planning for llm-cluster-deploy.

The helper intentionally uses only Python's standard library so discovery works
before any Python virtual environment is created.  It treats Hub metadata as a
preflight signal, never as proof that a model has completed a real vLLM load.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import json
import math
import os
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterable


CATALOG_VERSION = "2.0.0"
VLLM_COMPAT_VERSION = "0.22.1"
GIB = 1024**3
HF_ENDPOINT = os.environ.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/")
MS_ENDPOINT = os.environ.get("MODELSCOPE_ENDPOINT", "https://modelscope.cn").rstrip("/")
USER_AGENT = f"llm-cluster-deploy/{CATALOG_VERSION}"

# Generated from the official vLLM 0.22.1 supported-models page.  Runtime
# installation performs a second check against ModelRegistry in the pinned
# container image, so this list is only the discovery gate.
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
Qwen3_5MoeForConditionalGeneration QwenVLForConditionalGeneration
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


@dataclasses.dataclass
class GPU:
    index: int
    name: str
    memory_mib: int
    compute_capability: str = "unknown"


@dataclasses.dataclass
class Hardware:
    gpus: list[GPU]

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
        raise CatalogError(f"请求失败: {url}: {exc}") from exc


def request_optional_json(url: str, token: str | None = None) -> dict[str, Any]:
    try:
        data = request_json(url, token=token)
        return data if isinstance(data, dict) else {}
    except CatalogError:
        return {}


def detect_hardware(override: str | None = None) -> Hardware:
    if override:
        raw = json.loads(override)
        values = raw.get("gpus", raw) if isinstance(raw, dict) else raw
        return Hardware(
            [
                GPU(
                    int(item.get("index", i)),
                    str(item.get("name", "GPU")),
                    int(item["memory_mib"]),
                    str(item.get("compute_capability", "unknown")),
                )
                for i, item in enumerate(values)
            ]
        )
    query = "index,name,memory.total,compute_cap"
    try:
        completed = subprocess.run(
            ["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits"],
            check=True,
            text=True,
            capture_output=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return Hardware([])
    gpus: list[GPU] = []
    for line in completed.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) >= 4:
            gpus.append(GPU(int(parts[0]), parts[1], int(parts[2]), parts[3]))
    return Hardware(gpus)


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
    cfg = nested_config(config)
    try:
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
    qcfg = config.get("quantization_config")
    if isinstance(qcfg, dict):
        method = str(qcfg.get("quant_method") or qcfg.get("quantization_method") or "").lower()
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
                eprint(f"[catalog] Hugging Face 条目跳过: {exc}")
    return results


def ms_raw_file(model_id: str, revision: str, filename: str, token: str | None) -> dict[str, Any]:
    url = f"{MS_ENDPOINT}/models/{urllib.parse.quote(model_id, safe='/')}/resolve/{urllib.parse.quote(revision, safe='')}/{filename}"
    return request_optional_json(url, token=token)


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
                eprint(f"[catalog] ModelScope 条目跳过: {exc}")
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
    rev = revision or "master"
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
    config = model.get("config") or {}
    archs = architectures(config)
    supported = [arch for arch in archs if arch in SUPPORTED_ARCHITECTURES]
    tags = model.get("tags") or []
    model["architectures"] = archs
    model["supported_architectures"] = supported
    model["precision"] = normalize_precision(config, model["id"], tags)
    model["native_context"] = native_context(config)
    model["capabilities"] = capability_profile(model["id"], model["task"], config, model.get("tokenizer") or {})
    model["trust_remote_code"] = bool(config.get("auto_map"))

    reasons: list[str] = []
    if model["task"] not in GENERATIVE_TASKS:
        reasons.append(f"任务 {model['task']} 不是生成式 LLM/VLM")
    if not supported:
        reasons.append(f"架构 {','.join(archs) or '未知'} 不在 vLLM {VLLM_COMPAT_VERSION} 保守清单")
    source_token = (
        os.environ.get("HF_TOKEN")
        if model.get("source") == "huggingface"
        else os.environ.get("MODELSCOPE_API_TOKEN") or os.environ.get("MODELSCOPE_API_KEY")
    )
    if model.get("private") and not source_token:
        reasons.append("私有模型需要有效访问令牌")
    if model.get("gated") and not os.environ.get("HF_TOKEN"):
        reasons.append("受限模型需要先设置 Hugging Face Token 并接受许可")
    weight_bytes = int(model.get("weight_bytes") or 0)
    params = int(model.get("params") or 0)
    if weight_bytes <= 0:
        reasons.append("无法取得权重大小，不能安全规划显存")
    if params and weight_bytes and weight_bytes < params * 0.40:
        reasons.append("仓库体积显著小于参数量，可能是适配器或非完整权重")
    if not hardware.count:
        reasons.append("未检测到 NVIDIA GPU")

    # Preserve the model's useful long-context ceiling (capped at 256K) when a
    # single request fits. Scheduler slots are estimated separately at a 32K
    # reference length: max_num_seqs=7 means seven typical shorter requests,
    # not seven simultaneous max-length requests.
    desired = requested_context or min(model["native_context"], 262144)
    desired = max(8192, min(desired, model["native_context"], 262144))
    kv_bpt = kv_bytes_per_token(config)
    if kv_bpt is None:
        desired = min(desired, 32768)
    plan: dict[str, Any] | None = None
    if not reasons:
        min_mem = hardware.min_memory_bytes
        for tp in (1, 2, 4, 8):
            if tp > hardware.count or hardware.count % tp:
                continue
            usable = min_mem * utilization
            weight_per_gpu = weight_bytes * 1.08 / tp
            runtime = max(4 * GIB, min_mem * 0.06)
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
                    # The project-wide operational default is seven scheduler
                    # slots per replica.  Higher theoretical KV capacity is
                    # useful evidence, but should not silently increase the
                    # live concurrency limit chosen by the user.
                    seqs = min(7, estimated_seq_capacity)
                    plan = {
                        "tp": tp,
                        "replicas": hardware.count // tp,
                        "max_model_len": context,
                        "max_num_seqs": seqs,
                        "estimated_seq_capacity": estimated_seq_capacity,
                        "sequence_reference_context": reference_context,
                        "estimated_weight_per_gpu": int(weight_per_gpu),
                        "estimated_runtime_reserve_per_gpu": int(runtime),
                        "estimated_kv_bytes_per_token_total": kv_bpt or 0,
                        "usable_per_gpu": int(usable),
                    }
                    break
                context //= 2
            if plan:
                break
        if not plan:
            reasons.append("在当前 GPU 数量/显存及至少 8K 上下文下无法保守部署")

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
    model["recommendation_score"] = round(score, 2)
    return model


def strip_internal(model: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in model.items() if k not in {"config", "tokenizer"}}


def human_size(value: int) -> str:
    if not value:
        return "?"
    return f"{value / GIB:.1f}G"


def capability_text(model: dict[str, Any]) -> str:
    caps = model["capabilities"]
    values = ["文本"]
    if caps["image_input"]:
        values.append("图片")
    if caps["ocr_optimized"]:
        values.append("OCR")
    if caps["tool_parser"]:
        values.append("工具")
    if caps["reasoning_parser"]:
        values.append("思考")
    if caps["thinking_toggle"]:
        values.append("可关闭思考")
    return "/".join(values)


def render_table(models: list[dict[str, Any]], show_rejected: bool = False) -> None:
    visible = [m for m in models if m["installable"] or show_rejected]
    if not visible:
        print("没有找到同时满足 vLLM 兼容与当前硬件容量要求的模型。")
        return
    headers = ["#", "来源", "模型", "权重", "精度", "计划", "能力", "结论"]
    rows: list[list[str]] = []
    for i, model in enumerate(visible, 1):
        plan = model.get("plan")
        plan_text = (
            f"TP{plan['tp']}×{plan['replicas']} ctx={plan['max_model_len']} seq={plan['max_num_seqs']}"
            if plan
            else "不可部署"
        )
        conclusion = "推荐" if model["installable"] else "; ".join(model["rejection_reasons"][:2])
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


def shell_assignments(model: dict[str, Any]) -> str:
    if not model.get("installable") or not model.get("plan"):
        raise CatalogError("所选模型未通过兼容/硬件门禁")
    plan = model["plan"]
    caps = model["capabilities"]
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
        "TP_SIZE": plan["tp"],
        "MAX_MODEL_LEN": plan["max_model_len"],
        "MAX_NUM_SEQS": plan["max_num_seqs"],
        "ESTIMATED_MAX_NUM_SEQS": plan["estimated_seq_capacity"],
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
        raise CatalogError("；".join(errors))
    for error in errors:
        eprint(f"[catalog] 警告：{error}")
    evaluated = [evaluate(item, hardware, args.gpu_memory_utilization, args.max_model_len) for item in raw]
    # Prefer installable, then the hardware-aware recommendation score.
    evaluated.sort(key=lambda item: (bool(item["installable"]), item["recommendation_score"]), reverse=True)
    installable = [item for item in evaluated if item["installable"]]
    rejected = [item for item in evaluated if not item["installable"]]
    return (installable[: args.limit] + rejected[: args.limit]) if args.show_rejected else installable[: args.limit]


def add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--hardware-json", help="测试/离线规划用 GPU JSON")
    parser.add_argument("--gpu-memory-utilization", type=float, default=0.92)
    parser.add_argument("--max-model-len", type=int, default=0, help="0=按模型和硬件自动")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LLM 模型目录、兼容检查与硬件规划")
    parser.add_argument("--version", action="version", version=CATALOG_VERSION)
    sub = parser.add_subparsers(dest="command", required=True)

    hardware = sub.add_parser("hardware", help="显示检测到的 NVIDIA GPU")
    hardware.add_argument("--hardware-json")
    hardware.add_argument("--json", action="store_true")

    search = sub.add_parser("search", help="搜索并只列出当前硬件可部署的模型")
    search.add_argument("query", nargs="?", default="")
    search.add_argument("--source", choices=("all", "huggingface", "modelscope"), default="all")
    search.add_argument("--task", choices=("auto", "text", "vision"), default="auto")
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--show-rejected", action="store_true")
    search.add_argument("--json", action="store_true")
    search.add_argument("--output-json")
    add_common_options(search)

    inspect = sub.add_parser("inspect", help="检查指定模型并生成部署计划")
    inspect.add_argument("source", choices=("huggingface", "modelscope"))
    inspect.add_argument("model_id")
    inspect.add_argument("revision", nargs="?")
    inspect.add_argument("--json", action="store_true")
    inspect.add_argument("--shell", action="store_true")
    add_common_options(inspect)

    select = sub.add_parser("select", help="从 search 保存的 JSON 中选取模型")
    select.add_argument("input")
    select.add_argument("index", type=int, help="从 1 开始")
    select.add_argument("--shell", action="store_true")
    select.add_argument("--json", action="store_true")
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if hasattr(args, "limit") and not 1 <= args.limit <= 50:
        raise CatalogError("limit 必须在 1-50")
    if hasattr(args, "gpu_memory_utilization") and not 0.70 <= args.gpu_memory_utilization <= 0.96:
        raise CatalogError("gpu-memory-utilization 必须在 0.70-0.96")
    if hasattr(args, "max_model_len") and args.max_model_len not in range(0, 262145):
        raise CatalogError("max-model-len 必须在 0-262144")


def main() -> int:
    args = build_parser().parse_args()
    try:
        validate_args(args)
        if args.command == "hardware":
            hardware = detect_hardware(args.hardware_json)
            payload = dataclasses.asdict(hardware)
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                if not hardware.gpus:
                    print("未检测到 NVIDIA GPU")
                for gpu in hardware.gpus:
                    print(f"GPU {gpu.index}: {gpu.name}, {gpu.memory_mib} MiB, CC {gpu.compute_capability}")
            return 0

        if args.command == "select":
            with open(args.input, "r", encoding="utf-8") as handle:
                models = json.load(handle)
            if not 1 <= args.index <= len(models):
                raise CatalogError(f"选择范围必须是 1-{len(models)}")
            model = models[args.index - 1]
            if args.shell:
                print(shell_assignments(model))
            else:
                print(json.dumps(model, ensure_ascii=False, indent=2))
            return 0

        hardware = detect_hardware(args.hardware_json)
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
