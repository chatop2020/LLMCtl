#!/usr/bin/env python3
import json
import os
import pathlib
import subprocess
import tempfile
import textwrap
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
MANAGER = ROOT / "llmctl.sh"


def manager_implementation() -> str:
    """返回主入口和按命令域拆分模块组成的完整可审查源码。"""

    return "\n".join(
        [
            MANAGER.read_text(encoding="utf-8"),
            *(path.read_text(encoding="utf-8") for path in sorted((ROOT / "lib/llmctl").glob("*.sh"))),
        ]
    )


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
    def test_model_init_reloads_an_already_running_control_service(self):
        """恢复命令必须重启旧进程，而不只是对活动单元重复 enable --now。"""

        manager = manager_implementation()
        command = manager.split("cmd_model() {", 1)[1].split(
            "worker_config_value() {", 1
        )[0]
        init = command.split("    init)\n", 1)[1].split("    status)", 1)[0]
        self.assertIn("systemctl enable llm-model-control.service", init)
        self.assertIn("systemctl restart llm-model-control.service", init)
        self.assertNotIn("enable --now llm-model-control.service", init)

    def test_worker_start_passes_new_and_legacy_served_model_names_to_vllm(self):
        """升级后的 Worker 必须同时接受新内部名和旧客户端仍在使用的名称。"""

        manager = manager_implementation()
        worker_start = manager.split("cmd_worker_start() {", 1)[1].split(
            "set_env_value() {", 1
        )[0]
        self.assertIn('SERVED_MODEL_ALIASES:-', worker_start)
        self.assertIn('served_model_names+=("${alias}")', worker_start)
        self.assertIn(
            '--served-model-name "${served_model_names[@]}"', worker_start
        )

    def test_worker_start_enforces_server_side_output_token_ceiling(self):
        """客户端省略或放大 max_tokens 时，vLLM 仍必须限制单次输出。"""

        manager = manager_implementation()
        worker_start = manager.split("cmd_worker_start() {", 1)[1].split(
            "set_env_value() {", 1
        )[0]
        self.assertIn("MAX_OUTPUT_TOKENS_CEILING=32768", manager)
        self.assertIn("--override-generation-config", worker_start)
        self.assertIn('max_new_tokens\\\":${MAX_OUTPUT_TOKENS}', worker_start)
        self.assertIn("output<=${MAX_OUTPUT_TOKENS}", worker_start)

    def test_status_prefers_registry_and_worker_model_over_legacy_cluster_name(self):
        """多模型环境的状态页必须显示实际 1.5 部署，不能继续报告全局 1.0。"""

        with tempfile.TemporaryDirectory() as directory:
            config = pathlib.Path(directory) / "config"
            config.mkdir()
            registry = {
                "schema_version": 1,
                "revision": 3,
                "artifacts": {
                    "artifact-new": {
                        "hub": "modelscope",
                        "model_id": "ornith-ai/Ornith-1.5-35B-A3B-FP8",
                        "revision": "b" * 40,
                        "path": "/data/models/ornith-1.5",
                    }
                },
                "deployments": {
                    "legacy": {
                        "enabled": True,
                        "artifact_id": "artifact-new",
                        "model_id": "ornith-ai/Ornith-1.5-35B-A3B-FP8",
                        "served_model_name": "ornith-1.5-35b-a3b-fp8",
                        "served_model_aliases": ["ornith-1.0-35b-fp8"],
                        "runtime": {
                            "tensor_parallel_size": 2,
                            "max_num_seqs": 7,
                        },
                        "instances": [
                            {
                                "kind": "local",
                                "enabled": True,
                                "worker_id": index,
                            }
                            for index in range(4)
                        ],
                    }
                },
            }
            (config / "deployments.json").write_text(
                json.dumps(registry), encoding="utf-8"
            )
            script = textwrap.dedent(
                f"""
                set -Eeuo pipefail
                export LLMCTL_SOURCE_ONLY=1
                export LLM_CLUSTER_CONFIG_DIR={config!s}
                source {MANAGER!s}
                load_config() {{
                  MODEL_HUB=modelscope; MODEL_ID=protoLabsAI/Ornith-1.0-35B-FP8; MODEL_REVISION=master
                  MODEL_ARCHITECTURE=Qwen3_5MoeForConditionalGeneration; MODEL_PRECISION=fp8; MODEL_TASK=vision
                  MODEL_ROOT=/data/models; ACTIVE_WORKERS=0,1,2,3,4,5,6,7; TP_SIZE=1; PHYSICAL_GPU_COUNT=8
                  INSTANCE_COUNT=8; MAX_NUM_SEQS=7; ESTIMATED_MAX_NUM_SEQS=11; STARTUP_PARALLELISM=8
                  MAX_OUTPUT_TOKENS=32768
                  SUPPORTS_IMAGE_INPUT=1; MM_LIMIT='{{"image":8}}'; SUPPORTS_TOOL_CALLING=1; TOOL_CALL_PARSER=qwen3_xml
                  SUPPORTS_REASONING=1; REASONING_PARSER=qwen3; SUPPORTS_THINKING_TOGGLE=1
                  API_BIND=0.0.0.0; API_PORT=8000; SERVED_MODEL_NAME=ornith-1.0-35b-fp8
                  GATEWAY_KIND=omniroute; ACCOUNT_DB_PATH=/tmp/portal.db
                  ACCOUNT_BIND=127.0.0.1; ACCOUNT_PORT=8001
                }}
                gateway_display_name() {{ printf OmniRoute; }}
                router_health() {{ return 0; }}
                account_portal_health() {{ return 0; }}
                worker_health() {{ return 0; }}
                worker_port() {{ printf '%s\n' "$((8100 + $1))"; }}
                worker_devices() {{ printf '%s\n' "$((2 * $1)),$((2 * $1 + 1))"; }}
                worker_served_model() {{ printf 'ornith-1.5-35b-a3b-fp8\n'; }}
                systemctl() {{ [[ "$1" == is-active ]] && printf 'active\n' || return 0; }}
                cmd_status
                """
            )
            completed = subprocess.run(
                ["bash", "-c", script], check=False, text=True, capture_output=True
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn(
            "模型: modelscope:ornith-ai/Ornith-1.5-35B-A3B-FP8",
            completed.stdout,
        )
        self.assertIn("拓扑: TP=2", completed.stdout)
        self.assertIn("兼容别名: ornith-1.0-35b-fp8", completed.stdout)
        self.assertIn("ornith-1.5-35b-a3b-fp8", completed.stdout)

    def test_model_upgrade_cli_reuses_plan_revision_and_submits_stale_guard(self):
        """CLI apply 必须把计划版本带回控制服务，且不要求管理员手写 JSON。"""

        with tempfile.TemporaryDirectory() as directory:
            state_dir = pathlib.Path(directory) / "state"
            state_dir.mkdir()
            submitted = state_dir / "submitted.json"
            revision = "0" * 40
            script = textwrap.dedent(
                f"""
                set -Eeuo pipefail
                export LLM_CLUSTER_STATE_DIR={state_dir!s}
                export LLMCTL_SOURCE_ONLY=1
                source {MANAGER!s}
                model_control_request() {{
                  local operation="$1" input="$2"
                  case "${{operation}}" in
                    upgrade-plan)
                      jq -n --arg revision {revision!r} '{{
                        source_registry_revision:7,
                        upgrade:{{target_revision:$revision,target_model_id:"ornith-ai/Ornith-1.5-35B-A3B-FP8"}},
                        affected_worker_ids:[0,1,2,3,4,5,6,7],warnings:[]
                      }}'
                      ;;
                    upgrade-submit)
                      cp "${{input}}" {submitted!s}
                      printf '%s\n' '{{"id":"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa","kind":"upgrade","state":"waiting"}}'
                      ;;
                    *) return 1 ;;
                  esac
                }}
                cmd_model_upgrade apply legacy --hub modelscope --yes
                """
            )
            completed = subprocess.run(
                ["bash", "-c", script],
                check=True,
                text=True,
                capture_output=True,
            )
            payload = json.loads(submitted.read_text(encoding="utf-8"))
        self.assertEqual(payload["source_deployment_id"], "legacy")
        self.assertEqual(payload["target_hub"], "modelscope")
        self.assertEqual(payload["target_revision"], revision)
        self.assertEqual(payload["expected_registry_revision"], 7)
        self.assertIn("ornith-ai/Ornith-1.5-35B-A3B-FP8", completed.stdout)

    def test_model_publish_cli_retries_routes_without_deployment_payload(self):
        """部分完成恢复命令必须只提交 publish 操作和空对象。"""

        with tempfile.TemporaryDirectory() as directory:
            state = pathlib.Path(directory) / "state"
            state.mkdir()
            capture = pathlib.Path(directory) / "publish.txt"
            script = textwrap.dedent(
                f"""
                set -Eeuo pipefail
                export LLMCTL_SOURCE_ONLY=1
                export LLM_CLUSTER_STATE_DIR={state!s}
                source {MANAGER!s}
                require_root() {{ :; }}
                load_config() {{ :; }}
                model_control_request() {{
                  printf 'operation=%s payload=%s\n' "$1" "$(cat "$2")" >{capture!s}
                  printf '{{"id":"publish-job","kind":"publish","state":"waiting"}}\n'
                }}
                cmd_model publish
                """
            )
            completed = subprocess.run(
                ["bash", "-c", script],
                check=True,
                text=True,
                capture_output=True,
            )
            submitted = capture.read_text(encoding="utf-8")
        self.assertIn("operation=publish payload={}", submitted)
        self.assertIn('"kind": "publish"', completed.stdout)

    def test_workflow_init_preserves_existing_config_and_prints_recovery_steps(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = pathlib.Path(directory) / "state"
            config_dir = pathlib.Path(directory) / "config"
            workflow_dir = state_dir / "workflow"
            workflow_dir.mkdir(parents=True)
            config_dir.mkdir(parents=True)
            workflow_config = workflow_dir / "workflow.json"
            original = {
                "version": 1,
                "listen": "127.0.0.1:18100",
                "models": {
                    "gdn-inside-workflow": {
                        "enabled": False,
                        "base_model": "gdn-inside",
                        "pool": "text-generation",
                    }
                },
                "pools": {"text-generation": {"targets": []}},
                "adapters": {},
            }
            workflow_config.write_text(json.dumps(original), encoding="utf-8")
            script = textwrap.dedent(
                f"""
                set -Eeuo pipefail
                export LLM_CLUSTER_STATE_DIR={state_dir!s}
                export LLM_CLUSTER_CONFIG_DIR={config_dir!s}
                export LLMCTL_SOURCE_ONLY=1
                source {MANAGER!s}
                workflow_require_runtime() {{ :; }}
                ensure_workflow_env() {{ :; }}
                workflow_check_config() {{ :; }}
                systemctl() {{
                  [[ "$1" == is-active ]] && printf 'inactive\n'
                  return 0
                }}
                WORKFLOW_LISTEN=127.0.0.1:18100
                WORKFLOW_ROUTE_MODEL=gdn-inside-workflow
                SERVED_MODEL_NAME=gdn-inside
                ACTIVE_WORKERS=0,1
                WORKER_BASE_PORT=8100
                cmd_workflow_init
                printf '%s\n' '---CONFIG---'
                cat {workflow_config!s}
                """
            )
            completed = subprocess.run(
                ["bash", "-c", script],
                check=True,
                text=True,
                capture_output=True,
            )
        output, saved = completed.stdout.split("---CONFIG---\n", 1)
        self.assertIn("已有工作流配置已保留并通过校验", output)
        self.assertIn("workflow model enable gdn-inside-workflow", output)
        self.assertIn("workflow check && llmctl workflow enable", output)
        self.assertEqual(json.loads(saved), original)

    def test_workflow_status_normalizes_missing_unit_without_duplicate_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = pathlib.Path(directory) / "state"
            config_dir = pathlib.Path(directory) / "config"
            workflow_dir = state_dir / "workflow"
            workflow_dir.mkdir(parents=True)
            config_dir.mkdir(parents=True)
            (workflow_dir / "workflow.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "listen": "127.0.0.1:18100",
                        "models": {},
                        "pools": {},
                        "adapters": {},
                    }
                ),
                encoding="utf-8",
            )
            script = textwrap.dedent(
                f"""
                set -Eeuo pipefail
                export LLM_CLUSTER_STATE_DIR={state_dir!s}
                export LLM_CLUSTER_CONFIG_DIR={config_dir!s}
                export LLMCTL_SOURCE_ONLY=1
                source {MANAGER!s}
                require_root() {{ :; }}
                load_config() {{ :; }}
                systemctl() {{
                  case "$1" in
                    is-enabled) printf 'not-found\n'; return 1 ;;
                    is-active) printf 'inactive\n'; return 3 ;;
                  esac
                }}
                workflow_helper() {{
                  [[ "$1" == show ]] && printf '%s\n' '{{"listen":"127.0.0.1:18100","models":{{}},"pools":{{}},"adapters":{{}}}}'
                }}
                cmd_workflow status
                """
            )
            completed = subprocess.run(
                ["bash", "-c", script],
                check=True,
                text=True,
                capture_output=True,
            )
        lines = completed.stdout.splitlines()
        self.assertEqual(
            lines[0],
            "LLMCtl workflow: configured=yes enabled=not-installed active=inactive",
        )
        self.assertNotIn("disabled active=inactive", completed.stdout)

    def test_workflow_enable_names_the_disabled_route_in_recovery_command(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = pathlib.Path(directory) / "state"
            config_dir = pathlib.Path(directory) / "config"
            workflow_dir = state_dir / "workflow"
            workflow_dir.mkdir(parents=True)
            config_dir.mkdir(parents=True)
            (workflow_dir / "workflow.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "listen": "127.0.0.1:18100",
                        "models": {
                            "llmctl-workflow-ornith-1.0-35b-fp8": {
                                "enabled": False,
                                "base_model": "ornith-1.0-35b-fp8",
                                "pool": "text-generation",
                            }
                        },
                        "pools": {"text-generation": {"targets": []}},
                        "adapters": {},
                    }
                ),
                encoding="utf-8",
            )
            script = textwrap.dedent(
                f"""
                set -Eeuo pipefail
                export LLM_CLUSTER_STATE_DIR={state_dir!s}
                export LLM_CLUSTER_CONFIG_DIR={config_dir!s}
                export LLMCTL_SOURCE_ONLY=1
                source {MANAGER!s}
                require_root() {{ :; }}
                load_config() {{ :; }}
                ensure_workflow_env() {{ :; }}
                workflow_check_config() {{ :; }}
                cmd_workflow enable
                """
            )
            completed = subprocess.run(
                ["bash", "-c", script],
                check=False,
                text=True,
                capture_output=True,
            )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(
            "llmctl workflow model enable llmctl-workflow-ornith-1.0-35b-fp8",
            completed.stderr,
        )

    def test_huggingface_model_search_runs_network_preflight_before_catalog(self):
        output = run_bash(
            r"""
            require_root() { :; }
            load_config() { GPU_MEMORY_UTILIZATION=0.92; }
            prompt_proxy_if_needed() { printf 'network-preflight\n'; }
            export_proxy_env() { :; }
            run_catalog_maintenance() {
              printf 'catalog'
              printf ' %s' "$@"
              printf '\n'
            }
            cmd_models search ornith --source huggingface
            """
        )
        lines = output.splitlines()
        self.assertEqual(lines[0], "network-preflight")
        self.assertIn("catalog search", lines[1])
        self.assertIn("--source huggingface", lines[1])

    def test_nginx_front_door_keeps_portal_api_and_inference_paths_separate(self):
        output = run_bash(
            r"""
            API_BIND=0.0.0.0
            API_PORT=8000
            GATEWAY_INTERNAL_PORT=18000
            ACCOUNT_PORT=8001
            GATEWAY_KIND=omniroute
            render_nginx_config
            """
        )
        self.assertIn("listen 0.0.0.0:8000", output)
        self.assertIn("server_name localhost 127.0.0.1", output)
        self.assertNotIn("listen 80", output)
        self.assertNotIn("listen 443", output)
        self.assertNotIn("ssl_certificate", output)
        self.assertNotIn("zjguardian.com", output)
        self.assertIn("location ^~ /ui/", output)
        self.assertIn("location ^~ /portal-api/", output)
        self.assertIn("location ^~ /base_ui/", output)
        self.assertIn("location = /portal-api/auth/login", output)
        self.assertIn("limit_req zone=llmctl_auth", output)
        self.assertIn("limit_req_zone $binary_remote_addr", output)
        self.assertIn("proxy_set_header X-Forwarded-For $remote_addr", output)
        self.assertNotIn("proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for", output)
        self.assertIn('add_header X-Content-Type-Options "nosniff" always', output)
        api = output.split("location ^~ /v1/", 1)[1].split("}", 1)[0]
        self.assertIn("127.0.0.1:18000", api)
        self.assertIn("proxy_buffering off", api)
        self.assertNotIn("127.0.0.1:8001", api)

    def test_public_nginx_apply_command_reuses_transactional_install(self):
        output = run_bash(
            r"""
            require_root() { :; }
            load_config() { :; }
            cmd_nginx_install() { printf 'nginx-applied\n'; }
            cmd_nginx apply
            """
        )
        self.assertEqual(output.strip(), "nginx-applied")

    def test_default_logs_aggregate_router_database_and_active_workers(self):
        output = run_bash(
            r"""
            load_config() { ACTIVE_WORKERS=0,1; }
            journalctl() { printf '%s\n' "$*"; }
            cmd_logs
            """
        )
        arguments = output.splitlines()
        self.assertIn("llm-router.service", arguments)
        self.assertIn("llm-database.service", arguments)
        self.assertIn("llm-worker@0.service", arguments)
        self.assertIn("llm-worker@1.service", arguments)

    def test_reasoning_smoke_uses_model_sampling_and_requires_separate_answer(self):
        output = run_bash(
            r"""
            SERVED_MODEL_NAME=test-model
            SUPPORTS_THINKING_TOGGLE=1
            SUPPORTS_REASONING=1
            SUPPORTS_TOOL_CALLING=0
            SUPPORTS_IMAGE_INPUT=0
            api_post() {
              jq -e '.stream == false' "$3" >/dev/null
              if jq -e '.reasoning_effort == "none"' "$3" >/dev/null; then
                printf '%s\n' '{"choices":[{"finish_reason":"stop","message":{"content":"LLM_OK","reasoning":null}}]}'
              else
                jq -e '.max_tokens == 2048 and .temperature == 0.6 and .top_p == 0.95 and .top_k == 20' "$3" >/dev/null
                printf '%s\n' '{"choices":[{"finish_reason":"stop","message":{"content":"323","reasoning":"17 times 19"}}]}'
              fi
            }
            smoke_endpoint http://127.0.0.1:1 key 0
            """
        )
        self.assertIn("默认思考并独立解析：PASS", output)

    def test_truncated_reasoning_retries_and_writes_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            script = textwrap.dedent(
                f"""
                set -Eeuo pipefail
                export LLMCTL_SOURCE_ONLY=1
                source {MANAGER!s}
                SERVED_MODEL_NAME=test-model
                SUPPORTS_THINKING_TOGGLE=1
                SUPPORTS_REASONING=1
                SUPPORTS_TOOL_CALLING=0
                SUPPORTS_IMAGE_INPUT=0
                api_post() {{
                  jq -e '.stream == false' "$3" >/dev/null
                  if jq -e '.reasoning_effort == "none"' "$3" >/dev/null; then
                    printf '%s\\n' '{{"choices":[{{"finish_reason":"stop","message":{{"content":"LLM_OK","reasoning":null}}}}]}}'
                  else
                    printf '%s\\n' '{{"choices":[{{"finish_reason":"length","message":{{"content":null,"reasoning":"17 times 19 is 323"}}}}]}}'
                  fi
                }}
                smoke_endpoint http://127.0.0.1:1 key 0
                """
            )
            environment = os.environ.copy()
            environment["LLM_CLUSTER_STATE_DIR"] = directory
            completed = subprocess.run(
                ["bash", "-c", script],
                check=False,
                text=True,
                capture_output=True,
                env=environment,
            )
            diagnostics = list((pathlib.Path(directory) / "diagnostics" / "smoke").glob("*.json"))
            diagnostic_payloads = [json.loads(path.read_text(encoding="utf-8")) for path in diagnostics]
        self.assertEqual(completed.returncode, 1)
        self.assertIn("2048 token", completed.stderr)
        self.assertIn("4096 token", completed.stderr)
        self.assertIn('"finish_reason":"length"', completed.stderr)
        self.assertGreaterEqual(len(diagnostics), 3)
        self.assertTrue(all("choices" in payload for payload in diagnostic_payloads))

    def test_smoke_summary_identifies_sse_instead_of_only_invalid_json(self):
        output = run_bash(r'''smoke_response_summary 'data: {"choices":[]}' ''')
        summary = json.loads(output)
        self.assertTrue(summary["invalid_json"])
        self.assertEqual(summary["detected_format"], "sse")
        self.assertGreater(summary["body_chars"], 0)

    def test_ocr_fixture_is_written_outside_systemd_private_tmp(self):
        """后台维护必须在宿主机共享目录生成并回读 OCR 图片。"""

        with tempfile.TemporaryDirectory() as directory:
            script = textwrap.dedent(
                f"""
                set -Eeuo pipefail
                export LLMCTL_SOURCE_ONLY=1
                source {MANAGER!s}
                VLLM_IMAGE=fake-vllm
                docker() {{
                  local mount=""
                  while (($#)); do
                    if [[ "$1" == -v ]]; then mount="$2"; shift 2; else shift; fi
                  done
                  [[ -n "${{mount}}" ]]
                  printf 'synthetic-png' >"${{mount%:/out}}/llm-ocr-test.png"
                }}
                fixture=$(smoke_fixture_directory)
                printf '%s\n' "${{fixture}}"
                [[ "${{fixture}}" == "${{LLM_CLUSTER_STATE_DIR}}/diagnostics/smoke/.fixture."* ]]
                [[ -d "${{fixture}}" ]]
                make_ocr_fixture "${{fixture}}"
                [[ -s "${{fixture}}/llm-ocr-test.png" ]]
                rm "${{fixture}}/llm-ocr-test.png"
                rmdir "${{fixture}}"
                """
            )
            environment = os.environ.copy()
            environment["LLM_CLUSTER_STATE_DIR"] = directory
            completed = subprocess.run(
                ["bash", "-c", script],
                check=True,
                text=True,
                capture_output=True,
                env=environment,
            )

        self.assertTrue(completed.stdout.strip().startswith(directory))
        self.assertNotIn("/tmp/", completed.stdout.removeprefix(directory))

    def test_omniroute_request_writes_exactly_one_json_object(self):
        """OmniRoute CLI 不得在调用 Python 前给有效 JSON 追加右花括号。"""

        with tempfile.TemporaryDirectory() as directory:
            script = textwrap.dedent(
                f"""
                set -Eeuo pipefail
                export LLMCTL_SOURCE_ONLY=1
                source {MANAGER!s}
                omniroute_require_control() {{ :; }}
                model_control_request() {{
                  python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1])), sort_keys=True))' "$2"
                }}
                omniroute_request omniroute-assess '{{"deep":true}}'
                omniroute_request omniroute-status
                """
            )
            environment = os.environ.copy()
            environment["LLM_CLUSTER_STATE_DIR"] = directory
            completed = subprocess.run(
                ["bash", "-c", script],
                check=True,
                text=True,
                capture_output=True,
                env=environment,
            )

        payloads = [json.loads(line) for line in completed.stdout.splitlines()]
        self.assertEqual(payloads, [{"deep": True}, {}])

    def test_omniroute_request_rejects_multiple_json_documents(self):
        """内部调用意外拼接两段 JSON 时必须在 Shell 层失败关闭。"""

        with tempfile.TemporaryDirectory() as directory:
            script = textwrap.dedent(
                f"""
                set -Eeuo pipefail
                export LLMCTL_SOURCE_ONLY=1
                source {MANAGER!s}
                omniroute_require_control() {{ :; }}
                model_control_request() {{ printf 'unexpected call\n'; }}
                omniroute_request omniroute-assess '{{"deep":true}}}}'
                """
            )
            environment = os.environ.copy()
            environment["LLM_CLUSTER_STATE_DIR"] = directory
            completed = subprocess.run(
                ["bash", "-c", script],
                check=False,
                text=True,
                capture_output=True,
                env=environment,
            )

        self.assertEqual(completed.returncode, 1)
        self.assertIn("不是单个有效 JSON 对象", completed.stderr)
        self.assertNotIn("unexpected call", completed.stdout)

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

    def test_router_health_reloads_newapi_token_created_after_watcher_start(self):
        with tempfile.TemporaryDirectory() as directory:
            config_dir = pathlib.Path(directory) / "config"
            config_dir.mkdir()
            (config_dir / "secrets.env").write_text(
                "GATEWAY_API_KEY=sk-new-managed-token\n", encoding="utf-8"
            )
            script = textwrap.dedent(
                f"""
                set -Eeuo pipefail
                export LLM_CLUSTER_CONFIG_DIR={config_dir!s}
                export LLMCTL_SOURCE_ONLY=1
                source {MANAGER!s}
                GATEWAY_API_KEY=sk-stale-bootstrap-token
                LITELLM_MASTER_KEY=sk-stale-bootstrap-token
                API_BIND=0.0.0.0
                API_PORT=8000
                curl() {{
                  local argument found=0
                  for argument in "$@"; do
                    [[ "${{argument}}" == "Authorization: Bearer sk-new-managed-token" ]] && found=1
                  done
                  (( found == 1 ))
                }}
                router_health
                printf '%s|%s\n' "$GATEWAY_API_KEY" "$LITELLM_MASTER_KEY"
                """
            )
            completed = subprocess.run(
                ["bash", "-c", script],
                check=True,
                text=True,
                capture_output=True,
            )
        self.assertEqual(
            completed.stdout.strip(),
            "sk-new-managed-token|sk-new-managed-token",
        )

    def test_startup_watch_reports_database_and_gateway_dependencies(self):
        manager = MANAGER.read_text(encoding="utf-8")
        startup = manager.split("cmd_startup() {", 1)[1].split("api_post() {", 1)[0]
        watch = startup.split("watch)", 1)[1]
        self.assertIn("Dependencies=[PostgreSQL:${database_state}", startup)
        self.assertLess(watch.index("if router_health"), watch.index("log_worker_progress"))
        self.assertIn("gateway_ready == 1", watch)

    def test_gateway_key_reload_keeps_legacy_litellm_config_compatible(self):
        with tempfile.TemporaryDirectory() as directory:
            config_dir = pathlib.Path(directory) / "config"
            config_dir.mkdir()
            (config_dir / "secrets.env").write_text(
                "LITELLM_MASTER_KEY=sk-legacy-master\n", encoding="utf-8"
            )
            script = textwrap.dedent(
                f"""
                set -Eeuo pipefail
                export LLM_CLUSTER_CONFIG_DIR={config_dir!s}
                export LLMCTL_SOURCE_ONLY=1
                source {MANAGER!s}
                GATEWAY_API_KEY=stale
                LITELLM_MASTER_KEY=stale
                reload_gateway_api_key
                printf '%s|%s\n' "$GATEWAY_API_KEY" "$LITELLM_MASTER_KEY"
                """
            )
            completed = subprocess.run(
                ["bash", "-c", script],
                check=True,
                text=True,
                capture_output=True,
            )
        self.assertEqual(
            completed.stdout.strip(), "sk-legacy-master|sk-legacy-master"
        )

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
        manager = manager_implementation()
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

    def test_installer_rechecks_real_health_before_shutdown_after_watcher_failure(self):
        installer = (ROOT / "install-llm-cluster.sh").read_text(encoding="utf-8")
        fallback = installer.split(
            "start_cluster_with_fresh_health_fallback() {", 1
        )[1].split("\n}", 1)[0]
        install_acceptance = installer.split(
            "if ! start_cluster_with_fresh_health_fallback; then", 1
        )[1].split("fi", 1)[0]
        self.assertIn("start_cluster_with_progress", fallback)
        self.assertIn("/usr/local/sbin/llmctl health", fallback)
        self.assertLess(
            installer.index("start_cluster_with_fresh_health_fallback() {"),
            installer.index("if ! start_cluster_with_fresh_health_fallback; then"),
        )
        self.assertIn("shutdown --timeout 180", install_acceptance)

    def test_omniroute_password_rotation_updates_portal_then_gateway(self):
        with tempfile.TemporaryDirectory() as directory:
            config_dir = pathlib.Path(directory)
            secrets_file = config_dir / "secrets.env"
            secrets_file.write_text(
                "UI_PASSWORD=OldPassword123\nACCOUNT_ADMIN_PASSWORD=OldPassword123\n",
                encoding="utf-8",
            )
            script = textwrap.dedent(
                f"""
                set -Eeuo pipefail
                export LLM_CLUSTER_CONFIG_DIR={config_dir!s}
                export LLMCTL_SOURCE_ONLY=1
                source {MANAGER!s}
                require_root() {{ :; }}
                load_config() {{
                  GATEWAY_KIND=omniroute
                  UI_PASSWORD=OldPassword123
                  ACCOUNT_ADMIN_PASSWORD=OldPassword123
                  ACCOUNT_DB_PATH={directory!s}/account-portal.db
                }}
                account_helper() {{ printf 'portal <%s>\n' "$ACCOUNT_ADMIN_PASSWORD"; }}
                gateway_helper() {{
                  printf 'gateway <%s>\n' "$LLMCTL_NEW_PASSWORD"
                  printf 'UI_PASSWORD=%s\nACCOUNT_ADMIN_PASSWORD=%s\n' \
                    "$LLMCTL_NEW_PASSWORD" "$LLMCTL_NEW_PASSWORD" >"$SECRETS_ENV"
                }}
                router_local_base_url() {{ printf 'http://127.0.0.1:8000\n'; }}
                systemctl() {{ printf 'systemctl <%s> <%s>\n' "$1" "$2"; }}
                wait_account_portal() {{ printf 'portal-ready\n'; }}
                cmd_admin set-password NewPassword456
                """
            )
            completed = subprocess.run(
                ["bash", "-c", script],
                check=True,
                text=True,
                capture_output=True,
            )
        lines = completed.stdout.splitlines()
        self.assertLess(lines.index("portal <NewPassword456>"), lines.index("gateway <NewPassword456>"))
        self.assertNotIn("systemctl <restart> <llm-account.service>", lines)
        self.assertNotIn("portal-ready", lines)


if __name__ == "__main__":
    unittest.main()
