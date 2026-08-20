#!/usr/bin/env python3
import importlib.util
import contextlib
import io
import pathlib
import sys
import types
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("model_catalog", ROOT / "lib" / "model_catalog.py")
catalog = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = catalog
SPEC.loader.exec_module(catalog)


class CatalogPlanningTests(unittest.TestCase):
    def hardware(self, count=2, memory_mib=24576, **kwargs):
        return catalog.Hardware(
            [catalog.GPU(i, "test", memory_mib, "9.0") for i in range(count)],
            **kwargs,
        )

    def model(self, arch="Qwen3ForCausalLM", weight=8 * catalog.GIB):
        return {
            "source": "huggingface",
            "id": "example/Test-Instruct",
            "revision": "a" * 40,
            "task": "text-generation",
            "downloads": 100,
            "likes": 10,
            "license": "apache-2.0",
            "gated": False,
            "private": False,
            "tags": ["safetensors"],
            "params": 8_000_000_000,
            "weight_bytes": weight,
            "config": {
                "architectures": [arch],
                "max_position_embeddings": 32768,
                "num_hidden_layers": 32,
                "num_attention_heads": 32,
                "num_key_value_heads": 8,
                "hidden_size": 4096,
            },
            "tokenizer": {},
        }

    def test_supported_model_gets_hardware_plan(self):
        result = catalog.evaluate(self.model(), self.hardware(), 0.92, 0)
        self.assertTrue(result["installable"])
        self.assertEqual(result["plan"]["tp"], 1)
        self.assertEqual(result["plan"]["replicas"], 2)

    def test_unknown_architecture_is_rejected(self):
        result = catalog.evaluate(self.model("UnknownForCausalLM"), self.hardware(), 0.92, 0)
        self.assertFalse(result["installable"])
        self.assertIn("不在 vLLM", " ".join(result["rejection_reasons"]))

    def test_mlx_converted_weights_are_rejected_for_vllm_cuda(self):
        model = self.model()
        model["id"] = "mlx-community/Ornith-1.0-35B-8bit"
        model["tags"] = ["safetensors", "mlx"]
        result = catalog.evaluate(model, self.hardware(), 0.92, 0)
        self.assertFalse(result["installable"])
        self.assertIn("MLX", " ".join(result["rejection_reasons"]))

    def test_catalog_reports_why_mlx_candidates_were_excluded(self):
        mlx = self.model()
        mlx["id"] = "mlx-community/Ornith-1.0-35B-8bit"
        mlx["tags"] = ["mlx"]
        native = self.model()
        args = types.SimpleNamespace(
            source="huggingface",
            query="ornith",
            task="auto",
            limit=10,
            show_rejected=False,
            gpu_memory_utilization=0.92,
            max_model_len=0,
        )
        error = io.StringIO()
        with mock.patch.object(catalog, "hf_search", return_value=[mlx, native]):
            with contextlib.redirect_stderr(error):
                results = catalog.search_models(args, self.hardware())
        self.assertEqual([item["id"] for item in results], ["example/Test-Instruct"])
        self.assertIn("MLX", error.getvalue())

    def test_modelscope_branch_is_pinned_to_latest_repository_commit(self):
        """ModelScope 的 master 也必须在升级计划中转换为不可变 SHA。"""

        older = "a" * 40
        latest = "b" * 40
        response = {
            "Data": {
                "Files": [
                    {"CommittedDate": 10, "Revision": older},
                    {"CommittedDate": 20, "Revision": latest},
                ]
            }
        }
        with mock.patch.object(catalog, "request_json", return_value=response) as request:
            revision = catalog.ms_resolve_revision(
                "ornith-ai/Ornith-1.5-35B-A3B-FP8", "master", None
            )
        self.assertEqual(revision, latest)
        self.assertIn("Revision=master", request.call_args.args[0])

    def test_host_resources_drive_startup_parallelism(self):
        hardware = self.hardware(
            8,
            85651,
            cpu_threads=64,
            memory_total_bytes=768 * catalog.GIB,
            memory_available_bytes=700 * catalog.GIB,
            disk_path="/data",
            disk_free_bytes=2 * 1024 * catalog.GIB,
        )
        result = catalog.evaluate(self.model(weight=38 * catalog.GIB), hardware, 0.92, 0)
        self.assertTrue(result["installable"])
        self.assertEqual(result["plan"]["startup_parallelism"], 8)

    def test_pcie_only_tensor_parallel_plan_has_visible_warning(self):
        topology = "GPU0 X PHB\nGPU1 PHB X"
        hardware = self.hardware(
            2,
            85651,
            cpu_threads=32,
            memory_total_bytes=512 * catalog.GIB,
            memory_available_bytes=480 * catalog.GIB,
            topology_matrix=topology,
            topology_worst_path="PHB",
            pcie_max_width_min=16,
        )
        result = catalog.evaluate(self.model(weight=120 * catalog.GIB), hardware, 0.92, 0)
        self.assertTrue(result["installable"])
        self.assertEqual(result["plan"]["tp"], 2)
        self.assertEqual(result["plan"]["tp_topology_worst_path"], "PHB")
        self.assertTrue(any("NVLink" in item for item in result["plan"]["warnings"]))

    def test_selected_plan_explains_recommendation_and_limits_in_both_languages(self):
        result = catalog.evaluate(
            self.model(),
            self.hardware(
                memory_total_bytes=128 * catalog.GIB,
                memory_available_bytes=96 * catalog.GIB,
                cpu_threads=32,
                disk_path="/data",
                disk_free_bytes=500 * catalog.GIB,
            ),
            0.92,
            0,
        )
        original = catalog.LANGUAGE
        try:
            for language, expected in (("zh", "推荐原因"), ("en", "Why this is recommended")):
                catalog.LANGUAGE = language
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    catalog.render_selection_summary(result)
                self.assertIn(expected, output.getvalue())
                self.assertIn("max-num-seqs", output.getvalue())
                self.assertIn("262K", output.getvalue())
        finally:
            catalog.LANGUAGE = original

    def test_optional_pcie_query_failure_falls_back_without_losing_gpus(self):
        def fake_command(command, timeout=10):
            joined = " ".join(command)
            if "pcie.link" in joined:
                return ""
            if "--query-gpu=index,name,memory.total,compute_cap,driver_version" in joined:
                return "0, RTX PRO 6000D, 85651, 12.0, 580.65"
            if "topo -m" in joined:
                return "GPU0 X"
            if "lscpu" in joined:
                return "0,0,0"
            return ""

        with mock.patch.object(catalog, "run_host_command", side_effect=fake_command):
            hardware = catalog.detect_hardware(model_root="/")
        self.assertEqual(hardware.count, 1)
        self.assertEqual(hardware.driver_version, "580.65")
        self.assertEqual(hardware.pcie_current_width_min, 0)

    def test_ornith_capabilities_are_conservative_and_explicit(self):
        model = self.model("Qwen3_5MoeForConditionalGeneration", 38 * catalog.GIB)
        model["id"] = "protoLabsAI/Ornith-1.0-35B-FP8"
        model["task"] = "image-text-to-text"
        result = catalog.evaluate(model, self.hardware(8, 85651), 0.92, 262144)
        self.assertTrue(result["capabilities"]["image_input"])
        self.assertTrue(result["capabilities"]["ocr_optimized"])
        self.assertEqual(result["capabilities"]["tool_parser"], "qwen3_xml")
        self.assertEqual(result["capabilities"]["reasoning_parser"], "qwen3")

    def test_shell_output_does_not_emit_unquoted_commands(self):
        result = catalog.evaluate(self.model(), self.hardware(), 0.92, 0)
        assignments = catalog.shell_assignments(result)
        self.assertIn("MODEL_ID=example/Test-Instruct", assignments)
        self.assertNotIn("$(``,", assignments)


if __name__ == "__main__":
    unittest.main()
