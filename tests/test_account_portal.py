#!/usr/bin/env python3
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
        self.deleted_aliases = []
        self.logs = []
        self.test_error = None
        self.tested_models = []
        self.tested_provider_models = []
        self.hidden_models = {}
        self.call_log_details = {}
        self.combo_items = []
        self.context_updates = []
        self.output_updates = []
        self.aliases = []
        self.revealed = []

    def create_user_key(self, user_id, email):
        key_id = f"key-{len(self.created) + 1}"
        raw = f"sk-user-secret-{len(self.created) + 1}-abcdefghijklmnopqrstuvwxyz"
        self.created.append((key_id, user_id, email, raw))
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

    def combos(self):
        return self.combo_items

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

    def patch_key_permissions(self, key_id, allowed_models, allowed_combos, active):
        self.permissions.append((key_id, allowed_models, allowed_combos, active))

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

    def set_model_alias(self, public_id, source_model):
        self.aliases.append((public_id, source_model))
        return public_id


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
        self.assertIn("Accept: application/json", dashboard)
        self.assertIn("&quot;stream&quot;:false", dashboard)

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

    def test_due_recurring_grant_reactivates_key_without_waiting_for_request(self):
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
        self.assertEqual(self.fake_omni.permissions[-1], ("policy-key", [], ["gdn-inside"], True))

    def test_usage_reconciliation_skips_in_memory_rows_and_is_idempotent(self):
        self.insert_control_user_and_model(paid=True, source_kind="model")
        stamp = portal.now()
        self.insert_disabled_underlying_model(stamp - 100)
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
                "SELECT request_id,public_model_id,input_tokens,output_tokens FROM usage_ledger"
            ).fetchall()
        self.assertEqual(first["processed"], 1)
        self.assertEqual(second["processed"], 0)
        self.assertEqual(
            [tuple(row) for row in rows],
            [("request-complete", "gdn-inside", 100, 20)],
        )
        self.assertIn(("policy-key", False), self.fake_omni.activated)
        self.assertTrue(self.fake_omni.permissions[-1][-1])

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
