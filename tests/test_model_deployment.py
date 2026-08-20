import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "llmctl_model_deployment", ROOT / "lib/model_deployment.py"
)
MODEL = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODEL)


class ModelDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = self.root / "etc/llm-cluster"
        self.state = self.root / "var/lib/llm-cluster"
        self.models = self.root / "data/models"
        self.config.mkdir(parents=True)
        self.models.mkdir(parents=True)
        (self.config / "workers").mkdir()
        (self.models / "current").mkdir()
        self.paths = MODEL.Paths(
            config_dir=self.config,
            state_dir=self.state,
            model_root=self.models,
            registry=self.config / "deployments.json",
            socket=self.root / "run/model-control.sock",
            jobs_dir=self.state / "model-control/jobs",
            backups_dir=self.root / "backups/model-deployments",
            cluster_env=self.config / "cluster.env",
            secrets_env=self.config / "secrets.env",
            workers_dir=self.config / "workers",
            proxy_env=self.config / "proxy.env",
            gateway_helper=self.root / "gateway_config.py",
        )
        self.write_cluster("omniroute")
        self.gpus = [
            {
                "id": index,
                "name": "NVIDIA RTX PRO 6000D",
                "memory_mib": 85651,
                "uuid": f"GPU-{index}",
                "pci_bus_id": f"0000:{index:02x}:00.0",
            }
            for index in range(8)
        ]

    def tearDown(self):
        self.temporary.cleanup()

    def write_cluster(self, gateway: str) -> None:
        self.paths.cluster_env.write_text(
            "\n".join(
                [
                    f"MODEL_ROOT={self.models}",
                    "MODEL_HUB=local",
                    "MODEL_ID=protoLabsAI/Ornith-1.0-35B-FP8",
                    "MODEL_REVISION=current",
                    "SERVED_MODEL_NAME=ornith-1.0-35b-fp8",
                    "ACTIVE_WORKERS=0,1,2,3,4,5,6,7",
                    "WORKER_BASE_PORT=8100",
                    "TP_SIZE=1",
                    "MAX_MODEL_LEN=262144",
                    "GPU_MEMORY_UTILIZATION=0.9",
                    "MAX_NUM_SEQS=7",
                    "MAX_NUM_BATCHED_TOKENS=8192",
                    f"GATEWAY_KIND={gateway}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

    def request(
        self,
        *,
        deployment_id: str,
        public_id: str,
        model_id: str,
        artifact_path: Path,
        gpu_ids: list[int],
        preserve_alias: bool = False,
        publish: bool = True,
    ) -> dict:
        return {
            "deployment_id": deployment_id,
            "hub": "local",
            "model_id": model_id,
            "revision": "current",
            "artifact_path": str(artifact_path),
            "public_model_id": public_id,
            "served_model_name": public_id,
            "display_name": public_id,
            "publish_requested": publish,
            "preserve_legacy_alias": preserve_alias,
            "image": "vllm/vllm-openai:v0.22.1",
            "tensor_parallel_size": 1,
            "max_model_len": 32768,
            "gpu_memory_utilization": 0.9,
            "max_num_seqs": 4,
            "max_num_batched_tokens": 8192,
            "instances": [
                {
                    "kind": "local",
                    "worker_id": gpu_id,
                    "gpu_devices": [gpu_id],
                    "port": 8100 + gpu_id,
                    "enabled": True,
                }
                for gpu_id in gpu_ids
            ],
        }

    @staticmethod
    def ornith_15_catalog(tp: int = 2) -> dict:
        """返回无需网络且覆盖升级拓扑与能力映射的目录结果。"""

        return {
            "source": "modelscope",
            "id": "ornith-ai/Ornith-1.5-35B-A3B-FP8",
            "revision": "0" * 40,
            "weight_bytes": 39_365_175_520,
            "supported_architectures": ["Qwen3_5MoeForConditionalGeneration"],
            "capabilities": {
                "image_input": True,
                "ocr_optimized": True,
                "tool_parser": "qwen3_xml",
                "reasoning_parser": "qwen3",
                "thinking_toggle": True,
            },
            "trust_remote_code": False,
            "plan": {
                "tp": tp,
                "replicas": 8 // tp,
                "max_model_len": 32768,
                "max_num_seqs": 7,
            },
            "installable": True,
            "rejection_reasons": [],
        }

    def test_legacy_registry_is_synthesized_without_runtime_changes(self):
        registry = MODEL.RegistryStore(self.paths).read()
        deployment = registry["deployments"]["legacy"]
        self.assertTrue(registry["migrated_from_legacy"])
        self.assertEqual(
            [item["worker_id"] for item in deployment["instances"]], list(range(8))
        )
        self.assertEqual(deployment["public_model_ids"], ["ornith-1.0-35b-fp8"])
        self.assertTrue(deployment["publish_requested"])
        self.assertFalse(self.paths.registry.exists())

    def test_eight_gpu_split_and_legacy_alias_are_planned_in_two_steps(self):
        manager = MODEL.DeploymentManager(self.paths)
        qwen_path = self.models / "qwen"
        qwen_path.mkdir()
        qwen_request = self.request(
            deployment_id="qwen",
            public_id="gdn-inside-qwen",
            model_id="Qwen/Qwen3.5-35B-A3B-FP8",
            artifact_path=qwen_path,
            gpu_ids=[4, 5, 6, 7],
        )
        with mock.patch.object(MODEL, "gpu_inventory", return_value=self.gpus):
            plan = manager.plan(qwen_request)
        self.assertEqual(plan["affected_worker_ids"], [4, 5, 6, 7])
        self.assertTrue(
            any("释放部分 GPU/Worker" in warning for warning in plan["warnings"])
        )

        first_request = MODEL.normalize_request(qwen_request, self.paths)
        first_candidate, _ = MODEL.merge_deployment(manager.registry.read(), first_request)
        legacy_workers = [
            item["worker_id"]
            for item in first_candidate["deployments"]["legacy"]["instances"]
        ]
        self.assertEqual(legacy_workers, [0, 1, 2, 3])
        self.assertEqual(
            [item["worker_id"] for item in first_candidate["deployments"]["qwen"]["instances"]],
            [4, 5, 6, 7],
        )

        ornith_request = self.request(
            deployment_id="legacy",
            public_id="gdn-inside-ornith",
            model_id="protoLabsAI/Ornith-1.0-35B-FP8",
            artifact_path=self.models / "current",
            gpu_ids=[0, 1, 2, 3],
            preserve_alias=True,
        )
        second_request = MODEL.normalize_request(ornith_request, self.paths)
        second_candidate, affected = MODEL.merge_deployment(first_candidate, second_request)
        MODEL.validate_registry(second_candidate, self.paths)
        self.assertEqual(affected, {0, 1, 2, 3})
        self.assertEqual(
            second_candidate["deployments"]["legacy"]["public_model_ids"],
            ["gdn-inside-ornith"],
        )
        self.assertEqual(
            second_candidate["legacy_aliases"]["gdn-inside"], "gdn-inside-ornith"
        )
        self.assertTrue(second_candidate["deployments"]["qwen"]["enabled"])

    def test_local_artifact_must_stay_under_model_root(self):
        manager = MODEL.DeploymentManager(self.paths)
        request = self.request(
            deployment_id="unsafe",
            public_id="unsafe-model",
            model_id="unsafe/model",
            artifact_path=self.root / "outside",
            gpu_ids=[7],
        )
        with mock.patch.object(MODEL, "gpu_inventory", return_value=self.gpus):
            with self.assertRaisesRegex(ValueError, "模型根目录"):
                manager.plan(request)

    def test_non_omniroute_gateway_rejects_automatic_publication(self):
        self.write_cluster("litellm")
        manager = MODEL.DeploymentManager(self.paths)
        model_path = self.models / "qwen"
        model_path.mkdir()
        request = self.request(
            deployment_id="qwen",
            public_id="gdn-inside-qwen",
            model_id="Qwen/Qwen3.5-35B-A3B-FP8",
            artifact_path=model_path,
            gpu_ids=[4, 5, 6, 7],
        )
        with mock.patch.object(MODEL, "gpu_inventory", return_value=self.gpus):
            with self.assertRaisesRegex(ValueError, "不支持 LLMCtl 多模型自动发布"):
                manager.plan(request)

    def test_ornith_upgrade_plan_pins_revision_replans_tp_and_retains_rollback(self):
        """升级计划必须复用公开 ID、固定 SHA，并按目标模型重新分组 GPU。"""

        manager = MODEL.DeploymentManager(
            self.paths,
            upgrade_inspector=lambda *_args: self.ornith_15_catalog(tp=2),
        )
        with mock.patch.object(MODEL, "gpu_inventory", return_value=self.gpus):
            plan = manager.plan_upgrade(
                {"source_deployment_id": "legacy", "max_model_len": 32768}
            )
        request = plan["normalized_request"]
        deployment = request["deployment"]
        self.assertEqual(deployment["id"], "legacy")
        self.assertEqual(
            deployment["model_id"], "ornith-ai/Ornith-1.5-35B-A3B-FP8"
        )
        self.assertEqual(request["artifact"]["revision"], "0" * 40)
        self.assertEqual(request["artifact"]["hub"], "modelscope")
        self.assertEqual(deployment["public_model_ids"], ["ornith-1.0-35b-fp8"])
        self.assertEqual(deployment["served_model_name"], "ornith-1.5-35b-a3b-fp8")
        self.assertEqual(
            deployment["served_model_aliases"], ["ornith-1.0-35b-fp8"]
        )
        self.assertEqual(deployment["runtime"]["tensor_parallel_size"], 2)
        self.assertEqual(len(deployment["instances"]), 4)
        self.assertEqual(
            [item["gpu_devices"] for item in deployment["instances"]],
            [[0, 1], [2, 3], [4, 5], [6, 7]],
        )
        self.assertEqual(plan["affected_worker_ids"], list(range(8)))
        self.assertTrue(plan["upgrade"]["retains_current_artifact"])
        self.assertTrue(plan["upgrade"]["rollback_requires_worker_reload"])
        self.assertEqual(plan["source_registry_revision"], 0)

    def test_upgrade_inference_reports_each_instance_progress_and_result(self):
        """真实生成验收必须逐实例推进，不能长时间停在无法解释的 84%。"""

        manager = MODEL.DeploymentManager(self.paths)
        job = manager.jobs.create({"deployment": {}}, kind="upgrade")
        candidate = {
            "deployments": {
                "legacy": {
                    "served_model_name": "ornith-1.5-35b-a3b-fp8",
                    "instances": [
                        {
                            "id": f"worker-{index}",
                            "kind": "local",
                            "port": 8100 + index,
                            "enabled": True,
                        }
                        for index in range(2)
                    ],
                }
            }
        }
        with mock.patch.object(MODEL, "endpoint_healthy", return_value=True), mock.patch.object(
            MODEL, "endpoint_inference_ready", return_value=True
        ) as inference:
            manager._verify_instances(
                candidate, "legacy", inference=True, job=job
            )
        saved = manager.jobs.get(job["id"])
        self.assertEqual(saved["progress"], 91)
        self.assertIn("2/2", saved["message"])
        self.assertEqual(
            [entry["message"] for entry in saved["logs"]],
            ["实例 worker-0 真实生成通过", "实例 worker-1 真实生成通过"],
        )
        self.assertEqual(inference.call_count, 2)
        self.assertTrue(
            all(call.kwargs["timeout"] == 60 for call in inference.call_args_list)
        )

    def test_ornith_upgrade_submit_rejects_stale_registry_before_starting_job(self):
        """页面或 CLI 的旧确认不得覆盖已经变化的部署注册表。"""

        manager = MODEL.DeploymentManager(
            self.paths,
            upgrade_inspector=lambda *_args: self.ornith_15_catalog(),
        )
        with self.assertRaisesRegex(ValueError, "注册表已变化"):
            manager.submit_upgrade(
                {
                    "source_deployment_id": "legacy",
                    "max_model_len": 32768,
                    "expected_registry_revision": 99,
                }
            )
        self.assertEqual(manager.jobs.list(), [])

    def test_ornith_upgrade_rejects_unsafe_identity_and_mutable_revision(self):
        """目录调用前必须拒绝任意 URL、控制字符和可变 revision。"""

        inspector = mock.Mock(return_value=self.ornith_15_catalog())
        manager = MODEL.DeploymentManager(
            self.paths,
            upgrade_inspector=inspector,
        )
        with self.assertRaisesRegex(ValueError, "Ornith 目标模型"):
            manager.plan_upgrade(
                {
                    "source_deployment_id": "legacy",
                    "target_model_id": "https://example.test/ornith",
                }
            )
        with self.assertRaisesRegex(ValueError, "完整不可变提交 SHA"):
            manager.plan_upgrade(
                {
                    "source_deployment_id": "legacy",
                    "target_revision": "main",
                }
            )
        with self.assertRaisesRegex(ValueError, "目标来源"):
            manager.plan_upgrade(
                {
                    "source_deployment_id": "legacy",
                    "target_hub": "arbitrary-hub",
                }
            )
        inspector.assert_not_called()

    def test_ornith_upgrade_submit_persists_upgrade_metadata_before_background_start(self):
        """后台线程启动前必须持久化来源、目标和回退元数据。"""

        manager = MODEL.DeploymentManager(
            self.paths,
            upgrade_inspector=lambda *_args: self.ornith_15_catalog(),
        )
        thread = mock.Mock()
        with mock.patch.object(MODEL, "gpu_inventory", return_value=self.gpus), mock.patch.object(
            MODEL.threading, "Thread", return_value=thread
        ):
            job = manager.submit_upgrade(
                {
                    "source_deployment_id": "legacy",
                    "target_revision": "0" * 40,
                    "max_model_len": 32768,
                    "expected_registry_revision": 0,
                }
            )
        saved = manager.jobs.get(job["id"])
        self.assertEqual(saved["kind"], "upgrade")
        self.assertEqual(saved["upgrade"]["current_model_id"], "protoLabsAI/Ornith-1.0-35B-FP8")
        self.assertEqual(saved["upgrade"]["target_revision"], "0" * 40)
        thread.start.assert_called_once_with()

    def test_upgrade_profile_is_visible_in_snapshot_without_mutating_runtime(self):
        """管理页面读取目标目录时不能创建注册表或重启 Worker。"""

        manager = MODEL.DeploymentManager(self.paths)
        with mock.patch.object(MODEL, "gpu_inventory", return_value=self.gpus):
            snapshot = manager.snapshot()
        profiles = snapshot["upgrade_profiles"]
        self.assertGreater(len(profiles), 10)
        self.assertTrue(
            any(
                item["hub"] == "modelscope"
                and item["model_id"] == "ornith-ai/Ornith-1.5-35B-A3B-FP8"
                for item in profiles
            )
        )
        self.assertTrue(
            any(
                item["hub"] == "huggingface"
                and item["model_id"] == "ornith-ai/Ornith-1.5-9B"
                for item in profiles
            )
        )
        self.assertFalse(self.paths.registry.exists())

    def test_maintenance_proxy_is_translated_to_standard_download_variables(self):
        """保存字段必须真正变成 Hub 客户端识别的标准代理环境变量。"""

        self.paths.proxy_env.write_text(
            "MAINTENANCE_PROXY=http://127.0.0.1:7890\n"
            "MAINTENANCE_NO_PROXY=127.0.0.1,localhost,::1\n",
            encoding="utf-8",
        )
        environment = MODEL.maintenance_environment(self.paths.proxy_env)
        self.assertEqual(environment["HTTPS_PROXY"], "http://127.0.0.1:7890")
        self.assertEqual(environment["https_proxy"], "http://127.0.0.1:7890")
        self.assertEqual(environment["NO_PROXY"], "127.0.0.1,localhost,::1")

    def test_web_proxy_is_only_saved_after_real_hub_probe(self):
        """页面代理必须通过真实 curl 探测后才原子写入维护配置。"""

        runner = mock.Mock()
        runner.run.return_value = subprocess.CompletedProcess(
            args=["curl"], returncode=0, stdout=""
        )
        manager = MODEL.DeploymentManager(self.paths, runner=runner)
        result = manager.save_download_proxy(
            {
                "proxy_url": "http://proxy.internal:7890",
                "no_proxy": "127.0.0.1,localhost,::1",
                "hub": "huggingface",
            }
        )
        self.assertTrue(result["maintenance_proxy"]["configured"])
        self.assertEqual(
            MODEL.parse_env_file(self.paths.proxy_env)["MAINTENANCE_PROXY"],
            "http://proxy.internal:7890",
        )
        command = runner.run.call_args.args[0]
        self.assertIn("--proxy", command)
        self.assertIn("https://huggingface.co/api/models?limit=1", command)
        self.assertEqual(self.paths.proxy_env.stat().st_mode & 0o777, 0o600)

    def test_modelscope_downloader_is_automatically_prepared_when_missing(self):
        """旧环境缺少 Hub venv 时应安装固定依赖，而不是让升级立即失败。"""

        downloader = self.root / "opt/llm-cluster/hub-venv/bin/ms"
        paths = MODEL.dataclasses.replace(
            self.paths, modelscope_downloader=downloader
        )
        runner = mock.Mock()
        runner.run.return_value = subprocess.CompletedProcess(
            args=["command"], returncode=0, stdout=""
        )
        manager = MODEL.DeploymentManager(paths)
        resolved = manager._ensure_modelscope_downloader(runner, {})
        self.assertEqual(resolved, downloader)
        commands = [call.args[0] for call in runner.run.call_args_list]
        self.assertTrue(
            any(command[1:3] == ["-m", "venv"] for command in commands)
        )
        self.assertTrue(
            any("modelscope-hub==0.1.8" in command for command in commands)
        )
        self.assertTrue(
            any(command[-2:] == ["download", "--help"] for command in commands)
        )

    def test_proxy_rejects_credentials_and_does_not_probe_or_save(self):
        """Web UI 不得把代理凭据写入可展示的维护配置。"""

        runner = mock.Mock()
        manager = MODEL.DeploymentManager(self.paths, runner=runner)
        with self.assertRaisesRegex(ValueError, "无凭据"):
            manager.save_download_proxy(
                {"proxy_url": "http://user:secret@proxy.internal:7890"}
            )
        runner.run.assert_not_called()
        self.assertFalse(self.paths.proxy_env.exists())

    def test_upgrade_catalog_failure_surfaces_hub_error_instead_of_exit_code(self):
        """Hub 失败原因必须穿透到页面，不能只剩无法排障的退出码 2。"""

        runner = mock.Mock()
        runner.run.return_value = subprocess.CompletedProcess(
            args=["model_catalog"],
            returncode=2,
            stdout="[catalog] ERROR: ModelScope 请求失败: HTTP 403",
        )
        manager = MODEL.DeploymentManager(self.paths, runner=runner)
        with self.assertRaisesRegex(RuntimeError, "ModelScope 请求失败: HTTP 403"):
            manager._inspect_upgrade_target(
                "modelscope",
                "ornith-ai/Ornith-1.5-35B-A3B-FP8",
                "",
                262144,
                0.9,
            )
        command = runner.run.call_args.args[0]
        self.assertIn("modelscope", command)
        self.assertFalse(runner.run.call_args.kwargs["check"])

    def test_upgrade_inference_probe_requires_an_assistant_message(self):
        """升级发布前的真实探测不能把空 choices 或纯健康响应当成成功。"""

        class Response:
            def __init__(self, payload: dict, status: int = 200):
                self.payload = payload
                self.status = status

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit: int) -> bytes:
                return json.dumps(self.payload).encode()

        self.paths.secrets_env.write_text(
            "BACKEND_API_KEY=test-internal-key\n", encoding="utf-8"
        )
        with mock.patch.object(
            MODEL.urllib.request,
            "urlopen",
            return_value=Response(
                {"choices": [{"message": {"content": "OK"}}]}
            ),
        ):
            self.assertTrue(
                MODEL.endpoint_inference_ready(
                    "http://127.0.0.1:8100",
                    self.paths.secrets_env,
                    "ornith-1.5-35b-a3b-fp8",
                )
            )
        with mock.patch.object(
            MODEL.urllib.request,
            "urlopen",
            return_value=Response(
                {"choices": [{"message": {"content": None, "reasoning": "OK"}}]}
            ),
        ):
            self.assertTrue(
                MODEL.endpoint_inference_ready(
                    "http://127.0.0.1:8100",
                    self.paths.secrets_env,
                    "ornith-1.5-35b-a3b-fp8",
                )
            )
        detail = []
        with mock.patch.object(
            MODEL.urllib.request,
            "urlopen",
            return_value=Response({"error": "model not found"}, status=404),
        ):
            self.assertFalse(
                MODEL.endpoint_inference_ready(
                    "http://127.0.0.1:8100",
                    self.paths.secrets_env,
                    "ornith-1.5-35b-a3b-fp8",
                    detail=detail,
                )
            )
        self.assertEqual(detail, ["HTTP 404"])
        with mock.patch.object(
            MODEL.urllib.request,
            "urlopen",
            return_value=Response({"choices": []}),
        ):
            self.assertFalse(
                MODEL.endpoint_inference_ready(
                    "http://127.0.0.1:8100",
                    self.paths.secrets_env,
                    "ornith-1.5-35b-a3b-fp8",
                )
            )

    def test_rollback_waits_for_old_workers_before_republishing_gateway(self):
        """回退不能在旧权重恢复健康前把公开路由切回旧 Worker。"""

        manager = MODEL.DeploymentManager(self.paths, runner=mock.Mock())
        registry = manager.registry.read()
        manager.registry.write(registry)
        backup = self.paths.backups_dir / "rollback-order"
        (backup / "workers").mkdir(parents=True)
        (backup / "deployments.json").write_text(
            json.dumps(registry), encoding="utf-8"
        )
        (backup / "cluster.env").write_text(
            self.paths.cluster_env.read_text(encoding="utf-8"), encoding="utf-8"
        )
        (backup / "workers/0.env").write_text(
            "GPU_DEVICES=0\nWORKER_PORT=8100\n", encoding="utf-8"
        )
        (backup / "manifest.json").write_text(
            json.dumps({"affected_worker_ids": [0], "active_before": [0]}),
            encoding="utf-8",
        )
        order = []
        manager._wait_worker_ids = mock.Mock(side_effect=lambda _ids: order.append("wait"))
        manager._reconcile_gateway = mock.Mock(side_effect=lambda: order.append("publish"))
        with mock.patch.object(
            MODEL,
            "gateway_capabilities",
            return_value={"kind": "omniroute", "registry_publish": True},
        ):
            manager._restore_runtime(backup, wait_for_health=True)
        self.assertEqual(order, ["wait", "publish"])


if __name__ == "__main__":
    unittest.main()
