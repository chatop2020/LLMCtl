#!/usr/bin/env python3
"""验证管理员重置普通用户密码的权限、哈希、会话和审计契约。"""

import json
import unittest

from tests import test_account_portal as portal_tests


portal = portal_tests.portal


class UserPasswordResetTests(unittest.TestCase):
    """复用隔离门户，覆盖密码重置成功和失败后的完整登录行为。"""

    def setUp(self):
        self.fixture = portal_tests.PortalIntegrationTests(
            "test_admin_page_uses_explicit_status_selection_and_separate_sqlite"
        )
        self.fixture.setUp()
        self.fixture.insert_control_user_and_model()
        with self.fixture.server.db.connect() as connection:
            connection.execute(
                "UPDATE users SET login_name=email WHERE id='policy-user'"
            )

    def tearDown(self):
        self.fixture.tearDown()

    def login(self, identity: str, password: str):
        """从新的浏览器会话登录，并返回状态、响应和 Cookie 容器。"""

        client, jar = self.fixture.opener()
        self.fixture.get(client, "/portal-api/public")
        status, body, _ = self.fixture.json_post(
            client,
            jar,
            "/portal-api/auth/login",
            {"identity": identity, "password": password},
        )
        return client, jar, status, body

    def test_admin_reset_rehashes_password_revokes_sessions_and_preserves_api_key(self):
        """新密码生效后旧密码和已有会话必须失效，API Key 保持不变。"""

        user_client, user_jar, status, _ = self.login(
            "policy@example.com", "a secure password 123"
        )
        self.assertEqual(status, 200)
        session_status, session_raw, _ = self.fixture.get(
            user_client, "/portal-api/session"
        )
        self.assertEqual(session_status, 200)
        self.assertTrue(json.loads(session_raw)["authenticated"])

        denied, _, _ = self.fixture.json_post(
            user_client,
            user_jar,
            "/portal-api/admin/users/password/reset",
            {
                "user_id": "policy-user",
                "password": "ReplacementPass456",
                "confirm": "ReplacementPass456",
            },
        )
        self.assertEqual(denied, 403)

        admin_client, admin_jar = self.fixture.login_admin_api()
        reset_status, result, _ = self.fixture.json_post(
            admin_client,
            admin_jar,
            "/portal-api/admin/users/password/reset",
            {
                "user_id": "policy-user",
                "password": "ReplacementPass456",
                "confirm": "ReplacementPass456",
            },
        )
        self.assertEqual(reset_status, 200)
        self.assertEqual(
            result,
            {"ok": True, "user_id": "policy-user", "sessions_revoked": 1},
        )

        _, invalidated_raw, _ = self.fixture.get(user_client, "/portal-api/session")
        self.assertFalse(json.loads(invalidated_raw)["authenticated"])
        _, _, old_status, _ = self.login(
            "policy@example.com", "a secure password 123"
        )
        _, _, new_status, new_body = self.login(
            "policy@example.com", "ReplacementPass456"
        )
        self.assertEqual(old_status, 401)
        self.assertEqual(new_status, 200)
        self.assertEqual(new_body["user"]["id"], "policy-user")

        with self.fixture.server.db.connect() as connection:
            user = connection.execute(
                "SELECT password_hash,api_key_id FROM users WHERE id='policy-user'"
            ).fetchone()
            audit = connection.execute(
                "SELECT action,target,detail FROM audit_events "
                "WHERE action='admin/users/password/reset' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertTrue(portal.verify_password("ReplacementPass456", user["password_hash"]))
        self.assertEqual(user["api_key_id"], "policy-key")
        self.assertEqual(audit["target"], "policy-user")
        self.assertEqual(json.loads(audit["detail"]), {"ok": True})
        self.assertNotIn("ReplacementPass456", self.fixture.db_path.read_text(errors="ignore"))

    def test_invalid_reset_keeps_existing_password_and_session(self):
        """确认不一致或密码不合规时不得修改哈希或撤销会话。"""

        user_client, _, status, _ = self.login(
            "policy@example.com", "a secure password 123"
        )
        self.assertEqual(status, 200)
        with self.fixture.server.db.connect() as connection:
            original_hash = connection.execute(
                "SELECT password_hash FROM users WHERE id='policy-user'"
            ).fetchone()["password_hash"]

        admin_client, admin_jar = self.fixture.login_admin_api()
        mismatch_status, mismatch, _ = self.fixture.json_post(
            admin_client,
            admin_jar,
            "/portal-api/admin/users/password/reset",
            {
                "user_id": "policy-user",
                "password": "ReplacementPass456",
                "confirm": "DifferentPass789",
            },
        )
        numeric_status, numeric, _ = self.fixture.json_post(
            admin_client,
            admin_jar,
            "/portal-api/admin/users/password/reset",
            {
                "user_id": "policy-user",
                "password": "12345678",
                "confirm": "12345678",
            },
        )
        self.assertEqual((mismatch_status, numeric_status), (400, 400))
        self.assertEqual(mismatch["error"], "两次输入的密码不一致")
        self.assertEqual(numeric["error"], "密码不能全部由数字组成")

        with self.fixture.server.db.connect() as connection:
            current_hash = connection.execute(
                "SELECT password_hash FROM users WHERE id='policy-user'"
            ).fetchone()["password_hash"]
        self.assertEqual(current_hash, original_hash)
        _, session_raw, _ = self.fixture.get(user_client, "/portal-api/session")
        self.assertTrue(json.loads(session_raw)["authenticated"])


if __name__ == "__main__":
    unittest.main()
