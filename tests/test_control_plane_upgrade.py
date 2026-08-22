import subprocess
import tempfile
import textwrap
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UPGRADER = ROOT / "upgrade-llmctl.sh"
MANIFEST = ROOT / "upgrade-manifest.tsv"
MANAGER = "\n".join(
    [
        (ROOT / "llmctl.sh").read_text(encoding="utf-8"),
        *(path.read_text(encoding="utf-8") for path in sorted((ROOT / "lib/llmctl").glob("*.sh"))),
    ]
)
INSTALLER = (ROOT / "install-llm-cluster.sh").read_text(encoding="utf-8")
ACCOUNT_PORTAL = "\n".join(
    path.read_text(encoding="utf-8")
    for path in sorted((ROOT / "lib").glob("account_portal*.py"))
)


class ControlPlaneUpgradeTests(unittest.TestCase):
    def build_archive(self, archive: Path) -> None:
        files = [
            "llmctl.sh",
            "upgrade-llmctl.sh",
            "upgrade-manifest.tsv",
            "lib/model_catalog.py",
            "lib/runtime_optimizer.py",
            "lib/gateway_config.py",
            "lib/model_deployment.py",
            "lib/model_upgrade.py",
            "lib/omniroute_maintenance.py",
            "lib/account_portal.py",
            "lib/llm_benchmark.py",
            "lib/workflow_config.py",
            "lib/workflowd/llm-workflowd",
            "lib/workflowd/llm-workflowd-linux-amd64",
            "lib/workflowd/llm-workflowd-linux-arm64",
            "lib/workflowd/checksums.env",
            "systemd/llm-keepwarm.service",
            "systemd/llm-keepwarm.timer",
            "systemd/llm-workflow.service",
            "systemd/llm-model-control.service",
        ]
        files.extend(
            str(path.relative_to(ROOT))
            for path in sorted((ROOT / "lib").glob("account_portal_*.py"))
        )
        files.extend(
            str(path.relative_to(ROOT))
            for path in sorted((ROOT / "lib/llmctl").rglob("*"))
            if path.is_file()
        )
        files.extend(
            str(path.relative_to(ROOT))
            for path in sorted((ROOT / "lib/account_portal_ui").rglob("*"))
            if path.is_file()
        )
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as handle:
            for relative in files:
                source = ROOT / relative
                info = zipfile.ZipInfo.from_file(source, f"LLMCtl-main/{relative}")
                info.compress_type = zipfile.ZIP_DEFLATED
                handle.writestr(info, source.read_bytes())

    def test_manifest_is_limited_to_control_plane_paths(self):
        entries = []
        for raw_line in MANIFEST.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            entry_type, source, destination, mode, restart = line.split()
            entries.append((entry_type, source, destination, mode, restart))

        self.assertGreaterEqual(len(entries), 7)
        self.assertIn(
            ("file", "llmctl.sh", "/usr/local/sbin/llmctl", "0755", "none"),
            entries,
        )
        for entry_type, source, destination, mode, restart in entries:
            self.assertIn(entry_type, {"file", "dir"})
            self.assertTrue(
                destination == "/usr/local/sbin/llmctl"
                or destination.startswith("/usr/local/lib/llm-cluster/")
            )
            self.assertNotIn("llm-worker@", destination.lower())
            self.assertNotIn("/etc/llm-cluster/workflow", destination)
            self.assertNotIn("/var/lib/llm-cluster/workflow", destination)
            self.assertRegex(mode, r"^0[0-7]{3}$")
            self.assertIn(restart, {"none", "account"})

    def test_manifest_remains_accepted_by_the_previous_upgrader_allowlist(self):
        """The updater already installed on a host validates the new manifest first."""
        for raw_line in MANIFEST.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            _, _, destination, _, restart = line.split()
            self.assertTrue(
                destination == "/usr/local/sbin/llmctl"
                or destination.startswith("/usr/local/lib/llm-cluster/"),
                f"legacy updater rejects destination {destination}",
            )
            self.assertIn(restart, {"none", "account"})

    def test_llmctl_delegates_upgrade_and_installer_installs_helper(self):
        self.assertIn("cmd_upgrade() {", MANAGER)
        self.assertIn('exec "${CONTROL_PLANE_UPDATER}" --lang', MANAGER)
        self.assertIn('upgrade) cmd_upgrade "$@"', MANAGER)
        self.assertIn('rollback) cmd_rollback "$@"', MANAGER)
        self.assertIn('--rollback "$1"', MANAGER)
        self.assertIn("llmctl upgrade --from-zip FILE", MANAGER)
        self.assertIn(
            'install -m 755 "${UPGRADER_SOURCE}" /usr/local/lib/llm-cluster/upgrade-llmctl.sh',
            INSTALLER,
        )
        self.assertIn(
            'install -m 644 "${KEEPWARM_SERVICE_SOURCE}" /usr/local/lib/llm-cluster/systemd/llm-keepwarm.service',
            INSTALLER,
        )
        self.assertIn("cmd_responses() {", MANAGER)
        self.assertIn('responses) cmd_responses "$@"', MANAGER)
        self.assertIn("account_helper reconcile-public-routes", MANAGER)
        self.assertIn("短暂停止账户门户", MANAGER)
        self.assertIn("workflow_require_runtime()", MANAGER)
        self.assertIn("llmctl upgrade --force", MANAGER)

    def test_legacy_multimodel_upgrade_has_an_actionable_recovery_path(self):
        """旧升级器只能复制新文件，状态命令必须明确给出无中断初始化路径。"""
        model_command = MANAGER.split("cmd_model() {", 1)[1].split(
            "worker_config_value() {", 1
        )[0]
        status = model_command.split("    status)\n", 1)[1].split("    plan|deploy)", 1)[0]
        self.assertIn("enabled_state=not-installed", status)
        self.assertIn("llmctl model init", status)
        self.assertIn("不需要安装额外软件", status)
        self.assertIn("不重启 Router 或 Worker", status)

        self.assertIn('"setup_command": "llmctl model init"', ACCOUNT_PORTAL)
        self.assertIn("模型部署控制服务尚未注册；请运行 llmctl model init", ACCOUNT_PORTAL)

    def test_upgrader_preserves_runtime_and_only_refreshes_managed_nginx(self):
        source = UPGRADER.read_text(encoding="utf-8")
        normal_upgrade = source.split("install_control_plane() {", 1)[1].split(
            "rollback_from_backup() {", 1
        )[0]
        self.assertIn('systemctl stop "${ACCOUNT_SERVICE}"', source)
        self.assertNotIn('systemctl stop "${ROUTER_SERVICE}"', normal_upgrade)
        self.assertNotIn('systemctl restart "${ROUTER_SERVICE}"', normal_upgrade)
        self.assertNotIn("systemctl stop llm-worker", source)
        self.assertNotIn("systemctl restart llm-worker", source)
        self.assertNotIn("systemctl restart docker", source)
        self.assertNotIn("nginx -s", source)
        self.assertIn("refresh_managed_nginx()", source)
        self.assertIn("'^# Generated by LLMCtl '", source)
        self.assertIn("/usr/local/sbin/llmctl nginx apply", source)
        self.assertIn("域名、80/443、证书和 TLS 仍由外部出口管理", source)
        self.assertIn("restore_control_plane()", source)
        self.assertIn("restore_keepwarm_systemd_units", source)
        self.assertIn("configure_keepwarm_timer", source)
        self.assertIn("load_saved_proxy()", source)
        self.assertIn("prompt_new_proxy()", source)
        self.assertIn("refresh_workflow_unit_if_installed()", source)
        self.assertIn('[[ -e "${WORKFLOW_SERVICE_UNIT}" ]] || return 0', source)
        self.assertNotIn("systemctl enable --now llm-workflow", source)
        self.assertIn("validate_installed_workflow_runtime()", source)
        self.assertIn('"${WORKFLOW_RUNTIME}" --version >/dev/null', source)
        self.assertIn("validate_installed_workflow_runtime", normal_upgrade)

    def test_upgrader_restarts_model_control_and_requires_upgrade_capability(self):
        """覆盖运行文件前必须停旧进程，启动验收必须证明新升级 API 已加载。"""

        source = UPGRADER.read_text(encoding="utf-8")
        install = source.split("install_control_plane() {", 1)[1].split(
            "rollback_from_backup() {", 1
        )[0]
        stop = 'systemctl stop "${MODEL_CONTROL_SERVICE}"'
        manifest_loop = 'while read -r entry_type source destination mode restart'
        self.assertIn(stop, install)
        self.assertLess(install.index(stop), install.index(manifest_loop))
        wait = source.split("wait_for_model_control() {", 1)[1].split(
            "configure_model_control_service() {", 1
        )[0]
        self.assertIn("upgrade_profiles", wait)

    def test_upgrade_backup_and_explicit_rollback_cover_runtime_sqlite(self):
        source = UPGRADER.read_text(encoding="utf-8")
        self.assertIn("runtime-data/runtime-data.json", source)
        self.assertIn("restore_runtime_data()", source)
        self.assertIn("rollback_from_backup()", source)
        self.assertIn('systemctl stop "${ROUTER_SERVICE}"', source)
        self.assertIn("pre-rollback-", source)
        self.assertIn("GPU Worker、模型权重和 Worker 配置均未修改", source)
        self.assertGreaterEqual(source.count("systemctl stop llm-workflow.service"), 2)

    def test_local_zip_check_validates_without_deploying(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "LLMCtl-main.zip"
            self.build_archive(archive)
            result = subprocess.run(
                [
                    "bash",
                    str(UPGRADER),
                    "--from-zip",
                    str(archive),
                    "--check",
                    "--non-interactive",
                    "--lang",
                    "zh",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=20,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("升级包验证通过", result.stdout)
        self.assertIn("没有修改现有控制面或服务", result.stdout)

    def test_zip_path_traversal_is_rejected_before_deployment(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "unsafe.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("../escape", "unsafe")
                handle.writestr("upgrade-manifest.tsv", "placeholder")
            result = subprocess.run(
                [
                    "bash",
                    str(UPGRADER),
                    "--from-zip",
                    str(archive),
                    "--check",
                    "--non-interactive",
                    "--lang",
                    "en",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=20,
                check=False,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsafe ZIP path", result.stderr)

    def test_real_archive_timeout_prompts_for_proxy_and_retries(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            archive = temporary_path / "LLMCtl-main.zip"
            self.build_archive(archive)
            fake_bin = temporary_path / "bin"
            fake_bin.mkdir()
            fake_curl = fake_bin / "curl"
            fake_curl.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env bash
                    set -eu
                    output=""
                    url=""
                    while (( $# )); do
                      case "$1" in
                        -o) output="$2"; shift 2 ;;
                        http://*|https://*) url="$1"; shift ;;
                        *) shift ;;
                      esac
                    done
                    if [[ "${url}" == *api.github.com* ]]; then
                      if [[ -n "${output}" && "${output}" != "/dev/null" ]]; then
                        printf '{"sha":"1111111111111111111111111111111111111111"}\n' >"${output}"
                      fi
                      exit 0
                    fi
                    if [[ "${url}" == */archive/*.zip ]]; then
                      if [[ -z "${HTTPS_PROXY:-${https_proxy:-}}" ]]; then exit 28; fi
                      cp "${FAKE_ARCHIVE_SOURCE}" "${output}"
                      exit 0
                    fi
                    exit 0
                    """
                ),
                encoding="utf-8",
            )
            fake_curl.chmod(0o755)
            environment = {
                **dict(__import__("os").environ),
                "PATH": f"{fake_bin}:{__import__('os').environ['PATH']}",
                "FAKE_ARCHIVE_SOURCE": str(archive),
            }
            result = subprocess.run(
                [
                    "bash",
                    str(UPGRADER),
                    "--check",
                    "--yes",
                    "--lang",
                    "zh",
                ],
                cwd=ROOT,
                env=environment,
                input="192.168.9.104\n1082\nhttp\n",
                text=True,
                capture_output=True,
                timeout=20,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        combined = result.stdout + result.stderr
        self.assertIn("GitHub 升级包下载直连失败（curl=28）", combined)
        self.assertIn("代理后的 GitHub 网络测试通过", combined)
        self.assertIn("正在通过代理重试GitHub 升级包下载", combined)
        self.assertIn("预检完成：没有修改现有控制面或服务", combined)


if __name__ == "__main__":
    unittest.main()
