#!/usr/bin/env python3
import http.cookiejar
import importlib.util
import pathlib
import sqlite3
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "lib" / "account_portal.py"
SPEC = importlib.util.spec_from_file_location("account_portal", MODULE_PATH)
portal = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = portal
SPEC.loader.exec_module(portal)


class FakeOmniRoute:
    def __init__(self):
        self.created = []
        self.limits = []
        self.deleted = []
        self.activated = []

    def create_user_key(self, user_id, email):
        key_id = f"key-{len(self.created) + 1}"
        raw = f"sk-user-secret-{len(self.created) + 1}-abcdefghijklmnopqrstuvwxyz"
        self.created.append((key_id, user_id, email, raw))
        return key_id, raw

    def set_limit(self, key_id, quota, reset, reset_time, limit_id=None):
        result = limit_id or f"limit-{key_id}"
        self.limits.append((result, key_id, quota, reset, reset_time))
        return result

    def delete_key_and_limit(self, key_id, limit_id=""):
        self.deleted.append((key_id, limit_id))

    def activate_key(self, key_id, active):
        self.activated.append((key_id, active))

    def usage(self, key_id):
        return {
            "limits": [
                {
                    "id": f"limit-{key_id}",
                    "tokenLimit": 1000000,
                    "tokensUsed": 12345,
                }
            ]
        }

    def models(self):
        return [
            {
                "id": "ornith-1.0-35b-fp8",
                "owned_by": "LLMCtl",
                "capabilities": ["chat", "vision"],
            }
        ]


class PortalIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = pathlib.Path(self.tempdir.name) / "portal" / "account-portal.db"
        self.config = portal.Config(
            bind="127.0.0.1",
            port=0,
            db_path=self.db_path,
            gateway_url="http://127.0.0.1:9",
            gateway_manage_key="sk-management-test",
            public_url="http://portal.example.test",
            api_public_url="https://llm.example.test",
            admin_email="admin@example.com",
            admin_password="correct horse battery staple",
            initial_registration=True,
            initial_domains=["example.com"],
            initial_quota=1000000,
            initial_reset="monthly",
            initial_reset_time="00:00",
            verification_ttl=86400,
            cookie_secure=False,
            smtp_host="smtp.example.com",
            smtp_port=587,
            smtp_security="starttls",
            smtp_username="mailer@example.com",
            smtp_password="app-password",
            smtp_from="mailer@example.com",
            supports_ocr=True,
        )
        self.server = portal.PortalServer(self.config)
        self.fake_omni = FakeOmniRoute()
        self.server.omni = self.fake_omni
        self.thread = threading.Thread(target=self.server.httpd.serve_forever, daemon=True)
        self.thread.start()
        port = self.server.httpd.server_address[1]
        self.base_url = f"http://127.0.0.1:{port}"

    def tearDown(self):
        self.server.httpd.shutdown()
        self.server.httpd.server_close()
        self.thread.join(timeout=2)
        self.tempdir.cleanup()

    @staticmethod
    def opener():
        jar = http.cookiejar.CookieJar()
        client = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(jar)
        )
        return client, jar

    def get(self, client, path):
        try:
            with client.open(self.base_url + path, timeout=5) as response:
                return response.status, response.read().decode(), response.headers
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode(), error.headers

    def post(self, client, path, values):
        request = urllib.request.Request(
            self.base_url + path,
            data=urllib.parse.urlencode(values).encode(),
            method="POST",
        )
        try:
            with client.open(request, timeout=5) as response:
                return response.status, response.read().decode(), response.headers
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode(), error.headers

    @staticmethod
    def cookie_value(jar, name):
        return next(cookie.value for cookie in jar if cookie.name == name)

    def register(self, client, jar, email="alice@example.com"):
        status, _, _ = self.get(client, "/register")
        self.assertEqual(status, 200)
        csrf = self.cookie_value(jar, portal.CSRF_COOKIE)
        captured = []
        with mock.patch.object(
            portal,
            "send_verification_email",
            side_effect=lambda config, recipient, token: captured.append((recipient, token)),
        ):
            status, body, _ = self.post(
                client,
                "/register",
                {
                    "csrf": csrf,
                    "email": email,
                    "password": "a secure password 123",
                    "confirm": "a secure password 123",
                },
            )
        self.assertEqual(status, 200)
        self.assertIn("Verification email sent", body)
        self.assertEqual(captured[0][0], email)
        return captured[0][1]

    def test_verified_user_gets_one_time_key_quota_usage_models_and_curl(self):
        client, jar = self.opener()
        token = self.register(client, jar)
        status, body, _ = self.get(client, "/verify?token=" + urllib.parse.quote(token))
        self.assertEqual(status, 200)
        self.assertIn("Confirm email", body)
        csrf = self.cookie_value(jar, portal.CSRF_COOKIE)
        status, dashboard, _ = self.post(client, "/verify", {"csrf": csrf, "token": token})
        self.assertEqual(status, 200)
        raw_key = self.fake_omni.created[0][3]
        self.assertIn(raw_key, dashboard)
        self.assertIn("12,345 / 1,000,000", dashboard)
        self.assertIn("ornith-1.0-35b-fp8", dashboard)
        self.assertIn("https://llm.example.test/v1/chat/completions", dashboard)
        self.assertIn("curl", dashboard)

        status, second_view, _ = self.get(client, "/")
        self.assertEqual(status, 200)
        self.assertNotIn(raw_key, second_view)
        self.assertNotIn(raw_key.encode(), self.db_path.read_bytes())

        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT status,api_key_id,token_limit_id FROM users WHERE email=?",
                ("alice@example.com",),
            ).fetchone()
            audit_actions = {
                item[0] for item in connection.execute("SELECT action FROM audit_events")
            }
        self.assertEqual(row, ("active", "key-1", "limit-key-1"))
        self.assertIn("register.email", audit_actions)
        self.assertIn("verify.provision", audit_actions)

    def test_disabling_user_invalidates_existing_session(self):
        client, jar = self.opener()
        token = self.register(client, jar)
        self.get(client, "/verify?token=" + urllib.parse.quote(token))
        csrf = self.cookie_value(jar, portal.CSRF_COOKIE)
        status, _, _ = self.post(client, "/verify", {"csrf": csrf, "token": token})
        self.assertEqual(status, 200)
        with sqlite3.connect(self.db_path) as connection:
            connection.execute("UPDATE users SET status='disabled' WHERE email='alice@example.com'")
        status, body, _ = self.get(client, "/")
        self.assertEqual(status, 200)
        self.assertIn("Company LLM API portal", body)
        self.assertNotIn("Available models", body)

    def test_domain_is_checked_at_registration_and_again_at_verification(self):
        client, jar = self.opener()
        self.get(client, "/register")
        csrf = self.cookie_value(jar, portal.CSRF_COOKIE)
        status, body, _ = self.post(
            client,
            "/register",
            {
                "csrf": csrf,
                "email": "outsider@other.example",
                "password": "a secure password 123",
                "confirm": "a secure password 123",
            },
        )
        self.assertEqual(status, 200)
        self.assertIn("domain is not allowed", body)
        self.assertFalse(self.fake_omni.created)

        token = self.register(client, jar, "bob@example.com")
        with self.server.db.connect() as connection:
            connection.execute(
                "UPDATE settings SET value='new.example' WHERE key='allowed_domains'"
            )
        self.get(client, "/verify?token=" + urllib.parse.quote(token))
        csrf = self.cookie_value(jar, portal.CSRF_COOKIE)
        status, body, _ = self.post(client, "/verify", {"csrf": csrf, "token": token})
        self.assertEqual(status, 503)
        self.assertIn("Provisioning failed", body)
        self.assertFalse(self.fake_omni.created)

    def test_admin_page_uses_explicit_status_selection_and_separate_sqlite(self):
        self.assertEqual(self.db_path.name, "account-portal.db")
        self.assertNotEqual(self.db_path.name, "storage.sqlite")
        with self.server.db.connect() as connection:
            connection.execute(
                "INSERT INTO users(id,email,password_hash,role,status,api_key_id,token_limit_id,quota_tokens,quota_reset,quota_reset_time,created_at,verified_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "user-admin-view",
                    "viewer@example.com",
                    portal.hash_password("a secure password 123"),
                    "user",
                    "active",
                    "key-viewer",
                    "limit-viewer",
                    1000000,
                    "monthly",
                    "00:00",
                    portal.now(),
                    portal.now(),
                ),
            )
        client, jar = self.opener()
        self.get(client, "/login")
        csrf = self.cookie_value(jar, portal.CSRF_COOKIE)
        status, body, _ = self.post(
            client,
            "/login",
            {
                "csrf": csrf,
                "email": "admin@example.com",
                "password": "correct horse battery staple",
            },
        )
        self.assertEqual(status, 200)
        self.assertIn("Registration", body)
        self.assertNotIn("保存并设为", body)
        self.assertIn('<option value="active" selected>active</option>', body)
        self.assertIn('<option value="disabled" >disabled</option>', body)
        self.assertIn("门户数据库与 OmniRoute 数据库完全分离", body)


class PortalUnitTests(unittest.TestCase):
    def test_password_hash_is_salted_and_verifiable(self):
        first = portal.hash_password("a secure password 123")
        second = portal.hash_password("a secure password 123")
        self.assertNotEqual(first, second)
        self.assertTrue(portal.verify_password("a secure password 123", first))
        self.assertFalse(portal.verify_password("wrong password", first))

    def test_email_domains_are_exact_and_idna_normalized(self):
        self.assertEqual(portal.normalize_domains("Example.COM,example.com"), ["example.com"])
        self.assertEqual(portal.normalize_email("USER@Example.com"), ("user@example.com", "example.com"))
        with self.assertRaises(ValueError):
            portal.normalize_domain(".example.com")

    def test_invalid_origin_and_integer_configuration_fail_cleanly(self):
        with mock.patch.dict(
            portal.os.environ,
            {"ACCOUNT_PUBLIC_URL": "http://[broken", "ACCOUNT_REGISTRATION_ENABLED": "0"},
            clear=True,
        ):
            with self.assertRaisesRegex(SystemExit, "valid http"):
                portal.Config.from_env()
        with mock.patch.dict(
            portal.os.environ,
            {"ACCOUNT_PUBLIC_URL": "http://127.0.0.1:8001", "ACCOUNT_PORT": "not-a-port"},
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


if __name__ == "__main__":
    unittest.main()
