#!/usr/bin/env python3
"""验证账户门户不依赖服务夹具的公共输入与配置契约。"""

import json
import pathlib
import tempfile
import unittest
from unittest import mock

from tests.test_account_portal import portal


class PortalCommonContractTests(unittest.TestCase):
    """覆盖密码、标识规范化、内容脱敏、来源地址和重置边界。"""

    def test_password_hash_is_salted_and_verifiable(self):
        first = portal.hash_password("a secure password 123")
        second = portal.hash_password("a secure password 123")
        self.assertNotEqual(first, second)
        self.assertTrue(portal.verify_password("a secure password 123", first))
        self.assertFalse(portal.verify_password("wrong password", first))

    def test_password_accepts_eight_characters_but_rejects_numeric_only(self):
        encoded = portal.hash_password("abc12345")
        self.assertTrue(portal.verify_password("abc12345", encoded))
        with self.assertRaisesRegex(ValueError, "不能全部由数字"):
            portal.hash_password("12345678")
        with self.assertRaisesRegex(ValueError, "8-200"):
            portal.hash_password("abc1234")

    def test_admin_password_has_no_length_floor_but_rejects_numeric_only(self):
        encoded = portal.hash_admin_password("a")
        self.assertTrue(portal.verify_password("a", encoded))
        with self.assertRaisesRegex(ValueError, "不能全部由数字"):
            portal.hash_admin_password("39873987")
        with self.assertRaisesRegex(ValueError, "不能为空"):
            portal.hash_admin_password("")

    def test_group_names_support_unicode_but_reject_invisible_controls(self):
        self.assertEqual(portal.normalize_group_name("  中文　小组  "), "中文 小组")
        with self.assertRaisesRegex(ValueError, "不可见字符"):
            portal.normalize_group_name("研发\u200b组")

    def test_request_content_summary_keeps_text_and_hides_binary_payloads(self):
        summary = portal.request_content_summary(
            {
                "messages": [
                    {"role": "system", "content": "系统提示"},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "识别这张图片"},
                            {
                                "type": "image_url",
                                "image_url": {"url": "data:image/png;base64,secret"},
                            },
                        ],
                    },
                ],
                "authorization": "must-not-be-returned",
            }
        )
        self.assertTrue(summary["available"])
        self.assertEqual(summary["messages"][0]["content"], "系统提示")
        self.assertIn("[图像内容]", summary["messages"][1]["content"])
        self.assertNotIn("base64", json.dumps(summary, ensure_ascii=False))

    def test_email_domains_are_exact_and_idna_normalized(self):
        self.assertEqual(
            portal.normalize_domains("Example.COM,example.com"), ["example.com"]
        )
        self.assertEqual(
            portal.normalize_email("USER@Example.com"),
            ("user@example.com", "example.com"),
        )
        with self.assertRaises(ValueError):
            portal.normalize_domain(".example.com")

    def test_invalid_origin_and_integer_configuration_fail_cleanly(self):
        with mock.patch.dict(
            portal.os.environ,
            {"ACCOUNT_BIND": "0.0.0.0", "ACCOUNT_REGISTRATION_ENABLED": "0"},
            clear=True,
        ):
            with self.assertRaisesRegex(SystemExit, "must be 127.0.0.1"):
                portal.Config.from_env()
        with mock.patch.dict(
            portal.os.environ,
            {
                "ACCOUNT_PUBLIC_URL": "http://[broken",
                "ACCOUNT_REGISTRATION_ENABLED": "0",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(SystemExit, "valid http"):
                portal.Config.from_env()
        with mock.patch.dict(
            portal.os.environ,
            {
                "ACCOUNT_PUBLIC_URL": "http://127.0.0.1:8001",
                "ACCOUNT_PORT": "not-a-port",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(SystemExit, "ACCOUNT_PORT must be an integer"):
                portal.Config.from_env()
        with tempfile.TemporaryDirectory() as directory:
            shared = str(pathlib.Path(directory) / "storage.sqlite")
            with mock.patch.dict(
                portal.os.environ,
                {"ACCOUNT_DB_PATH": shared, "OMNIROUTE_DB_PATH": shared},
                clear=True,
            ):
                with self.assertRaisesRegex(SystemExit, "must not be OmniRoute"):
                    portal.Config.from_env()

    def test_public_portal_origin_is_canonicalized_to_ui_path(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(
                portal.os.environ,
                {
                    "ACCOUNT_PUBLIC_URL": "https://llm.example.com/",
                    "ACCOUNT_API_PUBLIC_URL": "https://llm.example.com",
                    "ACCOUNT_DB_PATH": str(pathlib.Path(directory) / "account.db"),
                    "OMNIROUTE_DB_PATH": str(pathlib.Path(directory) / "omni.db"),
                },
                clear=True,
            ):
                config = portal.Config.from_env()
        self.assertEqual(config.public_url, "https://llm.example.com/ui")
        self.assertEqual(config.api_public_url, "https://llm.example.com")

    def test_reset_boundaries_use_configured_wall_clock_and_timezone(self):
        current = int(
            portal.datetime.datetime(
                2026, 8, 2, 4, 0, tzinfo=portal.ZoneInfo("Asia/Shanghai")
            ).timestamp()
        )
        daily = portal.next_reset_at("daily", current, "03:30", "Asia/Shanghai")
        weekly = portal.next_reset_at("weekly", current, "03:30", "Asia/Shanghai")
        monthly = portal.next_reset_at("monthly", current, "03:30", "Asia/Shanghai")
        self.assertEqual(
            portal.datetime.datetime.fromtimestamp(
                daily, portal.ZoneInfo("Asia/Shanghai")
            ),
            portal.datetime.datetime(
                2026, 8, 3, 3, 30, tzinfo=portal.ZoneInfo("Asia/Shanghai")
            ),
        )
        self.assertEqual(
            portal.datetime.datetime.fromtimestamp(
                weekly, portal.ZoneInfo("Asia/Shanghai")
            ),
            portal.datetime.datetime(
                2026, 8, 3, 3, 30, tzinfo=portal.ZoneInfo("Asia/Shanghai")
            ),
        )
        self.assertEqual(
            portal.datetime.datetime.fromtimestamp(
                monthly, portal.ZoneInfo("Asia/Shanghai")
            ),
            portal.datetime.datetime(
                2026, 9, 1, 3, 30, tzinfo=portal.ZoneInfo("Asia/Shanghai")
            ),
        )


if __name__ == "__main__":
    unittest.main()
