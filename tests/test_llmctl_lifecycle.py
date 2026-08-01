#!/usr/bin/env python3
import pathlib
import subprocess
import textwrap
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
MANAGER = ROOT / "llmctl.sh"


def run_bash(body: str) -> str:
    script = textwrap.dedent(
        f"""
        set -Eeuo pipefail
        export LLMCTL_SOURCE_ONLY=1
        source {MANAGER!s}
        {body}
        """
    )
    completed = subprocess.run(
        ["bash", "-c", script],
        check=True,
        text=True,
        capture_output=True,
    )
    return completed.stdout


class LlmctlLifecycleTests(unittest.TestCase):
    def test_progress_snapshot_distinguishes_healthy_loading_and_pending(self):
        output = run_bash(
            r"""
            INSTANCE_COUNT=3
            WORKER_BASE_PORT=8100
            BACKEND_API_KEY=test
            worker_health_fast() { [[ "$1" == 0 ]]; }
            systemctl() {
              if [[ "$1" == show ]]; then
                case "$2" in
                  llm-worker@1.service) printf 'active\n' ;;
                  llm-worker@2.service) printf 'inactive\n' ;;
                  *) printf 'unknown\n' ;;
                esac
              fi
            }
            collect_worker_progress '0,1,2' 1
            printf '%s|%s|%s|%s|%s\n' \
              "$PROGRESS_HEALTHY" "$PROGRESS_LOADING" "$PROGRESS_PENDING" \
              "$PROGRESS_FAILED" "$PROGRESS_STATES"
            """
        )
        self.assertEqual(output.strip(), "1|1|1|0|0:healthy,1:loading,2:pending")

    def test_worker_batch_is_submitted_non_blocking_then_observed_as_a_group(self):
        output = run_bash(
            r"""
            systemctl() { printf 'systemctl'; printf ' <%s>' "$@"; printf '\n'; }
            wait_worker_batch() { printf 'wait <%s>\n' "$1"; }
            start_worker_batch '0,1'
            """
        )
        self.assertIn(
            "systemctl <start> <--no-block> <llm-worker@0.service> <llm-worker@1.service>",
            output,
        )
        self.assertIn("wait <0,1>", output)

    def test_worker_stop_is_submitted_non_blocking_then_observed_as_a_group(self):
        output = run_bash(
            r"""
            systemctl() { printf 'systemctl'; printf ' <%s>' "$@"; printf '\n'; }
            wait_worker_units_stopped() { printf 'wait-stop <%s> <%s>\n' "$1" "$2"; }
            refresh_router() { printf 'router-refreshed\n'; }
            stop_ids '0,1'
            """
        )
        self.assertIn(
            "systemctl <stop> <--no-block> <llm-worker@0.service> <llm-worker@1.service>",
            output,
        )
        self.assertIn("wait-stop <0,1> <150>", output)
        self.assertIn("router-refreshed", output)

    def test_cluster_exec_stop_does_not_recursively_call_systemctl(self):
        output = run_bash(
            r"""
            require_root() { :; }
            load_config() { INSTANCE_COUNT=8; }
            systemctl() { printf 'unexpected systemctl call\n'; return 1; }
            cmd_boot_stop
            """
        )
        self.assertNotIn("unexpected systemctl call", output)
        self.assertIn("8 个 Worker", output)

    def test_uninstall_uses_bounded_visible_stop_before_deleting_units(self):
        manager = MANAGER.read_text(encoding="utf-8")
        uninstall = manager.split("cmd_uninstall() {", 1)[1].split(
            "managed_container_names() {", 1
        )[0]
        self.assertIn("stop_managed_services_with_progress 180", uninstall)
        self.assertNotIn("disable --now", uninstall)
        self.assertLess(
            uninstall.index("stop_managed_services_with_progress 180"),
            uninstall.index("rm -f /etc/systemd/system/llm-cluster.service"),
        )
        self.assertLess(
            uninstall.index('remove_tree_with_progress "${CACHE_DIR}"'),
            uninstall.index('rm -rf "${CONFIG_DIR}"'),
        )
        self.assertIn("配置保留到最后一步", uninstall)

    def test_installer_attaches_visible_watcher_to_background_start(self):
        installer = (ROOT / "install-llm-cluster.sh").read_text(encoding="utf-8")
        start = installer.split("start_cluster_with_progress() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("systemctl start --no-block llm-cluster.service", start)
        self.assertIn("llmctl startup watch", start)


if __name__ == "__main__":
    unittest.main()
