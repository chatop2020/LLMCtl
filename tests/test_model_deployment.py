import importlib.util
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


if __name__ == "__main__":
    unittest.main()
