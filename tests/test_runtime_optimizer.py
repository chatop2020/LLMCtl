#!/usr/bin/env python3
import argparse
import importlib.util
import json
import pathlib
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


ROOT = pathlib.Path(__file__).resolve().parents[1]
OPTIMIZER_PATH = ROOT / "lib" / "runtime_optimizer.py"
SPEC = importlib.util.spec_from_file_location("runtime_optimizer", OPTIMIZER_PATH)
OPTIMIZER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = OPTIMIZER
SPEC.loader.exec_module(OPTIMIZER)


def benchmark_result(
    *,
    failures=0,
    throughput=100.0,
    ttft=1.0,
    itl=0.02,
    preemptions=0,
    waiting=2,
    gpu_util=70.0,
    temperature=70.0,
):
    return {
        "schema_version": 1,
        "outcome": {
            "successful_requests": 8 - failures,
            "failed_requests": failures,
            "success_rate": (8 - failures) / 8,
        },
        "performance": {
            "aggregate_output_tokens_per_second": throughput,
            "ttft_p95_seconds": ttft,
            "itl_p95_seconds": itl,
            "e2e_p95_seconds": 4.0,
        },
        "gpu": {
            "available": True,
            "utilization_average_pct": gpu_util,
            "utilization_peak_pct": 95.0,
            "vram_used_peak_pct": 92.0,
            "temperature_peak_c": temperature,
        },
        "host": {
            "available": True,
            "cpu_utilization_average_pct": 40.0,
            "cpu_utilization_peak_pct": 70.0,
            "load1_peak": 8.0,
            "memory_available_min_gib": 128.0,
            "memory_used_peak_pct": 50.0,
            "swap_used_peak_gib": 0.0,
        },
        "vllm": {
            "available": True,
            "preemptions_delta": preemptions,
            "waiting_requests_peak": waiting,
            "kv_cache_usage_peak_pct": 80.0,
            "prefix_cache_hits_delta": 0,
            "prefix_cache_queries_delta": 100,
            "prefix_cache_hit_rate": 0.0,
        },
    }


def adviser_args(result_path, **overrides):
    values = dict(
        result=str(result_path),
        profile="balanced",
        max_num_seqs=7,
        estimated_max_num_seqs=12,
        max_num_batched_tokens=8192,
        gpu_memory_utilization=0.92,
        max_model_len=262144,
        instance_count=8,
        tp_size=1,
        supports_image=True,
        pcie_only=False,
        quick=False,
        lang="zh",
    )
    values.update(overrides)
    return argparse.Namespace(**values)


class RuntimeOptimizerTests(unittest.TestCase):
    def write_result(self, directory, result):
        path = pathlib.Path(directory) / "result.json"
        path.write_text(json.dumps(result), encoding="utf-8")
        return path

    def test_prometheus_parser_accepts_current_and_legacy_counter_names(self):
        metrics = OPTIMIZER.parse_prometheus(
            """
            # HELP ignored
            vllm:num_preemptions_total{model_name="x"} 2
            vllm:num_preemptions{model_name="y"} 3
            vllm:kv_cache_usage_perc{model_name="x"} 0.75
            vllm:gpu_cache_usage_perc{model_name="legacy"} 0.70
            """
        )
        self.assertEqual(
            OPTIMIZER.metric_value(
                metrics,
                ("vllm:num_preemptions", "vllm:num_preemptions_total"),
                "sum",
            ),
            5,
        )
        self.assertEqual(
            OPTIMIZER.metric_value(metrics, ("vllm:kv_cache_usage_perc",), "max"),
            0.75,
        )
        self.assertEqual(
            OPTIMIZER.metric_value(
                metrics,
                ("vllm:kv_cache_usage_perc", "vllm:gpu_cache_usage_perc"),
                "max",
            ),
            0.75,
        )

    def test_resource_sampler_result_has_object_sections(self):
        sampler = OPTIMIZER.ResourceSampler(metric_urls=[], metric_key="")
        sampler.gpu_util_sum = 80
        sampler.gpu_util_count = 1
        sampler.cpu_util_sum = 40
        sampler.cpu_util_count = 1
        sampler.memory_available_min_gib = 64
        gpu, vllm, host = sampler.result()
        self.assertIsInstance(gpu, dict)
        self.assertIsInstance(vllm, dict)
        self.assertIsInstance(host, dict)
        self.assertEqual(gpu["utilization_average_pct"], 80)
        self.assertEqual(host["memory_available_min_gib"], 64)

    def test_balanced_clean_baseline_proposes_bounded_batch_trial(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_result(directory, benchmark_result())
            advice = OPTIMIZER.recommend(adviser_args(path))
        candidate = advice["candidates"][0]
        self.assertEqual(candidate["name"], "balanced-batch")
        self.assertEqual(candidate["max_num_batched_tokens"], 16384)
        self.assertEqual(candidate["max_num_seqs"], 7)
        self.assertTrue(any(item["code"] == "synthetic-workload" for item in advice["attentions"]))
        self.assertTrue(any(item["code"] == "long-context-boundary" for item in advice["attentions"]))

    def test_failures_only_produce_conservative_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_result(directory, benchmark_result(failures=2))
            advice = OPTIMIZER.recommend(adviser_args(path))
        self.assertEqual([item["name"] for item in advice["candidates"]], ["conservative-recovery"])
        self.assertLess(advice["candidates"][0]["max_num_seqs"], 7)
        self.assertLess(advice["candidates"][0]["max_num_batched_tokens"], 8192)

    def test_preemption_proposes_small_memory_step_and_pressure_reduction(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_result(directory, benchmark_result(preemptions=3))
            advice = OPTIMIZER.recommend(adviser_args(path))
        names = [item["name"] for item in advice["candidates"]]
        self.assertEqual(names, ["more-kv-cache", "lower-scheduler-pressure"])
        self.assertEqual(advice["candidates"][0]["gpu_memory_utilization"], 0.93)

    def test_missing_runtime_metrics_blocks_upward_tuning(self):
        result = benchmark_result()
        result["vllm"]["available"] = False
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_result(directory, result)
            advice = OPTIMIZER.recommend(adviser_args(path))
        self.assertEqual(advice["candidates"], [])
        self.assertTrue(
            any(item["code"] == "vllm-metrics-unavailable" for item in advice["attentions"])
        )

    def test_host_pressure_blocks_upward_tuning(self):
        result = benchmark_result()
        result["host"]["memory_available_min_gib"] = 4.0
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_result(directory, result)
            advice = OPTIMIZER.recommend(adviser_args(path))
        self.assertEqual(advice["candidates"], [])
        self.assertTrue(any(item["code"] == "host-pressure" for item in advice["attentions"]))

    def test_thermal_pressure_blocks_upward_tuning(self):
        result = benchmark_result(temperature=86)
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_result(directory, result)
            advice = OPTIMIZER.recommend(adviser_args(path))
        self.assertEqual(advice["candidates"], [])
        self.assertTrue(any(item["code"] == "thermal" for item in advice["attentions"]))

    def test_chooser_requires_real_improvement_and_rejects_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            base = self.write_result(directory, benchmark_result())
            better = pathlib.Path(directory) / "better.json"
            better.write_text(
                json.dumps(benchmark_result(throughput=120, ttft=0.8, itl=0.016)),
                encoding="utf-8",
            )
            failed = pathlib.Path(directory) / "failed.json"
            failed.write_text(
                json.dumps(benchmark_result(failures=1, throughput=200)),
                encoding="utf-8",
            )
            choice = OPTIMIZER.choose(
                argparse.Namespace(
                    baseline=str(base),
                    profile="balanced",
                    trial=[f"better={better}", f"failed={failed}"],
                )
            )
        self.assertEqual(choice["selected"], "better")
        failed_score = next(item for item in choice["scores"] if item["name"] == "failed")
        self.assertFalse(failed_score["eligible"])

    def test_clean_recovery_candidate_beats_failed_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            base = self.write_result(directory, benchmark_result(failures=2))
            recovered = pathlib.Path(directory) / "recovered.json"
            recovered.write_text(
                json.dumps(benchmark_result(throughput=98, ttft=1.02, itl=0.021)),
                encoding="utf-8",
            )
            choice = OPTIMIZER.choose(
                argparse.Namespace(
                    baseline=str(base),
                    profile="balanced",
                    trial=[f"recovered={recovered}"],
                )
            )
        self.assertEqual(choice["selected"], "recovered")
        self.assertFalse(choice["scores"][0]["eligible"])

    def test_lower_preemption_candidate_may_trade_small_performance_loss(self):
        with tempfile.TemporaryDirectory() as directory:
            base = self.write_result(directory, benchmark_result(preemptions=3))
            safer = pathlib.Path(directory) / "safer.json"
            safer.write_text(
                json.dumps(benchmark_result(throughput=99, ttft=1.01, itl=0.0202)),
                encoding="utf-8",
            )
            choice = OPTIMIZER.choose(
                argparse.Namespace(
                    baseline=str(base),
                    profile="balanced",
                    trial=[f"safer={safer}"],
                )
            )
        self.assertEqual(choice["selected"], "safer")

    def test_streaming_request_measures_ttft_and_exact_usage(self):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                chunks = [
                    {"choices": [{"delta": {"content": "hello"}}]},
                    {"choices": [{"delta": {"content": " world"}}]},
                    {"choices": [], "usage": {"completion_tokens": 2}},
                ]
                body = "".join(f"data: {json.dumps(item)}\n\n" for item in chunks)
                body += "data: [DONE]\n\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body.encode())))
                self.end_headers()
                self.wfile.write(body.encode())

            def log_message(self, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = OPTIMIZER.one_request(
                0,
                url=f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
                key="test",
                model="test",
                max_tokens=2,
                prompt_tokens=16,
                thinking_toggle=True,
                timeout=5,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertTrue(result["ok"])
        self.assertEqual(result["completion_tokens"], 2)
        self.assertTrue(result["exact_token_count"])
        self.assertGreaterEqual(result["ttft_seconds"], 0)


if __name__ == "__main__":
    unittest.main()
