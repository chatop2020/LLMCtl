#!/usr/bin/env python3
"""验证部署注册表到 CLI 当前运行配置的唯一投影。"""

import json
import pathlib
import shlex
import subprocess
import tempfile
import textwrap
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "lib/llmctl/registry_runtime.sh"


class RegistryRuntimeTests(unittest.TestCase):
    """覆盖活动部署优先、旧版回退和 systemd 非零状态输出。"""

    def test_active_qwen_registry_overrides_legacy_ornith_inventory(self):
        """活动部署及 Worker 私有参数必须完整覆盖旧版全局模型信息。"""

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            config = root / "config"
            workers = config / "workers"
            artifact = root / "models/qwen"
            workers.mkdir(parents=True)
            artifact.mkdir(parents=True)
            (artifact / "config.json").write_text(
                json.dumps({"architectures": ["Qwen4ExpForConditionalGeneration"]}),
                encoding="utf-8",
            )
            registry = {
                "artifacts": {
                    "ornith": {
                        "hub": "huggingface",
                        "revision": "old",
                        "path": str(root / "models/ornith"),
                    },
                    "qwen": {
                        "hub": "modelscope",
                        "revision": "a" * 40,
                        "path": str(artifact),
                    },
                },
                "deployments": {
                    "legacy": {
                        "enabled": False,
                        "publish_requested": True,
                        "artifact_id": "ornith",
                        "model_id": "ornith-ai/Ornith-1.5-35B-A3B-FP8",
                        "served_model_name": "ornith-old",
                        "public_model_ids": ["ornith-old"],
                        "instances": [],
                    },
                    "qwen38-flash-next": {
                        "enabled": True,
                        "publish_requested": True,
                        "artifact_id": "qwen",
                        "model_id": "RadixArk/Qwen3.8-Flash-Next-NVFP4",
                        "served_model_name": "gdn-inside",
                        "public_model_ids": ["gdn-inside"],
                        "instances": [
                            {
                                "kind": "local",
                                "enabled": True,
                                "worker_id": index,
                            }
                            for index in range(4)
                        ],
                    },
                },
            }
            (config / "deployments.json").write_text(
                json.dumps(registry), encoding="utf-8"
            )
            (workers / "0.env").write_text(
                "\n".join(
                    [
                        "MODEL_ID=RadixArk/Qwen3.8-Flash-Next-NVFP4",
                        "SERVED_MODEL_NAME=gdn-inside",
                        "VLLM_IMAGE=llmctl/qwen38-flash-next:runtime",
                        "TP_SIZE=2",
                        "MAX_MODEL_LEN=262144",
                        "MAX_NUM_SEQS=12",
                        "MAX_NUM_BATCHED_TOKENS=8192",
                        "GPU_MEMORY_UTILIZATION=0.94",
                        "PLE_CPU_OFFLOAD=1",
                        "ENABLE_EXPERT_PARALLEL=1",
                        "ENABLE_PREFIX_CACHING=0",
                        "ENABLE_FLASHINFER_AUTOTUNE=0",
                        "DISABLE_CUSTOM_ALL_REDUCE=0",
                        "MTP_SPECULATIVE_TOKENS=0",
                        "KV_CACHE_DTYPE=auto",
                        "YARN_FACTOR=1",
                        "SUPPORTS_IMAGE_INPUT=1",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            script = textwrap.dedent(
                f"""
                set -Eeuo pipefail
                CONFIG_DIR={shlex.quote(str(config))}
                MODEL_HUB=huggingface; MODEL_ID=ornith-old; MODEL_REVISION=master
                MODEL_ROOT={shlex.quote(str(root / 'models'))}; SERVED_MODEL_NAME=ornith-old
                MODEL_ARCHITECTURE=OldArchitecture; MODEL_PRECISION=fp8; MODEL_TASK=text
                INSTANCE_COUNT=8; ACTIVE_WORKERS=0,1,2,3; SUPPORTS_IMAGE_INPUT=0
                source {shlex.quote(str(RUNTIME))}
                activate_default_published_deployment
                jq -n \
                  --arg deployment "$ACTIVE_DEPLOYMENT_ID" --arg hub "$MODEL_HUB" \
                  --arg model "$MODEL_ID" --arg revision "$MODEL_REVISION" \
                  --arg path "$MODEL_LOCAL_DIR" --arg served "$SERVED_MODEL_NAME" \
                  --arg architecture "$MODEL_ARCHITECTURE" --arg precision "$MODEL_PRECISION" \
                  --arg task "$MODEL_TASK" --arg workers "$ACTIVE_WORKERS" \
                  --arg instances "$INSTANCE_COUNT" --arg tp "$TP_SIZE" \
                  --arg seqs "$MAX_NUM_SEQS" --arg image "$VLLM_IMAGE" \
                  '{{deployment:$deployment,hub:$hub,model:$model,revision:$revision,path:$path,
                    served:$served,architecture:$architecture,precision:$precision,task:$task,
                    workers:$workers,instances:$instances,tp:$tp,seqs:$seqs,image:$image}}'
                """
            )
            completed = subprocess.run(
                ["bash", "-c", script], check=True, text=True, capture_output=True
            )
            payload = json.loads(completed.stdout)

        self.assertEqual(payload["deployment"], "qwen38-flash-next")
        self.assertEqual(payload["hub"], "modelscope")
        self.assertEqual(payload["model"], "RadixArk/Qwen3.8-Flash-Next-NVFP4")
        self.assertEqual(payload["revision"], "a" * 40)
        self.assertEqual(payload["path"], str(artifact))
        self.assertEqual(payload["served"], "gdn-inside")
        self.assertEqual(payload["architecture"], "Qwen4ExpForConditionalGeneration")
        self.assertEqual(payload["precision"], "nvfp4-mixed")
        self.assertEqual(payload["task"], "vision")
        self.assertEqual(payload["workers"], "0,1,2,3")
        self.assertEqual(payload["instances"], "4")
        self.assertEqual(payload["tp"], "2")
        self.assertEqual(payload["seqs"], "12")
        self.assertEqual(payload["image"], "llmctl/qwen38-flash-next:runtime")

    def test_missing_registry_preserves_legacy_values(self):
        """没有部署注册表的旧安装继续使用原有全局配置。"""

        with tempfile.TemporaryDirectory() as directory:
            script = textwrap.dedent(
                f"""
                set -Eeuo pipefail
                CONFIG_DIR={shlex.quote(directory)}
                MODEL_ID=legacy-model; SERVED_MODEL_NAME=legacy-served; INSTANCE_COUNT=8
                source {shlex.quote(str(RUNTIME))}
                activate_default_published_deployment
                printf '%s|%s|%s\n' "$MODEL_ID" "$SERVED_MODEL_NAME" "$INSTANCE_COUNT"
                """
            )
            completed = subprocess.run(
                ["bash", "-c", script], check=True, text=True, capture_output=True
            )

        self.assertEqual(completed.stdout.strip(), "legacy-model|legacy-served|8")

    def test_inactive_systemd_state_is_not_duplicated_with_unknown(self):
        """systemctl 的有效非零状态必须原样显示且只能出现一次。"""

        script = textwrap.dedent(
            f"""
            set -Eeuo pipefail
            systemctl() {{ printf 'inactive\n'; return 3; }}
            source {shlex.quote(str(RUNTIME))}
            systemd_property_state is-active llm-cluster.service
            """
        )
        completed = subprocess.run(
            ["bash", "-c", script], check=True, text=True, capture_output=True
        )

        self.assertEqual(completed.stdout.strip(), "inactive")


if __name__ == "__main__":
    unittest.main()
