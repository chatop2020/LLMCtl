#!/usr/bin/env python3
import dataclasses
import datetime
import http.cookiejar
import importlib.util
import json
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
        self.permissions = []
        self.session_limits = []
        self.rate_limits = []
        self.deleted_aliases = []
        self.logs = []
        self.test_error = None
        self.tested_models = []
        self.tested_provider_models = []
        self.hidden_models = {}
        self.call_log_details = {}
        self.combo_items = []
        self.combo_upserts = []
        self.deleted_combos = []
        self.deleted_combo_mappings = []
        self.combo_active_updates = []
        self.context_updates = []
        self.output_updates = []
        self.aliases = []
        self.revealed = []

    def create_user_key(self, user_id, email, max_sessions=0):
        key_id = f"key-{len(self.created) + 1}"
        raw = f"sk-user-secret-{len(self.created) + 1}-abcdefghijklmnopqrstuvwxyz"
        self.created.append((key_id, user_id, email, raw))
        self.session_limits.append((key_id, max_sessions, "create"))
        return key_id, raw

    def reveal_user_key(self, key_id):
        self.revealed.append(key_id)
        for created_key_id, _user_id, _email, raw_key in self.created:
            if created_key_id == key_id:
                return raw_key
        return f"sk-existing-{key_id}-abcdefghijklmnopqrstuvwxyz"

    def delete_key(self, key_id):
        self.deleted.append((key_id, ""))

    def set_limit(self, key_id, quota, reset, reset_time, limit_id=None):
        result = limit_id or f"limit-{key_id}"
        self.limits.append((result, key_id, quota, reset, reset_time))
        return result

    def delete_key_and_limit(self, key_id, limit_id=""):
        self.deleted.append((key_id, limit_id))

    def delete_limit(self, limit_id):
        self.deleted.append(("", limit_id))

    def activate_key(self, key_id, active):
        self.activated.append((key_id, active))

    def set_key_max_sessions(self, key_id, max_sessions):
        self.session_limits.append((key_id, max_sessions, "patch"))

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

    def combos(self):
        return self.combo_items

    def upsert_combo(self, combo_id, payload, active=True):
        created = not bool(combo_id)
        result_id = combo_id or f"public-combo-{len(self.combo_upserts) + 1}"
        item = {"id": result_id, **payload, "isActive": active}
        self.combo_items = [
            existing
            for existing in self.combo_items
            if str(existing.get("id", "")) != result_id
        ]
        self.combo_items.append(item)
        self.combo_upserts.append((result_id, payload, active, created))
        return result_id, created

    def set_combo_active(self, combo_id, active):
        self.combo_active_updates.append((combo_id, active))
        for combo in self.combo_items:
            if str(combo.get("id", "")) == combo_id:
                combo["isActive"] = active

    def delete_combo(self, combo_id):
        self.deleted_combos.append(combo_id)
        self.combo_items = [
            combo
            for combo in self.combo_items
            if str(combo.get("id", "")) != combo_id
        ]

    def combo_builder_options(self):
        return {
            "providers": [
                {
                    "providerId": "local-a",
                    "models": [
                        {
                            "id": "ornith-a",
                            "qualifiedModel": "local-a/ornith-a",
                            "contextLength": 262144,
                            "outputTokenLimit": 32768,
                        }
                    ],
                },
                {
                    "providerId": "local-b",
                    "models": [
                        {
                            "id": "ornith-b",
                            "qualifiedModel": "local-b/ornith-b",
                            "contextLength": 131072,
                            "outputTokenLimit": 16384,
                        }
                    ],
                },
            ]
        }

    def alias_metadata(self, alias):
        return {"metadata": {"capabilities": {"vision": True, "reasoning": True}}}

    def set_context_window_override(self, provider, model_id, value):
        self.context_updates.append((provider, model_id, value))

    def set_max_output_override(self, provider, model_id, value):
        self.output_updates.append((provider, model_id, value))

    def patch_key_permissions(
        self, key_id, allowed_models, allowed_combos, active, max_sessions=0,
        requests_per_minute=0, requests_per_day=0
    ):
        self.permissions.append((key_id, allowed_models, allowed_combos, active))
        self.session_limits.append((key_id, max_sessions, "permissions"))
        self.rate_limits.append(
            (key_id, requests_per_minute, requests_per_day, "permissions")
        )

    def call_logs(self, key_id, limit=200, offset=0):
        return self.logs[offset : offset + limit]

    def test_model(self, model_id):
        self.tested_models.append(model_id)
        if self.test_error:
            raise RuntimeError(self.test_error)
        return 12, "OK"

    def test_provider_model(self, provider, model_id):
        self.tested_provider_models.append((provider, model_id))
        if self.test_error:
            raise RuntimeError(self.test_error)
        return 12, "OK"

    def hidden_provider_models(self, provider):
        return set(self.hidden_models.get(provider, set()))

    def call_log(self, request_id):
        return self.call_log_details.get(
            request_id,
            {
                "id": request_id,
                "detailState": "ready",
                "hasRequestBody": True,
                "requestBody": {
                    "messages": [{"role": "user", "content": "默认请求内容"}]
                },
            },
        )

    def delete_model_alias(self, public_id):
        self.deleted_aliases.append(public_id)

    def set_combo_mapping(self, pattern, combo_id, mapping_id="", enabled=True):
        return mapping_id or "mapping-1"

    def delete_combo_mapping(self, mapping_id):
        self.deleted_combo_mappings.append(mapping_id)

    def set_model_alias(self, public_id, source_model):
        self.aliases.append((public_id, source_model))
        return public_id


class RecordingWorkflowGateway(portal.OmniRouteClient):
    def __init__(self, combos=None):
        self.calls = []
        self.recorded_combos = combos or []

    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if (method, path) == ("GET", "/api/provider-nodes?limit=1000"):
            return {"nodes": []}
        if (method, path) == ("POST", "/api/provider-nodes"):
            return {"node": {"id": "workflow-node", **payload}}
        if (method, path) == ("GET", "/api/providers?limit=1000"):
            return {"connections": []}
        if (method, path) == ("POST", "/api/providers"):
            return {"connection": {"id": "workflow-connection", **payload}}
        if method == "GET" and path.startswith("/api/provider-models?"):
            return {"models": []}
        if method == "POST" and path == "/api/provider-models":
            return {"model": payload}
        if (method, path) == ("GET", "/api/combos?limit=1000"):
            return {"combos": self.recorded_combos}
        if method == "POST" and path == "/api/combos":
            return {"combo": {"id": "workflow-combo", **payload}}
        raise AssertionError((method, path, payload))


class WorkflowGatewayPublishingTests(unittest.TestCase):
    def test_publishing_is_explicit_collision_free_and_does_not_replace_public_alias(self):
        client = RecordingWorkflowGateway()
        result = client.sync_workflow_routes(
            {
                "gateway_base_url": "http://10.0.0.12:18100/v1",
                "models": {
                    "gdn-inside-workflow": {
                        "enabled": True,
                        "mode": "agent",
                        "base_model": "ornith-internal",
                    },
                    "disabled-route": {"enabled": False},
                },
            },
            "workflow-secret-with-at-least-24-characters",
        )
        self.assertEqual(
            [{"route_model": "gdn-inside-workflow", "combo": "llmctl-workflow-gdn-inside-workflow"}],
            result["published"],
        )
        node_payload = next(
            payload
            for method, path, payload in client.calls
            if (method, path) == ("POST", "/api/provider-nodes")
        )
        self.assertEqual("http://10.0.0.12:18100/v1", node_payload["baseUrl"])
        combo_payload = next(
            payload
            for method, path, payload in client.calls
            if (method, path) == ("POST", "/api/combos")
        )
        self.assertEqual("llmctl-workflow-gdn-inside-workflow", combo_payload["name"])
        self.assertFalse(any("alias" in path for _, path, _ in client.calls))

    def test_publishing_requires_an_enabled_route(self):
        with self.assertRaisesRegex(ValueError, "没有已启用"):
            RecordingWorkflowGateway().sync_workflow_routes(
                {
                    "gateway_base_url": "http://127.0.0.1:18100/v1",
                    "models": {"off": {"enabled": False}},
                },
                "workflow-secret-with-at-least-24-characters",
            )

    def test_publishing_rejects_query_credentials_before_gateway_calls(self):
        client = RecordingWorkflowGateway()
        with self.assertRaisesRegex(ValueError, "gateway_base_url"):
            client.sync_workflow_routes(
                {
                    "gateway_base_url": "https://workflow.example/v1?token=secret",
                    "models": {"route-a": {"enabled": True}},
                },
                "workflow-secret-with-at-least-24-characters",
            )
        self.assertEqual([], client.calls)

    def test_unmanaged_combo_collision_fails_before_any_gateway_mutation(self):
        client = RecordingWorkflowGateway(
            combos=[
                {
                    "id": "owned-by-someone-else",
                    "name": "llmctl-workflow-route-a",
                    "description": "manually managed",
                }
            ]
        )
        with self.assertRaisesRegex(RuntimeError, "不由 LLMCtl 管理"):
            client.sync_workflow_routes(
                {
                    "gateway_base_url": "http://127.0.0.1:18100/v1",
                    "models": {"route-a": {"enabled": True}},
                },
                "workflow-secret-with-at-least-24-characters",
            )
        self.assertEqual(
            [("GET", "/api/combos?limit=1000", None)], client.calls
        )


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
            admin_username="admin",
            admin_password="correct horse battery staple",
            initial_registration=True,
            initial_domains=["example.com"],
            initial_welcome_balance_micros=0,
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
        self.server.control.omni = self.fake_omni
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

    def json_post(self, client, jar, path, values):
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(values).encode(),
            headers={
                "Content-Type": "application/json",
                "X-CSRF-Token": self.cookie_value(jar, portal.CSRF_COOKIE),
            },
            method="POST",
        )
        try:
            with client.open(request, timeout=5) as response:
                return response.status, json.loads(response.read()), response.headers
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read()), error.headers

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
        self.assertIn("验证邮件已发送", body)
        self.assertEqual(captured[0][0], email)
        return captured[0][1]

    def login_admin_api(self):
        client, jar = self.opener()
        status, _, _ = self.get(client, "/portal-api/public")
        self.assertEqual(status, 200)
        status, body, _ = self.json_post(
            client,
            jar,
            "/portal-api/auth/login",
            {"identity": "admin", "password": "correct horse battery staple"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["user"]["role"], "admin")
        return client, jar

    def test_verified_user_gets_one_time_key_quota_usage_models_and_curl(self):
        self.server.db.update_settings(
            {"default_welcome_balance": "25.5", "default_quota_tokens": "0"}
        )
        client, jar = self.opener()
        token = self.register(client, jar)
        status, body, _ = self.get(client, "/verify?token=" + urllib.parse.quote(token))
        self.assertEqual(status, 200)
        self.assertIn("Confirm email", body)
        csrf = self.cookie_value(jar, portal.CSRF_COOKIE)
        status, dashboard, _ = self.post(client, "/verify", {"csrf": csrf, "token": token})
        self.assertEqual(status, 200)
        raw_key = self.fake_omni.created[0][3]
        self.assertIn(("key-1", 1, "create"), self.fake_omni.session_limits)
        self.assertIn(raw_key, dashboard)
        self.assertIn("$25.5", dashboard)
        self.assertIn("ornith-1.0-35b-fp8", dashboard)
        self.assertIn("https://llm.example.test/v1/chat/completions", dashboard)
        self.assertIn("curl", dashboard)
        self.assertIn("Accept: application/json", dashboard)
        self.assertIn("&quot;stream&quot;:false", dashboard)

        status, second_view, _ = self.get(client, "/")
        self.assertEqual(status, 200)
        self.assertNotIn(raw_key, second_view)
        self.assertNotIn(raw_key.encode(), self.db_path.read_bytes())

        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT status,api_key_id,token_limit_id,max_sessions FROM users WHERE email=?",
                ("alice@example.com",),
            ).fetchone()
            audit_actions = {
                item[0] for item in connection.execute("SELECT action FROM audit_events")
            }
            balance = connection.execute(
                "SELECT balance_micros FROM billing_accounts WHERE user_id=(SELECT id FROM users WHERE email=?)",
                ("alice@example.com",),
            ).fetchone()[0]
            welcome_rows = connection.execute(
                "SELECT COUNT(*) FROM balance_transactions WHERE source_ref=(SELECT 'welcome-credit:' || id FROM users WHERE email=?)",
                ("alice@example.com",),
            ).fetchone()[0]
        self.assertEqual(row, ("active", "key-1", None, 1))
        self.assertEqual(balance, 25_500_000)
        self.assertEqual(welcome_rows, 1)
        self.assertIn("register.email", audit_actions)
        self.assertIn("verify.provision", audit_actions)

    def test_fresh_install_keeps_configured_welcome_cash_without_legacy_quota(self):
        fresh_path = pathlib.Path(self.tempdir.name) / "fresh" / "portal.db"
        fresh_config = dataclasses.replace(
            self.config,
            db_path=fresh_path,
            initial_quota=0,
            initial_welcome_balance_micros=42_500_000,
        )
        database = portal.Database(fresh_config)
        database.initialize()
        self.assertEqual(database.settings()["default_welcome_balance"], "42.5")
        self.assertEqual(database.settings()["default_quota_tokens"], "0")

    def test_login_reveals_the_same_existing_key_without_rotating_or_auditing_secret(self):
        raw_key = "sk-existing-stable-user-key-abcdefghijklmnopqrstuvwxyz"
        stamp = portal.now()
        with self.server.db.connect() as connection:
            connection.execute(
                "INSERT INTO users(id,email,login_name,password_hash,role,status,api_key_id,quota_tokens,quota_reset,quota_reset_time,created_at,verified_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "stable-user", "stable@example.com", "stable@example.com",
                    portal.hash_password("a secure password 123"), "user", "active",
                    "stable-key", 0, "monthly", "00:00", stamp, stamp,
                ),
            )
        self.fake_omni.created.append(
            ("stable-key", "stable-user", "stable@example.com", raw_key)
        )

        client, jar = self.opener()
        status, _, _ = self.get(client, "/portal-api/public")
        self.assertEqual(status, 200)
        status, body, _ = self.json_post(
            client,
            jar,
            "/portal-api/auth/login",
            {"identity": "stable@example.com", "password": "a secure password 123"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["user"]["id"], "stable-user")
        created_before = list(self.fake_omni.created)

        first_status, first, _ = self.json_post(client, jar, "/portal-api/key/reveal", {})
        second_status, second, _ = self.json_post(client, jar, "/portal-api/key/reveal", {})
        self.assertEqual((first_status, second_status), (200, 200))
        self.assertEqual(first["api_key"], raw_key)
        self.assertEqual(second["api_key"], raw_key)
        self.assertEqual(self.fake_omni.created, created_before)
        self.assertEqual(self.fake_omni.revealed, ["stable-key", "stable-key"])
        self.assertNotIn(raw_key.encode(), self.db_path.read_bytes())

        with self.server.db.connect() as connection:
            audit_details = [
                row[0]
                for row in connection.execute(
                    "SELECT detail FROM audit_events WHERE action='key/reveal'"
                )
            ]
        self.assertEqual(audit_details, ['{"ok":true}', '{"ok":true}'])

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
        self.assertIn("LLMCtl model service portal", body)
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

    def test_vue_registration_throttles_repeated_verification_email(self):
        client, jar = self.opener()
        status, _, _ = self.get(client, "/portal-api/public")
        self.assertEqual(status, 200)
        payload = {
            "email": "throttle@example.com",
            "password": "a secure password 123",
            "confirm": "a secure password 123",
        }
        captured = []
        with mock.patch.object(
            portal,
            "send_verification_email",
            side_effect=lambda config, recipient, token: captured.append((recipient, token)),
        ):
            first_status, first, _ = self.json_post(
                client, jar, "/portal-api/auth/register", payload
            )
            second_status, second, _ = self.json_post(
                client, jar, "/portal-api/auth/register", payload
            )
        self.assertEqual(first_status, 200)
        self.assertEqual(first["message"], "Verification email sent")
        self.assertEqual(second_status, 200)
        self.assertIn("If eligible", second["message"])
        self.assertEqual(len(captured), 1)
        with self.server.db.connect() as connection:
            action = connection.execute(
                "SELECT action FROM audit_events WHERE target=? ORDER BY id DESC LIMIT 1",
                ("throttle@example.com",),
            ).fetchone()["action"]
        self.assertEqual(action, "register.throttled")

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
                "identity": "admin",
                "password": "correct horse battery staple",
            },
        )
        self.assertEqual(status, 200)
        self.assertIn("Registration", body)
        self.assertNotIn("保存并设为", body)
        self.assertIn('<option value="active" selected>active</option>', body)
        self.assertIn('<option value="disabled" >disabled</option>', body)
        self.assertIn("账户策略由 LLMCtl 统一管理", body)

    def test_smtp_can_be_saved_and_tested_without_validating_registration_urls(self):
        client, jar = self.login_admin_api()
        with self.server.db.connect() as connection:
            connection.execute("UPDATE settings SET value='' WHERE key='public_url'")
        smtp = {
            "scope": "smtp",
            "smtp_host": "mail.example.com",
            "smtp_port": 587,
            "smtp_security": "starttls",
            "smtp_username": "admin@example.com",
            "smtp_password": "new-app-password",
            "smtp_from": "admin@example.com",
        }
        status, body, _ = self.json_post(
            client, jar, "/portal-api/admin/settings", smtp
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["scope"], "smtp")
        with self.server.db.connect() as connection:
            saved = dict(
                connection.execute(
                    "SELECT key,value FROM settings WHERE key IN ('public_url','smtp_host','smtp_username','smtp_password','smtp_from')"
                ).fetchall()
            )
        self.assertEqual(saved["public_url"], "")
        self.assertEqual(saved["smtp_host"], "mail.example.com")
        self.assertEqual(saved["smtp_password"], "new-app-password")

        captured = []
        current_form = smtp | {
            "smtp_host": "preview.example.com",
            "smtp_password": "preview-password",
            "recipient": "admin@example.com",
        }
        with mock.patch.object(
            portal,
            "send_test_email",
            side_effect=lambda config, recipient: captured.append((config, recipient)),
        ):
            status, body, _ = self.json_post(
                client, jar, "/portal-api/admin/smtp/test", current_form
            )
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(captured[0][0].smtp_host, "preview.example.com")
        self.assertEqual(captured[0][0].smtp_password, "preview-password")
        self.assertEqual(captured[0][1], "admin@example.com")

    def test_registration_settings_store_default_native_session_limit(self):
        client, jar = self.login_admin_api()
        status, body, _ = self.json_post(
            client,
            jar,
            "/portal-api/admin/settings",
            {
                "scope": "registration",
                "registration_enabled": True,
                "allowed_domains": "zjguardian.com",
                "default_welcome_balance": "88.5",
                "default_max_sessions": 1,
                "default_requests_per_minute": 45,
                "default_requests_per_day": 3000,
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        with self.server.db.connect() as connection:
            value = connection.execute(
                "SELECT value FROM settings WHERE key='default_max_sessions'"
            ).fetchone()["value"]
        self.assertEqual(value, "1")
        with self.server.db.connect() as connection:
            values = {
                row["key"]: row["value"]
                for row in connection.execute(
                    "SELECT key,value FROM settings WHERE key IN "
                    "('default_requests_per_minute','default_requests_per_day',"
                    "'default_welcome_balance','default_quota_tokens')"
                )
            }
        self.assertEqual(values["default_requests_per_minute"], "45")
        self.assertEqual(values["default_requests_per_day"], "3000")
        self.assertEqual(values["default_welcome_balance"], "88.5")
        self.assertEqual(values["default_quota_tokens"], "0")

        status, body, _ = self.json_post(
            client,
            jar,
            "/portal-api/admin/settings",
            {"scope": "registration", "default_welcome_balance": "0"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

        status, body, _ = self.json_post(
            client,
            jar,
            "/portal-api/admin/settings",
            {"scope": "registration", "default_max_sessions": -1},
        )
        self.assertEqual(status, 400)
        self.assertIn("0-10000", body["error"])

    def test_existing_database_migrates_session_limit_without_restricting_old_keys(self):
        legacy_path = pathlib.Path(self.tempdir.name) / "legacy" / "account-portal.db"
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(legacy_path) as connection:
            connection.executescript(
                """
                CREATE TABLE users (
                  id TEXT PRIMARY KEY,
                  email TEXT NOT NULL UNIQUE,
                  login_name TEXT,
                  password_hash TEXT NOT NULL,
                  role TEXT NOT NULL CHECK(role IN ('admin','user')),
                  status TEXT NOT NULL CHECK(status IN ('pending','active','disabled')),
                  api_key_id TEXT,
                  token_limit_id TEXT,
                  quota_tokens INTEGER NOT NULL,
                  quota_reset TEXT NOT NULL,
                  quota_reset_time TEXT NOT NULL,
                  created_at INTEGER NOT NULL,
                  verified_at INTEGER,
                  last_login_at INTEGER
                );
                """
            )
            connection.execute(
                "INSERT INTO users(id,email,login_name,password_hash,role,status,api_key_id,quota_tokens,quota_reset,quota_reset_time,created_at,verified_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "legacy-admin",
                    "admin@llmctl.local",
                    "admin",
                    portal.hash_admin_password("correct horse battery staple"),
                    "admin",
                    "active",
                    "legacy-key",
                    0,
                    "monthly",
                    "00:00",
                    portal.now(),
                    portal.now(),
                ),
            )

        legacy_config = portal.dataclasses.replace(self.config, db_path=legacy_path)
        portal.Database(legacy_config).initialize()
        with sqlite3.connect(legacy_path) as connection:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(users)")
            }
            existing_limit = connection.execute(
                "SELECT max_sessions FROM users WHERE id='legacy-admin'"
            ).fetchone()[0]
            default_limit = connection.execute(
                "SELECT value FROM settings WHERE key='default_max_sessions'"
            ).fetchone()[0]
        self.assertIn("max_sessions", columns)
        self.assertEqual(existing_limit, 0)
        self.assertEqual(default_limit, "1")

    def test_optional_published_origin_overrides_links_and_can_be_cleared(self):
        client, jar = self.login_admin_api()
        status, body, _ = self.json_post(
            client,
            jar,
            "/portal-api/admin/settings",
            {
                "scope": "publishing",
                "portal_title": "守护者 AI",
                "published_origin": "https://LLM.ZJGUARDIAN.COM:443/",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["portal_title"], "守护者 AI")
        self.assertEqual(body["published_origin"], "https://llm.zjguardian.com")
        self.assertEqual(body["portal_public_url"], "https://llm.zjguardian.com/ui")
        self.assertEqual(body["api_public_url"], "https://llm.zjguardian.com")

        status, raw, _ = self.get(client, "/portal-api/public")
        self.assertEqual(status, 200)
        public = json.loads(raw)
        self.assertEqual(public["portal_title"], "守护者 AI")
        self.assertEqual(public["portal_public_url"], "https://llm.zjguardian.com/ui")
        self.assertEqual(public["api_public_url"], "https://llm.zjguardian.com")
        mail = portal.effective_mail_config(self.config, self.server.db.settings())
        self.assertEqual(mail.public_url, "https://llm.zjguardian.com/ui")

        status, body, _ = self.json_post(
            client,
            jar,
            "/portal-api/admin/settings",
            {
                "scope": "publishing",
                "portal_title": "LLMCtl",
                "published_origin": "",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["published_origin"], "")
        self.assertEqual(body["api_public_url"], "https://llm.example.test")

        status, _, _ = self.json_post(
            client,
            jar,
            "/portal-api/admin/settings",
            {
                "scope": "publishing",
                "portal_title": "守护者 AI",
                "published_origin": "https://llm.zjguardian.com",
            },
        )
        self.assertEqual(status, 200)
        status, session_raw, session_headers = self.get(client, "/portal-api/session")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(session_raw)["authenticated"])
        upgraded = session_headers.get_all("Set-Cookie") or []
        self.assertFalse(
            any(portal.SESSION_COOKIE in item and "; Secure" in item for item in upgraded)
        )

        fresh_client, fresh_jar = self.opener()
        status, _, _ = self.get(fresh_client, "/portal-api/public")
        self.assertEqual(status, 200)
        csrf_cookie = next(
            cookie for cookie in fresh_jar if cookie.name == portal.CSRF_COOKIE
        )
        self.assertFalse(csrf_cookie.secure)

    def test_published_origin_rejects_paths_credentials_queries_and_controls(self):
        client, jar = self.login_admin_api()
        for value in (
            "https://llm.example.com/not-ui",
            "https://user:password@llm.example.com",
            "https://llm.example.com?next=evil",
            "https://llm.example.com/#fragment",
            "https://bad host.example.com",
            "https://llm.example.com\nX-Test: injected",
        ):
            status, body, _ = self.json_post(
                client,
                jar,
                "/portal-api/admin/settings",
                {"scope": "publishing", "published_origin": value},
            )
            self.assertEqual(status, 400, value)
            self.assertIn("error", body)

        for value in ("", "x" * 41, "LLMCtl\nInjected"):
            status, body, _ = self.json_post(
                client,
                jar,
                "/portal-api/admin/settings",
                {"scope": "publishing", "portal_title": value},
            )
            self.assertEqual(status, 400, value)
            self.assertIn("error", body)

    def insert_control_user_and_model(self, *, paid=False, source_kind="combo", upstream_free=0):
        stamp = portal.now()
        with self.server.db.connect() as connection:
            connection.execute(
                "INSERT INTO users(id,email,password_hash,role,status,api_key_id,quota_tokens,quota_reset,quota_reset_time,created_at,verified_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "policy-user", "policy@example.com", portal.hash_password("a secure password 123"),
                    "user", "active", "policy-key", 0, "monthly", "00:00", stamp, stamp,
                ),
            )
            connection.execute(
                "INSERT INTO billing_accounts(user_id,balance_micros,suspended,updated_at) VALUES(?,?,0,?)",
                ("policy-user", 1_000_000, stamp),
            )
            connection.execute(
                """INSERT INTO published_models(
                     id,public_model_id,display_name,source_kind,source_ref,source_provider,
                     source_model,capabilities_json,input_price_micros,output_price_micros,
                     cached_price_micros,reasoning_price_micros,status,upstream_free,mapping_kind,
                     mapping_id,health_status,health_failures,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "model-1", "gdn-inside", "GDN Inside", source_kind, "combo-internal",
                    "local", "ornith-1.0-35b-fp8", '["chat"]',
                    1_000_000 if paid else 0, 2_000_000 if paid else 0, 0, 0,
                    "published", upstream_free, "combo" if source_kind == "combo" else "alias",
                    "mapping-1" if source_kind == "combo" else "gdn-inside", "healthy", 0,
                    stamp, stamp,
                ),
            )
            connection.execute(
                "INSERT INTO model_access(model_id,subject_type,subject_id,created_at) VALUES('model-1','all','',?)",
                (stamp,),
            )

    def insert_disabled_underlying_model(self, created_at=None):
        stamp = created_at or portal.now()
        with self.server.db.connect() as connection:
            connection.execute(
                """INSERT INTO published_models(
                     id,public_model_id,display_name,source_kind,source_ref,source_provider,
                     source_model,capabilities_json,input_price_micros,output_price_micros,
                     cached_price_micros,reasoning_price_micros,status,upstream_free,mapping_kind,
                     mapping_id,health_status,health_failures,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "model-underlying", "ornith-1.0-35b-fp8", "Underlying model",
                    "model", "ornith-1.0-35b-fp8", "local", "ornith-1.0-35b-fp8",
                    '["chat"]', 0, 0, 0, 0, "disabled", 0, "alias",
                    "ornith-1.0-35b-fp8", "healthy", 0, stamp, stamp,
                ),
            )

    def test_existing_database_gains_model_metadata_columns_without_reinstall(self):
        with self.server.db.connect() as connection:
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(published_models)")
            }
        self.assertTrue(
            {
                "context_window_tokens",
                "max_output_tokens",
                "metadata_json",
                "metadata_sync_status",
                "metadata_sync_error",
                "metadata_synced_at",
            }.issubset(columns)
        )

    def test_usage_ledger_has_query_indexes_and_server_side_filters(self):
        self.insert_control_user_and_model()
        stamp = portal.now()
        with self.server.db.connect() as connection:
            for index in range(25):
                public_model = "gdn-inside" if index % 2 == 0 else "other-model"
                connection.execute(
                    """INSERT INTO usage_ledger(
                         request_id,user_id,api_key_id,model_id,public_model_id,
                         provider,resolved_model,input_tokens,output_tokens,cached_tokens,
                         reasoning_tokens,granted_tokens,amount_micros,price_snapshot_json,
                         occurred_at,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        f"request-page-{index}",
                        "policy-user",
                        "policy-key",
                        "model-1" if public_model == "gdn-inside" else None,
                        public_model,
                        "local",
                        public_model,
                        index,
                        1,
                        0,
                        0,
                        0,
                        0,
                        "{}",
                        stamp - index,
                        stamp,
                    ),
                )
            indexes = {
                row["name"] for row in connection.execute("PRAGMA index_list('usage_ledger')")
            }
        self.assertTrue(
            {
                "idx_usage_user_time_v2",
                "idx_usage_time",
                "idx_usage_model_time",
                "idx_usage_user_model_time",
                "idx_usage_model_fk",
            }.issubset(
                indexes
            )
        )
        result = self.server.control.usage_page(
            owner_user_id="policy-user",
            model_id="gdn-inside",
            page=2,
            page_size=5,
        )
        self.assertEqual(result["total"], 13)
        self.assertEqual(result["page"], 2)
        self.assertEqual(len(result["items"]), 5)
        self.assertEqual({row["public_model_id"] for row in result["items"]}, {"gdn-inside"})
        self.assertEqual(
            self.server.control.usage_page(owner_user_id="missing-user")["total"],
            0,
        )

    def test_admin_analytics_uses_local_buckets_and_does_not_double_count_subtokens(self):
        self.insert_control_user_and_model()
        zone = portal.ZoneInfo("Asia/Shanghai")
        current = int(datetime.datetime(2026, 8, 3, 10, 30, tzinfo=zone).timestamp())
        with self.server.db.connect() as connection:
            connection.execute(
                "INSERT INTO users(id,email,password_hash,role,status,api_key_id,quota_tokens,quota_reset,quota_reset_time,created_at,verified_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "analytics-user", "analytics@example.com", portal.hash_password("another password"),
                    "user", "active", "analytics-key", 0, "none", "00:00", current, current,
                ),
            )
            for request_id, user_id, hour, input_tokens, output_tokens, cached, reasoning in (
                ("analytics-a", "policy-user", 9, 100, 50, 20, 10),
                ("analytics-b", "analytics-user", 10, 40, 10, 5, 2),
            ):
                occurred = int(
                    datetime.datetime(2026, 8, 3, hour, 15, tzinfo=zone).timestamp()
                )
                connection.execute(
                    """INSERT INTO usage_ledger(
                         request_id,user_id,api_key_id,model_id,public_model_id,
                         provider,resolved_model,input_tokens,output_tokens,cached_tokens,
                         reasoning_tokens,granted_tokens,amount_micros,price_snapshot_json,
                         occurred_at,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        request_id, user_id, f"{user_id}-key", "model-1", "gdn-inside",
                        "local", "ornith", input_tokens, output_tokens, cached,
                        reasoning, 0, 100, "{}", occurred, current,
                    ),
                )
        with mock.patch.dict(portal.os.environ, {"TZ": "Asia/Shanghai"}), mock.patch.object(
            portal, "now", return_value=current
        ):
            result = self.server.control.admin_analytics(
                range_key="today", selected_user_id="policy-user"
            )
        self.assertEqual(result["timezone"], "Asia/Shanghai")
        self.assertEqual(result["range"]["grain"], "hour")
        self.assertEqual(len(result["timeseries"]), 11)
        self.assertEqual(result["summary"]["input_tokens"], 140)
        self.assertEqual(result["summary"]["output_tokens"], 60)
        self.assertEqual(result["summary"]["total_tokens"], 200)
        self.assertEqual(result["summary"]["cached_tokens"], 25)
        self.assertEqual(result["summary"]["reasoning_tokens"], 12)
        self.assertEqual(result["summary"]["active_users"], 2)
        self.assertEqual(result["top_users"][0]["email"], "policy@example.com")
        self.assertEqual(result["active_pagination"]["total"], 2)
        self.assertEqual(result["selected_user"]["summary"]["total_tokens"], 150)
        self.assertEqual(result["timeseries"][9]["total_tokens"], 150)
        self.assertEqual(result["timeseries"][10]["total_tokens"], 50)

    def test_bulk_user_policy_update_is_explicit_atomic_and_synced(self):
        self.insert_control_user_and_model()
        stamp = portal.now()
        with self.server.db.connect() as connection:
            connection.execute(
                "INSERT INTO users(id,email,password_hash,role,status,api_key_id,quota_tokens,quota_reset,quota_reset_time,created_at,verified_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "bulk-user", "bulk@example.com", portal.hash_password("another password"),
                    "user", "active", "bulk-key", 0, "none", "00:00", stamp, stamp,
                ),
            )
        result = self.server.control.bulk_update_user_policies(
            {
                "user_ids": ["policy-user", "bulk-user"],
                "max_sessions": 5,
                "requests_per_minute": 100,
                "requests_per_day": 8000,
            },
            "admin",
        )
        self.assertEqual(result["updated"], 2)
        self.assertEqual(result["synced"], 2)
        self.assertEqual(result["failed"], [])
        with self.server.db.connect() as connection:
            rows = connection.execute(
                "SELECT max_sessions,requests_per_minute,requests_per_day FROM users WHERE id IN ('policy-user','bulk-user')"
            ).fetchall()
        self.assertEqual(
            {(row["max_sessions"], row["requests_per_minute"], row["requests_per_day"]) for row in rows},
            {(5, 100, 8000)},
        )
        self.assertIn(("policy-key", False), self.fake_omni.activated)
        self.assertIn(("bulk-key", False), self.fake_omni.activated)
        self.assertIn(("policy-key", 100, 8000, "permissions"), self.fake_omni.rate_limits)
        self.assertIn(("bulk-key", 100, 8000, "permissions"), self.fake_omni.rate_limits)

    def test_bulk_user_policy_update_validates_full_scope_before_disabling_keys(self):
        self.insert_control_user_and_model()
        with self.assertRaisesRegex(ValueError, "不存在"):
            self.server.control.bulk_update_user_policies(
                {
                    "user_ids": ["policy-user", "missing-user"],
                    "requests_per_minute": 100,
                },
                "admin",
            )
        self.assertNotIn(("policy-key", False), self.fake_omni.activated)

    def test_bulk_user_policy_update_keeps_failed_key_disabled(self):
        self.insert_control_user_and_model()
        original_patch = self.fake_omni.patch_key_permissions

        def fail_policy_sync(key_id, *args, **kwargs):
            if key_id == "policy-key":
                raise RuntimeError("gateway policy sync failed")
            return original_patch(key_id, *args, **kwargs)

        with mock.patch.object(
            self.fake_omni,
            "patch_key_permissions",
            side_effect=fail_policy_sync,
        ):
            result = self.server.control.bulk_update_user_policies(
                {
                    "user_ids": ["policy-user"],
                    "requests_per_minute": 100,
                },
                "admin",
            )
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["synced"], 0)
        self.assertEqual(result["failed"][0]["user_id"], "policy-user")
        self.assertIn(("policy-key", False), self.fake_omni.activated)
        self.assertNotIn(("policy-key", True), self.fake_omni.activated)
        with self.server.db.connect() as connection:
            user = connection.execute(
                "SELECT requests_per_minute FROM users WHERE id='policy-user'"
            ).fetchone()
            sync = connection.execute(
                "SELECT status,error FROM permission_sync WHERE user_id='policy-user'"
            ).fetchone()
        self.assertEqual(user["requests_per_minute"], 100)
        self.assertEqual(sync["status"], "failed")
        self.assertIn("gateway policy sync failed", sync["error"])

    def test_admin_analytics_and_bulk_policy_http_routes_require_admin_session(self):
        self.insert_control_user_and_model()
        client, jar = self.login_admin_api()
        status, raw, _ = self.get(
            client,
            "/portal-api/admin/analytics?range=today&active_page=1&active_page_size=10",
        )
        self.assertEqual(status, 200)
        analytics = json.loads(raw)
        self.assertEqual(analytics["source"], "usage_ledger")
        self.assertEqual(analytics["token_definition"], "input_tokens + output_tokens")

        status, result, _ = self.json_post(
            client,
            jar,
            "/portal-api/admin/users/bulk-policy",
            {
                "user_ids": ["policy-user"],
                "max_sessions": 5,
                "requests_per_minute": 100,
                "requests_per_day": 8000,
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["updated"], 1)
        with self.server.db.connect() as connection:
            event = connection.execute(
                "SELECT status,detail FROM audit_events "
                "WHERE action='admin/users/bulk-policy' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(event["status"], "success")
        self.assertIn('"requests_per_minute":100', event["detail"])

    def test_combo_metadata_uses_conservative_limits_and_syncs_every_target(self):
        self.fake_omni.combo_items = [
            {
                "id": "combo-1",
                "name": "ornith-cluster",
                "models": [
                    {"kind": "model", "providerId": "local-a", "modelId": "ornith-a"},
                    {"kind": "model", "providerId": "local-b", "modelId": "ornith-b"},
                ],
            }
        ]
        inspected = self.server.control.inspect_model(
            {
                "source_kind": "combo",
                "source_ref": "combo-1",
                "source_model": "ornith-cluster",
            }
        )
        self.assertEqual(inspected["target_count"], 2)
        self.assertEqual(inspected["context_window_tokens"], 131072)
        self.assertEqual(inspected["max_output_tokens"], 16384)
        self.assertIn("vision", inspected["capabilities"])

        result = self.server.control.save_model(
            {
                "public_model_id": "gdn-inside",
                "display_name": "GDN Inside",
                "description": "公司内部模型",
                "source_kind": "combo",
                "source_ref": "combo-1",
                "source_model": "ornith-cluster",
                "capabilities": ["chat", "vision", "ocr"],
                "context_window_tokens": 200000,
                "max_output_tokens": 24000,
                "sync_context_window": True,
                "sync_max_output_tokens": True,
                "status": "draft",
                "access": [{"type": "all", "id": ""}],
            },
            "admin@example.com",
        )
        self.assertEqual(result["metadata_sync"]["status"], "synced")
        self.assertEqual(
            self.fake_omni.context_updates,
            [("local-a", "ornith-a", 200000), ("local-b", "ornith-b", 200000)],
        )
        self.assertEqual(
            self.fake_omni.output_updates,
            [("local-a", "ornith-a", 24000), ("local-b", "ornith-b", 24000)],
        )
        self.assertEqual(len(self.fake_omni.combo_upserts), 1)
        public_combo = self.fake_omni.combo_upserts[0][1]
        self.assertEqual(public_combo["name"], "gdn-inside")
        self.assertEqual(
            public_combo["description"], portal.PUBLIC_COMBO_MANAGED_DESCRIPTION
        )
        self.assertEqual(
            public_combo["models"],
            [
                {
                    "kind": "combo-ref",
                    "comboName": "ornith-cluster",
                    "label": "LLMCtl public model → ornith-cluster",
                }
            ],
        )
        with self.server.db.connect() as connection:
            route = connection.execute(
                "SELECT source_ref,source_model,mapping_kind,mapping_id "
                "FROM published_models WHERE public_model_id='gdn-inside'"
            ).fetchone()
        self.assertEqual(route["source_ref"], "combo-1")
        self.assertEqual(route["source_model"], "ornith-cluster")
        self.assertEqual(route["mapping_kind"], "native-combo")
        self.assertTrue(route["mapping_id"].startswith("public-combo-"))
        snapshot = self.server.control.admin_snapshot()
        model = next(item for item in snapshot["models"] if item["public_model_id"] == "gdn-inside")
        self.assertEqual(model["description"], "公司内部模型")
        self.assertEqual(model["context_window_tokens"], 200000)
        self.assertEqual(model["metadata_sync_status"], "synced")

    def test_permission_sync_exposes_only_public_model_id(self):
        self.insert_control_user_and_model()
        self.server.control.sync_user("policy-user")
        self.assertEqual(
            self.fake_omni.permissions[-1],
            ("policy-key", [], ["gdn-inside"], True),
        )
        self.assertNotIn("ornith-1.0-35b-fp8", self.fake_omni.permissions[-1][2])
        self.assertNotIn("combo-internal", self.fake_omni.permissions[-1][2])

    def test_legacy_combo_mapping_migrates_idempotently_with_sqlite_snapshot(self):
        self.insert_control_user_and_model()
        gateway_db = pathlib.Path(self.tempdir.name) / "gateway" / "storage.sqlite"
        gateway_db.parent.mkdir(parents=True)
        with sqlite3.connect(gateway_db) as connection:
            connection.execute("CREATE TABLE route_marker(value TEXT NOT NULL)")
            connection.execute("INSERT INTO route_marker(value) VALUES('before-migration')")
        self.fake_omni.combo_items = [
            {
                "id": "combo-internal",
                "name": "ornith-1.0-35b-fp8",
                "description": "worker pool",
                "models": [{"kind": "model", "providerId": "local", "modelId": "ornith"}],
            }
        ]
        before = self.server.control.public_combo_route_status()
        self.assertFalse(before["ready"])
        self.assertEqual(before["routes"][0]["reason"], "native combo missing")
        result = self.server.control.reconcile_public_combo_routes()
        self.assertEqual(result, {"migrated": 1, "unchanged": 0, "failed": 0})
        self.assertEqual(self.fake_omni.deleted_combo_mappings, ["mapping-1"])
        self.assertEqual(self.fake_omni.combo_items[0]["id"], "combo-internal")
        public = next(item for item in self.fake_omni.combo_items if item["name"] == "gdn-inside")
        self.assertEqual(public["models"][0]["comboName"], "ornith-1.0-35b-fp8")
        backup_dir = self.server.control.public_combo_backup_dir
        self.assertIsNotNone(backup_dir)
        manifest = json.loads(
            (backup_dir / "runtime-data" / "runtime-data.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["migration"], portal.PUBLIC_COMBO_MIGRATION_NAME)
        self.assertEqual(manifest["legacy_routes"][0]["mapping_id"], "mapping-1")
        by_role = {entry["role"]: entry for entry in manifest["databases"]}
        self.assertEqual(set(by_role), {"account-portal", "omniroute"})
        portal_copy = backup_dir / by_role["account-portal"]["backup"]
        with sqlite3.connect(portal_copy) as connection:
            self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
            old = connection.execute(
                "SELECT mapping_kind,mapping_id FROM published_models WHERE id='model-1'"
            ).fetchone()
        self.assertEqual(old, ("combo", "mapping-1"))
        gateway_copy = backup_dir / by_role["omniroute"]["backup"]
        with sqlite3.connect(gateway_copy) as connection:
            self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(
                connection.execute("SELECT value FROM route_marker").fetchone()[0],
                "before-migration",
            )

        again = self.server.control.reconcile_public_combo_routes()
        self.assertEqual(again, {"migrated": 0, "unchanged": 1, "failed": 0})
        self.assertEqual(
            len([item for item in self.fake_omni.combo_items if item["name"] == "gdn-inside"]),
            1,
        )
        self.server.control.sync_user("policy-user")
        self.assertEqual(self.fake_omni.permissions[-1], ("policy-key", [], ["gdn-inside"], True))
        after = self.server.control.public_combo_route_status()
        self.assertTrue(after["ready"])
        self.assertEqual(after["ready_count"], 1)
        self.assertEqual(after["routes"][0]["mapping_kind"], "native-combo")

    def test_portal_server_defers_route_migration_until_upgrade_acceptance_window(self):
        self.insert_control_user_and_model()
        self.fake_omni.combo_items = [
            {
                "id": "combo-internal",
                "name": "ornith-1.0-35b-fp8",
                "description": "worker pool",
                "models": [],
            }
        ]
        self.server.reconcile_public_routes_after_acceptance()
        self.assertEqual(self.fake_omni.combo_upserts, [])
        self.server.route_migration_due = 0
        self.server.reconcile_public_routes_after_acceptance()
        self.assertTrue(self.server.route_migration_finished)
        self.assertEqual(len(self.fake_omni.combo_upserts), 1)

    def test_delayed_route_migration_retries_per_model_failure(self):
        self.server.route_migration_due = 0
        with mock.patch.object(
            self.server.control,
            "reconcile_public_combo_routes",
            return_value={"migrated": 0, "unchanged": 0, "failed": 1},
        ) as reconcile:
            self.server.reconcile_public_routes_after_acceptance()
            self.assertFalse(self.server.route_migration_finished)
            self.assertGreater(self.server.route_migration_due, portal.time.monotonic())
            self.server.route_migration_due = 0
            reconcile.return_value = {"migrated": 1, "unchanged": 0, "failed": 0}
            self.server.reconcile_public_routes_after_acceptance()
        self.assertTrue(self.server.route_migration_finished)
        self.assertEqual(reconcile.call_count, 2)

    def test_unmanaged_native_combo_collision_leaves_legacy_route_untouched(self):
        self.insert_control_user_and_model()
        self.fake_omni.combo_items = [
            {"id": "combo-internal", "name": "ornith-1.0-35b-fp8", "models": []},
            {"id": "manual-public", "name": "gdn-inside", "description": "manual"},
        ]
        result = self.server.control.reconcile_public_combo_routes()
        self.assertEqual(result, {"migrated": 0, "unchanged": 0, "failed": 1})
        self.assertEqual(self.fake_omni.deleted_combo_mappings, [])
        with self.server.db.connect() as connection:
            route = connection.execute(
                "SELECT mapping_kind,mapping_id FROM published_models WHERE id='model-1'"
            ).fetchone()
        self.assertEqual(tuple(route), ("combo", "mapping-1"))

    def test_legacy_grant_cannot_reactivate_zero_balance_key(self):
        self.insert_control_user_and_model(paid=True)
        stamp = portal.now()
        with self.server.db.connect() as connection:
            connection.execute("UPDATE billing_accounts SET balance_micros=0 WHERE user_id='policy-user'")
            connection.execute(
                """INSERT INTO token_grants(
                     id,user_id,label,tokens_initial,tokens_remaining,reset_interval,reset_time,
                     reset_at,status,created_at,updated_at)
                   VALUES('grant-1','policy-user','Monthly',5000,0,'daily','03:30',?,'active',?,?)""",
                (stamp - 1, stamp, stamp),
            )
        self.assertEqual(self.server.control.reset_due_grants(), 1)
        with self.server.db.connect() as connection:
            grant = connection.execute(
                "SELECT tokens_remaining,reset_at FROM token_grants WHERE id='grant-1'"
            ).fetchone()
        self.assertEqual(grant["tokens_remaining"], 5000)
        self.assertGreater(grant["reset_at"], stamp)
        self.assertEqual(self.fake_omni.permissions[-1], ("policy-key", [], [], False))

    def test_usage_reconciliation_skips_in_memory_rows_and_is_idempotent(self):
        self.insert_control_user_and_model(paid=True, source_kind="model")
        stamp = portal.now()
        self.insert_disabled_underlying_model(stamp - 100)
        activations_before = list(self.fake_omni.activated)
        permissions_before = list(self.fake_omni.permissions)
        self.fake_omni.logs = [
            {
                "id": "request-live", "status": 200, "active": False,
                "detailState": "in-memory", "requestedModel": "gdn-inside",
                "model": "ornith-1.0-35b-fp8", "tokens": {"in": 0, "out": 0},
                "timestamp": stamp,
            },
            {
                "id": "request-complete", "status": 200, "active": False,
                "detailState": "persisted", "requestedModel": "ornith-1.0-35b-fp8",
                "model": "ornith-1.0-35b-fp8", "provider": "local",
                "tokens": {"in": 100, "out": 20, "cacheRead": 0, "reasoning": 0},
                "timestamp": stamp - 1,
            },
        ]
        first = self.server.control.reconcile_usage()
        second = self.server.control.reconcile_usage()
        with self.server.db.connect() as connection:
            rows = connection.execute(
                "SELECT request_id,public_model_id,input_tokens,output_tokens,"
                "gross_amount_micros,grant_amount_micros,amount_micros "
                "FROM usage_ledger"
            ).fetchall()
            balance = connection.execute(
                "SELECT balance_micros FROM billing_accounts WHERE user_id='policy-user'"
            ).fetchone()["balance_micros"]
        self.assertEqual(first["processed"], 1)
        self.assertEqual(first["users"], 1)
        self.assertEqual(first["policy_updates"], 0)
        self.assertEqual(second["processed"], 0)
        self.assertEqual(
            [tuple(row) for row in rows],
            [("request-complete", "gdn-inside", 100, 20, 140, 0, 140)],
        )
        self.assertEqual(balance, 999_860)
        self.assertEqual(self.fake_omni.activated, activations_before)
        self.assertEqual(self.fake_omni.permissions, permissions_before)

        self.fake_omni.call_log_details["request-complete"] = {
            "detailState": "ready",
            "hasRequestBody": True,
            "requestBody": {
                "messages": [
                    {"role": "system", "content": "回答要简洁"},
                    {"role": "user", "content": "请求内容可以显示吗？"},
                ]
            },
            "finalClientResponse": {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "可以，输出只向管理员显示。",
                        }
                    }
                ]
            },
        }
        detail = self.server.control.user_request_detail(
            "policy-user", "request-complete"
        )
        self.assertTrue(detail["available"])
        self.assertEqual(detail["messages"][-1]["content"], "请求内容可以显示吗？")
        self.assertNotIn("response_messages", detail)
        admin_detail = self.server.control.admin_request_detail("request-complete")
        self.assertEqual(admin_detail["messages"][0]["content"], "回答要简洁")
        self.assertTrue(admin_detail["response_available"])
        self.assertEqual(
            admin_detail["response_messages"][-1]["content"],
            "可以，输出只向管理员显示。",
        )
        snapshot = self.server.control.admin_snapshot()
        self.assertEqual(snapshot["usage"][0]["request_id"], "request-complete")
        self.assertEqual(snapshot["usage"][0]["user_email"], "policy@example.com")
        with self.assertRaisesRegex(ValueError, "请求记录不存在"):
            self.server.control.user_request_detail("another-user", "request-complete")

    def test_usage_reconciliation_repairs_disabled_underlying_model_attribution(self):
        self.insert_control_user_and_model(paid=True, source_kind="model")
        stamp = portal.now()
        self.insert_disabled_underlying_model(stamp - 100)
        with self.server.db.connect() as connection:
            connection.execute(
                """INSERT INTO usage_ledger(
                     request_id,user_id,api_key_id,model_id,public_model_id,
                     provider,resolved_model,input_tokens,output_tokens,cached_tokens,
                     reasoning_tokens,granted_tokens,amount_micros,price_snapshot_json,
                     occurred_at,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "request-underlying", "policy-user", "policy-key",
                    "model-underlying", "ornith-1.0-35b-fp8", "local",
                    "ornith-1.0-35b-fp8", 12, 34, 0, 0, 46, 1234,
                    '{"input_price_micros":1000000}', stamp + 1, stamp + 1,
                ),
            )
        result = self.server.control.reconcile_usage(user_id="policy-user")
        with self.server.db.connect() as connection:
            row = connection.execute(
                "SELECT model_id,public_model_id,amount_micros,price_snapshot_json "
                "FROM usage_ledger WHERE request_id='request-underlying'"
            ).fetchone()
        self.assertEqual(result["relabeled"], 1)
        self.assertEqual(row["model_id"], "model-1")
        self.assertEqual(row["public_model_id"], "gdn-inside")
        self.assertEqual(row["amount_micros"], 1234)
        self.assertEqual(row["price_snapshot_json"], '{"input_price_micros":1000000}')

    def test_admin_snapshot_remains_available_when_gateway_is_down(self):
        def unavailable():
            raise RuntimeError("gateway temporarily unavailable")

        self.fake_omni.models = unavailable
        snapshot = self.server.control.admin_snapshot()
        self.assertEqual(snapshot["gateway_models"], [])
        self.assertEqual(snapshot["combos"], [])
        self.assertIn("temporarily unavailable", snapshot["gateway_error"])
        self.assertIn("users", snapshot)
        self.assertIn("settings", snapshot)

    def test_invalid_group_is_rejected_before_user_key_is_disabled(self):
        self.insert_control_user_and_model()
        with self.assertRaisesRegex(ValueError, "group does not exist"):
            self.server.control.update_user(
                {
                    "user_id": "policy-user",
                    "status": "active",
                    "group_ids": ["missing-group"],
                    "balance_delta": "0",
                    "grant_tokens": 0,
                },
                "admin@example.com",
            )
        self.assertNotIn(("policy-key", False), self.fake_omni.activated)

    def test_admin_updates_native_key_active_session_limit(self):
        self.insert_control_user_and_model()
        self.server.control.update_user(
            {
                "user_id": "policy-user",
                "status": "active",
                "max_sessions": 1,
                "requests_per_minute": 60,
                "requests_per_day": 5000,
                "group_ids": ["default"],
                "balance_delta": "0",
                "grant_tokens": 0,
            },
            "admin@example.com",
        )
        with self.server.db.connect() as connection:
            value = connection.execute(
                "SELECT max_sessions FROM users WHERE id='policy-user'"
            ).fetchone()["max_sessions"]
        self.assertEqual(value, 1)
        self.assertIn(("policy-key", 1, "permissions"), self.fake_omni.session_limits)
        self.assertIn(
            ("policy-key", 60, 5000, "permissions"), self.fake_omni.rate_limits
        )

    def test_policy_sync_retires_legacy_native_token_limit(self):
        self.insert_control_user_and_model(paid=True)
        with self.server.db.connect() as connection:
            connection.execute(
                "UPDATE users SET token_limit_id='legacy-limit' WHERE id='policy-user'"
            )
        self.server.control.sync_user("policy-user")
        with self.server.db.connect() as connection:
            token_limit_id = connection.execute(
                "SELECT token_limit_id FROM users WHERE id='policy-user'"
            ).fetchone()["token_limit_id"]
        self.assertIsNone(token_limit_id)
        self.assertIn(("", "legacy-limit"), self.fake_omni.deleted)
        self.assertIn(("policy-key", False), self.fake_omni.activated)
        self.assertTrue(self.fake_omni.permissions[-1][3])

    def test_token_grants_cannot_be_reissued_after_cash_migration(self):
        self.insert_control_user_and_model(paid=True)
        with self.assertRaisesRegex(ValueError, "Token 赠额已停用"):
            self.server.control.update_user(
                {
                    "user_id": "policy-user",
                    "status": "active",
                    "group_ids": ["default"],
                    "balance_delta": "0",
                    "grant_tokens": 100,
                },
                "admin@example.com",
            )
        self.assertNotIn(("policy-key", False), self.fake_omni.activated)

    def test_upgrade_converts_remaining_tokens_to_cash_once_at_highest_price(self):
        self.insert_control_user_and_model(paid=True)
        stamp = portal.now()
        with self.server.db.connect() as connection:
            connection.execute(
                "UPDATE published_models SET input_price_micros=6000000,"
                "output_price_micros=12000000,cached_price_micros=2000000,"
                "reasoning_price_micros=3000000 WHERE id='model-1'"
            )
            connection.execute(
                "UPDATE billing_accounts SET balance_micros=0 "
                "WHERE user_id='policy-user'"
            )
            connection.execute(
                """INSERT INTO token_grants(
                     id,user_id,label,tokens_initial,tokens_remaining,
                     reset_interval,reset_time,status,created_at,updated_at)
                   VALUES('legacy-grant','policy-user','Legacy grant',100000000,
                          100000000,'weekly','00:00','active',?,?)""",
                (stamp, stamp),
            )
            connection.execute(
                "UPDATE settings SET value='100000000' "
                "WHERE key='default_quota_tokens'"
            )
            connection.execute(
                "DELETE FROM settings WHERE key='default_welcome_balance'"
            )

        self.server.db.initialize()
        with self.server.db.connect() as connection:
            balance = connection.execute(
                "SELECT balance_micros FROM billing_accounts "
                "WHERE user_id='policy-user'"
            ).fetchone()["balance_micros"]
            grant = connection.execute(
                "SELECT status,tokens_remaining,converted_amount_micros,"
                "conversion_rate_micros FROM token_grants "
                "WHERE id='legacy-grant'"
            ).fetchone()
            transaction_count = connection.execute(
                "SELECT COUNT(*) FROM balance_transactions "
                "WHERE source_ref='token-grant-conversion:legacy-grant'"
            ).fetchone()[0]
            settings = {
                row["key"]: row["value"]
                for row in connection.execute(
                    "SELECT key,value FROM settings WHERE key IN "
                    "('default_welcome_balance','default_quota_tokens',"
                    "'token_grant_conversion_status')"
                )
            }
        self.assertEqual(balance, 1_200_000_000)
        self.assertEqual(tuple(grant), ("disabled", 0, 1_200_000_000, 12_000_000))
        self.assertEqual(transaction_count, 1)
        self.assertEqual(settings["default_welcome_balance"], "1200")
        self.assertEqual(settings["default_quota_tokens"], "0")
        self.assertEqual(settings["token_grant_conversion_status"], "complete")

        self.server.db.initialize()
        with self.server.db.connect() as connection:
            balance_after_retry = connection.execute(
                "SELECT balance_micros FROM billing_accounts "
                "WHERE user_id='policy-user'"
            ).fetchone()["balance_micros"]
            transaction_count_after_retry = connection.execute(
                "SELECT COUNT(*) FROM balance_transactions "
                "WHERE source_ref='token-grant-conversion:legacy-grant'"
            ).fetchone()[0]
            audit_count = connection.execute(
                "SELECT COUNT(*) FROM audit_events "
                "WHERE action='billing.grant-to-cash' "
                "AND target='policy-user'"
            ).fetchone()[0]
        self.assertEqual(balance_after_retry, 1_200_000_000)
        self.assertEqual(transaction_count_after_retry, 1)
        self.assertEqual(audit_count, 1)

    def test_completed_request_that_exhausts_balance_keeps_key_disabled(self):
        self.insert_control_user_and_model(paid=True)
        stamp = portal.now()
        with self.server.db.connect() as connection:
            connection.execute(
                "UPDATE billing_accounts SET balance_micros=100 "
                "WHERE user_id='policy-user'"
            )
        self.fake_omni.logs = [
            {
                "id": "request-exhausts-balance",
                "status": 200,
                "active": False,
                "detailState": "persisted",
                "requestedModel": "gdn-inside",
                "model": "ornith-1.0-35b-fp8",
                "provider": "local",
                "tokens": {"in": 100, "out": 20},
                "timestamp": stamp,
            }
        ]
        result = self.server.control.reconcile_usage(user_id="policy-user")
        with self.server.db.connect() as connection:
            balance = connection.execute(
                "SELECT balance_micros FROM billing_accounts "
                "WHERE user_id='policy-user'"
            ).fetchone()["balance_micros"]
        self.assertEqual(result["processed"], 1)
        self.assertEqual(result["policy_updates"], 1)
        self.assertEqual(balance, -40)
        self.assertNotIn(("policy-key", False), self.fake_omni.activated)
        self.assertEqual(self.fake_omni.permissions[-1][-1], False)

    def test_exact_zero_balance_removes_paid_model_permission(self):
        self.insert_control_user_and_model(paid=True)
        stamp = portal.now()
        with self.server.db.connect() as connection:
            connection.execute(
                "UPDATE billing_accounts SET balance_micros=100 "
                "WHERE user_id='policy-user'"
            )
        # $1/1M input + $2/1M output: 60 input and 20 output tokens cost
        # exactly 100 micro-dollars, leaving neither a rounding remainder nor
        # permission to issue another paid request.
        self.fake_omni.logs = [
            {
                "id": "request-reaches-zero",
                "status": 200,
                "active": False,
                "detailState": "persisted",
                "requestedModel": "gdn-inside",
                "model": "ornith-1.0-35b-fp8",
                "provider": "local",
                "tokens": {"in": 60, "out": 20},
                "timestamp": stamp,
            }
        ]
        result = self.server.control.reconcile_usage(user_id="policy-user")
        with self.server.db.connect() as connection:
            balance = connection.execute(
                "SELECT balance_micros FROM billing_accounts "
                "WHERE user_id='policy-user'"
            ).fetchone()["balance_micros"]
        self.assertEqual(result["processed"], 1)
        self.assertEqual(result["policy_updates"], 1)
        self.assertEqual(balance, 0)
        self.assertNotIn(("policy-key", False), self.fake_omni.activated)
        self.assertEqual(self.fake_omni.permissions[-1][-1], False)

    def test_exhausted_balance_sync_failure_fails_closed(self):
        self.insert_control_user_and_model(paid=True)
        stamp = portal.now()
        with self.server.db.connect() as connection:
            connection.execute(
                "UPDATE billing_accounts SET balance_micros=100 "
                "WHERE user_id='policy-user'"
            )
        self.fake_omni.logs = [
            {
                "id": "request-exhausts-before-sync-failure",
                "status": 200,
                "active": False,
                "detailState": "persisted",
                "requestedModel": "gdn-inside",
                "model": "ornith-1.0-35b-fp8",
                "provider": "local",
                "tokens": {"in": 100, "out": 20},
                "timestamp": stamp,
            }
        ]
        with mock.patch.object(
            self.fake_omni,
            "patch_key_permissions",
            side_effect=RuntimeError("permission patch unavailable"),
        ):
            result = self.server.control.reconcile_usage(user_id="policy-user")
        self.assertEqual(result["processed"], 1)
        self.assertEqual(result["policy_updates"], 1)
        self.assertEqual(result["sync_failed"], 1)
        self.assertIn(("policy-key", False), self.fake_omni.activated)

    def test_invalid_active_session_limit_is_rejected_before_key_is_disabled(self):
        self.insert_control_user_and_model()
        with self.assertRaisesRegex(ValueError, "必须在 0-10000 之间"):
            self.server.control.update_user(
                {
                    "user_id": "policy-user",
                    "status": "active",
                    "max_sessions": 10001,
                    "group_ids": ["default"],
                    "balance_delta": "0",
                    "grant_tokens": 0,
                },
                "admin@example.com",
            )
        self.assertNotIn(("policy-key", False), self.fake_omni.activated)

    def test_chinese_group_name_is_normalized_and_duplicate_is_actionable(self):
        group_id = self.server.control.save_group(
            {"name": "  研发Ａ组  ", "description": "研发成员", "status": "active"}
        )
        with self.server.db.connect() as connection:
            name = connection.execute(
                "SELECT name FROM user_groups WHERE id=?", (group_id,)
            ).fetchone()["name"]
        self.assertEqual(name, "研发A组")
        with self.assertRaisesRegex(ValueError, "用户组名称已存在"):
            self.server.control.save_group(
                {"name": "研发A组", "description": "重复", "status": "active"}
            )

    def insert_free_resource(self, configured):
        stamp = portal.now()
        with self.server.db.connect() as connection:
            connection.execute(
                """INSERT INTO free_resources(
                     resource_key,provider,model_id,display_name,free_type,monthly_tokens,
                     credit_tokens,terms_status,configured,available,source_json,
                     discovered_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "agentrouter:claude-haiku-4-5-20251001", "agentrouter",
                    "claude-haiku-4-5-20251001", "Claude Haiku", "one-time-initial",
                    None, None, "", configured, 0, "{}", stamp, stamp,
                ),
            )

    def test_free_resource_requires_provider_configuration_before_live_test(self):
        self.insert_free_resource(0)
        with self.assertRaisesRegex(ValueError, "请先配置并启用"):
            self.server.control.test_free_resource(
                "agentrouter:claude-haiku-4-5-20251001"
            )
        self.assertEqual(self.fake_omni.tested_provider_models, [])

    def test_free_resource_test_uses_omniroute_native_provider_probe(self):
        self.insert_free_resource(1)
        result = self.server.control.test_free_resource(
            "agentrouter:claude-haiku-4-5-20251001"
        )
        self.assertEqual(result["status"], "healthy")
        self.assertEqual(
            self.fake_omni.tested_provider_models[-1],
            ("agentrouter", "claude-haiku-4-5-20251001"),
        )

    def test_native_hidden_free_resource_is_reconciled_from_omniroute(self):
        self.insert_free_resource(1)
        self.fake_omni.hidden_models = {
            "agentrouter": {"claude-haiku-4-5-20251001"}
        }
        result = self.server.control.refresh_free_resource_visibility()
        self.assertEqual(result, {"providers": 1, "failed": 0, "hidden": 1})
        with self.server.db.connect() as connection:
            resource = connection.execute(
                "SELECT native_visible FROM free_resources WHERE resource_key=?",
                ("agentrouter:claude-haiku-4-5-20251001",),
            ).fetchone()
        self.assertEqual(resource["native_visible"], 0)

    def test_stress_run_is_backend_owned_and_keeps_key_out_of_arguments(self):
        self.insert_control_user_and_model()
        self.fake_omni.combo_items = [
            {
                "id": "combo-internal",
                "name": "ornith-cluster",
                "models": [
                    {
                        "providerId": "local-a",
                        "connectionId": "conn-worker-0",
                        "label": "LLMCtl worker 0",
                    }
                ],
            }
        ]
        with self.assertRaisesRegex(ValueError, "高负载压测必须确认风险"):
            self.server.control.start_stress_run(
                {
                    "model": "gdn-inside",
                    "concurrency": 20,
                    "input_tokens": 100,
                    "output_tokens": 128,
                    "request_multiplier": 1,
                },
                "admin",
            )

        process = mock.Mock(pid=4321)
        process.wait.return_value = 0
        with (
            mock.patch.object(portal.subprocess, "Popen", return_value=process) as popen,
            mock.patch.object(
                self.server.control, "process_alive", return_value=True
            ),
        ):
            run = self.server.control.start_stress_run(
                {
                    "model": "gdn-inside",
                    "concurrency": 2,
                    "input_tokens": 100,
                    "output_tokens": 128,
                    "request_multiplier": 1,
                },
                "admin",
            )

        command = popen.call_args.args[0]
        environment = popen.call_args.kwargs["env"]
        self.assertEqual(run["status"], "starting")
        self.assertEqual(run["request_count"], 2)
        self.assertIn("llm_benchmark.py", " ".join(command))
        self.assertIn("--route-map", command)
        self.assertNotIn("sk-management-test", " ".join(command))
        self.assertEqual(
            environment["LLMCTL_BENCHMARK_API_KEY"], "sk-management-test"
        )
        self.assertEqual(popen.call_args.kwargs["stderr"], portal.subprocess.STDOUT)
        self.assertTrue(popen.call_args.kwargs["stdout"].closed)
        route_map_path = pathlib.Path(command[command.index("--route-map") + 1])
        self.assertEqual(
            json.loads(route_map_path.read_text(encoding="utf-8")),
            {
                "conn-worker-0": "LLMCtl worker 0",
                "local-a": "LLMCtl worker 0",
            },
        )

        with self.server.db.connect() as connection:
            indexes = {
                row["name"]
                for row in connection.execute("PRAGMA index_list('stress_runs')")
            }
        self.assertIn("idx_stress_runs_created", indexes)
        self.assertIn("idx_stress_runs_status", indexes)

        with self.server.db.connect() as connection:
            stored = connection.execute(
                "SELECT result_dir FROM stress_runs WHERE id=?", (run["id"],)
            ).fetchone()
            connection.execute(
                "UPDATE stress_runs SET status='canceling' WHERE id=?", (run["id"],)
            )
        pathlib.Path(stored["result_dir"], "status.json").write_text(
            json.dumps(
                {
                    "status": "running",
                    "metrics": {"completed": 1},
                    "gpu": {
                        "available": True,
                        "peak_concurrent_active_gpu_count": 2,
                        "gpus": [{"index": 0}, {"index": 1}],
                    },
                }
            ),
            encoding="utf-8",
        )
        with mock.patch.object(self.server.control, "process_alive", return_value=True):
            canceling = self.server.control.sync_stress_run(run["id"])
        self.assertEqual(canceling["status"], "canceling")
        self.assertEqual(canceling["gpu"]["peak_concurrent_active_gpu_count"], 2)
        with mock.patch.object(self.server.control, "process_alive", return_value=False):
            canceled = self.server.control.sync_stress_run(run["id"])
        self.assertEqual(canceled["status"], "canceled")

    def test_failed_free_model_is_withdrawn_after_three_health_failures(self):
        self.insert_control_user_and_model(source_kind="model", upstream_free=1)
        with self.server.db.connect() as connection:
            connection.execute(
                "UPDATE published_models SET health_failures=2 WHERE id='model-1'"
            )
        self.fake_omni.test_error = "free upstream unavailable"
        with self.assertRaisesRegex(RuntimeError, "free upstream unavailable"):
            self.server.control.test_published_model("model-1")
        with self.server.db.connect() as connection:
            model = connection.execute(
                "SELECT status,health_status,health_failures FROM published_models WHERE id='model-1'"
            ).fetchone()
        self.assertEqual(tuple(model), ("error", "failed", 3))
        self.assertEqual(self.fake_omni.deleted_aliases, ["gdn-inside"])
        self.assertIn(("policy-key", False), self.fake_omni.activated)
        self.assertFalse(self.fake_omni.permissions[-1][-1])


class PortalUnitTests(unittest.TestCase):
    def test_token_credit_conversion_uses_ceil_and_preserves_value(self):
        self.assertEqual(
            portal.tokens_to_money_micros(100_000_000, 12_000_000),
            1_200_000_000,
        )
        self.assertEqual(portal.tokens_to_money_micros(1, 1), 1)

    def test_usage_price_separates_list_price_grant_discount_and_wallet_debit(self):
        prices = {
            "input_price_micros": 1_000_000,
            "output_price_micros": 4_000_000,
            "cached_price_micros": 200_000,
            "reasoning_price_micros": 6_000_000,
        }
        result = portal.price_usage(
            input_tokens=100,
            output_tokens=20,
            cached_tokens=40,
            reasoning_tokens=5,
            prices=prices,
            available_grant_tokens=10,
        )
        # Gross: input 60 + cache 8 + output 60 + reasoning 30 = 158 micro-USD.
        # Ten grants cover the five reasoning and five output tokens first:
        # 30 + 20 = 50 micro-USD, leaving 108 micro-USD for the wallet.
        self.assertEqual(result["gross_amount_micros"], 158)
        self.assertEqual(result["grant_amount_micros"], 50)
        self.assertEqual(result["amount_micros"], 108)
        self.assertEqual(result["granted_tokens"], 10)

    def test_free_token_classes_do_not_burn_promotional_grants(self):
        result = portal.price_usage(
            100, 20, 0, 0,
            {
                "input_price_micros": 0,
                "output_price_micros": 0,
                "cached_price_micros": 0,
                "reasoning_price_micros": 0,
            },
            1000,
        )
        self.assertEqual(result["granted_tokens"], 0)
        self.assertEqual(result["amount_micros"], 0)

    def test_usage_price_clamps_invalid_cache_and_reasoning_subtotals(self):
        result = portal.price_usage(
            10, 5, 50, 20,
            {
                "input_price_micros": 1_000_000,
                "output_price_micros": 2_000_000,
                "cached_price_micros": 100_000,
                "reasoning_price_micros": 3_000_000,
            },
        )
        # Cache and reasoning are subsets of input/output, not extra tokens.
        self.assertEqual(result["gross_amount_micros"], 16)
        self.assertEqual(result["amount_micros"], 16)

    def test_native_key_payload_and_policy_patch_include_max_sessions(self):
        config = mock.Mock(
            gateway_manage_key="sk-management-test",
            gateway_url="http://127.0.0.1:18000",
        )
        client = portal.OmniRouteClient(config)
        calls = []

        def request(method, path, payload=None):
            calls.append((method, path, payload))
            if method == "POST":
                return {
                    "id": "key-1",
                    "key": "sk-new-native-key-abcdefghijklmnopqrstuvwxyz",
                }
            return {}

        client.request = request
        client.create_user_key("user-1", "user@example.com", 1)
        client.patch_key_permissions("key-1", ["model-a"], [], True, 3, 60, 5000)
        self.assertEqual(calls[0][2]["maxSessions"], 1)
        self.assertEqual(calls[1][2]["maxSessions"], 3)
        self.assertEqual(
            calls[1][2]["rateLimits"],
            [{"limit": 60, "window": 60}, {"limit": 5000, "window": 86400}],
        )

    def test_existing_key_reveal_enables_omniroute_native_flag_without_restart(self):
        config = mock.Mock(
            gateway_manage_key="sk-management-test",
            gateway_url="http://127.0.0.1:18000",
        )
        client = portal.OmniRouteClient(config)
        calls = []

        def request(method, path, payload=None):
            calls.append((method, path, payload))
            if len(calls) == 1:
                raise RuntimeError(
                    "OmniRoute GET /api/keys/key-1/reveal: HTTP 403: API key reveal is disabled"
                )
            if method == "GET":
                return {"key": "sk-existing-stable-key-abcdefghijklmnopqrstuvwxyz"}
            return {"effectiveValue": "true"}

        client.request = request
        self.assertEqual(
            client.reveal_user_key("key-1"),
            "sk-existing-stable-key-abcdefghijklmnopqrstuvwxyz",
        )
        self.assertEqual(
            calls,
            [
                ("GET", "/api/keys/key-1/reveal", None),
                (
                    "PUT",
                    "/api/settings/feature-flags",
                    {"key": "ALLOW_API_KEY_REVEAL", "value": "true"},
                ),
                ("GET", "/api/keys/key-1/reveal", None),
            ],
        )

    def test_model_health_check_disables_thinking_and_has_a_bounded_timeout(self):
        request_state = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            @staticmethod
            def read():
                return b'{"choices":[{"message":{"content":"OK"}}]}'

        class Opener:
            @staticmethod
            def open(request, timeout):
                request_state["payload"] = json.loads(request.data)
                request_state["timeout"] = timeout
                return Response()

        config = mock.Mock(
            gateway_manage_key="sk-management-test",
            gateway_url="http://127.0.0.1:18000",
        )
        client = portal.OmniRouteClient(config)
        client.opener = Opener()
        _, content = client.test_model("gdn-inside")
        self.assertEqual(content, "OK")
        self.assertEqual(request_state["timeout"], 60)
        self.assertFalse(request_state["payload"]["stream"])
        self.assertEqual(request_state["payload"]["reasoning_effort"], "none")
        self.assertEqual(
            request_state["payload"]["chat_template_kwargs"], {"enable_thinking": False}
        )

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
        self.assertEqual(portal.normalize_domains("Example.COM,example.com"), ["example.com"])
        self.assertEqual(portal.normalize_email("USER@Example.com"), ("user@example.com", "example.com"))
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
            portal.datetime.datetime.fromtimestamp(daily, portal.ZoneInfo("Asia/Shanghai")),
            portal.datetime.datetime(2026, 8, 3, 3, 30, tzinfo=portal.ZoneInfo("Asia/Shanghai")),
        )
        self.assertEqual(
            portal.datetime.datetime.fromtimestamp(weekly, portal.ZoneInfo("Asia/Shanghai")),
            portal.datetime.datetime(2026, 8, 3, 3, 30, tzinfo=portal.ZoneInfo("Asia/Shanghai")),
        )
        self.assertEqual(
            portal.datetime.datetime.fromtimestamp(monthly, portal.ZoneInfo("Asia/Shanghai")),
            portal.datetime.datetime(2026, 9, 1, 3, 30, tzinfo=portal.ZoneInfo("Asia/Shanghai")),
        )


if __name__ == "__main__":
    unittest.main()
