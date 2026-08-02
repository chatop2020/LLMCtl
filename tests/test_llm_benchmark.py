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
            "data: [DONE]\n\n"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        benchmark._STOP.clear()

    def test_prompt_is_meaningful_and_close_to_target(self):
        prompt = benchmark.meaningful_prompt(300, "stable-seed")
        self.assertIn("Case 1:", prompt)
        self.assertIn("latency throughput cost reliability", prompt)
        self.assertGreaterEqual(len(prompt.split()), 280)
        self.assertLessEqual(len(prompt.split()), 330)

    def test_backend_runner_streams_and_writes_professional_metrics(self):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), StreamingHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
                benchmark.os.environ,
                {"LLMCTL_BENCHMARK_API_KEY": "sk-test"},
            ):
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
                )
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
        self.assertEqual(len(events), 2)


if __name__ == "__main__":
    unittest.main()
