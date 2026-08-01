#!/usr/bin/env python3
import json
import pathlib
import subprocess
import tempfile
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
    def test_optimizer_workload_is_bounded_by_active_scheduling_slots(self):
        output = run_bash(
            r"""
            ACTIVE_WORKERS=0,1
            MAX_NUM_SEQS=3
            optimizer_choose_workload throughput 0
            printf '%s|%s|%s|%s\n' "$OPT_BENCH_CONCURRENCY" "$OPT_BENCH_REQUESTS" \
              "$OPT_BENCH_MAX_TOKENS" "$OPT_BENCH_PROMPT_TOKENS"
            """
        )
        self.assertEqual(output.strip(), "6|12|256|512")

    def test_optimizer_candidate_config_is_derived_from_immutable_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            config_dir = pathlib.Path(directory) / "config"
            config_dir.mkdir()
            cluster = config_dir / "cluster.env"
            backup = pathlib.Path(directory) / "baseline.env"
            content = "MAX_NUM_SEQS=7\nMAX_NUM_BATCHED_TOKENS=8192\nGPU_MEMORY_UTILIZATION=0.92\nKEEP_ME=yes\n"
            cluster.write_text(content, encoding="utf-8")
            backup.write_text(content, encoding="utf-8")
            script = textwrap.dedent(
                f"""
                set -Eeuo pipefail
                export LLM_CLUSTER_CONFIG_DIR={config_dir!s}
                export LLMCTL_SOURCE_ONLY=1
                source {MANAGER!s}
                optimizer_apply_snapshot {backup!s} 9 16384 0.93
                cat {cluster!s}
                """
            )
            completed = subprocess.run(
                ["bash", "-c", script],
                check=True,
                text=True,
                capture_output=True,
            )
        self.assertIn("MAX_NUM_SEQS=9", completed.stdout)
        self.assertIn("MAX_NUM_BATCHED_TOKENS=16384", completed.stdout)
        self.assertIn("GPU_MEMORY_UTILIZATION=0.93", completed.stdout)
        self.assertIn("KEEP_ME=yes", completed.stdout)
        self.assertNotIn("MAX_NUM_SEQS=7", completed.stdout)

    def test_optimizer_report_summary_uses_selected_trial_not_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            report = pathlib.Path(directory) / "report.json"
            base_result = {
                "outcome": {"successful_requests": 1, "failed_requests": 0},
                "performance": {
                    "aggregate_output_tokens_per_second": 10,
                    "ttft_p95_seconds": 1,
                    "itl_p95_seconds": 1,
                    "e2e_p95_seconds": 1,
                },
                "gpu": {
                    "utilization_average_pct": 1,
                    "utilization_peak_pct": 1,
                    "vram_used_peak_pct": 1,
                    "temperature_peak_c": 1,
                },
                "host": {
                    "cpu_utilization_average_pct": 1,
                    "cpu_utilization_peak_pct": 1,
                    "memory_available_min_gib": 100,
                    "swap_used_peak_gib": 0,
                },
                "vllm": {
                    "kv_cache_usage_peak_pct": 1,
                    "preemptions_delta": 0,
                    "waiting_requests_peak": 0,
                },
            }
            trial_result = json.loads(json.dumps(base_result))
            trial_result["performance"]["aggregate_output_tokens_per_second"] = 25
            report.write_text(
                json.dumps(
                    {
                        "selected": "candidate",
                        "baseline": base_result,
                        "trials": [{"name": "candidate", "result": trial_result}],
                    }
                ),
                encoding="utf-8",
            )
            output = run_bash(f"INTERFACE_LANGUAGE=zh; optimizer_print_result {report!s}")
        self.assertIn("聚合输出：25 token/s", output)
        self.assertNotIn("聚合输出：10 token/s", output)

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
