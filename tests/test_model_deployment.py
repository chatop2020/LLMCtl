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
import model_verification as MODEL_VERIFICATION


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
                    "GATEWAY_INTERNAL_PORT=18000",
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

    def test_qwen38_runtime_is_model_scoped_and_restorable(self):
        """Qwen 专属参数必须进入 Worker 私有环境，不能污染 Ornith 全局值。"""

        artifact_path = self.models / "qwen38"
        artifact_path.mkdir()
        request = self.request(
            deployment_id="qwen38",
            public_id="qwen3.8-flash-next",
            model_id="RadixArk/Qwen3.8-Flash-Next-NVFP4",
            artifact_path=artifact_path,
            gpu_ids=[0, 1],
        )
        request.update(
            {
                "image": "vllm/vllm-openai:qwen38-flash-next",
                "tensor_parallel_size": 2,
                "max_model_len": 262144,
                "max_num_seqs": 8,
                "ple_cpu_offload": True,
                "enable_expert_parallel": True,
                "enable_prefix_caching": False,
                "enable_flashinfer_autotune": False,
                "mtp_speculative_tokens": 0,
                "kv_cache_dtype": "auto",
                "yarn_factor": 1,
                "instances": [
                    {
                        "kind": "local",
                        "worker_id": 0,
                        "gpu_devices": [0, 1],
                        "port": 8100,
                        "enabled": True,
                    }
                ],
            }
        )
        normalized = MODEL.normalize_request(request, self.paths)
        deployment = normalized["deployment"]
        environment = MODEL.worker_environment(
            deployment, deployment["instances"][0], normalized["artifact"]
        )
        self.assertEqual(environment["TP_SIZE"], 2)
        self.assertEqual(environment["PLE_CPU_OFFLOAD"], 1)
        self.assertEqual(environment["ENABLE_EXPERT_PARALLEL"], 1)
        self.assertEqual(environment["ENABLE_PREFIX_CACHING"], 0)
        self.assertEqual(environment["KV_CACHE_DTYPE"], "auto")
        self.assertEqual(environment["YARN_FACTOR"], 1.0)

        request["tensor_parallel_size"] = 1
        request["instances"][0]["gpu_devices"] = [0]
        with self.assertRaisesRegex(ValueError, "至少需要 TP2"):
            MODEL.normalize_request(request, self.paths)

    def test_qwen38_nvfp4_qsa_kv_is_gated_by_runtime_image(self):
        """实验 KV 精度必须在下载权重前读取镜像实际支持列表。"""

        runner = mock.Mock(spec=MODEL.CommandRunner)
        manager = MODEL.DeploymentManager(self.paths, runner=runner)
        manager._verify_runtime_image(
            "RadixArk/Qwen3.8-Flash-Next-NVFP4",
            {
                "image": "vllm/vllm-openai:qwen38-flash-next",
                "kv_cache_dtype": "nvfp4",
            },
        )
        command = runner.run.call_args.args[0]
        self.assertIn("architecture = sys.argv[2]", command[-3])
        self.assertEqual(command[-2], "nvfp4")
        self.assertEqual(command[-1], "Qwen4ExpForConditionalGeneration")

    def test_qwen38_missing_image_reports_docker_daemon_proxy_action(self):
        """镜像网络失败应说明修复 Docker daemon，而不是输出整段 daemon 异常。"""

        runner = mock.Mock(spec=MODEL.CommandRunner)
        runner.run.side_effect = [
            subprocess.CompletedProcess([], 1),
            RuntimeError("dial tcp registry-1.docker.io:443: i/o timeout"),
        ]
        manager = MODEL.DeploymentManager(self.paths, runner=runner)
        with self.assertRaisesRegex(RuntimeError, "Docker daemon"):
            manager._verify_runtime_image(
                "RadixArk/Qwen3.8-Flash-Next-NVFP4",
                {
                    "image": "vllm/vllm-openai:qwen38-flash-next",
                    "kv_cache_dtype": "auto",
                },
            )
        self.assertEqual(
            runner.run.call_args_list[1].args[0][:2], ["docker", "pull"]
        )

    def test_qwen38_artifact_requires_visual_ple_and_all_indexed_shards(self):
        """固定制品必须真的包含视觉编码器和 PLE，不能只凭模型名称放行。"""

        artifact = self.models / "qwen38-verified"
        artifact.mkdir()
        (artifact / "config.json").write_text(
            json.dumps(
                {
                    "architectures": ["Qwen4ExpForConditionalGeneration"],
                    "language_model_only": False,
                    "vision_config": {"depth": 27},
                    "text_config": {
                        "max_position_embeddings": 262144,
                        "ple_embedding_dtype": "float8_e4m3fn",
                    },
                    "quantization_config": {
                        "quant_method": "modelopt",
                        "quant_algo": "NVFP4",
                    },
                }
            ),
            encoding="utf-8",
        )
        weight_map = {
            "model.visual.blocks.0.attn.qkv.weight": "visual.safetensors",
            "model.language_model.layers.1.ple.key_proj.weight": "ple.safetensors",
        }
        (artifact / "model.safetensors.index.json").write_text(
            json.dumps({"weight_map": weight_map}), encoding="utf-8"
        )
        for shard in set(weight_map.values()):
            (artifact / shard).write_bytes(b"weight")
        MODEL.verify_qwen38_artifact(artifact)
        del weight_map["model.visual.blocks.0.attn.qkv.weight"]
        (artifact / "model.safetensors.index.json").write_text(
            json.dumps({"weight_map": weight_map}), encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "视觉编码器"):
            MODEL.verify_qwen38_artifact(artifact)

    def test_qwen38_quick_plan_owns_public_id_and_builds_four_tp2_instances(self):
        """一键计划必须用全部八卡替换旧模型，并让 gdn-inside 成为真实公开 ID。"""

        runner = mock.Mock(spec=MODEL.CommandRunner)
        runner.run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        manager = MODEL.DeploymentManager(self.paths, runner=runner)
        resources = {
            "memory_total_bytes": 512 * 1024**3,
            "memory_available_bytes": 470 * 1024**3,
            "disk_free_bytes": 600 * 1024**3,
        }
        pairing = {
            "groups": [[0, 2], [1, 3], [4, 6], [5, 7]],
            "links": [],
            "source": "nvidia-smi",
        }
        with mock.patch.object(MODEL, "gpu_inventory", return_value=self.gpus), \
             mock.patch.object(MODEL, "host_resource_snapshot", return_value=resources), \
             mock.patch.object(MODEL, "qwen38_gpu_groups", return_value=pairing):
            plan = manager.plan_qwen38({})

        deployment = plan["normalized_request"]["deployment"]
        artifact = plan["normalized_request"]["artifact"]
        self.assertEqual(artifact["hub"], "modelscope")
        self.assertEqual(artifact["model_id"], "RadixArk/Qwen3.8-Flash-Next-NVFP4")
        self.assertEqual(
            artifact["revision"], "a6cc3dfc4d4d4617b6ede29f53e751215510e681"
        )
        self.assertEqual(deployment["public_model_ids"], ["gdn-inside"])
        self.assertEqual(deployment["served_model_name"], "gdn-inside")
        self.assertEqual(deployment["runtime"]["tensor_parallel_size"], 2)
        self.assertTrue(deployment["runtime"]["supports_image_input"])
        self.assertEqual(
            json.loads(deployment["runtime"]["mm_limit"]),
            {"image": 4, "video": 0},
        )
        self.assertEqual(
            [item["gpu_devices"] for item in deployment["instances"]],
            pairing["groups"],
        )
        self.assertEqual(plan["requested_gpu_ids"], list(range(8)))
        self.assertNotIn("gdn-inside", plan["legacy_aliases"])
        self.assertEqual(plan["source_registry_revision"], 0)

    def test_qwen38_quick_visual_limit_is_server_owned_and_bounded(self):
        """图片上限必须由后端写入 vLLM 参数，并拒绝资源风险过大的值。"""

        manager = MODEL.DeploymentManager(self.paths, runner=mock.Mock())
        with mock.patch.object(
            MODEL,
            "qwen38_gpu_groups",
            return_value={
                "groups": [[0, 1], [2, 3], [4, 5], [6, 7]],
                "links": [],
                "source": "nvidia-smi",
            },
        ):
            request, _pairing = manager._qwen38_request(
                {"max_images_per_request": 6}, self.gpus
            )
            self.assertEqual(
                json.loads(request["mm_limit"]), {"image": 6, "video": 0}
            )
            with self.assertRaisesRegex(ValueError, "每请求最大图片数"):
                manager._qwen38_request({"max_images_per_request": 17}, self.gpus)

    def test_qwen38_quick_submit_rejects_stale_plan_and_persists_job_before_start(self):
        """一键按钮只能提交刚预检的状态，后台线程前必须保存回滚所需任务。"""

        runner = mock.Mock(spec=MODEL.CommandRunner)
        runner.run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        manager = MODEL.DeploymentManager(self.paths, runner=runner)
        resources = {
            "memory_total_bytes": 512 * 1024**3,
            "memory_available_bytes": 470 * 1024**3,
            "disk_free_bytes": 600 * 1024**3,
        }
        pairing = {
            "groups": [[0, 1], [2, 3], [4, 5], [6, 7]],
            "links": [],
            "source": "nvidia-smi",
        }
        patches = (
            mock.patch.object(MODEL, "gpu_inventory", return_value=self.gpus),
            mock.patch.object(MODEL, "host_resource_snapshot", return_value=resources),
            mock.patch.object(MODEL, "qwen38_gpu_groups", return_value=pairing),
        )
        with patches[0], patches[1], patches[2]:
            with self.assertRaisesRegex(ValueError, "当前部署已变化"):
                manager.submit_qwen38({"expected_registry_revision": 99})
            with mock.patch.object(MODEL.threading, "Thread") as thread:
                job = manager.submit_qwen38({"expected_registry_revision": 0})
        stored = manager.jobs.get(job["id"])
        self.assertEqual(stored["kind"], "qwen38")
        self.assertEqual(stored["source_registry_revision"], 0)
        self.assertEqual(stored["request"]["deployment"]["served_model_name"], "gdn-inside")
        thread.return_value.start.assert_called_once()

    def test_qwen38_quick_job_runs_real_inference_before_publication(self):
        """一键任务必须在 gdn-inside 上线前让四个实例完成真实生成。"""

        runner = mock.Mock(spec=MODEL.CommandRunner)
        runner.run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        manager = MODEL.DeploymentManager(self.paths, runner=runner)
        pairing = {
            "groups": [[0, 1], [2, 3], [4, 5], [6, 7]],
            "links": [],
            "source": "nvidia-smi",
        }
        with mock.patch.object(MODEL, "qwen38_gpu_groups", return_value=pairing):
            request, _pairing = manager._qwen38_request({}, self.gpus)
        normalized = MODEL.normalize_request(request, self.paths)
        artifact = Path(normalized["artifact"]["path"])
        artifact.mkdir(parents=True)
        (artifact / "config.json").write_text("{}\n", encoding="utf-8")
        (artifact / "model.safetensors").write_bytes(b"weight")
        job = manager.jobs.create(normalized, kind="qwen38")
        order = []

        def apply_candidate(candidate, _affected, _deployment_id):
            candidate["revision"] = int(candidate.get("revision", 0)) + 1

        with mock.patch.object(manager, "_verify_runtime_image"), \
             mock.patch.object(MODEL, "verify_qwen38_artifact"), \
             mock.patch.object(manager, "_backup_runtime", return_value=self.root / "backup"), \
             mock.patch.object(manager, "_apply_candidate", side_effect=apply_candidate), \
             mock.patch.object(manager, "_start_and_wait"), \
             mock.patch.object(
                 manager,
                 "_verify_instances",
                 side_effect=lambda *_args, **kwargs: order.append(
                     ("verify", kwargs.get("inference"))
                 ),
             ), \
             mock.patch.object(
                 manager, "_reconcile_gateway", side_effect=lambda: order.append(("publish", None))
             ):
            manager._run_job(job["id"])

        stored = manager.jobs.get(job["id"])
        self.assertEqual(order, [("verify", True), ("publish", None)])
        self.assertEqual(stored["state"], "succeeded")
        self.assertEqual(stored["result_registry_revision"], 1)
        self.assertIn("一键恢复", stored["message"])

    def test_qwen38_topology_pairing_prefers_shorter_nonadjacent_links(self):
        """GPU 编号相邻不是最短链路时，应按 nvidia-smi 的 PIX 关系配对。"""

        preferred = {frozenset(pair) for pair in ((0, 2), (1, 3), (4, 6), (5, 7))}
        lines = []
        for left in range(8):
            values = []
            for right in range(8):
                if left == right:
                    values.append("X")
                elif frozenset((left, right)) in preferred:
                    values.append("PIX")
                else:
                    values.append("SYS")
            lines.append(f"GPU{left} " + " ".join(values))
        runner = mock.Mock(spec=MODEL.CommandRunner)
        runner.run.return_value = subprocess.CompletedProcess(
            [], 0, stdout="\n".join(lines), stderr=""
        )
        result = MODEL.qwen38_gpu_groups(runner, self.gpus)
        self.assertEqual(
            {frozenset(group) for group in result["groups"]}, preferred
        )
        self.assertEqual(result["source"], "nvidia-smi")

    def test_qwen38_instance_acceptance_uses_real_multi_image_request(self):
        """Qwen 上线验收必须把图片送入视觉编码器，而不是只测文本端点。"""

        manager = MODEL.DeploymentManager(self.paths, runner=mock.Mock())
        with mock.patch.object(
            MODEL,
            "qwen38_gpu_groups",
            return_value={
                "groups": [[0, 1], [2, 3], [4, 5], [6, 7]],
                "links": [],
                "source": "nvidia-smi",
            },
        ):
            request, _pairing = manager._qwen38_request({}, self.gpus)
        normalized = MODEL.normalize_request(request, self.paths)
        candidate, _affected = MODEL.merge_deployment(
            manager.registry.read(), normalized
        )
        with mock.patch.object(MODEL, "endpoint_healthy", return_value=True), \
             mock.patch.object(MODEL, "endpoint_inference_ready", return_value=True) as probe:
            manager._verify_instances(
                candidate, "qwen38-flash-next", inference=True
            )
        self.assertEqual(probe.call_count, 4)
        self.assertTrue(all(call.kwargs["image_count"] == 2 for call in probe.call_args_list))

    def test_qwen38_quick_page_blocks_unsafe_host_capacity(self):
        """主内存或模型盘不足时，一键页面必须禁用部署而不是让任务中途 OOM。"""

        runner = mock.Mock(spec=MODEL.CommandRunner)
        runner.run.return_value = subprocess.CompletedProcess([], 1, stdout="", stderr="")
        manager = MODEL.DeploymentManager(self.paths, runner=runner)
        resources = {
            "memory_total_bytes": 256 * 1024**3,
            "memory_available_bytes": 200 * 1024**3,
            "disk_free_bytes": 100 * 1024**3,
        }
        pairing = {
            "groups": [[0, 1], [2, 3], [4, 5], [6, 7]],
            "links": [],
            "source": "nvidia-smi",
        }
        with mock.patch.object(MODEL, "host_resource_snapshot", return_value=resources), \
             mock.patch.object(MODEL, "qwen38_gpu_groups", return_value=pairing):
            snapshot = manager.qwen38_quick_snapshot(inventory=self.gpus)
        self.assertFalse(snapshot["available"])
        self.assertTrue(any("主内存不足" in item for item in snapshot["blockers"]))
        self.assertTrue(any("模型盘" in item for item in snapshot["blockers"]))

    def test_qwen38_workers_load_sequentially_to_bound_host_memory(self):
        """四个 PLE 实例必须逐个健康后再启动下一个，避免并行加载耗尽主内存。"""

        events = []
        runner = mock.Mock(spec=MODEL.CommandRunner)

        def run(command, **_kwargs):
            if command[:2] == ["systemctl", "start"]:
                events.append(("start", command[2]))
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        runner.run.side_effect = run
        manager = MODEL.DeploymentManager(self.paths, runner=runner)
        pairing = {
            "groups": [[0, 1], [2, 3], [4, 5], [6, 7]],
            "links": [],
            "source": "nvidia-smi",
        }
        with mock.patch.object(MODEL, "qwen38_gpu_groups", return_value=pairing):
            request, _pairing = manager._qwen38_request({}, self.gpus)
        normalized = MODEL.normalize_request(request, self.paths)
        candidate, affected = MODEL.merge_deployment(manager.registry.read(), normalized)

        def healthy(origin, _secrets):
            events.append(("healthy", origin))
            return True

        with mock.patch.object(MODEL, "endpoint_healthy", side_effect=healthy):
            manager._start_and_wait(candidate, affected)
        self.assertEqual(
            events,
            [
                ("start", "llm-worker@0.service"), ("healthy", "http://127.0.0.1:8100"),
                ("start", "llm-worker@1.service"), ("healthy", "http://127.0.0.1:8101"),
                ("start", "llm-worker@2.service"), ("healthy", "http://127.0.0.1:8102"),
                ("start", "llm-worker@3.service"), ("healthy", "http://127.0.0.1:8103"),
            ],
        )

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
            ["实例 worker-0 真实文本生成通过", "实例 worker-1 真实文本生成通过"],
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

    def test_qwen38_modelscope_weights_bypass_saved_international_proxy(self):
        """ModelScope 大权重应国内直连，维护代理只可用于准备下载器。"""

        self.paths.proxy_env.write_text(
            "MAINTENANCE_PROXY=http://127.0.0.1:1802\n",
            encoding="utf-8",
        )
        manager = MODEL.DeploymentManager(self.paths)
        command_runner = mock.Mock()
        command_runner.run.return_value = subprocess.CompletedProcess([], 0)
        artifact = {
            "hub": "modelscope",
            "model_id": "RadixArk/Qwen3.8-Flash-Next-NVFP4",
            "revision": "a6cc3dfc4d4d4617b6ede29f53e751215510e681",
            "path": str(self.models / "qwen38-ms"),
        }
        with mock.patch.object(
            manager,
            "_ensure_modelscope_downloader",
            return_value=Path("/usr/local/bin/ms"),
        ), mock.patch.object(
            MODEL, "CommandRunner", return_value=command_runner
        ), mock.patch.object(MODEL, "verify_artifact"):
            manager._download_artifact(artifact, {"image": "unused"}, {})
        environment = command_runner.run.call_args.kwargs["env"]
        self.assertNotIn("HTTPS_PROXY", environment)
        self.assertNotIn("https_proxy", environment)
        self.assertNotIn("ALL_PROXY", environment)
        self.assertIn("--revision", command_runner.run.call_args.args[0])

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

    def test_command_runner_includes_last_output_lines_on_failure(self):
        """网关助手失败必须把真实原因带回任务，不能只留下退出码 1。"""

        completed = subprocess.CompletedProcess(
            args=["gateway-helper"],
            returncode=1,
            stdout="preparing\nOmniRoute combo gdn-inside ownership conflict\n",
        )
        with mock.patch.object(MODEL.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(
                RuntimeError, "gdn-inside ownership conflict"
            ):
                MODEL.CommandRunner().run(["gateway-helper"])

    def test_gateway_reconcile_derives_local_url_for_systemd_controller(self):
        """后台服务没有临时 Shell 变量时必须从 cluster.env 构造 OmniRoute 地址。"""

        runner = mock.Mock()
        runner.run.return_value = subprocess.CompletedProcess(
            args=["gateway-helper"], returncode=0, stdout=""
        )
        manager = MODEL.DeploymentManager(self.paths, runner=runner)
        with mock.patch.dict(MODEL.os.environ, {}, clear=True):
            manager._reconcile_gateway()
        environment = runner.run.call_args.kwargs["env"]
        self.assertEqual(environment["GATEWAY_LOCAL_URL"], "http://127.0.0.1:18000")

    def test_publish_retry_only_reconciles_gateway_and_keeps_workers_untouched(self):
        """部分完成状态应能仅重试路由发布，不重新加载已经验收的模型。"""

        manager = MODEL.DeploymentManager(self.paths)
        manager.registry.write(manager.registry.read())
        job = manager.jobs.create({"registry_revision": 0}, kind="publish")
        manager._reconcile_gateway = mock.Mock()
        manager._run_publish_job(job["id"])
        saved = manager.jobs.get(job["id"])
        self.assertEqual(saved["state"], "succeeded")
        self.assertEqual(saved["progress"], 100)
        self.assertIn("Worker 未重启", saved["message"])
        manager._reconcile_gateway.assert_called_once_with()

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
            MODEL_VERIFICATION.urllib.request,
            "urlopen",
            return_value=Response(
                {"choices": [{"message": {"content": "OK"}}]}
            ),
        ):
            self.assertTrue(
                MODEL.endpoint_inference_ready(
                    "http://127.0.0.1:8100",
                    "test-internal-key",
                    "ornith-1.5-35b-a3b-fp8",
                )
            )
        with mock.patch.object(
            MODEL_VERIFICATION.urllib.request,
            "urlopen",
            return_value=Response({"choices": [{"message": {"content": "OK"}}]}),
        ) as urlopen:
            self.assertTrue(
                MODEL.endpoint_inference_ready(
                    "http://127.0.0.1:8100",
                    "test-internal-key",
                    "gdn-inside",
                    image_count=2,
                )
            )
            request_body = json.loads(urlopen.call_args.args[0].data)
            content = request_body["messages"][0]["content"]
            self.assertEqual(
                sum(item.get("type") == "image_url" for item in content), 2
            )
            self.assertTrue(
                all(
                    item["image_url"]["url"].startswith("data:image/png;base64,")
                    for item in content
                    if item.get("type") == "image_url"
                )
            )
        with mock.patch.object(
            MODEL_VERIFICATION.urllib.request,
            "urlopen",
            return_value=Response(
                {"choices": [{"message": {"content": None, "reasoning": "OK"}}]}
            ),
        ):
            self.assertTrue(
                MODEL.endpoint_inference_ready(
                    "http://127.0.0.1:8100",
                    "test-internal-key",
                    "ornith-1.5-35b-a3b-fp8",
                )
            )
        detail = []
        with mock.patch.object(
            MODEL_VERIFICATION.urllib.request,
            "urlopen",
            return_value=Response({"error": "model not found"}, status=404),
        ):
            self.assertFalse(
                MODEL.endpoint_inference_ready(
                    "http://127.0.0.1:8100",
                    "test-internal-key",
                    "ornith-1.5-35b-a3b-fp8",
                    detail=detail,
                )
            )
        self.assertEqual(detail, ["HTTP 404"])
        with mock.patch.object(
            MODEL_VERIFICATION.urllib.request,
            "urlopen",
            return_value=Response({"choices": []}),
        ):
            self.assertFalse(
                MODEL.endpoint_inference_ready(
                    "http://127.0.0.1:8100",
                    "test-internal-key",
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
