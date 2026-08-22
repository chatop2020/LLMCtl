import json
import unittest
from unittest import mock

from tests import test_account_portal as account_fixture


class OmniRoutePortalTests(unittest.TestCase):
    """验证管理员门户只通过白名单 Unix Socket 转发 OmniRoute 运维操作。"""

    def setUp(self):
        """创建隔离门户、SQLite 和 Fake OmniRoute，不调用真实 root 服务。"""

        self.portal_case = account_fixture.PortalIntegrationTests("runTest")
        self.portal_case.setUp()

    def tearDown(self):
        self.portal_case.tearDown()

    def test_admin_reads_status_and_submits_whitelisted_operations(self):
        """状态、评估、提交、任务和取消必须映射到固定控制操作。"""

        responses = {
            "omniroute-status": {"available": True, "backups": [], "jobs": []},
            "omniroute-assess": {"health": "healthy"},
            "omniroute-submit": {"id": "job-1", "state": "waiting"},
            "omniroute-job": {"id": "job-1", "state": "running"},
            "omniroute-cancel": {"id": "job-1", "state": "running"},
        }

        def request(operation, payload=None):
            """记录控制操作并返回对应的无敏感信息测试结果。"""

            return responses[operation]

        client, jar = self.portal_case.login_admin_api()
        with mock.patch.object(
            self.portal_case.server.models, "request", side_effect=request
        ) as control:
            status, snapshot_raw, _ = self.portal_case.get(
                client, "/portal-api/admin/omniroute"
            )
            snapshot = json.loads(snapshot_raw)
            assess_status, assessment, _ = self.portal_case.json_post(
                client,
                jar,
                "/portal-api/admin/omniroute/assess",
                {"deep": True},
            )
            submit_status, submitted, _ = self.portal_case.json_post(
                client,
                jar,
                "/portal-api/admin/omniroute/submit",
                {
                    "action": "update",
                    "image": "diegosouzapw/omniroute:3.8.49",
                    "confirmation": "UPDATE OMNIROUTE",
                },
            )
            job_status, job, _ = self.portal_case.json_post(
                client,
                jar,
                "/portal-api/admin/omniroute/job",
                {"id": "job-1"},
            )
            cancel_status, cancelled, _ = self.portal_case.json_post(
                client,
                jar,
                "/portal-api/admin/omniroute/cancel",
                {"id": "job-1"},
            )

        self.assertEqual((status, snapshot["available"]), (200, True))
        self.assertEqual((assess_status, assessment["health"]), (200, "healthy"))
        self.assertEqual((submit_status, submitted["state"]), (200, "waiting"))
        self.assertEqual((job_status, job["state"]), (200, "running"))
        self.assertEqual((cancel_status, cancelled["state"]), (200, "running"))
        self.assertEqual(
            [call.args[0] for call in control.call_args_list],
            [
                "omniroute-status",
                "omniroute-assess",
                "omniroute-submit",
                "omniroute-job",
                "omniroute-cancel",
            ],
        )
        with self.portal_case.server.db.connect() as connection:
            actions = [
                row["action"]
                for row in connection.execute(
                    "SELECT action FROM audit_events WHERE action LIKE 'admin/omniroute/%'"
                ).fetchall()
            ]
        self.assertIn("admin/omniroute/submit", actions)
        self.assertIn("admin/omniroute/assess", actions)
        self.assertIn("admin/omniroute/cancel", actions)
        self.assertNotIn("admin/omniroute/job", actions)

    def test_ordinary_user_cannot_read_or_submit_maintenance(self):
        """普通用户不能越过管理员 API 访问 root 运维控制面。"""

        stamp = account_fixture.portal.now()
        with self.portal_case.server.db.connect() as connection:
            connection.execute(
                "INSERT INTO users(id,email,login_name,password_hash,role,status,"
                "api_key_id,quota_tokens,quota_reset,quota_reset_time,created_at,verified_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "ordinary-maintenance-user",
                    "ordinary-maintenance@example.com",
                    "ordinary-maintenance@example.com",
                    account_fixture.portal.hash_password("ValidPassword123"),
                    "user",
                    "active",
                    "ordinary-maintenance-key",
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
            {
                "identity": "ordinary-maintenance@example.com",
                "password": "ValidPassword123",
            },
        )
        self.assertEqual(login_status, 200)
        with mock.patch.object(self.portal_case.server.models, "request") as control:
            status, _snapshot, _ = self.portal_case.get(
                client, "/portal-api/admin/omniroute"
            )
            submit_status, _result, _ = self.portal_case.json_post(
                client,
                jar,
                "/portal-api/admin/omniroute/submit",
                {"action": "backup"},
            )
        self.assertEqual(status, 403)
        self.assertEqual(submit_status, 403)
        control.assert_not_called()


if __name__ == "__main__":
    unittest.main()
