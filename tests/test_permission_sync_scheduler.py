#!/usr/bin/env python3
"""验证用户权限采用事件同步、定向重试和低频全量兜底。"""

import unittest

from tests import test_account_portal as portal_tests


portal = portal_tests.portal


class PermissionSyncSchedulerTests(unittest.TestCase):
    """复用隔离门户，证明周期维护不会反复 PATCH 已同步 Key。"""

    def setUp(self):
        self.fixture = portal_tests.PortalIntegrationTests(
            "test_permission_sync_exposes_only_public_model_id"
        )
        self.fixture.setUp()
        self.fixture.insert_control_user_and_model()

    def tearDown(self):
        self.fixture.tearDown()

    def test_periodic_tick_retries_only_due_users_between_full_windows(self):
        """首次全量后每分钟跳过稳定用户，并定向重试失败用户。"""

        control = self.fixture.server.control
        control.permission_full_reconciled_at = 0
        control.background_tick()
        self.assertEqual(len(self.fixture.fake_omni.permissions), 1)

        control.background_tick()
        self.assertEqual(len(self.fixture.fake_omni.permissions), 1)

        with self.fixture.server.db.connect() as connection:
            connection.execute(
                "UPDATE permission_sync SET status='failed',error='retry me' "
                "WHERE user_id='policy-user'"
            )
        control.background_tick()
        self.assertEqual(len(self.fixture.fake_omni.permissions), 2)
        with self.fixture.server.db.connect() as connection:
            row = connection.execute(
                "SELECT status,error FROM permission_sync WHERE user_id='policy-user'"
            ).fetchone()
        self.assertEqual(tuple(row), ("synced", ""))

        control.permission_full_reconciled_at = (
            portal.now() - portal.PERMISSION_FULL_RECONCILE_INTERVAL_SECONDS
        )
        control.background_tick()
        self.assertEqual(len(self.fixture.fake_omni.permissions), 3)

    def test_old_token_limit_remains_due_even_after_previous_sync(self):
        """旧版 Token 限制迁移不能因定向调度而被永久跳过。"""

        control = self.fixture.server.control
        control.permission_full_reconciled_at = portal.now()
        with self.fixture.server.db.connect() as connection:
            connection.execute(
                "UPDATE users SET token_limit_id='legacy-limit' WHERE id='policy-user'"
            )
            connection.execute(
                "INSERT INTO permission_sync(user_id,status,error,updated_at) "
                "VALUES('policy-user','synced','',?)",
                (portal.now(),),
            )
        control.background_tick()
        self.assertEqual(len(self.fixture.fake_omni.permissions), 1)
        self.assertIn(("", "legacy-limit"), self.fixture.fake_omni.deleted)


if __name__ == "__main__":
    unittest.main()
