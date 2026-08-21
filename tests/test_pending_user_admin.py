import unittest
from unittest import mock

from tests import test_account_portal as account_fixture


class PendingUserAdminTests(unittest.TestCase):
    def setUp(self):
        """复用隔离门户与 Fake OmniRoute，不访问真实 SMTP 或用户数据。"""

        self.portal_case = account_fixture.PortalIntegrationTests("runTest")
        self.portal_case.setUp()

    def tearDown(self):
        self.portal_case.tearDown()

    def insert_pending_user(self, user_id: str = "pending-user") -> tuple[str, str]:
        """创建带旧验证令牌的待验证用户，并返回邮箱和旧令牌哈希。"""

        email = f"{user_id}@example.com"
        stamp = account_fixture.portal.now()
        raw_old_token = f"old-token-{user_id}"
        old_hash = account_fixture.portal.token_hash(raw_old_token)
        with self.portal_case.server.db.connect() as connection:
            connection.execute(
                "INSERT INTO users(id,email,login_name,password_hash,role,status,quota_tokens,quota_reset,quota_reset_time,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    user_id,
                    email,
                    email,
                    account_fixture.portal.hash_password("ValidPassword123"),
                    "user",
                    "pending",
                    0,
                    "monthly",
                    "00:00",
                    stamp - 120,
                ),
            )
            connection.execute(
                "INSERT INTO verification_tokens(token_hash,user_id,expires_at,created_at) "
                "VALUES(?,?,?,?)",
                (old_hash, user_id, stamp + 3600, stamp - 120),
            )
        return email, old_hash

    def test_admin_resend_replaces_old_token_only_after_email_success(self):
        email, old_hash = self.insert_pending_user()
        client, jar = self.portal_case.login_admin_api()
        delivered: list[tuple[str, str]] = []
        with mock.patch.object(
            account_fixture.portal_http,
            "send_verification_email",
            side_effect=lambda _config, recipient, token: delivered.append(
                (recipient, token)
            ),
        ):
            status, result, _ = self.portal_case.json_post(
                client,
                jar,
                "/portal-api/admin/users/verification/resend",
                {"user_id": "pending-user"},
            )

        self.assertEqual(status, 200)
        self.assertEqual(result, {"ok": True, "user_id": "pending-user"})
        self.assertEqual(delivered[0][0], email)
        new_hash = account_fixture.portal.token_hash(delivered[0][1])
        with self.portal_case.server.db.connect() as connection:
            tokens = connection.execute(
                "SELECT token_hash FROM verification_tokens WHERE user_id=?",
                ("pending-user",),
            ).fetchall()
            audit = connection.execute(
                "SELECT target,detail FROM audit_events "
                "WHERE action='admin/users/verification/resend' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual([row["token_hash"] for row in tokens], [new_hash])
        self.assertNotEqual(new_hash, old_hash)
        self.assertEqual(audit["target"], "pending-user")
        self.assertNotIn(delivered[0][1], audit["detail"])

    def test_resend_failure_removes_new_token_and_preserves_old_link(self):
        _email, old_hash = self.insert_pending_user("smtp-failure")
        client, jar = self.portal_case.login_admin_api()
        with mock.patch.object(
            account_fixture.portal_http,
            "send_verification_email",
            side_effect=RuntimeError("SMTP unavailable"),
        ):
            status, _result, _ = self.portal_case.json_post(
                client,
                jar,
                "/portal-api/admin/users/verification/resend",
                {"user_id": "smtp-failure"},
            )

        self.assertEqual(status, 502)
        with self.portal_case.server.db.connect() as connection:
            tokens = connection.execute(
                "SELECT token_hash FROM verification_tokens WHERE user_id=?",
                ("smtp-failure",),
            ).fetchall()
        self.assertEqual([row["token_hash"] for row in tokens], [old_hash])

    def test_admin_deletes_only_unverified_registration_placeholder(self):
        self.insert_pending_user("delete-pending")
        client, jar = self.portal_case.login_admin_api()
        status, result, _ = self.portal_case.json_post(
            client,
            jar,
            "/portal-api/admin/users/pending/delete",
            {"user_id": "delete-pending"},
        )

        self.assertEqual(status, 200)
        self.assertEqual(result, {"ok": True, "user_id": "delete-pending"})
        with self.portal_case.server.db.connect() as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT id FROM users WHERE id='delete-pending'"
                ).fetchone()
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM verification_tokens WHERE user_id='delete-pending'"
                ).fetchone()[0],
                0,
            )

    def test_delete_rejects_verified_user(self):
        email, _old_hash = self.insert_pending_user("verified-user")
        with self.portal_case.server.db.connect() as connection:
            connection.execute(
                "UPDATE users SET status='active',verified_at=?,api_key_id='key-verified' "
                "WHERE id='verified-user'",
                (account_fixture.portal.now(),),
            )
        client, jar = self.portal_case.login_admin_api()
        status, result, _ = self.portal_case.json_post(
            client,
            jar,
            "/portal-api/admin/users/pending/delete",
            {"user_id": "verified-user"},
        )

        self.assertEqual(status, 400)
        self.assertIn("只能删除尚未验证", result["error"])
        with self.portal_case.server.db.connect() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT email FROM users WHERE id='verified-user'"
                ).fetchone()["email"],
                email,
            )

    def test_audit_persists_details_beyond_the_old_two_thousand_character_limit(self):
        long_detail = "detail-" * 1000
        self.portal_case.server.db.audit(
            "admin", "test.long-detail", "target", "success", "127.0.0.1", long_detail
        )
        with self.portal_case.server.db.connect() as connection:
            stored = connection.execute(
                "SELECT detail FROM audit_events WHERE action='test.long-detail'"
            ).fetchone()["detail"]
        self.assertEqual(stored, long_detail)


if __name__ == "__main__":
    unittest.main()
