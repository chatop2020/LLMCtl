#!/usr/bin/env python3
import argparse
import http.server
import importlib.util
import json
import pathlib
import sys
import tempfile
import threading
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "lib" / "llm_benchmark.py"
SPEC = importlib.util.spec_from_file_location("llm_benchmark", MODULE_PATH)
benchmark = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = benchmark
SPEC.loader.exec_module(benchmark)


class StreamingHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        if self.path != "/v1/chat/completions" or payload.get("model") != "gdn-inside":
            self.send_response(400)
            self.end_headers()
            return
        body = (
            'data: {"id":"req-1","model":"gdn-inside","choices":[{"delta":{"content":"OK"}}]}\n\n'
            'data: {"choices":[],"usage":{"prompt_tokens":52,"completion_tokens":4,"total_tokens":56}}\n\n'
            ': x-omniroute-provider=llmctl-w0\n'
            "data: [DONE]\n\n"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("X-OmniRoute-Selected-Connection-Id", "conn-0")
        self.send_header(
            "X-OmniRoute-Decision", "strategy=round-robin; provider=llmctl-w0"
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        benchmark._STOP.clear()

    def test_prompt_is_meaningful_and_close_to_target(self):
        prompt = benchmark.meaningful_prompt(300, "stable-seed")
        self.assertIn("案例1：", prompt)
        self.assertIn("延迟、吞吐、成本、可靠性", prompt)
        self.assertEqual(len(prompt), 300)

    def test_largest_prompt_is_byte_compact(self):
        """30K 文字压测应减少传输放大，但其准入不能依赖请求体大小。"""

        prompt = benchmark.meaningful_prompt(30_000, "largest-plan")
        payload = json.dumps(
            {
                "model": "gdn-inside",
                "stream": True,
                "messages": [{"role": "user", "content": prompt}],
            },
            ensure_ascii=False,
        ).encode()
        self.assertEqual(len(prompt), 30_000)
        self.assertLess(len(payload), 128 * 1024)

    def test_backend_runner_streams_and_writes_professional_metrics(self):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), StreamingHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
                benchmark.os.environ,
                {"LLMCTL_BENCHMARK_API_KEY": "sk-test"},
            ):
                route_map = pathlib.Path(directory) / "route-map.json"
                route_map.write_text(
                    json.dumps({"conn-0": "LLMCtl worker 0"}), encoding="utf-8"
                )
                arguments = argparse.Namespace(
                    run_id="run-1",
                    base_url=f"http://127.0.0.1:{server.server_address[1]}",
                    model="gdn-inside",
                    concurrency=2,
                    input_tokens=50,
                    output_tokens=64,
                    request_multiplier=1,
                    timeout=5,
                    result_dir=directory,
                    route_map=str(route_map),
                )
                with mock.patch.object(benchmark.shutil, "which", return_value=None):
                    self.assertEqual(benchmark.execute(arguments), 0)
                status = json.loads(
                    (pathlib.Path(directory) / "status.json").read_text()
                )
                events = (pathlib.Path(directory) / "events.jsonl").read_text().splitlines()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["metrics"]["successful"], 2)
        self.assertEqual(status["metrics"]["input_tokens"], 104)
        self.assertEqual(status["metrics"]["output_tokens"], 8)
        self.assertIn("request_rps", status["metrics"])
        self.assertIn("p95", status["metrics"]["ttft_ms"])
        self.assertEqual(
            status["metrics"]["routing"]["targets"], {"LLMCtl worker 0": 2}
        )
        self.assertFalse(status["gpu"]["available"])
        self.assertEqual(len(events), 2)
        self.assertTrue(
            all(json.loads(event)["route_target"] == "LLMCtl worker 0" for event in events)
        )

    def test_gpu_sampler_reports_peak_concurrent_activity(self):
        parsed = benchmark.GpuSampler.parse_output(
            "0, 100, 77561, 224.0\n1, 100, 77711, 236.0\n2, 0, 77469, 77.0\n"
        )
        self.assertEqual(parsed[1]["utilization_percent"], 100)
        sampler = benchmark.GpuSampler()
        sampler.samples = {
            0: [parsed[0], {**parsed[0], "utilization_percent": 0}],
            1: [parsed[1], {**parsed[1], "utilization_percent": 100}],
            2: [parsed[2], {**parsed[2], "utilization_percent": 100}],
        }
        snapshot = sampler.snapshot()
        self.assertEqual(snapshot["peak_concurrent_active_gpu_count"], 2)
        self.assertEqual(snapshot["current_active_gpu_count"], 2)
        self.assertEqual(snapshot["sample_count"], 2)


if __name__ == "__main__":
    unittest.main()
