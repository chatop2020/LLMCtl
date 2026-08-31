#!/usr/bin/env python3
import importlib.util
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "lib" / "gateway_config.py"
SPEC = importlib.util.spec_from_file_location("gateway_config", MODULE_PATH)
gateway = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(gateway)


BASE_ENV = {
    "SERVED_MODEL_NAME": "local-model",
    "WORKER_BASE_PORT": "8100",
    "MAX_NUM_SEQS": "7",
    "MAX_MODEL_LEN": "32768",
    "MAX_OUTPUT_TOKENS": "32768",
    "ROUTING_STRATEGY": "least-busy",
    "GATEWAY_DB_PORT": "15432",
    "POSTGRES_DB": "llm_gateway",
    "POSTGRES_USER": "llmadmin",
    "POSTGRES_PASSWORD": "db-secret",
    "BACKEND_API_KEY": "sk-backend-secret",
    "GATEWAY_API_KEY": "sk-bf-public-secret",
    "BIFROST_ENCRYPTION_KEY": "encryption-secret",
    "UI_USERNAME": "admin",
    "UI_PASSWORD": "admin-pass",
    "SUPPORTS_IMAGE_INPUT": "1",
}


class FakeNewAPIClient:
    def __init__(self):
        self.calls = []
        self.channels = [{"id": 9, "tag": gateway.MANAGED_TAG}]
        self.tokens = []
        self.next_channel = 10
        self.next_token = 21

    def setup(self):
        self.calls.append(("SETUP", ""))

    def login(self):
        self.calls.append(("LOGIN", ""))

    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if method == "PUT" and path == "/api/option/":
            if payload != {"key": "RetryTimes", "value": 1}:
                raise AssertionError(payload)
            return {"success": True}
        if method == "GET" and path.startswith("/api/channel/"):
            return {"success": True, "data": {"items": list(self.channels)}}
        if method == "POST" and path == "/api/channel/":
            channel = dict(payload["channel"])
            channel["id"] = self.next_channel
            self.next_channel += 1
            self.channels.append(channel)
            return {"success": True, "data": None}
        if method == "DELETE" and path.startswith("/api/channel/"):
            channel_id = int(path.rsplit("/", 1)[1])
            self.channels = [item for item in self.channels if item.get("id") != channel_id]
            return {"success": True}
        if method == "GET" and path.startswith("/api/token/"):
            return {"success": True, "data": {"items": list(self.tokens)}}
        if method == "POST" and path == "/api/token/":
            self.tokens.append({"id": self.next_token, "name": payload["name"]})
            self.next_token += 1
            return {"success": True}
        if method == "POST" and path.startswith("/api/token/") and path.endswith("/key"):
            return {"success": True, "data": {"key": "x" * 48}}
        if method == "DELETE" and path.startswith("/api/token/"):
            token_id = int(path.rsplit("/", 1)[1])
            self.tokens = [item for item in self.tokens if item.get("id") != token_id]
            return {"success": True}
        raise AssertionError((method, path, payload))


class FakeOmniRouteClient:
    def __init__(self):
        self.calls = []
        self.nodes = [
            {"id": "node-0", "name": "LLMCtl worker 0"},
            {"id": "node-stale", "name": "LLMCtl worker 7"},
        ]
        self.connections = [
            {"id": "conn-0", "provider": "node-0"},
            {"id": "conn-duplicate", "provider": "node-0"},
            {"id": "conn-stale", "provider": "node-stale"},
        ]
        self.models = {"node-0": [{"id": "local-model"}], "node-stale": []}
        self.combo = {
            "id": "combo-1",
            "name": "local-model",
            "description": gateway.OMNIROUTE_DESCRIPTION,
        }
        self.combos = [self.combo]
        self.reasoning_rules = []
        self.next_reasoning_rule = 1
        self.reasoning_rules_api_available = True
        self.model_aliases = {"admin-alias": "admin-model"}

    def login(self):
        self.calls.append(("LOGIN", "", None))

    def management_key_works(self, key):
        return key == BASE_ENV["GATEWAY_API_KEY"]

    def request(self, method, path, payload=None, bearer=""):
        self.calls.append((method, path, payload))
        if (method, path) == ("GET", "/api/provider-nodes?limit=200"):
            return {"nodes": list(self.nodes)}
        if (method, path) == ("PATCH", "/api/settings"):
            return {"visionBridgeEnabled": payload["visionBridgeEnabled"]}
        if (method, path) == ("GET", "/api/providers?limit=200"):
            return {"connections": list(self.connections)}
        if method == "PUT" and path.startswith("/api/provider-nodes/"):
            node_id = path.rsplit("/", 1)[1]
            return {"node": {"id": node_id, **payload}}
        if (method, path) == ("POST", "/api/provider-nodes"):
            node = {"id": "node-1", **payload}
            self.nodes.append(node)
            return {"node": node}
        if method == "PUT" and path.startswith("/api/providers/"):
            connection_id = path.rsplit("/", 1)[1]
            current = next(item for item in self.connections if item["id"] == connection_id)
            current.update(payload)
            return {"connection": dict(current)}
        if (method, path) == ("POST", "/api/providers"):
            connection = {"id": "conn-1", **payload}
            self.connections.append(connection)
            return {"connection": connection}
        if method == "GET" and path.startswith("/api/provider-models?"):
            provider = path.split("provider=", 1)[1]
            return {"models": list(self.models.get(provider, []))}
        if method in {"POST", "PUT"} and path == "/api/provider-models":
            return {"model": payload}
        if (method, path) == ("GET", "/api/combos?limit=200"):
            return {"combos": [dict(item) for item in self.combos]}
        if (method, path) == (
            "GET",
            "/api/settings/reasoning-routing-rules",
        ):
            if not self.reasoning_rules_api_available:
                raise RuntimeError(
                    "OmniRoute GET /api/settings/reasoning-routing-rules returned "
                    "HTTP 404: unknown_route"
                )
            return {"rules": [dict(item) for item in self.reasoning_rules]}
        if (method, path) == ("GET", "/api/settings/model-aliases"):
            return {"custom": dict(self.model_aliases)}
        if (method, path) == ("POST", "/api/settings/model-aliases"):
            self.model_aliases[str(payload["from"])] = str(payload["to"])
            return {"success": True, "custom": dict(self.model_aliases)}
        if (method, path) == ("DELETE", "/api/settings/model-aliases"):
            self.model_aliases.pop(str(payload["from"]), None)
            return {"success": True, "custom": dict(self.model_aliases)}
        if (method, path) == (
            "POST",
            "/api/settings/reasoning-routing-rules",
        ):
            rule = {"id": f"reasoning-{self.next_reasoning_rule}", **payload}
            self.next_reasoning_rule += 1
            self.reasoning_rules.append(rule)
            return {"rule": dict(rule)}
        if method == "PATCH" and path.startswith(
            "/api/settings/reasoning-routing-rules/"
        ):
            rule_id = path.rsplit("/", 1)[1]
            rule = next(item for item in self.reasoning_rules if item["id"] == rule_id)
            rule.update(payload)
            return {"rule": dict(rule)}
        if method == "DELETE" and path.startswith(
            "/api/settings/reasoning-routing-rules/"
        ):
            rule_id = path.rsplit("/", 1)[1]
            self.reasoning_rules = [
                item for item in self.reasoning_rules if item["id"] != rule_id
            ]
            return {"success": True}
        if method == "PUT" and path.startswith("/api/combos/"):
            combo_id = path.rsplit("/", 1)[1]
            combo = next(item for item in self.combos if item["id"] == combo_id)
            combo.update(payload)
            return dict(combo)
        if (method, path) == ("POST", "/api/combos"):
            combo = {"id": f"combo-{len(self.combos) + 1}", **payload}
            self.combos.append(combo)
            return dict(combo)
        if method == "DELETE" and path.startswith("/api/combos/"):
            combo_id = path.rsplit("/", 1)[1]
            self.combos = [item for item in self.combos if item["id"] != combo_id]
            return {"success": True}
        if method == "DELETE" and path.startswith("/api/providers/"):
            connection_id = path.rsplit("/", 1)[1]
            self.connections = [item for item in self.connections if item["id"] != connection_id]
            return {"success": True}
        if method == "DELETE" and path.startswith("/api/provider-nodes/"):
            node_id = path.rsplit("/", 1)[1]
            self.nodes = [item for item in self.nodes if item["id"] != node_id]
            return {"success": True}
        raise AssertionError((method, path, payload))


class GatewayConfigTests(unittest.TestCase):
    def test_model_registry_accepts_account_portal_managed_combo(self):
        """两个 LLMCtl 控制面必须互认 Combo 所有权，避免发布阶段冲突。"""

        self.assertTrue(
            gateway.is_llmctl_managed_combo(
                {"description": gateway.OMNIROUTE_DESCRIPTION}
            )
        )
        self.assertTrue(
            gateway.is_llmctl_managed_combo(
                {"description": gateway.PORTAL_PUBLIC_COMBO_DESCRIPTION}
            )
        )
        self.assertFalse(
            gateway.is_llmctl_managed_combo({"description": "user managed"})
        )

    def test_render_litellm_has_one_backend_per_worker_and_no_plaintext_secret(self):
        with mock.patch.dict(os.environ, BASE_ENV, clear=True):
            content = gateway.litellm_config([0, 1, 7])
        self.assertEqual(content.count('model_name: "local-model"'), 3)
        self.assertIn("http://127.0.0.1:8100/v1", content)
        self.assertIn("http://127.0.0.1:8107/v1", content)
        self.assertIn("os.environ/BACKEND_API_KEY", content)
        self.assertNotIn("sk-backend-secret", content)

    def test_gateway_metadata_never_advertises_output_above_worker_hard_limit(self):
        """接入层必须区分 256K 上下文与 32K 单次生成硬上限。"""

        environment = {**BASE_ENV, "MAX_MODEL_LEN": "262144"}
        with mock.patch.dict(os.environ, environment, clear=True):
            content = gateway.litellm_config([0])
        self.assertIn("max_input_tokens: 262144", content)
        self.assertIn("max_output_tokens: 32768", content)

    def test_gateway_rejects_output_limit_above_platform_ceiling(self):
        """root 配置也不能把服务端硬上限提高到 32768 以上。"""

        environment = {**BASE_ENV, "MAX_OUTPUT_TOKENS": "32769"}
        with mock.patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(SystemExit, "between 1 and 32768"):
                gateway.litellm_config([0])

    def test_render_bifrost_has_weighted_vllm_keys_postgres_and_virtual_key(self):
        with mock.patch.dict(os.environ, BASE_ENV, clear=True):
            config = json.loads(gateway.bifrost_config(range(8)))
        keys = config["providers"]["vllm"]["keys"]
        self.assertEqual(len(keys), 8)
        self.assertEqual([key["weight"] for key in keys], [1.0] * 8)
        self.assertEqual(keys[7]["vllm_key_config"]["url"], "http://127.0.0.1:8107")
        self.assertEqual(keys[0]["value"], "env.BACKEND_API_KEY")
        self.assertEqual(config["config_store"]["type"], "postgres")
        self.assertEqual(config["logs_store"]["type"], "postgres")
        virtual_key = config["governance"]["virtual_keys"][0]
        self.assertEqual(virtual_key["value"], "env.GATEWAY_API_KEY")
        self.assertEqual(virtual_key["provider_configs"][0]["allowed_models"], ["local-model"])
        serialized = json.dumps(config)
        self.assertNotIn("sk-backend-secret", serialized)
        self.assertNotIn("sk-bf-public-secret", serialized)

    def test_render_newapi_plan_is_auditable_and_non_secret(self):
        with mock.patch.dict(os.environ, BASE_ENV, clear=True):
            plan = json.loads(gateway.newapi_plan([0, 7]))
        self.assertEqual(plan["gateway"], "newapi")
        self.assertEqual(plan["retry_times"], 1)
        self.assertEqual(plan["channels"][1]["base_url"], "http://127.0.0.1:8107")
        self.assertNotIn("key", json.dumps(plan).lower())

    def test_render_omniroute_plan_has_isolated_worker_targets_without_secrets(self):
        with mock.patch.dict(os.environ, BASE_ENV, clear=True):
            plan = json.loads(gateway.omniroute_plan([0, 7]))
        self.assertEqual(plan["gateway"], "omniroute")
        self.assertEqual(plan["strategy"], "round-robin")
        self.assertEqual(plan["sticky_round_robin_limit"], 1)
        self.assertEqual(
            plan["concurrency_per_worker"], gateway.OMNIROUTE_INFLIGHT_PER_WORKER
        )
        self.assertEqual(plan["queue_timeout_ms"], gateway.OMNIROUTE_QUEUE_TIMEOUT_MS)
        self.assertTrue(plan["supports_vision"])
        self.assertFalse(plan["vision_bridge_enabled"])
        self.assertEqual(plan["workers"][1]["base_url"], "http://127.0.0.1:8107/v1")
        self.assertNotIn("sk-backend-secret", json.dumps(plan))

    def test_omniroute_reconcile_commits_combo_before_removing_stale_resources(self):
        client = FakeOmniRouteClient()
        with tempfile.TemporaryDirectory() as directory:
            secrets = pathlib.Path(directory) / "secrets.env"
            secrets.write_text("GATEWAY_API_KEY=sk-bf-public-secret\n", encoding="utf-8")
            with mock.patch.dict(os.environ, BASE_ENV, clear=True):
                gateway.reconcile_omniroute(client, [0, 1], secrets)
        combo_position = next(
            index
            for index, call in enumerate(client.calls)
            if call[0:2] == ("PUT", "/api/combos/combo-1")
        )
        cleanup_positions = [
            index
            for index, call in enumerate(client.calls)
            if call[0] == "DELETE"
        ]
        self.assertTrue(cleanup_positions)
        self.assertLess(combo_position, min(cleanup_positions))
        self.assertEqual({item["id"] for item in client.nodes}, {"node-0", "node-1"})
        self.assertEqual({item["id"] for item in client.connections}, {"conn-0", "conn-1", "conn-stale"})
        update_connection = next(
            call for call in client.calls if call[0:2] == ("PUT", "/api/providers/conn-0")
        )
        self.assertEqual(update_connection[2]["apiKey"], "sk-backend-secret")
        self.assertEqual(
            update_connection[2]["maxConcurrent"], gateway.OMNIROUTE_INFLIGHT_PER_WORKER
        )
        create_connection = next(
            call for call in client.calls if call[0:2] == ("POST", "/api/providers")
        )
        self.assertEqual(
            create_connection[2]["maxConcurrent"], gateway.OMNIROUTE_INFLIGHT_PER_WORKER
        )
        self.assertEqual(client.combo["strategy"], "round-robin")
        self.assertTrue(client.combo["config"]["disableSessionStickiness"])
        self.assertEqual(client.combo["config"]["stickyRoundRobinLimit"], 1)
        self.assertEqual(
            client.combo["config"]["concurrencyPerModel"],
            gateway.OMNIROUTE_INFLIGHT_PER_WORKER,
        )
        self.assertEqual(
            client.combo["config"]["queueTimeoutMs"], gateway.OMNIROUTE_QUEUE_TIMEOUT_MS
        )
        self.assertNotIn("queueDepth", client.combo["config"])
        self.assertIn(
            ("PATCH", "/api/settings", {"visionBridgeEnabled": False}),
            client.calls,
        )
        model_updates = [
            call[2]
            for call in client.calls
            if call[0] in {"POST", "PUT"} and call[1] == "/api/provider-models"
        ]
        self.assertTrue(model_updates)
        self.assertTrue(
            all(payload["max_output_tokens"] == 32768 for payload in model_updates)
        )

    def test_omniroute_reconcile_leaves_vision_bridge_untouched_for_text_model(self):
        client = FakeOmniRouteClient()
        environment = {**BASE_ENV, "SUPPORTS_IMAGE_INPUT": "0"}
        with tempfile.TemporaryDirectory() as directory:
            secrets = pathlib.Path(directory) / "secrets.env"
            secrets.write_text("GATEWAY_API_KEY=sk-bf-public-secret\n", encoding="utf-8")
            with mock.patch.dict(os.environ, environment, clear=True):
                gateway.reconcile_omniroute(client, [0, 1], secrets)
        self.assertFalse(
            any(call[0:2] == ("PATCH", "/api/settings") for call in client.calls)
        )

    def test_registry_reconcile_excludes_and_removes_disabled_deployment_routes(self):
        """停用部署不得留在公开 Combo，相关连接和节点也必须被清理。"""

        client = FakeOmniRouteClient()
        client.nodes = [
            {"id": "node-qwen", "name": "LLMCtl qwen38-flash-next worker 0"},
            {"id": "node-ornith", "name": "LLMCtl legacy worker 4"},
        ]
        client.connections = [
            {"id": "conn-qwen", "provider": "node-qwen"},
            {"id": "conn-ornith", "provider": "node-ornith"},
        ]
        client.models = {
            "node-qwen": [{"id": "gdn-inside"}],
            "node-ornith": [{"id": "ornith-1.0-35b-fp8"}],
        }
        client.combo = {
            "id": "combo-qwen",
            "name": "gdn-inside",
            "description": gateway.OMNIROUTE_DESCRIPTION,
        }
        client.combos = [
            client.combo,
            {
                "id": "combo-ornith",
                "name": "ornith-1.0-35b-fp8",
                "description": gateway.OMNIROUTE_DESCRIPTION,
            },
        ]
        registry = {
            "schema_version": 1,
            "legacy_aliases": {},
            "deployments": {
                "qwen38-flash-next": {
                    "enabled": True,
                    "publish_requested": True,
                    "model_id": "RadixArk/Qwen3.8-Flash-Next-NVFP4",
                    "served_model_name": "gdn-inside",
                    "public_model_ids": ["gdn-inside"],
                    "runtime": {
                        "max_model_len": 262144,
                        "supports_image_input": True,
                    },
                    "instances": [
                        {
                            "id": "qwen-worker-0",
                            "kind": "local",
                            "worker_id": 0,
                            "port": 8100,
                            "enabled": True,
                        }
                    ],
                },
                "legacy": {
                    "enabled": False,
                    "publish_requested": True,
                    "served_model_name": "ornith-1.0-35b-fp8",
                    "public_model_ids": ["ornith-1.0-35b-fp8"],
                    "runtime": {
                        "max_model_len": 262144,
                        "supports_image_input": True,
                    },
                    "instances": [
                        {
                            "id": "ornith-worker-4",
                            "kind": "local",
                            "worker_id": 4,
                            "port": 8104,
                            "enabled": True,
                        }
                    ],
                },
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            registry_file = root / "deployments.json"
            secrets = root / "secrets.env"
            registry_file.write_text(json.dumps(registry), encoding="utf-8")
            secrets.write_text("GATEWAY_API_KEY=sk-bf-public-secret\n", encoding="utf-8")
            with mock.patch.dict(os.environ, BASE_ENV, clear=True):
                specs = gateway.registry_omniroute_specs(registry_file)
                gateway.reconcile_omniroute_registry(client, registry_file, secrets)

        self.assertEqual([item["deployment_id"] for item in specs], ["qwen38-flash-next"])
        self.assertEqual({item["id"] for item in client.nodes}, {"node-qwen"})
        self.assertEqual({item["id"] for item in client.connections}, {"conn-qwen"})
        self.assertEqual({item["id"] for item in client.combos}, {"combo-qwen"})
        self.assertEqual(
            {item["provider"] for item in client.combo["models"]}, {"node-qwen"}
        )
        self.assertEqual(
            {item["model"] for item in client.combo["models"]},
            {"llmctl-native-vision-qwen38-flash-next"},
        )
        self.assertEqual(
            client.model_aliases["llmctl-native-vision-qwen38-flash-next"],
            "gdn-inside",
        )
        self.assertEqual(client.model_aliases["admin-alias"], "admin-model")
        combo_update = next(
            index
            for index, call in enumerate(client.calls)
            if call[0:2] == ("PUT", "/api/combos/combo-qwen")
        )
        connection_delete = next(
            index
            for index, call in enumerate(client.calls)
            if call[0:2] == ("DELETE", "/api/providers/conn-ornith")
        )
        node_delete = next(
            index
            for index, call in enumerate(client.calls)
            if call[0:2] == ("DELETE", "/api/provider-nodes/node-ornith")
        )
        self.assertLess(combo_update, connection_delete)
        self.assertLess(connection_delete, node_delete)

        self.assertEqual(len(client.reasoning_rules), 1)
        qwen_rule = client.reasoning_rules[0]
        self.assertEqual(qwen_rule["scope"], "model")
        self.assertEqual(qwen_rule["modelPattern"], "gdn-inside")
        self.assertEqual(qwen_rule["sourceEffort"], "high")
        self.assertEqual(qwen_rule["effortMode"], "force")
        self.assertEqual(qwen_rule["targetEffort"], "xhigh")
        self.assertEqual(qwen_rule["targetKind"], "keep")

        # 重复同步必须更新同一条规则；切回其他模型时只清理 LLMCtl 自动规则。
        gateway.reconcile_omniroute_reasoning_rules(client, specs)
        self.assertEqual(len(client.reasoning_rules), 1)
        client.reasoning_rules.append(
            {
                "id": "manual-rule",
                "name": "管理员规则",
                "description": "user managed",
            }
        )
        gateway.reconcile_omniroute_reasoning_rules(
            client,
            [
                {
                    "public_model_ids": ["ornith-1.5-35b-a3b-fp8"],
                    "reasoning_effort_aliases": {},
                }
            ],
        )
        self.assertEqual(
            [item["id"] for item in client.reasoning_rules], ["manual-rule"]
        )
        gateway.reconcile_omniroute_model_aliases(client, {})
        self.assertEqual(client.model_aliases, {"admin-alias": "admin-model"})

    def test_reasoning_effort_aliases_reject_unknown_omniroute_values(self):
        """非法等级必须在写入 OmniRoute 前失败，不能留下半有效规则。"""

        deployment = {
            "runtime": {"reasoning_effort_aliases": {"high": "extreme"}}
        }
        with self.assertRaisesRegex(RuntimeError, "目标推理等级"):
            gateway.deployment_reasoning_effort_aliases("custom", deployment)

    def test_old_omniroute_fails_before_mutating_routes_with_upgrade_guidance(self):
        """旧运行镜像缺少规则 API 时必须在任何路由写入前失败关闭。"""

        client = FakeOmniRouteClient()
        client.reasoning_rules_api_available = False
        registry = {
            "schema_version": 1,
            "legacy_aliases": {},
            "deployments": {
                "qwen38-flash-next": {
                    "enabled": True,
                    "publish_requested": True,
                    "model_id": "RadixArk/Qwen3.8-Flash-Next-NVFP4",
                    "served_model_name": "gdn-inside",
                    "public_model_ids": ["gdn-inside"],
                    "runtime": {
                        "max_model_len": 262144,
                        "supports_image_input": True,
                    },
                    "instances": [
                        {
                            "id": "qwen-worker-0",
                            "kind": "local",
                            "worker_id": 0,
                            "port": 8100,
                            "enabled": True,
                        }
                    ],
                }
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            registry_file = root / "deployments.json"
            secrets = root / "secrets.env"
            registry_file.write_text(json.dumps(registry), encoding="utf-8")
            secrets.write_text(
                "GATEWAY_API_KEY=sk-bf-public-secret\n", encoding="utf-8"
            )
            with mock.patch.dict(os.environ, BASE_ENV, clear=True):
                with self.assertRaisesRegex(
                    RuntimeError, "llmctl omniroute status"
                ):
                    gateway.reconcile_omniroute_registry(
                        client, registry_file, secrets
                    )

        mutating_calls = [
            call for call in client.calls if call[0] in {"POST", "PUT", "PATCH", "DELETE"}
        ]
        self.assertEqual(mutating_calls, [])

        recovery_client = FakeOmniRouteClient()
        recovery_client.reasoning_rules_api_available = False
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            registry_file = root / "deployments.json"
            secrets = root / "secrets.env"
            registry_file.write_text(json.dumps(registry), encoding="utf-8")
            secrets.write_text(
                "GATEWAY_API_KEY=sk-bf-public-secret\n", encoding="utf-8"
            )
            environment = {
                **BASE_ENV,
                "LLMCTL_ALLOW_LEGACY_OMNIROUTE": "1",
            }
            with mock.patch.dict(os.environ, environment, clear=True):
                gateway.reconcile_omniroute_registry(
                    recovery_client, registry_file, secrets
                )

        self.assertTrue(
            any(
                call[0:2] == ("POST", "/api/combos")
                and call[2].get("name") == "gdn-inside"
                for call in recovery_client.calls
            )
        )
        self.assertEqual(recovery_client.reasoning_rules, [])
        recovery_combo = next(
            item for item in recovery_client.combos if item["name"] == "gdn-inside"
        )
        self.assertEqual(
            {item["model"] for item in recovery_combo["models"]}, {"gdn-inside"}
        )

    def test_newapi_reconcile_creates_replacements_before_deleting_old_routes(self):
        client = FakeNewAPIClient()
        with tempfile.TemporaryDirectory() as directory:
            secrets = pathlib.Path(directory) / "secrets.env"
            secrets.write_text("KEEP=yes\nGATEWAY_API_KEY=old\n", encoding="utf-8")
            with mock.patch.dict(os.environ, BASE_ENV, clear=True):
                gateway.reconcile_newapi(client, [0, 1], secrets)
            persisted = secrets.read_text(encoding="utf-8")
        create_positions = [
            index for index, call in enumerate(client.calls)
            if call[0:2] == ("POST", "/api/channel/")
        ]
        delete_position = next(
            index for index, call in enumerate(client.calls)
            if call[0:2] == ("DELETE", "/api/channel/9")
        )
        self.assertLess(max(create_positions), delete_position)
        self.assertEqual([item["base_url"] for item in client.channels], [
            "http://127.0.0.1:8100", "http://127.0.0.1:8101"
        ])
        self.assertIn("GATEWAY_API_KEY=sk-" + "x" * 48, persisted)
        self.assertIn("LITELLM_MASTER_KEY=sk-" + "x" * 48, persisted)
        self.assertIn("NEWAPI_MANAGED_TOKEN_ID=21", persisted)
        self.assertIn("KEEP=yes", persisted)

    def test_newapi_reconcile_preserves_old_routes_when_replacement_creation_fails(self):
        client = FakeNewAPIClient()
        original_request = client.request

        def fail_second_channel(method, path, payload=None):
            if method == "POST" and path == "/api/channel/" and client.next_channel == 11:
                raise RuntimeError("injected channel failure")
            return original_request(method, path, payload)

        client.request = fail_second_channel
        with tempfile.TemporaryDirectory() as directory:
            secrets = pathlib.Path(directory) / "secrets.env"
            secrets.write_text("GATEWAY_API_KEY=old\n", encoding="utf-8")
            with mock.patch.dict(os.environ, BASE_ENV, clear=True):
                with self.assertRaisesRegex(RuntimeError, "injected"):
                    gateway.reconcile_newapi(client, [0, 1], secrets)
        self.assertTrue(any(item.get("id") == 9 for item in client.channels))

    def test_newapi_rotation_creates_and_persists_new_token_before_deleting_old(self):
        client = FakeNewAPIClient()
        client.tokens = [{"id": 20, "name": gateway.MANAGED_TOKEN}]
        with tempfile.TemporaryDirectory() as directory:
            secrets = pathlib.Path(directory) / "secrets.env"
            secrets.write_text("GATEWAY_API_KEY=old\n", encoding="utf-8")
            with mock.patch.dict(os.environ, BASE_ENV, clear=True):
                gateway.ensure_newapi_token(client, secrets, rotate=True)
            persisted = secrets.read_text(encoding="utf-8")
        create_position = next(
            index for index, call in enumerate(client.calls)
            if call[0:2] == ("POST", "/api/token/")
        )
        delete_position = next(
            index for index, call in enumerate(client.calls)
            if call[0:2] == ("DELETE", "/api/token/20")
        )
        self.assertLess(create_position, delete_position)
        self.assertEqual(client.tokens, [{"id": 21, "name": gateway.MANAGED_TOKEN}])
        self.assertIn("NEWAPI_MANAGED_TOKEN_ID=21", persisted)

    def test_worker_id_parser_rejects_empty_and_non_numeric_values(self):
        with self.assertRaises(SystemExit):
            gateway.parse_worker_ids("")
        with self.assertRaises(SystemExit):
            gateway.parse_worker_ids("0,nope")


if __name__ == "__main__":
    unittest.main()
