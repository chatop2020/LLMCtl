import importlib.util
import json
import pathlib
import tempfile
import unittest


MODULE_PATH = pathlib.Path(__file__).parents[1] / "lib" / "workflow_config.py"
SPEC = importlib.util.spec_from_file_location("workflow_config", MODULE_PATH)
workflow_config = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(workflow_config)


class WorkflowConfigTests(unittest.TestCase):
    def test_remote_targets_are_first_class_and_do_not_require_docker(self):
        parser = workflow_config.build_parser()
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "workflow.json"
            args = parser.parse_args(
                [
                    "--config",
                    str(path),
                    "init",
                    "--listen",
                    "127.0.0.1:18100",
                    "--base-model",
                    "ornith-internal",
                    "--target",
                    "https://worker-a.example/v1",
                    "--target",
                    "https://worker-b.example/v1",
                ]
            )
            args.handler(args)
            config = json.loads(path.read_text())
            targets = config["pools"]["text-generation"]["targets"]
            self.assertEqual(2, len(targets))
            self.assertEqual("https://worker-a.example/v1", targets[0]["base_url"])
            self.assertFalse(config["models"]["llmctl-workflow-gdn-inside"]["enabled"])
            self.assertEqual(
                "http://127.0.0.1:18100/v1", config["gateway_base_url"]
            )

    def test_gateway_base_url_can_target_a_remote_workflow_host(self):
        parser = workflow_config.build_parser()
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "workflow.json"
            args = parser.parse_args(
                [
                    "--config",
                    str(path),
                    "init",
                    "--listen",
                    "127.0.0.1:18100",
                    "--gateway-base-url",
                    "https://workflow.internal.example/v1/",
                    "--base-model",
                    "ornith-internal",
                    "--target",
                    "https://worker-a.example/v1",
                ]
            )
            args.handler(args)
            config = json.loads(path.read_text())
            self.assertEqual(
                "https://workflow.internal.example/v1",
                config["gateway_base_url"],
            )

    def test_target_update_is_atomic_and_preserves_other_configuration(self):
        parser = workflow_config.build_parser()
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "workflow.json"
            init = parser.parse_args(
                [
                    "--config",
                    str(path),
                    "init",
                    "--listen",
                    "127.0.0.1:18100",
                    "--base-model",
                    "model-a",
                    "--target",
                    "http://10.0.0.10:8000/v1",
                ]
            )
            init.handler(init)
            update = parser.parse_args(
                [
                    "--config",
                    str(path),
                    "target-add",
                    "--pool",
                    "text-generation",
                    "--id",
                    "gpu-remote-4",
                    "--base-url",
                    "http://10.0.0.20:8000/v1",
                    "--api-key-env",
                    "REMOTE_KEY",
                ]
            )
            update.handler(update)
            config = json.loads(path.read_text())
            self.assertEqual("model-a", config["models"]["llmctl-workflow-gdn-inside"]["base_model"])
            self.assertEqual(2, len(config["pools"]["text-generation"]["targets"]))

    def test_rejects_credential_in_endpoint_url(self):
        with self.assertRaises(ValueError):
            workflow_config.normalized_base_url("https://user:secret@example.test/v1")

    def test_rejects_empty_embedded_credentials_and_invalid_listen(self):
        with self.assertRaises(ValueError):
            workflow_config.normalized_base_url("https://@example.test/v1")
        with self.assertRaises(ValueError):
            workflow_config.normalized_listen("127.0.0.1:not-a-port")
        with self.assertRaises(ValueError):
            workflow_config.normalized_listen("127.0.0.1:0")

    def test_rejects_query_fragment_and_invalid_environment_names(self):
        with self.assertRaises(ValueError):
            workflow_config.normalized_base_url(
                "https://example.test/v1?token=secret"
            )
        with self.assertRaises(ValueError):
            workflow_config.normalized_base_url("https://example.test/v1#fragment")
        with self.assertRaises(ValueError):
            workflow_config.normalized_env_name("lowercase-secret")
        self.assertEqual(
            "IMAGE_API_KEY", workflow_config.normalized_env_name("IMAGE_API_KEY")
        )


if __name__ == "__main__":
    unittest.main()
