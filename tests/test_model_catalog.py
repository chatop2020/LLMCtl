#!/usr/bin/env python3
import importlib.util
import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("model_catalog", ROOT / "lib" / "model_catalog.py")
catalog = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = catalog
SPEC.loader.exec_module(catalog)


class CatalogPlanningTests(unittest.TestCase):
    def hardware(self, count=2, memory_mib=24576):
        return catalog.Hardware([catalog.GPU(i, "test", memory_mib, "9.0") for i in range(count)])

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
