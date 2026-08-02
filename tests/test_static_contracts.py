#!/usr/bin/env python3
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
INSTALLER = (ROOT / "install-llm-cluster.sh").read_text(encoding="utf-8")
MANAGER = (ROOT / "llmctl.sh").read_text(encoding="utf-8")
OPTIMIZER = (ROOT / "lib" / "runtime_optimizer.py").read_text(encoding="utf-8")
GATEWAY = (ROOT / "lib" / "gateway_config.py").read_text(encoding="utf-8")
ACCOUNT = (ROOT / "lib" / "account_portal.py").read_text(encoding="utf-8")


class StaticDeploymentContracts(unittest.TestCase):
    def test_systemd_delegates_worker_arguments_to_manager(self):
        self.assertIn("ExecStart=/usr/local/sbin/llmctl _worker-start %i", INSTALLER)
        unit = INSTALLER.split("Description=vLLM model worker instance %i", 1)[1].split("EOF", 1)[0]
        self.assertNotIn("--tool-call-parser qwen3_xml", unit)
        self.assertNotIn("--reasoning-parser qwen3", unit)

    def test_systemd_delegates_selected_gateway_to_manager(self):
        self.assertIn("ExecStart=/usr/local/sbin/llmctl _gateway-start", INSTALLER)
        self.assertIn('newapi) GATEWAY_IMAGE="${GATEWAY_IMAGE:-${NEWAPI_IMAGE}}"', MANAGER)
        self.assertIn('litellm) GATEWAY_IMAGE="${GATEWAY_IMAGE:-${LITELLM_IMAGE}}"', MANAGER)
        self.assertIn('bifrost) GATEWAY_IMAGE="${GATEWAY_IMAGE:-${BIFROST_IMAGE}}"', MANAGER)
        self.assertIn('omniroute) GATEWAY_IMAGE="${GATEWAY_IMAGE:-${OMNIROUTE_IMAGE}}"', MANAGER)
        self.assertIn("reconcile-newapi", MANAGER)
        self.assertIn("reconcile-omniroute", MANAGER)
        self.assertIn("wait_gateway_process", MANAGER)
        self.assertIn("GATEWAY_API_KEY", MANAGER)

    def test_gateway_versions_are_pinned_and_only_selected_image_is_pulled(self):
        self.assertIn("calciumion/new-api:v1.0.0-rc.22", INSTALLER)
        self.assertIn("ghcr.io/berriai/litellm:v1.94.0", INSTALLER)
        self.assertIn("maximhq/bifrost:v1.6.7", INSTALLER)
        self.assertIn("diegosouzapw/omniroute:3.8.48", INSTALLER)
        pull = INSTALLER.split("pull_images() {", 1)[1].split("\n}", 1)[0]
        self.assertIn('selected_gateway_image=$(gateway_image)', pull)
        self.assertIn('ensure_image "${selected_gateway_image}"', pull)
        self.assertNotIn('ensure_image "${NEWAPI_IMAGE}"', pull)
        self.assertIn('docker image inspect "${image}"', INSTALLER)
        self.assertIn('if ! docker pull "${image}"; then', INSTALLER)
        self.assertIn('"--${GATEWAY_KIND}-image"', pull)

    def test_gateway_generated_configs_reference_secrets_by_environment(self):
        self.assertIn("os.environ/BACKEND_API_KEY", GATEWAY)
        self.assertIn("env.BACKEND_API_KEY", GATEWAY)
        self.assertIn("env.GATEWAY_API_KEY", GATEWAY)
        self.assertNotIn("sk-backend-$(", GATEWAY)

    def test_runtime_is_offline_and_capability_gated(self):
        self.assertIn("HF_HUB_OFFLINE=1", MANAGER)
        self.assertIn("TRANSFORMERS_OFFLINE=1", MANAGER)
        self.assertIn("SUPPORTS_IMAGE_INPUT == 1", MANAGER)
        self.assertIn("SUPPORTS_TOOL_CALLING == 1", MANAGER)
        self.assertIn("SUPPORTS_REASONING == 1", MANAGER)

    def test_smoke_diagnostics_and_default_aggregate_logs_are_operational(self):
        smoke = MANAGER.split("smoke_endpoint() {", 1)[1].split("cmd_smoke() {", 1)[0]
        logs = MANAGER.split("cmd_logs() {", 1)[1].split("api_post() {", 1)[0]
        api_post = MANAGER.split("api_post() {", 1)[1].split("smoke_response_summary() {", 1)[0]
        self.assertIn("for reasoning_limit in 2048 4096", smoke)
        self.assertIn("temperature:0.6,top_p:0.95,top_k:20", smoke)
        self.assertGreaterEqual(smoke.count("stream:false"), 4)
        self.assertIn(
            "stream:false",
            MANAGER.split("ocr_request_file() {", 1)[1].split("smoke_endpoint() {", 1)[0],
        )
        self.assertIn(
            '"stream": False',
            MANAGER.split("cmd_bench() {", 1)[1].split("optimizer_metrics_urls() {", 1)[0],
        )
        self.assertIn("Accept: application/json", api_post)
        self.assertIn("detected_format", MANAGER)
        self.assertIn("finish_reason", smoke)
        self.assertIn("smoke_fail_response", smoke)
        self.assertIn('local target="${1:-all}"', logs)
        self.assertIn("llm-router.service", logs)
        self.assertIn("llm-database.service", logs)
        self.assertNotIn("请检查 llmctl logs'", INSTALLER)

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
        self.assertIn('"stream":false', chinese)
        self.assertIn('"stream":false', english)

    def test_bilingual_docs_cover_all_gateway_choices_and_clean_install_semantics(self):
        for chinese_name, english_name in (("README.md", "README_EN.md"), ("USAGE.md", "USAGE_EN.md")):
            chinese = (ROOT / chinese_name).read_text(encoding="utf-8")
            english = (ROOT / english_name).read_text(encoding="utf-8")
            for term in ("New API", "LiteLLM", "Bifrost", "--gateway", "GATEWAY_API_KEY"):
                self.assertIn(term, chinese)
                self.assertIn(term, english)
            self.assertIn("OmniRoute", chinese)
            self.assertIn("OmniRoute", english)
            self.assertIn("不做在线迁移", chinese)
            self.assertIn("no online migration", english.lower())

    def test_omniroute_account_portal_has_isolated_sqlite_and_company_registration_controls(self):
        self.assertIn("account-portal.db", INSTALLER)
        self.assertIn("storage.sqlite", MANAGER)
        self.assertIn("User=llm-account", INSTALLER)
        self.assertIn('install -d -m 751 -o root -g llm-account "${STATE_DIR}/omniroute"', INSTALLER)
        self.assertIn("UMask=0077", INSTALLER)
        self.assertIn("ACCOUNT_ALLOWED_EMAIL_DOMAINS", ACCOUNT)
        self.assertIn("registration_enabled", ACCOUNT)
        self.assertIn("verification_tokens", ACCOUNT)
        self.assertIn("SMTP_HOST", ACCOUNT)
        self.assertIn("/api/usage/token-limits", ACCOUNT)
        self.assertIn("/v1/models", ACCOUNT)
        self.assertIn("curl", ACCOUNT)
        self.assertIn("audit_events", ACCOUNT)
        self.assertIn("API key plaintext is returned once", ACCOUNT)


if __name__ == "__main__":
    unittest.main()
