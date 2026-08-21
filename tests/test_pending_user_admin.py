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

    def test_admin_manual_approval_runs_complete_provisioning_and_audits(self):
        """手动通过必须创建 Key、欢迎余额和权限，并使旧验证链接失效。"""

        email, _old_hash = self.insert_pending_user("manual-approval")
        self.portal_case.server.db.update_settings(
            {"default_welcome_balance": "12.5"}
        )
        client, jar = self.portal_case.login_admin_api()
        status, result, _ = self.portal_case.json_post(
            client,
            jar,
            "/portal-api/admin/users/verification/approve",
            {
                "user_id": "manual-approval",
                "confirmation_email": email,
            },
        )

        self.assertEqual(status, 200)
        self.assertEqual(result, {"ok": True, "user_id": "manual-approval"})
        self.assertNotIn("api_key", result)
        with self.portal_case.server.db.connect() as connection:
            user = connection.execute(
                "SELECT status,verified_at,api_key_id FROM users WHERE id=?",
                ("manual-approval",),
            ).fetchone()
            token = connection.execute(
                "SELECT used_at FROM verification_tokens WHERE user_id=?",
                ("manual-approval",),
            ).fetchone()
            balance = connection.execute(
                "SELECT balance_micros FROM billing_accounts WHERE user_id=?",
                ("manual-approval",),
            ).fetchone()
            membership = connection.execute(
                "SELECT 1 FROM user_group_members WHERE user_id=? AND group_id='default'",
                ("manual-approval",),
            ).fetchone()
            permission = connection.execute(
                "SELECT status FROM permission_sync WHERE user_id=?",
                ("manual-approval",),
            ).fetchone()
            audit = connection.execute(
                "SELECT status,detail FROM audit_events "
                "WHERE action='admin/users/verification/approve' "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(user["status"], "active")
        self.assertIsNotNone(user["verified_at"])
        self.assertEqual(user["api_key_id"], "key-1")
        self.assertIsNotNone(token["used_at"])
        self.assertEqual(balance["balance_micros"], 12_500_000)
        self.assertIsNotNone(membership)
        self.assertEqual(permission["status"], "synced")
        self.assertEqual(audit["status"], "success")
        self.assertNotIn("sk-user-secret", audit["detail"])

        created_before = list(self.portal_case.fake_omni.created)
        repeated_status, repeated, _ = self.portal_case.json_post(
            client,
            jar,
            "/portal-api/admin/users/verification/approve",
            {
                "user_id": "manual-approval",
                "confirmation_email": email,
            },
        )
        self.assertEqual(repeated_status, 400)
        self.assertIn("已经验证", repeated["error"])
        self.assertEqual(self.portal_case.fake_omni.created, created_before)

    def test_api_email_verification_uses_the_shared_provisioning_state_machine(self):
        """新版门户的 API 邮件验证仍应返回一次性 Key 并完成全部开户状态。"""

        client, jar = self.portal_case.opener()
        self.portal_case.get(client, "/portal-api/public")
        delivered: list[str] = []
        with mock.patch.object(
            account_fixture.portal_http,
            "send_verification_email",
            side_effect=lambda _config, _recipient, token: delivered.append(token),
        ):
            register_status, _registered, _ = self.portal_case.json_post(
                client,
                jar,
                "/portal-api/auth/register",
                {
                    "email": "api-verify@example.com",
                    "password": "ValidPassword123",
                    "confirm": "ValidPassword123",
                },
            )
        self.assertEqual(register_status, 200)
        self.assertEqual(len(delivered), 1)

        verify_status, verified, _ = self.portal_case.json_post(
            client,
            jar,
            "/portal-api/auth/verify",
            {"token": delivered[0]},
        )
        self.assertEqual(verify_status, 200)
        self.assertTrue(verified["ok"])
        self.assertTrue(verified["api_key"].startswith("sk-user-secret-"))
        with self.portal_case.server.db.connect() as connection:
            user = connection.execute(
                "SELECT status,verified_at,api_key_id FROM users WHERE email=?",
                ("api-verify@example.com",),
            ).fetchone()
        self.assertEqual(user["status"], "active")
        self.assertIsNotNone(user["verified_at"])
        self.assertEqual(user["api_key_id"], "key-1")

    def test_manual_approval_rejects_wrong_confirmation_and_disallowed_domain(self):
        """邮箱确认或注册域名不符合策略时不得创建任何 API Key。"""

        email, _old_hash = self.insert_pending_user("manual-rejected")
        client, jar = self.portal_case.login_admin_api()
        status, result, _ = self.portal_case.json_post(
            client,
            jar,
            "/portal-api/admin/users/verification/approve",
            {
                "user_id": "manual-rejected",
                "confirmation_email": "different@example.com",
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("确认邮箱", result["error"])
        self.assertFalse(self.portal_case.fake_omni.created)

        self.portal_case.server.db.update_settings({"allowed_domains": "allowed.test"})
        status, result, _ = self.portal_case.json_post(
            client,
            jar,
            "/portal-api/admin/users/verification/approve",
            {"user_id": "manual-rejected", "confirmation_email": email},
        )
        self.assertEqual(status, 400)
        self.assertIn("允许注册范围", result["error"])
        self.assertFalse(self.portal_case.fake_omni.created)

    def test_manual_approval_rejects_ordinary_user(self):
        """普通用户即使知道目标 ID 和邮箱也不能调用管理员手动通过接口。"""

        target_email, _old_hash = self.insert_pending_user("admin-only-target")
        stamp = account_fixture.portal.now()
        with self.portal_case.server.db.connect() as connection:
            connection.execute(
                "INSERT INTO users(id,email,login_name,password_hash,role,status,"
                "api_key_id,quota_tokens,quota_reset,quota_reset_time,created_at,verified_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "ordinary-user",
                    "ordinary@example.com",
                    "ordinary@example.com",
                    account_fixture.portal.hash_password("ValidPassword123"),
                    "user",
                    "active",
                    "ordinary-key",
                    0,
                    "monthly",
                    "00:00",
                    stamp,
                    stamp,
                ),
            )
        client, jar = self.portal_case.opener()
        self.portal_case.get(client, "/portal-api/public")
        login_status, _login, _ = self.portal_case.json_post(
            client,
            jar,
            "/portal-api/auth/login",
            {"identity": "ordinary@example.com", "password": "ValidPassword123"},
        )
        self.assertEqual(login_status, 200)
        status, _result, _ = self.portal_case.json_post(
            client,
            jar,
            "/portal-api/admin/users/verification/approve",
            {
                "user_id": "admin-only-target",
                "confirmation_email": target_email,
            },
        )
        self.assertEqual(status, 403)
        self.assertFalse(self.portal_case.fake_omni.created)

    def test_manual_approval_rolls_back_key_and_account_when_sync_fails(self):
        """权限同步失败时必须撤销欢迎余额和 Key，并保留可重试的验证链接。"""

        email, _old_hash = self.insert_pending_user("manual-sync-failure")
        self.portal_case.server.db.update_settings({"default_welcome_balance": "5"})
        client, jar = self.portal_case.login_admin_api()
        with mock.patch.object(
            self.portal_case.server.control,
            "sync_user",
            side_effect=RuntimeError("permission sync unavailable"),
        ):
            status, result, _ = self.portal_case.json_post(
                client,
                jar,
                "/portal-api/admin/users/verification/approve",
                {
                    "user_id": "manual-sync-failure",
                    "confirmation_email": email,
                },
            )

        self.assertEqual(status, 502)
        self.assertIn("permission sync unavailable", result["error"])
        self.assertIn(("key-1", ""), self.portal_case.fake_omni.deleted)
        with self.portal_case.server.db.connect() as connection:
            user = connection.execute(
                "SELECT status,verified_at,api_key_id FROM users WHERE id=?",
                ("manual-sync-failure",),
            ).fetchone()
            token = connection.execute(
                "SELECT used_at FROM verification_tokens WHERE user_id=?",
                ("manual-sync-failure",),
            ).fetchone()
            balance_count = connection.execute(
                "SELECT COUNT(*) FROM billing_accounts WHERE user_id=?",
                ("manual-sync-failure",),
            ).fetchone()[0]
            membership_count = connection.execute(
                "SELECT COUNT(*) FROM user_group_members WHERE user_id=?",
                ("manual-sync-failure",),
            ).fetchone()[0]
            welcome_count = connection.execute(
                "SELECT COUNT(*) FROM balance_transactions WHERE user_id=?",
                ("manual-sync-failure",),
            ).fetchone()[0]
        self.assertEqual(
            (user["status"], user["verified_at"], user["api_key_id"]),
            ("pending", None, None),
        )
        self.assertIsNone(token["used_at"])
        self.assertEqual((balance_count, membership_count, welcome_count), (0, 0, 0))

    def test_failed_retry_preserves_preexisting_welcome_credit(self):
        """失败补偿只能撤销本次赠额，不得删除较早存在的同源账务记录。"""

        email, _old_hash = self.insert_pending_user("manual-existing-credit")
        stamp = account_fixture.portal.now()
        with self.portal_case.server.db.connect() as connection:
            connection.execute(
                "INSERT INTO billing_accounts(user_id,balance_micros,suspended,updated_at) "
                "VALUES(?,?,0,?)",
                ("manual-existing-credit", 7_000_000, stamp),
            )
            connection.execute(
                "INSERT INTO balance_transactions"
                "(user_id,kind,amount_micros,balance_after_micros,actor,note,source_ref,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    "manual-existing-credit",
                    "credit",
                    7_000_000,
                    7_000_000,
                    "system:registration",
                    "Existing welcome balance",
                    "welcome-credit:manual-existing-credit",
                    stamp,
                ),
            )
        self.portal_case.server.db.update_settings({"default_welcome_balance": "7"})
        client, jar = self.portal_case.login_admin_api()
        with mock.patch.object(
            self.portal_case.server.control,
            "sync_user",
            side_effect=RuntimeError("permission sync unavailable"),
        ):
            status, _result, _ = self.portal_case.json_post(
                client,
                jar,
                "/portal-api/admin/users/verification/approve",
                {
                    "user_id": "manual-existing-credit",
                    "confirmation_email": email,
                },
            )
        self.assertEqual(status, 502)
        with self.portal_case.server.db.connect() as connection:
            balance = connection.execute(
                "SELECT balance_micros FROM billing_accounts WHERE user_id=?",
                ("manual-existing-credit",),
            ).fetchone()["balance_micros"]
            transactions = connection.execute(
                "SELECT COUNT(*) FROM balance_transactions WHERE user_id=?",
                ("manual-existing-credit",),
            ).fetchone()[0]
        self.assertEqual(balance, 7_000_000)
        self.assertEqual(transactions, 1)

    def test_concurrent_manual_approval_does_not_rollback_completed_activation(self):
        """并发状态变化时只删除本次 Key，不得覆盖另一笔已经完成的开通。"""

        email, _old_hash = self.insert_pending_user("manual-race")
        client, jar = self.portal_case.login_admin_api()
        original_create = self.portal_case.fake_omni.create_user_key

        def create_after_concurrent_activation(user_id, target_email, max_sessions=0):
            """模拟另一个管理员在本次 Key 创建期间先完成账户开通。"""

            key_id, raw_key = original_create(user_id, target_email, max_sessions)
            with self.portal_case.server.db.connect() as connection:
                connection.execute(
                    "UPDATE users SET status='active',verified_at=?,api_key_id=? "
                    "WHERE id=?",
                    (account_fixture.portal.now(), "concurrent-key", user_id),
                )
            return key_id, raw_key

        with mock.patch.object(
            self.portal_case.fake_omni,
            "create_user_key",
            side_effect=create_after_concurrent_activation,
        ):
            status, result, _ = self.portal_case.json_post(
                client,
                jar,
                "/portal-api/admin/users/verification/approve",
                {"user_id": "manual-race", "confirmation_email": email},
            )

        self.assertEqual(status, 400)
        self.assertIn("状态已经变化", result["error"])
        self.assertIn(("key-1", ""), self.portal_case.fake_omni.deleted)
        with self.portal_case.server.db.connect() as connection:
            user = connection.execute(
                "SELECT status,verified_at,api_key_id FROM users WHERE id='manual-race'"
            ).fetchone()
        self.assertEqual(user["status"], "active")
        self.assertIsNotNone(user["verified_at"])
        self.assertEqual(user["api_key_id"], "concurrent-key")

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
