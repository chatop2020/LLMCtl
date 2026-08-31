#!/usr/bin/env python3
"""验证账户门户在模型切换后采用活动部署注册表。"""

import json
import os
import pathlib
import unittest
from unittest import mock

from tests import test_account_portal as portal_tests


portal = portal_tests.portal


class PortalRegistrySeedTests(unittest.TestCase):
    """复用隔离门户夹具，验证当前Qwen绑定和旧自动种子退役。"""

    def setUp(self):
        self.fixture = portal_tests.PortalIntegrationTests(
            "test_portal_accepts_model_registry_managed_public_combo"
        )
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def test_registry_seed_rebinds_qwen_and_retires_old_auto_model(self):
        """模型切换后停止迁移已退役自动种子，手工模型不受影响。"""

        fixture = self.fixture
        fixture.insert_control_user_and_model()
        stamp = portal.now()
        with fixture.server.db.connect() as connection:
            connection.execute(
                "UPDATE published_models SET description=? WHERE id='model-1'",
                (portal.AUTO_SEEDED_MODEL_DESCRIPTION,),
            )
            connection.execute(
                """INSERT INTO published_models(
                     id,public_model_id,display_name,description,source_kind,source_ref,
                     source_model,capabilities_json,status,mapping_kind,mapping_id,
                     health_status,health_failures,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "old-ornith", "ornith-1.0-35b-fp8", "Old Ornith",
                    portal.AUTO_SEEDED_MODEL_DESCRIPTION, "combo", "old-combo",
                    "ornith-1.0-35b-fp8", '["chat","vision"]', "published",
                    "combo", "old-mapping", "healthy", 0, stamp, stamp,
                ),
            )
        fixture.fake_omni.combo_items = [
            {
                "id": "qwen-combo",
                "name": "gdn-inside",
                "description": portal.MODEL_REGISTRY_COMBO_MANAGED_DESCRIPTION,
                "models": [
                    {
                        "kind": "model",
                        "providerId": "qwen-worker-0",
                        "modelId": "gdn-inside",
                    }
                ],
                "strategy": "round-robin",
            }
        ]
        registry_path = pathlib.Path(fixture.tempdir.name) / "deployments.json"
        registry_path.write_text(
            json.dumps(
                {
                    "deployments": {
                        "legacy": {
                            "enabled": False,
                            "publish_requested": True,
                            "public_model_ids": ["ornith-1.0-35b-fp8"],
                            "runtime": {"supports_image_input": True},
                        },
                        "qwen38-flash-next": {
                            "enabled": True,
                            "publish_requested": True,
                            "public_model_ids": ["gdn-inside"],
                            "runtime": {
                                "supports_image_input": True,
                                "supports_tool_calling": True,
                                "supports_reasoning": True,
                            },
                        },
                    }
                }
            ),
            encoding="utf-8",
        )
        with mock.patch.dict(
            os.environ, {"LLM_DEPLOYMENT_REGISTRY": str(registry_path)}
        ):
            fixture.server.control.seed_managed_model()
            result = fixture.server.control.reconcile_public_combo_routes()

        self.assertEqual(result, {"migrated": 1, "unchanged": 0, "failed": 0})
        with fixture.server.db.connect() as connection:
            qwen = connection.execute(
                "SELECT source_ref,source_model,mapping_kind,mapping_id,health_status,"
                "capabilities_json FROM published_models WHERE id='model-1'"
            ).fetchone()
            old = connection.execute(
                "SELECT health_status,health_failures FROM published_models "
                "WHERE id='old-ornith'"
            ).fetchone()
        self.assertEqual(qwen["source_ref"], "qwen-combo")
        self.assertEqual(qwen["source_model"], "gdn-inside")
        self.assertEqual(qwen["mapping_kind"], "source-combo")
        self.assertEqual(qwen["mapping_id"], "qwen-combo")
        self.assertEqual(qwen["health_status"], "healthy")
        self.assertEqual(
            json.loads(qwen["capabilities_json"]),
            ["chat", "vision", "tools", "reasoning"],
        )
        self.assertEqual(old["health_status"], "failed")
        self.assertGreaterEqual(old["health_failures"], 3)

    def test_delayed_reconciliation_retries_seed_after_router_becomes_ready(self):
        """Router 延迟出现 Combo 时，对账必须重绑当前模型并退役旧模型。"""

        fixture = self.fixture
        fixture.insert_control_user_and_model()
        stamp = portal.now()
        with fixture.server.db.connect() as connection:
            connection.execute(
                "UPDATE published_models SET description=? WHERE id='model-1'",
                (portal.AUTO_SEEDED_MODEL_DESCRIPTION,),
            )
            connection.execute(
                """INSERT INTO published_models(
                     id,public_model_id,display_name,description,source_kind,source_ref,
                     source_model,capabilities_json,status,mapping_kind,mapping_id,
                     health_status,health_failures,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "old-ornith", "ornith-1.0-35b-fp8", "Old Ornith",
                    portal.AUTO_SEEDED_MODEL_DESCRIPTION, "combo", "old-combo",
                    "ornith-1.0-35b-fp8", '["chat","vision"]', "published",
                    "combo", "old-mapping", "healthy", 0, stamp, stamp,
                ),
            )
        registry_path = pathlib.Path(fixture.tempdir.name) / "deployments.json"
        registry_path.write_text(
            json.dumps(
                {
                    "deployments": {
                        "legacy": {
                            "enabled": False,
                            "public_model_ids": ["ornith-1.0-35b-fp8"],
                        },
                        "qwen38-flash-next": {
                            "enabled": True,
                            "publish_requested": True,
                            "public_model_ids": ["gdn-inside"],
                            "runtime": {"supports_image_input": True},
                        },
                    }
                }
            ),
            encoding="utf-8",
        )
        fixture.fake_omni.combo_items = []
        with mock.patch.dict(
            os.environ, {"LLM_DEPLOYMENT_REGISTRY": str(registry_path)}
        ):
            fixture.server.control.seed_managed_model()
            fixture.fake_omni.combo_items = [
                {
                    "id": "qwen-combo",
                    "name": "gdn-inside",
                    "description": portal.MODEL_REGISTRY_COMBO_MANAGED_DESCRIPTION,
                    "models": [
                        {
                            "kind": "model",
                            "providerId": "qwen-worker-0",
                            "modelId": "llmctl-native-vision-qwen38-flash-next",
                        }
                    ],
                    "strategy": "round-robin",
                }
            ]
            result = fixture.server.control.reconcile_public_combo_routes()

        self.assertEqual(result, {"migrated": 1, "unchanged": 0, "failed": 0})
        with fixture.server.db.connect() as connection:
            qwen = connection.execute(
                "SELECT source_ref,source_model,mapping_kind,mapping_id,health_status "
                "FROM published_models WHERE id='model-1'"
            ).fetchone()
            old = connection.execute(
                "SELECT health_status,health_failures FROM published_models "
                "WHERE id='old-ornith'"
            ).fetchone()
        self.assertEqual(tuple(qwen), ("qwen-combo", "gdn-inside", "source-combo", "qwen-combo", "healthy"))
        self.assertEqual(old["health_status"], "failed")
        self.assertGreaterEqual(old["health_failures"], 3)


if __name__ == "__main__":
    unittest.main()
