#!/usr/bin/env python3
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
INSTALLER = (ROOT / "install-llm-cluster.sh").read_text(encoding="utf-8")
MANAGER = "\n".join(
    [
        (ROOT / "llmctl.sh").read_text(encoding="utf-8"),
        *(path.read_text(encoding="utf-8") for path in sorted((ROOT / "lib/llmctl").glob("*.sh"))),
    ]
)
OPTIMIZER = (ROOT / "lib" / "runtime_optimizer.py").read_text(encoding="utf-8")
GATEWAY = (ROOT / "lib" / "gateway_config.py").read_text(encoding="utf-8")
ACCOUNT = "\n".join(
    path.read_text(encoding="utf-8")
    for path in sorted((ROOT / "lib").glob("account_portal*.py"))
)
UPGRADER = (ROOT / "upgrade-llmctl.sh").read_text(encoding="utf-8")
MANIFEST = (ROOT / "upgrade-manifest.tsv").read_text(encoding="utf-8")


class StaticDeploymentContracts(unittest.TestCase):
    def test_shell_json_defaults_do_not_append_a_literal_closing_brace(self):
        """`${value:-{}}` 会在 Bash 中追加 `}`，所有受管脚本必须禁用该写法。"""

        self.assertNotIn(":-{}}", MANAGER)

    def test_llmctl_info_is_a_comprehensive_root_recovery_inventory(self):
        info = MANAGER.split("cmd_info() {", 1)[1].split("cmd_health() {", 1)[0]
        for marker in (
            "[主机与运行时 / Host and runtimes]",
            "[统一公开入口 / Public front door]",
            "[内部网络 / Internal networking]",
            "[接入层与管理员 / Gateway and administrators]",
            "[API 与内部密钥 / API and internal secrets]",
            "[数据库 / Databases]",
            "[注册、余额与 SMTP / Registration, balance and SMTP]",
            "[维护网络 / Maintenance networking]",
            "[模型与推理 / Model and inference]",
            "[服务、自启与 Worker / Services and workers]",
            "[文件、日志与维护 / Files, logs and maintenance]",
        ):
            self.assertIn(marker, info)
        for secret in (
            "GATEWAY_API_KEY",
            "BACKEND_API_KEY",
            "UI_PASSWORD",
            "ACCOUNT_ADMIN_PASSWORD",
            "POSTGRES_PASSWORD",
            "DATABASE_URL",
            "SMTP_PASSWORD",
            "OMNIROUTE_STORAGE_ENCRYPTION_KEY",
        ):
            self.assertIn(secret, info)
        self.assertIn("--redact", info)
        self.assertIn("dump-config --show-secrets", info)

    def test_nginx_install_isolated_config_has_validation_rollback_and_restore(self):
        nginx = MANAGER.split("render_nginx_config() {", 1)[1].split("database_health() {", 1)[0]
        self.assertIn("/etc/nginx/conf.d/llm-cluster.conf", MANAGER)
        self.assertIn("nginx -t", nginx)
        self.assertIn("install-mode", nginx)
        self.assertIn("previous.conf", nginx)
        self.assertIn("已恢复安装前同名 Nginx 配置", nginx)
        self.assertIn("现有 Nginx 软件包和其他站点均保留", nginx)

    def test_only_nginx_has_a_public_host_listener(self):
        gateway_start = MANAGER.split("cmd_gateway_start() {", 1)[1].split(
            "cmd_worker_start() {", 1
        )[0]
        worker_start = MANAGER.split("cmd_worker_start() {", 1)[1].split(
            "worker_port() {", 1
        )[0]
        database_unit = INSTALLER.split(
            "Description=PostgreSQL database for the LLMCtl API gateway", 1
        )[1].split("EOF", 1)[0]
        self.assertEqual(
            gateway_start.count('-p "127.0.0.1:${GATEWAY_INTERNAL_PORT}:${GATEWAY_INTERNAL_PORT}"'),
            4,
        )
        self.assertIn('-p "127.0.0.1:${WORKER_PORT}:${WORKER_PORT}"', worker_start)
        self.assertIn("-p 127.0.0.1:${GATEWAY_DB_PORT}:5432", database_unit)
        self.assertIn('[[ "${ACCOUNT_BIND}" == 127.0.0.1 ]]', INSTALLER)
        self.assertIn('bind != "127.0.0.1"', ACCOUNT)
        self.assertIn("listen ${listen_address}", MANAGER)

    def test_portal_health_distinguishes_local_liveness_from_gateway_readiness(self):
        health = MANAGER.split("account_portal_health() {", 1)[1].split(
            "account_local_base_url() {", 1
        )[0]
        self.assertIn('account_local_base_url)/health', health)
        self.assertIn('account_portal_ready() {', health)
        self.assertIn('account_local_base_url)/ready', health)
        self.assertIn('elif account_portal_health; then', MANAGER)
        self.assertIn('portal_state=degraded', MANAGER)
        account_unit = INSTALLER.split(
            "Description=LLMCtl account portal", 1
        )[1].split("EOF", 1)[0]
        self.assertIn("Wants=llm-router.service", account_unit)
        self.assertIn("PartOf=llm-cluster.service", account_unit)
        self.assertNotIn("Requires=llm-router.service", account_unit)
        self.assertNotIn("PartOf=llm-cluster.service llm-router.service", account_unit)

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
        self.assertIn("-e ALLOW_API_KEY_REVEAL=true", MANAGER)

    def test_router_can_reconcile_live_without_restarting_workers(self):
        router = MANAGER.split("cmd_router() {", 1)[1].split("cmd_database() {", 1)[0]
        self.assertIn("start|stop|restart|reconcile|status", MANAGER)
        self.assertIn("reconcile)", router)
        self.assertIn('reconcile_gateway "${healthy}"', router)
        self.assertIn("Router 和 Worker 均未重启", router)
        self.assertNotIn("refresh_router", router.split("reconcile)", 1)[1].split(";;", 1)[0])

    def test_mysql_command_only_activates_the_portal_driver(self):
        command = MANAGER.split("cmd_database_enable_mysql() {", 1)[1].split(
            "cmd_database() {", 1
        )[0]
        self.assertIn("PyMySQL==1.1.2", command)
        self.assertIn("cryptography==46.0.7", command)
        self.assertIn("mysql-capability.json", MANAGER)
        self.assertIn("systemctl restart llm-account.service", command)
        self.assertIn("Router 与 GPU Worker 保持运行", command)
        self.assertIn("在 WebUI 中填写连接、测试并迁移", command)
        self.assertIn("没有安装 MySQL Server、没有迁移数据", command)
        self.assertNotIn("mysql -", command)
        self.assertNotIn("mysqld", command)
        self.assertNotIn("CREATE DATABASE", command)

    def test_mysql_cli_helpers_and_recovery_inventory_use_the_activated_runtime(self):
        helper = MANAGER.split("account_helper() {", 1)[1].split(
            "persisted_published_origin() {", 1
        )[0]
        info = MANAGER.split("cmd_info() {", 1)[1].split("cmd_health() {", 1)[0]
        self.assertIn('ACCOUNT_MYSQL_VENV}/bin/python', helper)
        self.assertIn(".enabled == true", helper)
        self.assertIn("门户活动后端", info)
        self.assertIn("MySQL 能力", info)
        self.assertIn("ACCOUNT_DATABASE_MIGRATION", info)
        self.assertIn("迁移备份", info)

    def test_gateway_versions_are_pinned_and_only_selected_image_is_pulled(self):
        self.assertIn("calciumion/new-api:v1.0.0-rc.22", INSTALLER)
        self.assertIn("ghcr.io/berriai/litellm:v1.94.0", INSTALLER)
        self.assertIn("maximhq/bifrost:v1.6.7", INSTALLER)
        self.assertIn("diegosouzapw/omniroute:3.8.49", INSTALLER)
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
        self.assertIn("MAX_OUTPUT_TOKENS=32768", INSTALLER)
        self.assertIn("--override-generation-config", MANAGER)
        self.assertIn("vLLM 服务端硬上限", MANAGER)
        self.assertIn("CALL_LOG_PIPELINE_CAPTURE_STREAM_CHUNKS=false", MANAGER)
        self.assertIn("CALL_LOG_PIPELINE_MAX_SIZE_KB=4096", MANAGER)
        self.assertIn("CHAT_LOG_TEXT_LIMIT=1048576", MANAGER)
        self.assertIn("ensure_request_content_logging", ACCOUNT)

    def test_worker_keepwarm_is_direct_bounded_observable_and_upgrade_safe(self):
        keepwarm = MANAGER.split("keepwarm_one_worker() {", 1)[1].split(
            "gpu_memory_snapshot() {", 1
        )[0]
        startup = MANAGER.split("start_worker_ids_batched() {", 1)[1].split(
            "start_ids() {", 1
        )[0]
        self.assertIn('http://127.0.0.1:${port}/v1/chat/completions', keepwarm)
        self.assertIn('Authorization: Bearer ${BACKEND_API_KEY}', keepwarm)
        self.assertIn("stream:false", keepwarm)
        self.assertIn("max_tokens:1", keepwarm)
        self.assertNotIn("gateway_base_url", keepwarm)
        self.assertIn('keepwarm_run_ids "${ids}" startup', startup)
        self.assertIn("KEEPWARM_ENABLED=1", INSTALLER)
        self.assertIn('KEEPWARM_ENABLED="${KEEPWARM_ENABLED:-0}"', MANAGER)
        self.assertIn("/usr/local/lib/llm-cluster/systemd/llm-keepwarm.service", MANIFEST)
        self.assertIn("/usr/local/lib/llm-cluster/systemd/llm-keepwarm.timer", MANIFEST)
        self.assertIn("install_keepwarm_units", MANAGER)
        self.assertIn("configure_keepwarm_timer", UPGRADER)
        service = (ROOT / "systemd" / "llm-keepwarm.service").read_text(encoding="utf-8")
        timer = (ROOT / "systemd" / "llm-keepwarm.timer").read_text(encoding="utf-8")
        self.assertIn("ExecStart=/usr/local/sbin/llmctl _keepwarm-tick", service)
        self.assertIn("ProtectSystem=strict", service)
        self.assertIn("OnUnitActiveSec=60s", timer)

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
        self.assertIn("tmp_dir=$(smoke_fixture_directory)", smoke)
        fixture = MANAGER.split("smoke_fixture_directory() {", 1)[1].split(
            "make_ocr_fixture() {", 1
        )[0]
        self.assertIn('"${SMOKE_DIAGNOSTIC_DIR}/.fixture.XXXXXX"', fixture)
        self.assertNotIn("mktemp -d /tmp", fixture)
        make_fixture = MANAGER.split("make_ocr_fixture() {", 1)[1].split(
            "ocr_request_file() {", 1
        )[0]
        self.assertIn("--user 0:0", make_fixture)
        self.assertIn('[[ -s "${out_dir}/llm-ocr-test.png" ]]', make_fixture)
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

    def test_runtime_proxy_is_scoped_to_router_and_optional_workflow(self):
        gateway_start = MANAGER.split("cmd_gateway_start() {", 1)[1].split(
            "cmd_worker_start() {", 1
        )[0]
        worker_start = MANAGER.split("cmd_worker_start() {", 1)[1].split(
            "image_supports_architecture() {", 1
        )[0]
        runtime_proxy = MANAGER.split("load_runtime_proxy() {", 1)[1].split(
            "load_saved_proxy() {", 1
        )[0]
        workflow_unit = (ROOT / "systemd" / "llm-workflow.service").read_text(
            encoding="utf-8"
        )
        self.assertIn("runtime_proxy_docker_args runtime_proxy_args", gateway_start)
        self.assertEqual(gateway_start.count('"${runtime_proxy_args[@]}"'), 4)
        self.assertNotIn("RUNTIME_HTTP_PROXY", worker_start)
        self.assertNotIn("runtime-proxy.env", worker_start)
        self.assertIn("GPU Worker=未重启", runtime_proxy)
        self.assertNotIn("systemctl restart docker", runtime_proxy)
        self.assertIn(
            "EnvironmentFile=-/etc/llm-cluster/runtime-proxy.env", workflow_unit
        )

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

    def test_bilingual_docs_cover_worker_keepwarm_behavior_and_tradeoff(self):
        for chinese_name, english_name in (("README.md", "README_EN.md"), ("USAGE.md", "USAGE_EN.md")):
            chinese = (ROOT / chinese_name).read_text(encoding="utf-8")
            english = (ROOT / english_name).read_text(encoding="utf-8")
            for command in (
                "llmctl keepwarm enable 300",
                "llmctl keepwarm status",
                "llmctl keepwarm run all",
                "llmctl keepwarm disable",
            ):
                self.assertIn(command, chinese)
                self.assertIn(command, english)
            self.assertIn("不占用户额度", chinese)
            self.assertIn("standby power", english)

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
        self.assertIn("API Key 明文只返回一次", ACCOUNT)

    def test_omniroute_lifecycle_has_shared_backup_recovery_and_bilingual_docs(self):
        """CLI、root 控制服务、升级包和文档必须描述同一套安全运维入口。"""

        maintenance = (ROOT / "lib" / "omniroute_maintenance.py").read_text(
            encoding="utf-8"
        )
        manifest = (ROOT / "upgrade-manifest.tsv").read_text(encoding="utf-8")
        for marker in (
            "cmd_omniroute()",
            "omniroute sqlite assess",
            "MAINTAIN ONLINE",
            "COMPACT SQLITE",
            "UPDATE OMNIROUTE",
            "ROLLBACK ${backup_id}",
            "正在转交安全升级状态机",
        ):
            self.assertIn(marker, MANAGER)
        for marker in (
            "source.backup",
            "PRAGMA quick_check",
            "PRAGMA wal_checkpoint(PASSIVE)",
            "VACUUM",
            "_restore_database",
            "_restart_and_smoke",
            "MUTABLE_IMAGE_TAGS",
        ):
            self.assertIn(marker, maintenance)
        self.assertIn("lib/omniroute_maintenance.py", manifest)
        self.assertIn("diegosouzapw/omniroute:3.8.49", INSTALLER)
        for chinese_name, english_name in (
            ("README.md", "README_EN.md"),
            ("USAGE.md", "USAGE_EN.md"),
        ):
            chinese = (ROOT / chinese_name).read_text(encoding="utf-8")
            english = (ROOT / english_name).read_text(encoding="utf-8")
            for command in (
                "llmctl omniroute sqlite assess",
                "llmctl omniroute sqlite maintain online",
                "llmctl omniroute update",
                "llmctl omniroute rollback",
            ):
                self.assertIn(command, chinese)
                self.assertIn(command, english)


if __name__ == "__main__":
    unittest.main()
