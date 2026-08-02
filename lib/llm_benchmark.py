#!/usr/bin/env python3
"""Backend load generator for the LLMCtl administration console.

The browser only submits and observes a benchmark plan.  This process owns all
concurrency, streams responses from the local OpenAI-compatible gateway, and
publishes an atomic status file plus an append-only request event log.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import json
import math
import os
import pathlib
import random
import re
import shutil
import signal
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any


CONCURRENCY_CHOICES = (1, 2, 4, 6, 8, 10, 15, 20, 25, 30, 40, 50, 60, 70, 80, 100)
INPUT_TOKEN_CHOICES = (50, 100, 300, 800, 1500, 3000, 6000, 8000, 15000, 30000)
OUTPUT_TOKEN_CHOICES = (64, 128, 256, 512, 1024)
REQUEST_MULTIPLIER_CHOICES = (1, 2, 3, 4)

_STOP = threading.Event()
_WRITE_LOCK = threading.Lock()
ROUTE_DECISION_PART = re.compile(r"^\s*([^=;]+)=([^;]*)\s*$")

TOPICS = (
    "distributed inference capacity planning",
    "incident response for a regional payment service",
    "database migration with zero customer downtime",
    "energy-efficient scheduling for a GPU cluster",
    "quality assurance for an enterprise document workflow",
    "privacy review for an internal knowledge assistant",
    "supply-chain forecasting under uncertain demand",
    "observability design for a high-volume API platform",
)
VERBS = ("analyze", "compare", "validate", "explain", "prioritize", "estimate", "review")
QUALIFIERS = (
    "using explicit assumptions",
    "with measurable acceptance criteria",
    "while identifying operational risks",
    "and separate facts from recommendations",
    "with a concise executive summary",
    "including failure modes and mitigations",
)


def _finite_float(value: str) -> float | None:
    try:
        result = float(value.strip())
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


class GpuSampler:
    """Sample host GPU activity without making benchmark success depend on NVML.

    The portal service intentionally has no NVIDIA Python dependency.  A fixed
    ``nvidia-smi`` query gives enough evidence to catch routing hot spots while
    remaining fail-open on CPU-only hosts or installations where the service
    account cannot inspect the driver.
    """

    QUERY = (
        "index,utilization.gpu,memory.used,power.draw"
    )

    def __init__(self, interval_seconds: float = 1.0) -> None:
        self.interval_seconds = max(0.25, interval_seconds)
        self.executable = shutil.which("nvidia-smi")
        self.samples: dict[int, list[dict[str, float]]] = {}
        self.error = ""
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    @staticmethod
    def parse_output(output: str) -> dict[int, dict[str, float]]:
        parsed: dict[int, dict[str, float]] = {}
        for raw_line in output.splitlines():
            values = [value.strip() for value in raw_line.split(",")]
            if len(values) != 4 or not values[0].isdigit():
                continue
            numeric = [_finite_float(value) for value in values[1:]]
            if any(value is None for value in numeric):
                continue
            parsed[int(values[0])] = {
                "utilization_percent": float(numeric[0]),
                "memory_used_mib": float(numeric[1]),
                "power_watts": float(numeric[2]),
            }
        return parsed

    def sample_once(self) -> None:
        if not self.executable:
            self.error = "nvidia-smi unavailable"
            return
        try:
            completed = subprocess.run(  # noqa: S603 - fixed executable and arguments
                [
                    self.executable,
                    f"--query-gpu={self.QUERY}",
                    "--format=csv,noheader,nounits",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=3,
            )
        except (OSError, subprocess.SubprocessError) as error:
            self.error = str(error)[:200]
            return
        if completed.returncode != 0:
            self.error = (completed.stderr.strip() or "nvidia-smi failed")[:200]
            return
        sample = self.parse_output(completed.stdout)
        if not sample:
            self.error = "nvidia-smi returned no GPU samples"
            return
        with self._lock:
            for index, values in sample.items():
                self.samples.setdefault(index, []).append(values)

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self.sample_once()

    def start(self) -> None:
        self.sample_once()
        self._thread = threading.Thread(
            target=self._run, name="llm-benchmark-gpu-sampler", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=max(1.0, self.interval_seconds * 2))
        self.sample_once()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            samples = {index: list(values) for index, values in self.samples.items()}
        gpus: list[dict[str, Any]] = []
        for index, values in sorted(samples.items()):
            utilization = [item["utilization_percent"] for item in values]
            memory = [item["memory_used_mib"] for item in values]
            power = [item["power_watts"] for item in values]
            active_samples = sum(value >= 5 for value in utilization)
            gpus.append(
                {
                    "index": index,
                    "samples": len(values),
                    "utilization_average": round(statistics.fmean(utilization), 2),
                    "utilization_peak": round(max(utilization), 2),
                    "active_sample_percent": round(active_samples * 100 / len(values), 2),
                    "memory_used_peak_mib": round(max(memory), 1),
                    "power_average_watts": round(statistics.fmean(power), 2),
                    "power_peak_watts": round(max(power), 2),
                }
            )
        aligned_sample_count = min(
            (len(values) for values in samples.values()), default=0
        )
        concurrent_active_counts = [
            sum(
                values[sample_index]["utilization_percent"] >= 5
                for values in samples.values()
            )
            for sample_index in range(aligned_sample_count)
        ]
        return {
            "available": bool(gpus),
            "sample_count": max((item["samples"] for item in gpus), default=0),
            "current_active_gpu_count": (
                concurrent_active_counts[-1] if concurrent_active_counts else 0
            ),
            "peak_concurrent_active_gpu_count": (
                max(concurrent_active_counts) if concurrent_active_counts else 0
            ),
            "average_concurrent_active_gpu_count": round(
                statistics.fmean(concurrent_active_counts), 2
            )
            if concurrent_active_counts
            else 0,
            "gpus": gpus,
            "error": "" if gpus else self.error,
        }


def parse_route_decision(value: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for part in value.split(";"):
        match = ROUTE_DECISION_PART.match(part)
        if match:
            result[match.group(1).strip().lower()] = match.group(2).strip()
    return result


def route_label(value: str, route_map: dict[str, str]) -> str:
    value = value.strip()
    if not value:
        return ""
    return str(route_map.get(value) or value)


def percentile(values: list[float], value: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered) - 1) * value
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return round(ordered[lower], 3)
    return round(
        ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower), 3
    )


def meaningful_prompt(target_tokens: int, seed: str) -> str:
    """Produce varied prose close to the requested tokenizer-independent target.

    Common English words are used because they are approximately one token for
    the model families LLMCtl targets.  The gateway's returned usage remains the
    source of truth and is reported separately from this requested target.
    """
    generator = random.Random(seed)
    words: list[str] = []
    word_budget = max(1, target_tokens - 24)
    case_number = 1
    while len(words) < word_budget:
        topic = generator.choice(TOPICS)
        sentence = (
            f"Case {case_number}: {generator.choice(VERBS)} {topic} "
            f"{generator.choice(QUALIFIERS)}. Consider latency throughput cost "
            "reliability security maintainability and user impact. State the evidence "
            "needed, the tradeoffs, and a practical next action."
        )
        words.extend(sentence.split())
        case_number += 1
    words = words[:word_budget]
    return (
        "You are evaluating realistic enterprise scenarios. Read every case, then "
        "answer only the final case with a short numbered recommendation.\n\n"
        + " ".join(words)
    )


def error_kind(error: BaseException) -> str:
    if isinstance(error, urllib.error.HTTPError):
        return f"http_{error.code}"
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, urllib.error.URLError):
        return "network"
    if isinstance(error, json.JSONDecodeError):
        return "invalid_json"
    return type(error).__name__.lower()


def stream_request(
    *,
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
    max_tokens: int,
    timeout: int,
    route_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    payload = json.dumps(
        {
            "model": model,
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": 0.7,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        },
        ensure_ascii=False,
    ).encode()
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/chat/completions",
        data=payload,
        headers={
            "Accept": "text/event-stream",
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "LLMCtl-Benchmark/1",
        },
        method="POST",
    )
    first_token_at: float | None = None
    usage: dict[str, Any] = {}
    request_id = ""
    response_model = ""
    content_chars = 0
    route_values: dict[str, str] = {}
    with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
        for key in (
            "X-OmniRoute-Provider",
            "X-OmniRoute-Selected-Connection-Id",
            "X-OmniRoute-Decision",
        ):
            value = response.headers.get(key)
            if value:
                route_values[key.lower()] = value.strip()
        for raw_line in response:
            if _STOP.is_set():
                raise InterruptedError("benchmark canceled")
            line = raw_line.decode("utf-8", errors="replace").strip()
            if line.startswith(":"):
                # OmniRoute v3.8.x emits late response metadata as SSE comment
                # trailers because normal HTTP headers are already committed.
                trailer = line[1:].strip()
                if "=" in trailer:
                    key, value = trailer.split("=", 1)
                    route_values[key.strip().lower()] = value.strip()
                continue
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            event = json.loads(data)
            request_id = request_id or str(event.get("id", ""))
            response_model = response_model or str(event.get("model", ""))
            if isinstance(event.get("usage"), dict):
                usage = event["usage"]
            choices = event.get("choices")
            if not isinstance(choices, list) or not choices:
                continue
            delta = choices[0].get("delta", {})
            if not isinstance(delta, dict):
                continue
            text = str(delta.get("content") or delta.get("reasoning_content") or "")
            if text:
                content_chars += len(text)
                if first_token_at is None:
                    first_token_at = time.monotonic()
    finished = time.monotonic()
    input_tokens = int(usage.get("prompt_tokens", 0) or 0)
    output_tokens = int(usage.get("completion_tokens", 0) or 0)
    if output_tokens <= 0 and content_chars:
        output_tokens = max(1, round(content_chars / 3.5))
    ttft_ms = (first_token_at - started) * 1000 if first_token_at else None
    latency_ms = (finished - started) * 1000
    generation_seconds = max(
        0.001, finished - (first_token_at if first_token_at is not None else started)
    )
    decision = parse_route_decision(route_values.get("x-omniroute-decision", ""))
    provider = route_values.get("x-omniroute-provider", "") or decision.get("provider", "")
    connection = route_values.get("x-omniroute-selected-connection-id", "")
    route_map = route_map or {}
    return {
        "ok": True,
        "request_id": request_id,
        "response_model": response_model,
        "ttft_ms": round(ttft_ms, 3) if ttft_ms is not None else None,
        "latency_ms": round(latency_ms, 3),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "tokens_per_second": round(output_tokens / generation_seconds, 3),
        "route_provider": provider,
        "route_connection": connection,
        "route_target": route_label(connection or provider, route_map),
        "route_strategy": decision.get("strategy", ""),
        "error_kind": "",
        "error": "",
    }


def summarize(results: list[dict[str, Any]], elapsed_seconds: float) -> dict[str, Any]:
    completed = len(results)
    successful = [item for item in results if item.get("ok")]
    failures = [item for item in results if not item.get("ok")]
    latency = [float(item["latency_ms"]) for item in successful]
    ttft = [float(item["ttft_ms"]) for item in successful if item.get("ttft_ms") is not None]
    request_tps = [float(item["tokens_per_second"]) for item in successful]
    errors: dict[str, int] = {}
    for item in failures:
        key = str(item.get("error_kind") or "unknown")
        errors[key] = errors.get(key, 0) + 1
    route_targets: dict[str, int] = {}
    unknown_routes = 0
    for item in successful:
        target = str(item.get("route_target") or "").strip()
        if not target:
            unknown_routes += 1
            continue
        route_targets[target] = route_targets.get(target, 0) + 1
    elapsed_seconds = max(0.001, elapsed_seconds)
    total_output = sum(int(item.get("output_tokens", 0) or 0) for item in successful)
    return {
        "completed": completed,
        "successful": len(successful),
        "failed": len(failures),
        "success_rate": round(len(successful) * 100 / completed, 3) if completed else 0,
        "request_rps": round(completed / elapsed_seconds, 3),
        "successful_rps": round(len(successful) / elapsed_seconds, 3),
        "output_tokens_per_second": round(total_output / elapsed_seconds, 3),
        "input_tokens": sum(int(item.get("input_tokens", 0) or 0) for item in successful),
        "output_tokens": total_output,
        "ttft_ms": {
            "average": round(statistics.fmean(ttft), 3) if ttft else None,
            "p50": percentile(ttft, 0.50),
            "p95": percentile(ttft, 0.95),
            "p99": percentile(ttft, 0.99),
        },
        "latency_ms": {
            "average": round(statistics.fmean(latency), 3) if latency else None,
            "p50": percentile(latency, 0.50),
            "p95": percentile(latency, 0.95),
            "p99": percentile(latency, 0.99),
        },
        "request_tokens_per_second": {
            "average": round(statistics.fmean(request_tps), 3) if request_tps else None,
            "p50": percentile(request_tps, 0.50),
            "p95": percentile(request_tps, 0.95),
        },
        "errors": errors,
        "routing": {
            "targets": dict(sorted(route_targets.items())),
            "observed_target_count": len(route_targets),
            "unknown_requests": unknown_routes,
        },
    }


def atomic_json(path: pathlib.Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def execute(args: argparse.Namespace) -> int:
    result_dir = pathlib.Path(args.result_dir)
    result_dir.mkdir(parents=True, exist_ok=True)
    status_path = result_dir / "status.json"
    event_path = result_dir / "events.jsonl"
    api_key = os.environ.get("LLMCTL_BENCHMARK_API_KEY", "")
    if not api_key:
        raise SystemExit("LLMCTL_BENCHMARK_API_KEY is required")
    route_map: dict[str, str] = {}
    route_map_path = pathlib.Path(args.route_map) if getattr(args, "route_map", "") else None
    if route_map_path and route_map_path.is_file():
        try:
            route_document = json.loads(route_map_path.read_text(encoding="utf-8"))
            if isinstance(route_document, dict):
                route_map = {
                    str(key): str(value)
                    for key, value in route_document.items()
                    if str(key).strip() and str(value).strip()
                }
        except (OSError, json.JSONDecodeError):
            route_map = {}
    total_requests = args.concurrency * args.request_multiplier
    started_epoch = int(time.time())
    started = time.monotonic()
    results: list[dict[str, Any]] = []
    gpu_sampler = GpuSampler()

    def publish(status: str, error: str = "") -> None:
        metrics = summarize(results, time.monotonic() - started)
        atomic_json(
            status_path,
            {
                "id": args.run_id,
                "status": status,
                "model": args.model,
                "concurrency": args.concurrency,
                "target_input_tokens": args.input_tokens,
                "max_output_tokens": args.output_tokens,
                "request_count": total_requests,
                "started_at": started_epoch,
                "updated_at": int(time.time()),
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "progress": round(metrics["completed"] * 100 / total_requests, 2),
                "metrics": metrics,
                "gpu": gpu_sampler.snapshot(),
                "error": error,
            },
        )

    def perform(index: int) -> dict[str, Any]:
        request_started = time.monotonic()
        try:
            result = stream_request(
                base_url=args.base_url,
                api_key=api_key,
                model=args.model,
                prompt=meaningful_prompt(args.input_tokens, f"{args.run_id}:{index}"),
                max_tokens=args.output_tokens,
                timeout=args.timeout,
                route_map=route_map,
            )
        except BaseException as error:
            detail = str(error)
            if isinstance(error, urllib.error.HTTPError):
                with contextlib.suppress(Exception):
                    detail = error.read().decode("utf-8", errors="replace")[:500]
            result = {
                "ok": False,
                "request_id": "",
                "response_model": "",
                "ttft_ms": None,
                "latency_ms": round((time.monotonic() - request_started) * 1000, 3),
                "input_tokens": 0,
                "output_tokens": 0,
                "tokens_per_second": 0,
                "route_provider": "",
                "route_connection": "",
                "route_target": "",
                "route_strategy": "",
                "error_kind": "canceled" if isinstance(error, InterruptedError) else error_kind(error),
                "error": detail[:500],
            }
        result["index"] = index
        return result

    gpu_sampler.start()
    publish("running")
    try:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=args.concurrency, thread_name_prefix="llm-benchmark"
        ) as executor:
            futures = [executor.submit(perform, index) for index in range(total_requests)]
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                with _WRITE_LOCK:
                    results.append(result)
                    with event_path.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(result, ensure_ascii=False) + "\n")
                    publish("canceling" if _STOP.is_set() else "running")
                if _STOP.is_set():
                    for pending in futures:
                        pending.cancel()
        gpu_sampler.stop()
        publish("canceled" if _STOP.is_set() else "completed")
        return 130 if _STOP.is_set() else 0
    except BaseException as error:
        gpu_sampler.stop()
        publish("failed", str(error)[:500])
        return 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--concurrency", required=True, type=int, choices=CONCURRENCY_CHOICES)
    parser.add_argument("--input-tokens", required=True, type=int, choices=INPUT_TOKEN_CHOICES)
    parser.add_argument("--output-tokens", type=int, default=128, choices=OUTPUT_TOKEN_CHOICES)
    parser.add_argument("--request-multiplier", type=int, default=2, choices=REQUEST_MULTIPLIER_CHOICES)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--result-dir", required=True)
    parser.add_argument("--route-map", default="")
    return parser.parse_args()


def main() -> None:
    signal.signal(signal.SIGTERM, lambda *_: _STOP.set())
    signal.signal(signal.SIGINT, lambda *_: _STOP.set())
    raise SystemExit(execute(parse_args()))


if __name__ == "__main__":
    main()
