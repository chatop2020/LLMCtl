#!/usr/bin/env python3
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
INSTALLER = (ROOT / "install-llm-cluster.sh").read_text(encoding="utf-8")
MANAGER = (ROOT / "llmctl.sh").read_text(encoding="utf-8")
OPTIMIZER = (ROOT / "lib" / "runtime_optimizer.py").read_text(encoding="utf-8")


class StaticDeploymentContracts(unittest.TestCase):
    def test_systemd_delegates_worker_arguments_to_manager(self):
        self.assertIn("ExecStart=/usr/local/sbin/llmctl _worker-start %i", INSTALLER)
        unit = INSTALLER.split("Description=vLLM model worker instance %i", 1)[1].split("EOF", 1)[0]
        self.assertNotIn("--tool-call-parser qwen3_xml", unit)
        self.assertNotIn("--reasoning-parser qwen3", unit)

    def test_runtime_is_offline_and_capability_gated(self):
        self.assertIn("HF_HUB_OFFLINE=1", MANAGER)
        self.assertIn("TRANSFORMERS_OFFLINE=1", MANAGER)
        self.assertIn("SUPPORTS_IMAGE_INPUT == 1", MANAGER)
        self.assertIn("SUPPORTS_TOOL_CALLING == 1", MANAGER)
        self.assertIn("SUPPORTS_REASONING == 1", MANAGER)

    def test_catalog_metadata_is_persisted(self):
        for key in (
            "MODEL_HUB",
            "MODEL_ARCHITECTURE",
            "MODEL_WEIGHT_BYTES",
            "TOOL_CALL_PARSER",
            "REASONING_PARSER",
            "SUPPORTS_IMAGE_INPUT",
            "TRUST_REMOTE_CODE",
        ):
            self.assertIn(f"{key}=${{{key}}}", INSTALLER)
        self.assertIn("INTERFACE_LANGUAGE=${INTERFACE_LANGUAGE}", INSTALLER)
        self.assertIn('local -a command=(--lang "${language}" "$@")', MANAGER)

    def test_proxy_is_not_in_worker_unit(self):
        unit = INSTALLER.split("Description=vLLM model worker instance %i", 1)[1].split("EOF", 1)[0]
        self.assertNotIn("HTTP_PROXY", unit)
        self.assertNotIn("HTTPS_PROXY", unit)

    def test_runtime_optimizer_is_installed_and_modelscope_uses_real_cli(self):
        self.assertIn(
            'install -m 755 "${OPTIMIZER_SOURCE}" /usr/local/lib/llm-cluster/runtime_optimizer.py',
            INSTALLER,
        )
        self.assertNotIn("ms-hub", INSTALLER)
        self.assertNotIn("ms-hub", MANAGER)
        self.assertIn("/opt/llm-cluster/hub-venv/bin/ms download", MANAGER)

    def test_optimization_has_consent_backup_acceptance_and_rollback_contract(self):
        flow = MANAGER.split("optimizer_analyze_or_run() {", 1)[1].split(
            "cmd_optimize() {", 1
        )[0]
        self.assertLess(flow.index("optimizer_print_advice"), flow.index("read -r -p"))
        self.assertLess(flow.index("read -r -p"), flow.index('cp -p "${CLUSTER_ENV}" "${backup}"'))
        self.assertIn("optimizer_recover_baseline", flow)
        self.assertIn("cmd_smoke --full", flow)
        self.assertIn("minimum_improvement", OPTIMIZER)

    def test_bilingual_docs_cover_optimizer_and_modelscope_entrypoint(self):
        chinese = (ROOT / "USAGE.md").read_text(encoding="utf-8")
        english = (ROOT / "USAGE_EN.md").read_text(encoding="utf-8")
        for command in (
            "llmctl optimize analyze",
            "llmctl optimize run",
            "llmctl optimize report",
            "llmctl optimize restore",
            "/opt/llm-cluster/hub-venv/bin/ms",
        ):
            self.assertIn(command, chinese)
            self.assertIn(command, english)


if __name__ == "__main__":
    unittest.main()
