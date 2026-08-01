#!/usr/bin/env python3
"""Runtime benchmark and conservative tuning adviser for LLMCtl.

The helper deliberately owns measurement and scoring only. Privileged config
changes, service restarts, acceptance checks, and rollback remain in llmctl.sh.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import math
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def parse_prometheus(text: str) -> dict[str, list[float]]:
    parsed: dict[str, list[float]] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            left, raw_value = line.rsplit(None, 1)
            name = left.split("{", 1)[0]
            value = float(raw_value)
        except (ValueError, IndexError):
            continue
        if math.isfinite(value):
            parsed.setdefault(name, []).append(value)
    return parsed


def metric_value(
    metrics: dict[str, list[float]], names: tuple[str, ...], aggregation: str
) -> float:
    values: list[float] = []
    for name in names:
        values.extend(metrics.get(name, []))
    if not values:
        return 0.0
    return max(values) if aggregation == "max" else sum(values)


def fetch_text(url: str, key: str, timeout: float = 1.5) -> str:
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def collect_vllm_metrics(urls: list[str], key: str) -> dict[str, float]:
    merged: dict[str, list[float]] = {}
    if urls:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(8, len(urls))
        ) as pool:
            futures = [pool.submit(fetch_text, url, key) for url in urls]
            for future in futures:
                try:
                    current = parse_prometheus(future.result())
                except Exception:
                    continue
                for name, values in current.items():
                    merged.setdefault(name, []).extend(values)
    return {
        "preemptions": metric_value(
            merged,
            ("vllm:num_preemptions", "vllm:num_preemptions_total"),
            "sum",
        ),
        "prefix_hits": metric_value(
            merged,
            ("vllm:prefix_cache_hits", "vllm:prefix_cache_hits_total"),
            "sum",
        ),
        "prefix_queries": metric_value(
            merged,
            ("vllm:prefix_cache_queries", "vllm:prefix_cache_queries_total"),
            "sum",
        ),
        "kv_cache_usage": metric_value(
            merged,
            ("vllm:kv_cache_usage_perc", "vllm:gpu_cache_usage_perc"),
            "max",
        ),
        "running": metric_value(
            merged, ("vllm:num_requests_running",), "sum"
        ),
        "waiting": metric_value(
            merged, ("vllm:num_requests_waiting",), "sum"
        ),
    }


@dataclass
class ResourceSampler:
    metric_urls: list[str]
    metric_key: str
    stop_event: threading.Event = field(default_factory=threading.Event)
    gpu_util_sum: float = 0.0
    gpu_util_count: int = 0
    gpu_util_max: float = 0.0
    vram_peak_pct: float = 0.0
    temperature_max_c: float = 0.0
    power_max_w: float = 0.0
    kv_cache_peak_pct: float = 0.0
    running_peak: float = 0.0
    waiting_peak: float = 0.0
    metrics_available: bool = False
    initial_metrics: dict[str, float] = field(default_factory=dict)
    final_metrics: dict[str, float] = field(default_factory=dict)
    cpu_util_sum: float = 0.0
    cpu_util_count: int = 0
    cpu_util_max: float = 0.0
    memory_available_min_gib: float = math.inf
    memory_used_peak_pct: float = 0.0
    swap_used_peak_gib: float = 0.0
    load1_peak: float = 0.0
    last_cpu: tuple[float, float] | None = None

    def sample_gpu(self) -> None:
        queries = (
            "utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
            "utilization.gpu,memory.used,memory.total,temperature.gpu",
        )
        completed = None
        selected_query = ""
        for query in queries:
            try:
                completed = subprocess.run(
                    [
                        "nvidia-smi",
                        f"--query-gpu={query}",
                        "--format=csv,noheader,nounits",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                selected_query = query
                break
            except Exception:
                continue
        if completed is None:
            return
        has_power = selected_query.endswith("power.draw")
        for raw in completed.stdout.splitlines():
            try:
                values = [float(item.strip()) for item in raw.split(",")]
                util, used, total, temperature = values[:4]
                power = values[4] if has_power else 0.0
            except (ValueError, TypeError, IndexError):
                continue
            self.gpu_util_sum += util
            self.gpu_util_count += 1
            self.gpu_util_max = max(self.gpu_util_max, util)
            if total > 0:
                self.vram_peak_pct = max(self.vram_peak_pct, used / total * 100)
            self.temperature_max_c = max(self.temperature_max_c, temperature)
            self.power_max_w = max(self.power_max_w, power)

    def sample_metrics(self) -> None:
        current = collect_vllm_metrics(self.metric_urls, self.metric_key)
        if any(current.values()):
            self.metrics_available = True
        self.kv_cache_peak_pct = max(
            self.kv_cache_peak_pct, current.get("kv_cache_usage", 0.0) * 100
        )
        self.running_peak = max(self.running_peak, current.get("running", 0.0))
        self.waiting_peak = max(self.waiting_peak, current.get("waiting", 0.0))

    def sample_host(self) -> None:
        try:
            cpu_values = [
                float(item)
                for item in Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()[1:]
            ]
            idle = cpu_values[3] + (cpu_values[4] if len(cpu_values) > 4 else 0.0)
            total = sum(cpu_values)
            if self.last_cpu is not None:
                previous_total, previous_idle = self.last_cpu
                total_delta = total - previous_total
                idle_delta = idle - previous_idle
                if total_delta > 0:
                    utilization = max(0.0, min(100.0, (total_delta - idle_delta) / total_delta * 100))
                    self.cpu_util_sum += utilization
                    self.cpu_util_count += 1
                    self.cpu_util_max = max(self.cpu_util_max, utilization)
            self.last_cpu = (total, idle)

            memory: dict[str, float] = {}
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                key, raw = line.split(":", 1)
                memory[key] = float(raw.strip().split()[0]) * 1024
            total_memory = memory.get("MemTotal", 0.0)
            available = memory.get("MemAvailable", 0.0)
            if total_memory > 0:
                self.memory_available_min_gib = min(
                    self.memory_available_min_gib, available / 1024**3
                )
                self.memory_used_peak_pct = max(
                    self.memory_used_peak_pct,
                    (total_memory - available) / total_memory * 100,
                )
            swap_used = max(
                0.0, memory.get("SwapTotal", 0.0) - memory.get("SwapFree", 0.0)
            )
            self.swap_used_peak_gib = max(self.swap_used_peak_gib, swap_used / 1024**3)
            self.load1_peak = max(self.load1_peak, os.getloadavg()[0])
        except (OSError, ValueError, IndexError):
            return

    def run(self) -> None:
        self.initial_metrics = collect_vllm_metrics(self.metric_urls, self.metric_key)
        while not self.stop_event.is_set():
            self.sample_gpu()
            self.sample_metrics()
            self.sample_host()
            self.stop_event.wait(1.0)
        self.sample_gpu()
        self.sample_metrics()
        self.sample_host()
        self.final_metrics = collect_vllm_metrics(self.metric_urls, self.metric_key)

    def result(self) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        initial = self.initial_metrics
        final = self.final_metrics
        preemptions = max(
            0.0, final.get("preemptions", 0.0) - initial.get("preemptions", 0.0)
        )
        prefix_hits = max(
            0.0, final.get("prefix_hits", 0.0) - initial.get("prefix_hits", 0.0)
        )
        prefix_queries = max(
            0.0,
            final.get("prefix_queries", 0.0)
            - initial.get("prefix_queries", 0.0),
        )
        gpu = {
            "available": self.gpu_util_count > 0,
            "utilization_average_pct": (
                round(self.gpu_util_sum / self.gpu_util_count, 3)
                if self.gpu_util_count
                else 0.0
            ),
            "utilization_peak_pct": round(self.gpu_util_max, 3),
            "vram_used_peak_pct": round(self.vram_peak_pct, 3),
            "temperature_peak_c": round(self.temperature_max_c, 3),
            "power_peak_w": round(self.power_max_w, 3),
        }
        vllm = {
            "available": self.metrics_available,
            "kv_cache_usage_peak_pct": round(self.kv_cache_peak_pct, 3),
            "running_requests_peak": int(self.running_peak),
            "waiting_requests_peak": int(self.waiting_peak),
            "preemptions_delta": int(preemptions),
            "prefix_cache_hits_delta": int(prefix_hits),
            "prefix_cache_queries_delta": int(prefix_queries),
            "prefix_cache_hit_rate": round(
                prefix_hits / prefix_queries if prefix_queries else 0.0, 6
            ),
        }
        host = {
            "available": self.cpu_util_count > 0 and math.isfinite(self.memory_available_min_gib),
            "cpu_utilization_average_pct": (
                round(self.cpu_util_sum / self.cpu_util_count, 3)
                if self.cpu_util_count
                else 0.0
            ),
            "cpu_utilization_peak_pct": round(self.cpu_util_max, 3),
            "load1_peak": round(self.load1_peak, 3),
            "memory_available_min_gib": (
                round(self.memory_available_min_gib, 3)
                if math.isfinite(self.memory_available_min_gib)
                else 0.0
            ),
            "memory_used_peak_pct": round(self.memory_used_peak_pct, 3),
            "swap_used_peak_gib": round(self.swap_used_peak_gib, 3),
        }
        return gpu, vllm, host


def benchmark_prompt(approximate_tokens: int) -> str:
    seed = (
        "Distributed systems require explicit failure boundaries, bounded retries, "
        "idempotency, observability, and capacity planning. "
    )
    # English prose averages a little over one token per word. Exact prompt token
    # count is intentionally not claimed because the selected tokenizer varies.
    repeats = max(1, approximate_tokens // 16)
    return (seed * repeats) + (
        "Continue with numbered, non-repeating engineering observations until the "
        "output limit is reached. Do not finish early."
    )


def one_request(
    index: int,
    *,
    url: str,
    key: str,
    model: str,
    max_tokens: int,
    prompt_tokens: int,
    thinking_toggle: bool,
    timeout: int,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": True,
        "stream_options": {"include_usage": True},
        "messages": [{"role": "user", "content": benchmark_prompt(prompt_tokens)}],
    }
    if thinking_toggle:
        payload.update(
            {
                "reasoning_effort": "none",
                "chat_template_kwargs": {"enable_thinking": False},
            }
        )
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
    )
    started = time.monotonic()
    ttft: float | None = None
    completion_tokens = 0
    stream_events = 0
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            for raw in response:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                chunk = json.loads(data)
                usage = chunk.get("usage") or {}
                completion_tokens = max(
                    completion_tokens, int(usage.get("completion_tokens") or 0)
                )
                choices = chunk.get("choices") or []
                if choices:
                    delta = choices[0].get("delta") or {}
                    emitted = delta.get("content") or delta.get("reasoning_content")
                    if emitted:
                        stream_events += 1
                        if ttft is None:
                            ttft = time.monotonic() - started
        elapsed = time.monotonic() - started
        exact_tokens = completion_tokens > 0
        if completion_tokens <= 0:
            completion_tokens = stream_events
        if completion_tokens <= 0 or ttft is None:
            raise RuntimeError("stream returned no generated tokens")
        itl = max(0.0, elapsed - ttft) / max(1, completion_tokens - 1)
        return {
            "index": index,
            "ok": True,
            "elapsed_seconds": elapsed,
            "ttft_seconds": ttft,
            "itl_seconds": itl,
            "completion_tokens": completion_tokens,
            "exact_token_count": exact_tokens,
            "output_tokens_per_second": completion_tokens / elapsed,
        }
    except Exception as exc:
        return {
            "index": index,
            "ok": False,
            "elapsed_seconds": time.monotonic() - started,
            "error": repr(exc)[:500],
        }


def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    key = os.environ.get("LLMCTL_BENCH_KEY", "")
    metric_key = os.environ.get("LLMCTL_METRICS_KEY", key)
    if not key:
        raise SystemExit("LLMCTL_BENCH_KEY is required")
    metric_urls = [item for item in args.metrics_urls.split(",") if item]
    sampler = ResourceSampler(metric_urls=metric_urls, metric_key=metric_key)
    sampler_thread = threading.Thread(target=sampler.run, daemon=True)
    sampler_thread.start()
    wall_started = time.monotonic()
    try:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=args.concurrency
        ) as pool:
            futures = [
                pool.submit(
                    one_request,
                    index,
                    url=args.url,
                    key=key,
                    model=args.model,
                    max_tokens=args.max_tokens,
                    prompt_tokens=args.prompt_tokens,
                    thinking_toggle=args.thinking_toggle,
                    timeout=args.timeout,
                )
                for index in range(args.requests)
            ]
            results = [future.result() for future in futures]
    finally:
        wall = time.monotonic() - wall_started
        sampler.stop_event.set()
        sampler_thread.join(timeout=5)
    good = [item for item in results if item["ok"]]
    bad = [item for item in results if not item["ok"]]
    tokens = sum(int(item["completion_tokens"]) for item in good)
    gpu, vllm, host = sampler.result()
    performance = {
        "aggregate_output_tokens_per_second": round(tokens / wall if wall else 0, 3),
        "wall_seconds": round(wall, 3),
        "completion_tokens": tokens,
        "exact_token_counts": all(item.get("exact_token_count", False) for item in good),
        "e2e_p50_seconds": round(
            percentile([item["elapsed_seconds"] for item in good], 0.50), 6
        ),
        "e2e_p95_seconds": round(
            percentile([item["elapsed_seconds"] for item in good], 0.95), 6
        ),
        "ttft_p50_seconds": round(
            percentile([item["ttft_seconds"] for item in good], 0.50), 6
        ),
        "ttft_p95_seconds": round(
            percentile([item["ttft_seconds"] for item in good], 0.95), 6
        ),
        "itl_p50_seconds": round(
            percentile([item["itl_seconds"] for item in good], 0.50), 6
        ),
        "itl_p95_seconds": round(
            percentile([item["itl_seconds"] for item in good], 0.95), 6
        ),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "workload": {
            "label": args.label,
            "concurrency": args.concurrency,
            "requests": args.requests,
            "max_output_tokens": args.max_tokens,
            "approximate_prompt_tokens": args.prompt_tokens,
        },
        "outcome": {
            "successful_requests": len(good),
            "failed_requests": len(bad),
            "success_rate": round(len(good) / len(results) if results else 0.0, 6),
        },
        "performance": performance,
        "gpu": gpu,
        "host": host,
        "vllm": vllm,
        "errors": [item.get("error", "unknown") for item in bad[:3]],
    }


def localized(zh: str, en: str, language: str) -> str:
    return en if language == "en" else zh


def make_change(
    key: str,
    old: Any,
    new: Any,
    reason_zh: str,
    reason_en: str,
    caution_zh: str,
    caution_en: str,
    language: str,
) -> dict[str, Any]:
    return {
        "key": key,
        "from": old,
        "to": new,
        "reason": localized(reason_zh, reason_en, language),
        "caution": localized(caution_zh, caution_en, language),
    }


def recommend(args: argparse.Namespace) -> dict[str, Any]:
    result = json.loads(Path(args.result).read_text(encoding="utf-8"))
    failures = int(result["outcome"]["failed_requests"])
    preemptions = int(result["vllm"]["preemptions_delta"])
    waiting = int(result["vllm"]["waiting_requests_peak"])
    language = args.lang
    gpu_available = bool(result.get("gpu", {}).get("available", False))
    host_available = bool(result.get("host", {}).get("available", False))
    metrics_available = bool(result.get("vllm", {}).get("available", False))
    host_pressure = host_available and (
        float(result["host"].get("cpu_utilization_average_pct", 0.0)) >= 90
        or float(result["host"].get("memory_used_peak_pct", 0.0)) >= 92
        or float(result["host"].get("memory_available_min_gib", 0.0)) < 8
        or float(result["host"].get("swap_used_peak_gib", 0.0)) >= 1
    )
    gpu_pressure = gpu_available and (
        float(result["gpu"].get("temperature_peak_c", 0.0)) >= 85
        or float(result["gpu"].get("vram_used_peak_pct", 0.0)) >= 97
    )
    candidates: list[dict[str, Any]] = []
    attentions: list[dict[str, str]] = []

    def attention(code: str, zh: str, en: str) -> None:
        attentions.append({"code": code, "text": localized(zh, en, language)})

    def add_candidate(
        name: str,
        *,
        seqs: int | None = None,
        batched_tokens: int | None = None,
        memory_utilization: float | None = None,
        changes: list[dict[str, Any]],
    ) -> None:
        candidate = {
            "name": name,
            "max_num_seqs": seqs if seqs is not None else args.max_num_seqs,
            "max_num_batched_tokens": (
                batched_tokens
                if batched_tokens is not None
                else args.max_num_batched_tokens
            ),
            "gpu_memory_utilization": round(
                memory_utilization
                if memory_utilization is not None
                else args.gpu_memory_utilization,
                2,
            ),
            "changes": changes,
        }
        signature = (
            candidate["max_num_seqs"],
            candidate["max_num_batched_tokens"],
            candidate["gpu_memory_utilization"],
        )
        baseline = (
            args.max_num_seqs,
            args.max_num_batched_tokens,
            round(args.gpu_memory_utilization, 2),
        )
        if signature != baseline and not any(
            (
                item["max_num_seqs"],
                item["max_num_batched_tokens"],
                item["gpu_memory_utilization"],
            )
            == signature
            for item in candidates
        ):
            candidates.append(candidate)

    attention(
        "synthetic-workload",
        "自动测试是可复现的合成文本负载，不代表你的真实 prompt、输出长度、工具调用或图片比例；上线前仍应回放真实业务样本。",
        "The automatic test is a reproducible synthetic text workload; it does not represent your real prompts, output lengths, tool calls, or image mix. Replay production-like samples before rollout.",
    )
    if args.max_model_len >= 131072:
        attention(
            "long-context-boundary",
            f"当前最大上下文为 {args.max_model_len}；本测试不证明多个请求可以同时占满该窗口。",
            f"The configured maximum context is {args.max_model_len}; this test does not prove that multiple requests can simultaneously fill that window.",
        )
    if args.supports_image:
        attention(
            "multimodal-boundary",
            "性能矩阵仅测试文本；最终完整冒烟会验证图片/OCR，但不会给出图片吞吐容量。",
            "The performance matrix is text-only. Final full smoke testing validates image/OCR behavior but does not establish image throughput capacity.",
        )
    if args.pcie_only and args.tp_size > 1:
        attention(
            "pcie-tp",
            "当前 TP 跨卡通信未检测到全互联 NVLink；PCIe 通信可能限制 TP 扩展，自动流程不会改变 TP。",
            "No fully connected NVLink path was detected for tensor parallelism. PCIe communication may limit TP scaling, and the automatic flow will not change TP.",
        )
    if gpu_available and result["gpu"]["temperature_peak_c"] >= 85:
        attention(
            "thermal",
            f"测试峰值温度达到 {result['gpu']['temperature_peak_c']:.1f}°C；先检查风道、功耗上限和降频，禁止继续激进调参。",
            f"Peak temperature reached {result['gpu']['temperature_peak_c']:.1f}°C. Check airflow, power limits, and throttling before aggressive tuning.",
        )
    if gpu_available and result["gpu"]["vram_used_peak_pct"] >= 97:
        attention(
            "vram-pressure",
            f"测试显存峰值达到 {result['gpu']['vram_used_peak_pct']:.1f}%；不会自动扩大调度或显存占用。",
            f"Peak VRAM usage reached {result['gpu']['vram_used_peak_pct']:.1f}%. Scheduler or memory allocation will not be increased automatically.",
        )
    if not gpu_available:
        attention(
            "gpu-metrics-unavailable",
            "未能采集 GPU 利用率、显存和温度；不会自动扩大调度参数。请先检查 nvidia-smi 查询权限与字段支持。",
            "GPU utilization, VRAM, and temperature metrics were unavailable. Scheduler limits will not be increased automatically. Check nvidia-smi query permissions and field support.",
        )
    if not metrics_available:
        attention(
            "vllm-metrics-unavailable",
            "未能从 Worker /metrics 读取 KV Cache、排队和抢占；除故障恢复外，不生成向上调优候选。",
            "KV-cache, queue, and preemption metrics were unavailable from worker /metrics. No upward-tuning candidate is generated except conservative failure recovery.",
        )
    if not host_available:
        attention(
            "host-metrics-unavailable",
            "未能采集 CPU、内存和 Swap；除降低故障压力外，不生成向上调优候选。",
            "CPU, memory, and swap metrics were unavailable. No upward-tuning candidate is generated except conservative failure reduction.",
        )
    elif host_pressure:
        attention(
            "host-pressure",
            f"主机资源存在压力：CPU 平均 {result['host']['cpu_utilization_average_pct']:.1f}%，最低可用内存 {result['host']['memory_available_min_gib']:.1f} GiB，Swap 峰值 {result['host']['swap_used_peak_gib']:.1f} GiB；不会自动扩大调度参数。",
            f"Host pressure was detected: average CPU {result['host']['cpu_utilization_average_pct']:.1f}%, minimum available memory {result['host']['memory_available_min_gib']:.1f} GiB, peak swap {result['host']['swap_used_peak_gib']:.1f} GiB. Scheduler limits will not be increased automatically.",
        )
    if failures:
        attention(
            "request-failures",
            f"基线有 {failures} 个请求失败；候选只允许降低调度压力，不会向上扩容。",
            f"The baseline had {failures} failed requests. Candidates may only reduce scheduler pressure, not increase it.",
        )
        new_seqs = max(1, math.floor(args.max_num_seqs * 0.75))
        new_tokens = max(2048, (args.max_num_batched_tokens // 2 // 1024) * 1024)
        changes: list[dict[str, Any]] = []
        if new_seqs != args.max_num_seqs:
            changes.append(
                make_change(
                    "max-num-seqs",
                    args.max_num_seqs,
                    new_seqs,
                    "基线请求失败，先降低每实例同时调度的序列数。",
                    "Baseline requests failed; reduce simultaneously scheduled sequences per replica.",
                    "吞吐可能下降，但可减少排队后集中抢占或 OOM 风险。",
                    "Throughput may decrease, but burst preemption and OOM risk should fall.",
                    language,
                )
            )
        if new_tokens != args.max_num_batched_tokens:
            changes.append(
                make_change(
                    "max-num-batched-tokens",
                    args.max_num_batched_tokens,
                    new_tokens,
                    "基线失败时缩小调度批次，建立可用的保守候选。",
                    "Reduce the scheduler token budget to establish a conservative candidate after baseline failures.",
                    "TTFT 或聚合吞吐可能降低。",
                    "TTFT or aggregate throughput may decrease.",
                    language,
                )
            )
        add_candidate(
            "conservative-recovery",
            seqs=new_seqs,
            batched_tokens=new_tokens,
            changes=changes,
        )
    elif not metrics_available:
        pass
    elif preemptions:
        attention(
            "preemption",
            f"测试期间记录到 {preemptions} 次 KV Cache 抢占；这会触发重算并放大尾延迟。",
            f"The test recorded {preemptions} KV-cache preemptions, which cause recomputation and increase tail latency.",
        )
        vram_peak = float(result["gpu"]["vram_used_peak_pct"])
        if gpu_available and not gpu_pressure and host_available and not host_pressure and args.gpu_memory_utilization < 0.94 and vram_peak < 97:
            new_memory = min(0.94, round(args.gpu_memory_utilization + 0.01, 2))
            add_candidate(
                "more-kv-cache",
                memory_utilization=new_memory,
                changes=[
                    make_change(
                        "gpu-memory-utilization",
                        args.gpu_memory_utilization,
                        new_memory,
                        "检测到 KV Cache 抢占，按 1% 小步增加 vLLM 可预分配显存。",
                        "KV-cache preemption was detected; increase vLLM's allocatable VRAM in a 1% step.",
                        "会重启 Worker；若模型加载、CUDA Graph 或完整冒烟失败将自动回滚。",
                        "Workers restart; model-load, CUDA Graph, or full-smoke failure triggers automatic rollback.",
                        language,
                    )
                ],
            )
        new_seqs = max(1, args.max_num_seqs - 1)
        new_tokens = max(2048, (args.max_num_batched_tokens // 2 // 1024) * 1024)
        changes = []
        if new_seqs != args.max_num_seqs:
            changes.append(
                make_change(
                    "max-num-seqs",
                    args.max_num_seqs,
                    new_seqs,
                    "减少同时占用 KV Cache 的序列。",
                    "Reduce the number of sequences occupying KV cache concurrently.",
                    "峰值吞吐可能下降。",
                    "Peak throughput may decrease.",
                    language,
                )
            )
        if new_tokens != args.max_num_batched_tokens:
            changes.append(
                make_change(
                    "max-num-batched-tokens",
                    args.max_num_batched_tokens,
                    new_tokens,
                    "缩小单次调度 token 预算以减少抢占压力。",
                    "Reduce the per-step token budget to lower preemption pressure.",
                    "较长 prompt 的 TTFT 可能增加。",
                    "TTFT for longer prompts may increase.",
                    language,
                )
            )
        add_candidate(
            "lower-scheduler-pressure",
            seqs=new_seqs,
            batched_tokens=new_tokens,
            changes=changes,
        )
    elif not gpu_available or gpu_pressure or not host_available or host_pressure:
        pass
    else:
        if args.profile == "latency":
            target = max(2048, (args.max_num_batched_tokens // 2 // 1024) * 1024)
            add_candidate(
                "latency-batch",
                batched_tokens=target,
                changes=[
                    make_change(
                        "max-num-batched-tokens",
                        args.max_num_batched_tokens,
                        target,
                        "较小的 chunked-prefill token 预算通常减少 prefill 对 decode 的干扰，候选将实测 ITL/TTFT。",
                        "A smaller chunked-prefill token budget usually reduces prefill interference with decode; the trial measures ITL and TTFT.",
                        "长 prompt 的首 token 延迟和聚合吞吐可能变差。",
                        "TTFT for long prompts and aggregate throughput may regress.",
                        language,
                    )
                ],
            )
        elif args.profile == "balanced":
            if args.max_num_batched_tokens < 16384:
                add_candidate(
                    "balanced-batch",
                    batched_tokens=16384,
                    changes=[
                        make_change(
                            "max-num-batched-tokens",
                            args.max_num_batched_tokens,
                            16384,
                            "基线无失败/抢占；测试更大的 chunked-prefill 预算是否改善 TTFT 与总吞吐。",
                            "The baseline had no failures or preemptions; test whether a larger chunked-prefill budget improves TTFT and throughput.",
                            "ITL 可能增加；只有综合得分至少提高 5% 才会保留。",
                            "ITL may increase; the candidate is kept only if the composite score improves by at least 5%.",
                            language,
                        )
                    ],
                )
        else:
            targets = [value for value in (16384, 32768) if value > args.max_num_batched_tokens]
            if args.quick:
                targets = targets[:1]
            for target in targets:
                add_candidate(
                    f"throughput-batch-{target}",
                    batched_tokens=target,
                    changes=[
                        make_change(
                            "max-num-batched-tokens",
                            args.max_num_batched_tokens,
                            target,
                            "基线无失败/抢占；vLLM 对吞吐优先负载建议测试大于 8192 的调度 token 预算。",
                            "The baseline had no failures or preemptions; vLLM recommends testing token budgets above 8192 for throughput-oriented workloads.",
                            "更大的批次可能增加 ITL、KV Cache 压力和尾延迟。",
                            "Larger batches may increase ITL, KV-cache pressure, and tail latency.",
                            language,
                        )
                    ],
                )
    if (
        not failures
        and not preemptions
        and metrics_available
        and gpu_available
        and not gpu_pressure
        and host_available
        and not host_pressure
        and waiting > 0
        and args.profile != "latency"
        and args.max_num_seqs < args.estimated_max_num_seqs
    ):
        target = min(
            args.estimated_max_num_seqs,
            max(args.max_num_seqs + 1, math.ceil(args.max_num_seqs * 1.5)),
        )
        add_candidate(
            "scheduler-slots",
            seqs=target,
            changes=[
                make_change(
                    "max-num-seqs",
                    args.max_num_seqs,
                    target,
                    "测试出现排队，且硬件规划仍有保守序列余量。",
                    "The test observed queueing and the hardware plan still has conservative sequence headroom.",
                    "真实长上下文或图片请求会比合成文本占用更多 KV/临时显存。",
                    "Real long-context or image requests consume more KV and temporary VRAM than synthetic text.",
                    language,
                )
            ],
        )
    if gpu_available and result["gpu"]["utilization_average_pct"] < 45 and not failures:
        attention(
            "low-gpu-utilization",
            f"压测平均 GPU 利用率仅 {result['gpu']['utilization_average_pct']:.1f}%；可能受客户端、路由、短输出或模型稀疏性限制，不能只靠增加显存参数解决。",
            f"Average GPU utilization was only {result['gpu']['utilization_average_pct']:.1f}%. Client, routing, short outputs, or model sparsity may be limiting; VRAM tuning alone cannot fix this.",
        )
    queries = int(result["vllm"]["prefix_cache_queries_delta"])
    hit_rate = float(result["vllm"]["prefix_cache_hit_rate"])
    if args.instance_count > 1 and queries and hit_rate < 0.10:
        attention(
            "prefix-cache-locality",
            f"前缀缓存命中率仅 {hit_rate * 100:.1f}%；多副本路由会分散缓存。生产请求若有重复长前缀，应额外评估会话亲和路由，自动流程不会改路由语义。",
            f"Prefix-cache hit rate was only {hit_rate * 100:.1f}%. Multi-replica routing fragments cache locality. If production uses repeated long prefixes, separately evaluate session affinity; the automatic flow will not change routing semantics.",
        )
    if not candidates:
        attention(
            "no-safe-candidate",
            "当前证据没有产生可安全自动试验的参数；保留现配置，并用真实业务负载补测。",
            "Current evidence produced no safely testable automatic parameter change. Keep the current configuration and benchmark production-like traffic.",
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "profile": args.profile,
        "candidates": candidates[:2],
        "attentions": attentions,
    }


def safe_ratio(numerator: float, denominator: float) -> float:
    if numerator <= 0 or denominator <= 0:
        return 1.0
    return max(0.25, min(4.0, numerator / denominator))


def score_trial(profile: str, baseline: dict[str, Any], trial: dict[str, Any]) -> float:
    base = baseline["performance"]
    item = trial["performance"]
    throughput = safe_ratio(
        float(item["aggregate_output_tokens_per_second"]),
        float(base["aggregate_output_tokens_per_second"]),
    )
    ttft = safe_ratio(
        float(base["ttft_p95_seconds"]), float(item["ttft_p95_seconds"])
    )
    itl = safe_ratio(
        float(base["itl_p95_seconds"]), float(item["itl_p95_seconds"])
    )
    if profile == "latency":
        return 0.10 * throughput + 0.45 * ttft + 0.45 * itl
    if profile == "throughput":
        return 0.80 * throughput + 0.10 * ttft + 0.10 * itl
    return 0.40 * throughput + 0.30 * ttft + 0.30 * itl


def choose(args: argparse.Namespace) -> dict[str, Any]:
    baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    base_failures = int(baseline["outcome"]["failed_requests"])
    base_preemptions = int(baseline["vllm"]["preemptions_delta"])
    baseline_eligible = base_failures == 0
    scores = [
        {
            "name": "baseline",
            "score": 1.0,
            "eligible": baseline_eligible,
            "reason": "" if baseline_eligible else "request failures",
        }
    ]
    best_name = "baseline"
    best_score = 1.0 if baseline_eligible else 0.0
    for trial_arg in args.trial:
        name, path = trial_arg.split("=", 1)
        trial = json.loads(Path(path).read_text(encoding="utf-8"))
        failures = int(trial["outcome"]["failed_requests"])
        preemptions = int(trial["vllm"]["preemptions_delta"])
        eligible = failures == 0
        reason = ""
        if failures:
            reason = "request failures"
        elif base_failures == 0 and preemptions > base_preemptions:
            eligible = False
            reason = "more KV-cache preemptions than baseline"
        score = score_trial(args.profile, baseline, trial) if eligible else 0.0
        scores.append(
            {
                "name": name,
                "score": round(score, 6),
                "eligible": eligible,
                "reason": reason,
            }
        )
        threshold = 1.05
        if base_failures > 0 and failures == 0:
            threshold = 0.95
        elif base_preemptions > 0 and preemptions < base_preemptions:
            threshold = 0.98
        if eligible and score >= threshold and (
            best_name == "baseline" or score > best_score
        ):
            best_name = name
            best_score = score
    return {
        "schema_version": SCHEMA_VERSION,
        "profile": args.profile,
        "selected": best_name,
        "selected_score": round(best_score, 6),
        "minimum_improvement": 0.05,
        "scores": scores,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    benchmark = subparsers.add_parser("benchmark")
    benchmark.add_argument("--url", required=True)
    benchmark.add_argument("--model", required=True)
    benchmark.add_argument("--metrics-urls", default="")
    benchmark.add_argument("--concurrency", type=int, required=True)
    benchmark.add_argument("--requests", type=int, required=True)
    benchmark.add_argument("--max-tokens", type=int, required=True)
    benchmark.add_argument("--prompt-tokens", type=int, required=True)
    benchmark.add_argument("--timeout", type=int, default=7200)
    benchmark.add_argument("--label", default="baseline")
    benchmark.add_argument("--thinking-toggle", action="store_true")

    adviser = subparsers.add_parser("recommend")
    adviser.add_argument("--result", required=True)
    adviser.add_argument("--profile", choices=("latency", "balanced", "throughput"), required=True)
    adviser.add_argument("--max-num-seqs", type=int, required=True)
    adviser.add_argument("--estimated-max-num-seqs", type=int, required=True)
    adviser.add_argument("--max-num-batched-tokens", type=int, required=True)
    adviser.add_argument("--gpu-memory-utilization", type=float, required=True)
    adviser.add_argument("--max-model-len", type=int, required=True)
    adviser.add_argument("--instance-count", type=int, required=True)
    adviser.add_argument("--tp-size", type=int, required=True)
    adviser.add_argument("--supports-image", action="store_true")
    adviser.add_argument("--pcie-only", action="store_true")
    adviser.add_argument("--quick", action="store_true")
    adviser.add_argument("--lang", choices=("zh", "en"), default="zh")

    chooser = subparsers.add_parser("choose")
    chooser.add_argument("--baseline", required=True)
    chooser.add_argument("--profile", choices=("latency", "balanced", "throughput"), required=True)
    chooser.add_argument("--trial", action="append", default=[])
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "benchmark":
        if not (1 <= args.concurrency <= 256):
            raise SystemExit("concurrency must be between 1 and 256")
        if not (args.concurrency <= args.requests <= 10000):
            raise SystemExit("requests must be >= concurrency and <= 10000")
        output = run_benchmark(args)
    elif args.command == "recommend":
        output = recommend(args)
    else:
        output = choose(args)
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
