#!/usr/bin/env python3
"""Small, dependency-free account portal for LLMCtl's OmniRoute mode.

The portal owns a separate SQLite database.  It never reads or mutates
OmniRoute's SQLite schema; all gateway operations use documented HTTP APIs.
API key plaintext is returned once and is never persisted by the portal.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import dataclasses
import hashlib
import hmac
import html
import http.cookies
import http.server
import json
import os
import pathlib
import re
import secrets
import shlex
import smtplib
import sqlite3
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from email.message import EmailMessage
from typing import Any


APP_VERSION = "1.0.0"
SESSION_COOKIE = "llm_account_session"
CSRF_COOKIE = "llm_account_csrf"
MAX_FORM_BYTES = 64 * 1024
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+$")
DOMAIN_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
DUMMY_PASSWORD_HASH = "pbkdf2_sha256$600000$bGxtY3RsLWR1bW15LXNhbHQ$O5LpuYky-CKHcJaJEAX3-3B1rSxvRmdsFnyMXd5fUrg"


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError as error:
        raise SystemExit(f"{name} must be an integer") from error


def normalize_domain(value: str) -> str:
    value = value.strip().lower().rstrip(".")
    try:
        value = value.encode("idna").decode("ascii")
    except UnicodeError as error:
        raise ValueError("invalid email domain") from error
    if not value or len(value) > 253 or not DOMAIN_RE.fullmatch(value):
        raise ValueError("invalid email domain")
    return value


def normalize_domains(value: str) -> list[str]:
    result: list[str] = []
    for raw in value.split(","):
        if not raw.strip():
            continue
        domain = normalize_domain(raw)
        if domain not in result:
            result.append(domain)
    return result


def normalize_email(value: str) -> tuple[str, str]:
    value = value.strip().lower()
    if len(value) > 254 or not EMAIL_RE.fullmatch(value):
        raise ValueError("invalid email")
    local, raw_domain = value.rsplit("@", 1)
    if not local or len(local) > 64:
        raise ValueError("invalid email")
    domain = normalize_domain(raw_domain)
    return f"{local}@{domain}", domain


def hash_password(password: str, salt: bytes | None = None) -> str:
    if not 12 <= len(password) <= 200:
        raise ValueError("password length must be 12-200")
    salt = salt or secrets.token_bytes(16)
    iterations = 600_000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations, dklen=32)
    return "pbkdf2_sha256$600000$%s$%s" % (
        base64.urlsafe_b64encode(salt).decode().rstrip("="),
        base64.urlsafe_b64encode(digest).decode().rstrip("="),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_raw, expected_raw = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_raw + "=" * (-len(salt_raw) % 4))
        expected = base64.urlsafe_b64decode(expected_raw + "=" * (-len(expected_raw) % 4))
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), salt, int(iterations), dklen=len(expected)
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def now() -> int:
    return int(time.time())


@dataclasses.dataclass(frozen=True)
class Config:
    bind: str
    port: int
    db_path: pathlib.Path
    gateway_url: str
    gateway_manage_key: str
    public_url: str
    api_public_url: str
    admin_email: str
    admin_password: str
    initial_registration: bool
    initial_domains: list[str]
    initial_quota: int
    initial_reset: str
    initial_reset_time: str
    verification_ttl: int
    cookie_secure: bool
    smtp_host: str
    smtp_port: int
    smtp_security: str
    smtp_username: str
    smtp_password: str
    smtp_from: str
    supports_ocr: bool

    @classmethod
    def from_env(cls) -> "Config":
        public_url = os.environ.get("ACCOUNT_PUBLIC_URL", "http://127.0.0.1:8001").rstrip("/")
        api_public = os.environ.get("ACCOUNT_API_PUBLIC_URL", "").rstrip("/")
        for name, value in (("ACCOUNT_PUBLIC_URL", public_url), ("ACCOUNT_API_PUBLIC_URL", api_public)):
            if value:
                try:
                    parsed_value = urllib.parse.urlsplit(value)
                    parsed_value.port
                except ValueError as error:
                    raise SystemExit(f"{name} must be a valid http(s) origin") from error
                if (
                    parsed_value.scheme not in {"http", "https"}
                    or not parsed_value.hostname
                    or parsed_value.username
                    or parsed_value.password
                    or parsed_value.path not in {"", "/"}
                    or parsed_value.query
                    or parsed_value.fragment
                ):
                    raise SystemExit(f"{name} must be an http(s) origin without credentials or a path")
        if not api_public:
            parsed = urllib.parse.urlsplit(public_url)
            api_port = env_int("API_PORT", 8000)
            hostname = parsed.hostname or "127.0.0.1"
            host = f"[{hostname}]" if ":" in hostname else hostname
            api_public = f"{parsed.scheme or 'http'}://{host}:{api_port}"
        domains = normalize_domains(os.environ.get("ACCOUNT_ALLOWED_EMAIL_DOMAINS", ""))
        reset = os.environ.get("ACCOUNT_QUOTA_RESET", "monthly").strip().lower()
        if reset not in {"daily", "weekly", "monthly"}:
            raise SystemExit("ACCOUNT_QUOTA_RESET must be daily, weekly, or monthly")
        reset_time = os.environ.get("ACCOUNT_QUOTA_RESET_TIME", "00:00")
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", reset_time):
            raise SystemExit("ACCOUNT_QUOTA_RESET_TIME must be HH:MM")
        smtp_security = os.environ.get("SMTP_SECURITY", "starttls").lower()
        if smtp_security not in {"starttls", "ssl", "plain"}:
            raise SystemExit("SMTP_SECURITY must be starttls, ssl, or plain")
        initial_registration = env_bool("ACCOUNT_REGISTRATION_ENABLED", False)
        smtp_host = os.environ.get("SMTP_HOST", "").strip()
        smtp_from = os.environ.get("SMTP_FROM", "").strip()
        try:
            admin_email, _ = normalize_email(
                os.environ.get("ACCOUNT_ADMIN_EMAIL", "admin@llmctl.local")
            )
        except ValueError as error:
            raise SystemExit("ACCOUNT_ADMIN_EMAIL is invalid") from error
        port = env_int("ACCOUNT_PORT", 8001)
        smtp_port = env_int("SMTP_PORT", 587)
        initial_quota = env_int("ACCOUNT_DEFAULT_QUOTA_TOKENS", 1000000)
        verification_ttl = env_int("ACCOUNT_VERIFICATION_TTL", 86400)
        if not 1 <= port <= 65535:
            raise SystemExit("ACCOUNT_PORT must be 1-65535")
        if not 1 <= smtp_port <= 65535:
            raise SystemExit("SMTP_PORT must be 1-65535")
        if not 1 <= initial_quota <= 10**12:
            raise SystemExit("ACCOUNT_DEFAULT_QUOTA_TOKENS must be 1-1000000000000")
        if not 300 <= verification_ttl <= 7 * 86400:
            raise SystemExit("ACCOUNT_VERIFICATION_TTL must be 300-604800 seconds")
        if smtp_from:
            try:
                normalize_email(smtp_from)
            except ValueError as error:
                raise SystemExit("SMTP_FROM is invalid") from error
        if initial_registration and (not public_url or not domains or not smtp_host or not smtp_from):
            raise SystemExit(
                "registration requires ACCOUNT_PUBLIC_URL, ACCOUNT_ALLOWED_EMAIL_DOMAINS, SMTP_HOST, and SMTP_FROM"
            )
        db_path = pathlib.Path(
            os.environ.get(
                "ACCOUNT_DB_PATH", "/var/lib/llm-cluster/omniroute/portal/account-portal.db"
            )
        )
        gateway_db_path = pathlib.Path(
            os.environ.get(
                "OMNIROUTE_DB_PATH", "/var/lib/llm-cluster/omniroute/gateway/storage.sqlite"
            )
        )
        if db_path.resolve(strict=False) == gateway_db_path.resolve(strict=False):
            raise SystemExit("ACCOUNT_DB_PATH must not be OmniRoute's storage.sqlite")
        return cls(
            bind=os.environ.get("ACCOUNT_BIND", "0.0.0.0"),
            port=port,
            db_path=db_path,
            gateway_url=os.environ.get(
                "ACCOUNT_GATEWAY_LOCAL_URL",
                f"http://127.0.0.1:{env_int('API_PORT', 8000)}",
            ).rstrip("/"),
            gateway_manage_key=os.environ.get("GATEWAY_API_KEY", ""),
            public_url=public_url,
            api_public_url=api_public,
            admin_email=admin_email,
            admin_password=os.environ.get("ACCOUNT_ADMIN_PASSWORD", os.environ.get("UI_PASSWORD", "")),
            initial_registration=initial_registration,
            initial_domains=domains,
            initial_quota=initial_quota,
            initial_reset=reset,
            initial_reset_time=reset_time,
            verification_ttl=verification_ttl,
            cookie_secure=env_bool("ACCOUNT_COOKIE_SECURE", public_url.startswith("https://")),
            smtp_host=smtp_host,
            smtp_port=smtp_port,
            smtp_security=smtp_security,
            smtp_username=os.environ.get("SMTP_USERNAME", ""),
            smtp_password=os.environ.get("SMTP_PASSWORD", ""),
            smtp_from=smtp_from,
            supports_ocr=env_bool("SUPPORTS_OCR", False),
        )


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  email TEXT NOT NULL UNIQUE,
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
CREATE TABLE IF NOT EXISTS verification_tokens (
  token_hash TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  expires_at INTEGER NOT NULL,
  used_at INTEGER,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  csrf_token TEXT NOT NULL,
  expires_at INTEGER NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS login_failures (
  identity_hash TEXT PRIMARY KEY,
  attempts INTEGER NOT NULL,
  window_started_at INTEGER NOT NULL,
  locked_until INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS audit_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at INTEGER NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  target TEXT NOT NULL,
  status TEXT NOT NULL,
  remote_addr TEXT NOT NULL,
  detail TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_verify_user ON verification_tokens(user_id);
"""


class Database:
    def __init__(self, config: Config):
        self.config = config

    @contextlib.contextmanager
    def connect(self):
        connection = sqlite3.connect(self.config.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        self.config.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            defaults = {
                "registration_enabled": "1" if self.config.initial_registration else "0",
                "allowed_domains": ",".join(self.config.initial_domains),
                "default_quota_tokens": str(self.config.initial_quota),
                "default_quota_reset": self.config.initial_reset,
                "default_quota_reset_time": self.config.initial_reset_time,
            }
            for key, value in defaults.items():
                connection.execute(
                    "INSERT OR IGNORE INTO settings(key,value,updated_at) VALUES(?,?,?)",
                    (key, value, now()),
                )
            admin = connection.execute(
                "SELECT id FROM users WHERE role='admin' LIMIT 1"
            ).fetchone()
            if not admin:
                if not self.config.admin_password:
                    raise SystemExit("ACCOUNT_ADMIN_PASSWORD (or UI_PASSWORD) is required")
                connection.execute(
                    "INSERT INTO users(id,email,password_hash,role,status,quota_tokens,quota_reset,quota_reset_time,created_at,verified_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        str(uuid.uuid4()),
                        self.config.admin_email,
                        hash_password(self.config.admin_password),
                        "admin",
                        "active",
                        0,
                        self.config.initial_reset,
                        self.config.initial_reset_time,
                        now(),
                        now(),
                    ),
                )
        os.chmod(self.config.db_path, 0o600)

    def settings(self) -> dict[str, str]:
        with self.connect() as connection:
            return {row["key"]: row["value"] for row in connection.execute("SELECT key,value FROM settings")}

    def audit(
        self, actor: str, action: str, target: str, status: str, remote: str, detail: Any = ""
    ) -> None:
        if not isinstance(detail, str):
            detail = json.dumps(detail, ensure_ascii=False, separators=(",", ":"))
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO audit_events(created_at,actor,action,target,status,remote_addr,detail) VALUES(?,?,?,?,?,?,?)",
                (now(), actor, action, target, status, remote, detail[:2000]),
            )


class OmniRouteClient:
    def __init__(self, config: Config):
        if not config.gateway_manage_key:
            raise RuntimeError("GATEWAY_API_KEY is missing")
        self.base_url = config.gateway_url
        self.key = config.gateway_manage_key
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, method: str, path: str, payload: Any = None) -> dict[str, Any]:
        data = None
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self.key}"}
        if payload is not None:
            data = json.dumps(payload, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=data, headers=headers, method=method
        )
        try:
            with self.opener.open(request, timeout=15) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")[:500]
            raise RuntimeError(f"OmniRoute {method} {path}: HTTP {error.code}: {detail}") from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"OmniRoute {method} {path}: {error.reason}") from error
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as error:
            raise RuntimeError(f"OmniRoute {method} {path}: invalid JSON") from error
        if not isinstance(parsed, dict):
            raise RuntimeError(f"OmniRoute {method} {path}: unexpected response")
        return parsed

    def create_user_key(self, user_id: str, email: str) -> tuple[str, str]:
        response = self.request(
            "POST",
            "/api/keys",
            {"name": f"portal:{user_id}:{email}", "scopes": ["self:usage"]},
        )
        key_id, raw_key = str(response.get("id", "")), str(response.get("key", ""))
        if not key_id or len(raw_key) < 16:
            raise RuntimeError("OmniRoute did not return the new API key")
        return key_id, raw_key

    def set_limit(
        self,
        key_id: str,
        quota: int,
        reset: str,
        reset_time: str,
        limit_id: str | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "apiKeyId": key_id,
            "scopeType": "global",
            "scopeValue": "",
            "tokenLimit": quota,
            "resetInterval": reset,
            "resetTime": reset_time,
            "enabled": True,
        }
        if limit_id:
            payload["id"] = limit_id
        response = self.request("POST", "/api/usage/token-limits", payload)
        limit_data = response.get("limit") or {}
        result = str(limit_data.get("id", "")) if isinstance(limit_data, dict) else ""
        if not result:
            raise RuntimeError("OmniRoute did not return the token limit id")
        return result

    def delete_key(self, key_id: str) -> None:
        self.request("DELETE", f"/api/keys/{urllib.parse.quote(key_id, safe='')}")

    def delete_limit(self, limit_id: str) -> None:
        self.request(
            "DELETE",
            f"/api/usage/token-limits?id={urllib.parse.quote(limit_id, safe='')}",
        )

    def delete_key_and_limit(self, key_id: str, limit_id: str = "") -> None:
        if limit_id:
            with contextlib.suppress(RuntimeError):
                self.delete_limit(limit_id)
        self.delete_key(key_id)

    def activate_key(self, key_id: str, active: bool) -> None:
        self.request(
            "PATCH",
            f"/api/keys/{urllib.parse.quote(key_id, safe='')}",
            {"isActive": active},
        )

    def usage(self, key_id: str) -> dict[str, Any]:
        return self.request(
            "GET", f"/api/usage/token-limits?apiKeyId={urllib.parse.quote(key_id, safe='')}"
        )

    def models(self) -> list[dict[str, Any]]:
        response = self.request("GET", "/v1/models")
        data = response.get("data", [])
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def send_verification_email(config: Config, recipient: str, raw_token: str) -> None:
    if not config.smtp_host or not config.smtp_from:
        raise RuntimeError("SMTP is not configured")
    verify_url = f"{config.public_url}/verify?token={urllib.parse.quote(raw_token)}"
    message = EmailMessage()
    message["Subject"] = "验证您的 LLM API 账户 / Verify your LLM API account"
    message["From"] = config.smtp_from
    message["To"] = recipient
    message.set_content(
        "请打开下面的链接并确认邮箱，链接仅在限定时间内有效：\n"
        f"{verify_url}\n\n"
        "Open the link below to verify your email. The link expires automatically:\n"
        f"{verify_url}\n"
    )
    context = ssl.create_default_context()
    if config.smtp_security == "ssl":
        client: smtplib.SMTP = smtplib.SMTP_SSL(
            config.smtp_host, config.smtp_port, timeout=20, context=context
        )
    else:
        client = smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=20)
    try:
        client.ehlo()
        if config.smtp_security == "starttls":
            client.starttls(context=context)
            client.ehlo()
        if config.smtp_username:
            client.login(config.smtp_username, config.smtp_password)
        client.send_message(message)
    finally:
        with contextlib.suppress(Exception):
            client.quit()


STYLE = """
:root{color-scheme:dark;--bg:#0b1020;--panel:#131a2b;--muted:#91a0b8;--line:#26314a;--blue:#4d8dff;--green:#42d39c;--red:#ff6b7a}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 10% 0,#162447 0,var(--bg) 34%);color:#eef3ff;font:15px/1.55 system-ui,-apple-system,sans-serif}a{color:#8db6ff}.shell{max-width:1120px;margin:auto;padding:28px 18px 60px}.nav{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:24px}.brand{font-weight:750;font-size:20px}.sub,.muted{color:var(--muted)}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(290px,1fr));gap:16px}.card{background:rgba(19,26,43,.94);border:1px solid var(--line);border-radius:16px;padding:20px;box-shadow:0 12px 40px #0004}.wide{grid-column:1/-1}h1,h2,h3{line-height:1.2;margin:0 0 14px}h1{font-size:28px}h2{font-size:19px}label{display:block;margin:12px 0 6px;color:#cdd8eb}input,select,textarea{width:100%;border:1px solid #34415d;background:#0e1525;color:#f4f7ff;border-radius:10px;padding:11px 12px}button,.button{display:inline-flex;border:0;border-radius:10px;padding:10px 14px;background:var(--blue);color:white;font-weight:650;text-decoration:none;cursor:pointer}.secondary{background:#27334b}.danger{background:#a93b4b}.ok{color:var(--green)}.bad{color:var(--red)}.flash{padding:12px 14px;border:1px solid #315585;background:#12284a;border-radius:12px;margin-bottom:16px}.error{border-color:#7e3440;background:#361923}.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.spacer{flex:1}.stat{font-size:28px;font-weight:750}.models{display:grid;gap:10px}.model{display:grid;grid-template-columns:minmax(180px,1fr) auto;gap:12px;padding:14px;border:1px solid var(--line);border-radius:12px}.model code{overflow-wrap:anywhere}.tag{display:inline-block;border:1px solid #375177;color:#aac7ff;border-radius:99px;padding:2px 8px;font-size:12px;margin:2px}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#090e19;border:1px solid var(--line);border-radius:12px;padding:14px}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:10px 8px;border-bottom:1px solid var(--line);vertical-align:top}.key{font:14px/1.5 ui-monospace,monospace;background:#08101e;border:1px solid #38639d;padding:12px;border-radius:10px;overflow-wrap:anywhere}.notice{border-left:4px solid #ffbd4a;padding:10px 14px;background:#2b2415}form.inline{display:inline}.small{font-size:12px}@media(max-width:680px){.model{grid-template-columns:1fr}.nav{align-items:flex-start}.hide-mobile{display:none}}
"""


def page(title: str, body: str, user: sqlite3.Row | None = None, lang: str = "zh") -> str:
    auth = ""
    if user:
        auth = (
            f'<span class="muted">{html.escape(user["email"])}</span> '
            '<form class="inline" method="post" action="/logout"><input type="hidden" name="csrf" value="__CSRF__"><button class="secondary">退出 / Sign out</button></form>'
        )
    else:
        auth = '<a href="/login">登录 / Sign in</a>'
    return f"""<!doctype html><html lang="{lang}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)} · LLMCtl</title><style>{STYLE}</style></head><body><main class="shell"><nav class="nav"><div><a class="brand" href="/">LLMCtl Account Portal</a><div class="sub">OmniRoute account control plane</div></div><div class="row">{auth}</div></nav>{body}</main><script>function cp(id){{navigator.clipboard.writeText(document.getElementById(id).innerText).then(()=>{{const b=document.querySelector('[data-copy="'+id+'"]');if(b)b.innerText='已复制 / Copied'}})}}document.querySelectorAll('[data-copy]').forEach(b=>b.onclick=()=>cp(b.dataset.copy));</script></body></html>"""


class PortalHandler(http.server.BaseHTTPRequestHandler):
    server_version = "LLMCtlAccountPortal/1"

    @property
    def app(self) -> "PortalServer":
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        # Never emit query strings: verification tokens must not enter journals.
        path = urllib.parse.urlsplit(self.path).path
        sys.stderr.write("[account-portal] %s %s %s\n" % (self.client_address[0], self.command, path))

    def cookies(self) -> http.cookies.SimpleCookie:
        result = http.cookies.SimpleCookie()
        with contextlib.suppress(http.cookies.CookieError):
            result.load(self.headers.get("Cookie", ""))
        return result

    def current_session(self) -> tuple[sqlite3.Row | None, str]:
        morsel = self.cookies().get(SESSION_COOKIE)
        if not morsel:
            return None, ""
        with self.app.db.connect() as connection:
            row = connection.execute(
                "SELECT u.*,s.csrf_token FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>? AND u.status='active'",
                (token_hash(morsel.value), now()),
            ).fetchone()
        return (row, row["csrf_token"]) if row else (None, "")

    def csrf_token(self) -> str:
        morsel = self.cookies().get(CSRF_COOKIE)
        return morsel.value if morsel else ""

    def send_headers(self, status: int, content_type: str = "text/html; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'",
        )
        for cookie in getattr(self, "extra_response_cookies", []):
            self.send_header("Set-Cookie", cookie)
        self.extra_response_cookies = []

    def response(self, status: int, content: str, user: sqlite3.Row | None = None) -> None:
        raw = content.encode()
        self.send_headers(status)
        if not self.csrf_token():
            csrf = secrets.token_urlsafe(24)
            secure = "; Secure" if self.app.config.cookie_secure else ""
            self.send_header(
                "Set-Cookie", f"{CSRF_COOKIE}={csrf}; Path=/; SameSite=Lax; Max-Age=86400{secure}"
            )
            content = content.replace("__CSRF__", html.escape(csrf))
            raw = content.encode()
        else:
            content = content.replace("__CSRF__", html.escape(self.csrf_token()))
            raw = content.encode()
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def json_response(self, status: int, value: Any) -> None:
        raw = json.dumps(value, ensure_ascii=False).encode()
        self.send_headers(status, "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def redirect(self, location: str, cookies: list[str] | None = None) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        for value in cookies or []:
            self.send_header("Set-Cookie", value)
        self.end_headers()

    def form(self) -> dict[str, str]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("invalid content length") from error
        if length <= 0 or length > MAX_FORM_BYTES:
            raise ValueError("invalid form size")
        parsed = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
        return {key: values[-1] for key, values in parsed.items()}

    def verify_csrf(self, form: dict[str, str], session_csrf: str = "") -> bool:
        expected = session_csrf or self.csrf_token()
        return bool(expected and hmac.compare_digest(expected, form.get("csrf", "")))

    def require_user(self, admin: bool = False) -> tuple[sqlite3.Row | None, str]:
        user, csrf = self.current_session()
        if not user or user["status"] != "active" or (admin and user["role"] != "admin"):
            self.redirect("/login")
            return None, ""
        return user, csrf

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/health":
            try:
                with self.app.db.connect() as connection:
                    connection.execute("SELECT 1").fetchone()
                self.json_response(200, {"status": "ok", "version": APP_VERSION})
            except sqlite3.Error as error:
                self.json_response(503, {"status": "error", "error": str(error)})
            return
        if parsed.path == "/ready":
            try:
                self.app.omni.models()
                self.json_response(200, {"status": "ready"})
            except RuntimeError as error:
                self.json_response(503, {"status": "unavailable", "error": str(error)})
            return
        if parsed.path == "/login":
            self.show_login()
            return
        if parsed.path == "/register":
            self.show_register()
            return
        if parsed.path == "/verify":
            self.show_verify(urllib.parse.parse_qs(parsed.query).get("token", [""])[-1])
            return
        if parsed.path == "/admin":
            self.show_admin()
            return
        if parsed.path == "/":
            user, _ = self.current_session()
            if not user:
                self.show_landing()
            elif user["role"] == "admin":
                self.redirect("/admin")
            else:
                one_time = self.cookies().get("llm_key_once")
                raw_key = urllib.parse.unquote(one_time.value) if one_time else ""
                if one_time:
                    secure = "; Secure" if self.app.config.cookie_secure else ""
                    self.extra_response_cookies = [
                        f"llm_key_once=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0{secure}"
                    ]
                self.show_dashboard(user, raw_key=raw_key)
            return
        self.response(404, page("Not found", '<div class="card"><h1>404</h1></div>'))

    def do_POST(self) -> None:
        try:
            form = self.form()
        except ValueError:
            self.response(400, page("Bad request", '<div class="card error">Invalid request</div>'))
            return
        path = urllib.parse.urlsplit(self.path).path
        if path == "/login":
            self.handle_login(form)
        elif path == "/logout":
            self.handle_logout(form)
        elif path == "/register":
            self.handle_register(form)
        elif path == "/verify":
            self.handle_verify(form)
        elif path == "/rotate-key":
            self.handle_rotate(form)
        elif path == "/admin/settings":
            self.handle_admin_settings(form)
        elif path == "/admin/user":
            self.handle_admin_user(form)
        else:
            self.response(404, page("Not found", '<div class="card"><h1>404</h1></div>'))

    def show_landing(self) -> None:
        settings = self.app.db.settings()
        registration = settings.get("registration_enabled") == "1"
        register = '<a class="button" href="/register">注册 / Register</a>' if registration else '<span class="muted">注册已关闭 / Registration is closed</span>'
        body = f'<section class="card"><h1>公司 LLM API 门户</h1><p class="muted">Company LLM API portal · OmniRoute</p><p>验证公司邮箱后获得个人 API Key、周期额度、用量和可调用模型。</p><div class="row"><a class="button" href="/login">登录 / Sign in</a>{register}</div></section>'
        self.response(200, page("Account portal", body))

    def show_login(self, message: str = "", error: bool = False) -> None:
        flash = f'<div class="flash {"error" if error else ""}">{html.escape(message)}</div>' if message else ""
        body = f'''{flash}<section class="card" style="max-width:520px;margin:auto"><h1>登录 / Sign in</h1><form method="post" action="/login"><input type="hidden" name="csrf" value="__CSRF__"><label>邮箱 / Email</label><input name="email" type="email" autocomplete="username" required><label>密码 / Password</label><input name="password" type="password" autocomplete="current-password" required><p><button>登录 / Sign in</button></p></form><a href="/register">注册新账户 / Create account</a></section>'''
        self.response(200, page("Sign in", body))

    def show_register(self, message: str = "", error: bool = False) -> None:
        settings = self.app.db.settings()
        if settings.get("registration_enabled") != "1":
            self.response(403, page("Registration closed", '<div class="card notice">注册已关闭 / Registration is closed.</div>'))
            return
        domains = settings.get("allowed_domains", "")
        flash = f'<div class="flash {"error" if error else ""}">{html.escape(message)}</div>' if message else ""
        body = f'''{flash}<section class="card" style="max-width:560px;margin:auto"><h1>注册 / Register</h1><p class="muted">仅允许：{html.escape(domains)}</p><form method="post" action="/register"><input type="hidden" name="csrf" value="__CSRF__"><label>公司邮箱 / Company email</label><input name="email" type="email" required><label>密码 / Password</label><input name="password" type="password" minlength="12" maxlength="200" required><label>确认密码 / Confirm</label><input name="confirm" type="password" minlength="12" maxlength="200" required><p class="small muted">至少 12 个字符；API Key 不会通过邮件发送。</p><button>发送验证邮件 / Send verification</button></form></section>'''
        self.response(200, page("Register", body))

    def show_verify(self, raw_token: str) -> None:
        if len(raw_token) < 32:
            self.response(400, page("Verify", '<div class="card error">验证链接无效 / Invalid verification link.</div>'))
            return
        with self.app.db.connect() as connection:
            row = connection.execute(
                "SELECT v.*,u.email,u.status FROM verification_tokens v JOIN users u ON u.id=v.user_id WHERE v.token_hash=? AND v.used_at IS NULL AND v.expires_at>?",
                (token_hash(raw_token), now()),
            ).fetchone()
        if not row:
            self.response(410, page("Verify", '<div class="card error">验证链接已失效 / Verification link expired.</div>'))
            return
        body = f'''<section class="card" style="max-width:620px;margin:auto"><h1>确认邮箱 / Confirm email</h1><p>{html.escape(row["email"])}</p><p class="muted">确认后将创建个人 API Key 并启用周期额度。邮件扫描器访问此页面不会自动开通账户。</p><form method="post" action="/verify"><input type="hidden" name="csrf" value="__CSRF__"><input type="hidden" name="token" value="{html.escape(raw_token)}"><button>确认并创建 API Key / Verify &amp; create key</button></form></section>'''
        self.response(200, page("Verify email", body))

    def show_dashboard(self, user: sqlite3.Row, raw_key: str = "", message: str = "") -> None:
        usage: dict[str, Any] = {}
        gateway_error = ""
        models: list[dict[str, Any]] = []
        try:
            if user["api_key_id"]:
                usage = self.app.omni.usage(user["api_key_id"])
            models = self.app.omni.models()
        except RuntimeError as error:
            gateway_error = str(error)
        limits = usage.get("limits", []) if isinstance(usage, dict) else []
        limit = limits[0] if isinstance(limits, list) and limits else {}
        used = int(limit.get("tokensUsed", 0)) if isinstance(limit, dict) else 0
        total = int(limit.get("tokenLimit", user["quota_tokens"])) if isinstance(limit, dict) else int(user["quota_tokens"])
        remaining = max(0, total - used)
        next_reset = str(limit.get("nextResetAt", "")) if isinstance(limit, dict) else ""
        reset_label = next_reset or f'{user["quota_reset"]} · {user["quota_reset_time"]}'
        key_box = ""
        if raw_key:
            key_box = f'''<div class="card wide notice"><h2>请立即复制 API Key / Copy now</h2><p>明文只显示这一次；门户不会保存它。</p><div id="new-key" class="key">{html.escape(raw_key)}</div><p><button data-copy="new-key">复制 / Copy</button></p></div>'''
        model_rows = []
        for index, model in enumerate(models[:500]):
            model_id = str(model.get("id", ""))
            if not model_id:
                continue
            owned = str(model.get("owned_by", model.get("provider", "OmniRoute")))
            capabilities = model.get("capabilities", model.get("input_modalities", []))
            caps = json.dumps(capabilities, ensure_ascii=False) if capabilities else "chat"
            if self.app.config.supports_ocr and model_id == os.environ.get("SERVED_MODEL_NAME", ""):
                caps += ", vision, OCR"
            dom_id = f"model-{index}"
            model_rows.append(f'''<div class="model"><div><code id="{dom_id}">{html.escape(model_id)}</code><div><span class="tag">{html.escape(owned)}</span><span class="tag">{html.escape(caps[:120])}</span></div></div><button class="secondary" data-copy="{dom_id}">复制 ID / Copy</button></div>''')
        sample_model = str(models[0].get("id", "MODEL_ID")) if models else "MODEL_ID"
        sample_payload = json.dumps(
            {"model": sample_model, "messages": [{"role": "user", "content": "你好"}]},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        curl = f'''curl {shlex.quote(self.app.config.api_public_url + "/v1/chat/completions")} \\
  -H 'Authorization: Bearer YOUR_API_KEY' \\
  -H 'Content-Type: application/json' \\
  -d {shlex.quote(sample_payload)}'''
        flash = f'<div class="flash">{html.escape(message)}</div>' if message else ""
        error = f'<div class="flash error">OmniRoute: {html.escape(gateway_error)}</div>' if gateway_error else ""
        body = f'''{flash}{error}<div class="grid">{key_box}<section class="card"><h2>本周期用量 / Usage</h2><div class="stat">{used:,} / {total:,}</div><p class="muted">剩余 {remaining:,} tokens · 下次重置 / next reset: {html.escape(reset_label)}</p></section><section class="card"><h2>API 地址 / Endpoint</h2><div id="api-base" class="key">{html.escape(self.app.config.api_public_url)}/v1</div><p><button class="secondary" data-copy="api-base">复制 / Copy</button></p></section><section class="card wide"><h2>调用示例 / curl demo</h2><pre id="curl-demo">{html.escape(curl)}</pre><button class="secondary" data-copy="curl-demo">复制示例 / Copy demo</button></section><section class="card wide"><div class="row"><h2>开放模型 / Available models</h2><span class="spacer"></span><span class="muted">{len(model_rows)} models</span></div><div class="models">{''.join(model_rows) or '<p class="muted">No models exposed by OmniRoute.</p>'}</div></section><section class="card wide"><h2>密钥安全 / Key security</h2><p class="muted">轮换会先创建并验证新 Key，再停用旧 Key。新 Key 仍只显示一次。</p><form method="post" action="/rotate-key"><input type="hidden" name="csrf" value="__CSRF__"><button class="danger">轮换 API Key / Rotate key</button></form></section></div>'''
        self.response(200, page("Dashboard", body, user), user)

    def show_admin(self, message: str = "", error: bool = False) -> None:
        user, _ = self.require_user(admin=True)
        if not user:
            return
        settings = self.app.db.settings()
        with self.app.db.connect() as connection:
            users = connection.execute("SELECT * FROM users WHERE role='user' ORDER BY created_at DESC LIMIT 500").fetchall()
            audits = connection.execute("SELECT * FROM audit_events ORDER BY id DESC LIMIT 100").fetchall()
        rows = []
        for item in users:
            status_options = "".join(
                f'<option value="{value}" {"selected" if item["status"] == value else ""}>{value}</option>'
                for value in ("active", "disabled")
            )
            provisioned = bool(item["api_key_id"])
            controls = (
                f'''<form method="post" action="/admin/user"><input type="hidden" name="csrf" value="__CSRF__"><input type="hidden" name="user_id" value="{html.escape(item["id"])}"><label>Token quota</label><input name="quota" value="{item["quota_tokens"]}" inputmode="numeric"><label>Reset</label><select name="reset"><option {"selected" if item["quota_reset"]=="monthly" else ""}>monthly</option><option {"selected" if item["quota_reset"]=="weekly" else ""}>weekly</option><option {"selected" if item["quota_reset"]=="daily" else ""}>daily</option></select><input name="reset_time" value="{html.escape(item["quota_reset_time"])}"><label>Status</label><select name="status">{status_options}</select><p><button class="secondary">保存 / Save</button></p></form>'''
                if provisioned
                else '<span class="muted">等待邮箱验证 / Pending email verification</span>'
            )
            rows.append(f'''<tr><td>{html.escape(item["email"])}</td><td>{html.escape(item["status"])}</td><td>{controls}</td></tr>''')
        audit_rows = "".join(f'<tr><td>{time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(a["created_at"]))}</td><td>{html.escape(a["actor"])}</td><td>{html.escape(a["action"])}</td><td>{html.escape(a["status"])}</td><td>{html.escape(a["detail"])}</td></tr>' for a in audits)
        checked = "checked" if settings.get("registration_enabled") == "1" else ""
        flash = f'<div class="flash {"error" if error else ""}">{html.escape(message)}</div>' if message else ""
        body = f'''{flash}<div class="grid"><section class="card"><h2>注册策略 / Registration</h2><form method="post" action="/admin/settings"><input type="hidden" name="csrf" value="__CSRF__"><label><input style="width:auto" type="checkbox" name="enabled" value="1" {checked}> 允许新用户注册</label><label>允许的邮箱后缀（逗号分隔）</label><input name="domains" value="{html.escape(settings.get("allowed_domains", ""))}"><label>默认 Token 额度</label><input name="quota" value="{html.escape(settings.get("default_quota_tokens", "1000000"))}" inputmode="numeric"><label>重置周期</label><select name="reset"><option {"selected" if settings.get("default_quota_reset")=="monthly" else ""}>monthly</option><option {"selected" if settings.get("default_quota_reset")=="weekly" else ""}>weekly</option><option {"selected" if settings.get("default_quota_reset")=="daily" else ""}>daily</option></select><label>重置时间</label><input name="reset_time" value="{html.escape(settings.get("default_quota_reset_time", "00:00"))}"><p><button>保存策略 / Save</button></p></form><p class="small muted">开启注册要求安装时已配置 SMTP；域名在验证时会再次检查。</p></section><section class="card"><h2>服务入口</h2><p>OmniRoute: <a href="{html.escape(self.app.config.api_public_url)}">{html.escape(self.app.config.api_public_url)}</a></p><p>Portal: {html.escape(self.app.config.public_url)}</p><p class="muted">门户数据库与 OmniRoute 数据库完全分离。</p></section><section class="card wide"><h2>用户 / Users</h2><div style="overflow:auto"><table><tr><th>Email</th><th>Status</th><th>Quota / status</th></tr>{''.join(rows) or '<tr><td colspan="3">暂无用户</td></tr>'}</table></div></section><section class="card wide"><h2>门户审计 / Portal audit</h2><div style="overflow:auto"><table><tr><th>Time</th><th>Actor</th><th>Action</th><th>Status</th><th>Detail</th></tr>{audit_rows}</table></div></section></div>'''
        self.response(200, page("Admin", body, user), user)

    def handle_login(self, form: dict[str, str]) -> None:
        if not self.verify_csrf(form):
            self.response(403, page("Forbidden", '<div class="card error">CSRF validation failed</div>'))
            return
        remote = self.client_address[0]
        identity = token_hash(f"{remote}|{form.get('email','').lower()}")
        audit_action = ""
        audit_target = ""
        with self.app.db.connect() as connection:
            failure = connection.execute("SELECT * FROM login_failures WHERE identity_hash=?", (identity,)).fetchone()
            if failure and failure["locked_until"] > now():
                audit_action, audit_target = "login.locked", "account"
                user = None
                valid = False
                locked = True
            else:
                locked = False
            try:
                email, _ = normalize_email(form.get("email", ""))
            except ValueError:
                email = ""
            if not locked:
                user = connection.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
                candidate_hash = user["password_hash"] if user else DUMMY_PASSWORD_HASH
                password_valid = verify_password(form.get("password", ""), candidate_hash)
                valid = bool(user and user["status"] == "active" and password_valid)
            if locked:
                pass
            if not valid:
                if not locked:
                    current = now()
                    attempts = 1 if not failure or current - failure["window_started_at"] > 900 else failure["attempts"] + 1
                    lock_until = current + 900 if attempts >= 5 else 0
                    connection.execute("INSERT INTO login_failures(identity_hash,attempts,window_started_at,locked_until) VALUES(?,?,?,?) ON CONFLICT(identity_hash) DO UPDATE SET attempts=excluded.attempts,window_started_at=excluded.window_started_at,locked_until=excluded.locked_until", (identity, attempts, current if attempts == 1 else failure["window_started_at"], lock_until))
                    audit_action, audit_target = "login.failed", email or "invalid"
            else:
                connection.execute("DELETE FROM login_failures WHERE identity_hash=?", (identity,))
                raw_session = secrets.token_urlsafe(32)
                csrf = secrets.token_urlsafe(24)
                connection.execute("DELETE FROM sessions WHERE user_id=? OR expires_at<=?", (user["id"], now()))
                connection.execute("INSERT INTO sessions(token_hash,user_id,csrf_token,expires_at,created_at) VALUES(?,?,?,?,?)", (token_hash(raw_session), user["id"], csrf, now() + 7 * 86400, now()))
                connection.execute("UPDATE users SET last_login_at=? WHERE id=?", (now(), user["id"]))
        if not valid:
            self.app.db.audit("anonymous", audit_action, audit_target, "denied", remote)
            if locked:
                self.show_login("尝试次数过多，请稍后重试 / Too many attempts", True)
            else:
                self.show_login("邮箱、密码或账户状态无效 / Invalid credentials or account", True)
            return
        self.app.db.audit(email, "login.success", user["id"], "success", remote)
        secure = "; Secure" if self.app.config.cookie_secure else ""
        self.redirect("/admin" if user["role"] == "admin" else "/", [f"{SESSION_COOKIE}={raw_session}; Path=/; HttpOnly; SameSite=Lax; Max-Age=604800{secure}", f"{CSRF_COOKIE}={csrf}; Path=/; SameSite=Lax; Max-Age=604800{secure}"])

    def handle_logout(self, form: dict[str, str]) -> None:
        user, csrf = self.current_session()
        if not user or not self.verify_csrf(form, csrf):
            self.response(403, page("Forbidden", '<div class="card error">CSRF validation failed</div>'))
            return
        morsel = self.cookies().get(SESSION_COOKIE)
        if morsel:
            with self.app.db.connect() as connection:
                connection.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(morsel.value),))
        self.redirect("/", [f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"])

    def handle_register(self, form: dict[str, str]) -> None:
        if not self.verify_csrf(form):
            self.response(403, page("Forbidden", '<div class="card error">CSRF validation failed</div>'))
            return
        settings = self.app.db.settings()
        if settings.get("registration_enabled") != "1":
            self.show_register("注册已关闭 / Registration is closed", True)
            return
        try:
            email, domain = normalize_email(form.get("email", ""))
            allowed = normalize_domains(settings.get("allowed_domains", ""))
            if domain not in allowed:
                raise ValueError("email domain is not allowed")
            if form.get("password") != form.get("confirm"):
                raise ValueError("passwords do not match")
            password_hash = hash_password(form.get("password", ""))
        except ValueError as error:
            self.show_register(str(error), True)
            return
        remote = self.client_address[0]
        raw_token = secrets.token_urlsafe(40)
        duplicate_active = False
        throttled = False
        with self.app.db.connect() as connection:
            user = connection.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            if user and user["status"] != "pending":
                duplicate_active = True
            elif user:
                user_id = user["id"]
                latest = connection.execute(
                    "SELECT created_at FROM verification_tokens WHERE user_id=? ORDER BY created_at DESC LIMIT 1",
                    (user_id,),
                ).fetchone()
                if latest and now() - latest["created_at"] < 60:
                    throttled = True
                else:
                    connection.execute("UPDATE users SET password_hash=? WHERE id=?", (password_hash, user_id))
                    connection.execute("DELETE FROM verification_tokens WHERE user_id=?", (user_id,))
            else:
                user_id = str(uuid.uuid4())
                connection.execute("INSERT INTO users(id,email,password_hash,role,status,quota_tokens,quota_reset,quota_reset_time,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (user_id, email, password_hash, "user", "pending", int(settings["default_quota_tokens"]), settings["default_quota_reset"], settings["default_quota_reset_time"], now()))
            if not duplicate_active and not throttled:
                connection.execute("INSERT INTO verification_tokens(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)", (token_hash(raw_token), user_id, now() + self.app.config.verification_ttl, now()))
        if duplicate_active or throttled:
            self.app.db.audit("anonymous", "register.duplicate" if duplicate_active else "register.throttled", email, "ignored", remote)
            self.show_register("若该邮箱可注册，验证邮件已经发送 / If eligible, a verification email was sent")
            return
        try:
            send_verification_email(self.app.config, email, raw_token)
        except Exception as error:
            with self.app.db.connect() as connection:
                connection.execute("DELETE FROM verification_tokens WHERE token_hash=?", (token_hash(raw_token),))
            self.app.db.audit("anonymous", "register.email", email, "failed", remote, type(error).__name__)
            self.show_register("验证邮件发送失败，请联系管理员 / Email delivery failed", True)
            return
        self.app.db.audit("anonymous", "register.email", email, "success", remote)
        self.show_register("验证邮件已发送，请查收 / Verification email sent")

    def handle_verify(self, form: dict[str, str]) -> None:
        if not self.verify_csrf(form):
            self.response(403, page("Forbidden", '<div class="card error">CSRF validation failed</div>'))
            return
        raw_token = form.get("token", "")
        with self.app.db.connect() as connection:
            record = connection.execute("SELECT v.*,u.* FROM verification_tokens v JOIN users u ON u.id=v.user_id WHERE v.token_hash=? AND v.used_at IS NULL AND v.expires_at>?", (token_hash(raw_token), now())).fetchone()
        if not record:
            self.response(410, page("Expired", '<div class="card error">验证链接已失效 / Verification link expired.</div>'))
            return
        settings = self.app.db.settings()
        try:
            _, domain = normalize_email(record["email"])
            if domain not in normalize_domains(settings.get("allowed_domains", "")):
                raise ValueError("email domain is no longer allowed")
            key_id, raw_key = self.app.omni.create_user_key(record["user_id"], record["email"])
            limit_id = ""
            try:
                limit_id = self.app.omni.set_limit(key_id, record["quota_tokens"], record["quota_reset"], record["quota_reset_time"])
            except Exception:
                with contextlib.suppress(Exception):
                    self.app.omni.delete_key_and_limit(key_id, limit_id)
                raise
            try:
                with self.app.db.connect() as connection:
                    changed = connection.execute("UPDATE verification_tokens SET used_at=? WHERE token_hash=? AND used_at IS NULL", (now(), token_hash(raw_token))).rowcount
                    if changed != 1:
                        raise RuntimeError("verification token was already consumed")
                    connection.execute("UPDATE users SET status='active',verified_at=?,api_key_id=?,token_limit_id=? WHERE id=?", (now(), key_id, limit_id, record["user_id"]))
            except Exception:
                with contextlib.suppress(Exception):
                    self.app.omni.delete_key_and_limit(key_id, limit_id)
                raise
        except Exception as error:
            self.app.db.audit(record["email"], "verify.provision", record["user_id"], "failed", self.client_address[0], type(error).__name__)
            self.response(503, page("Provisioning failed", '<div class="card error">账户开通失败，未保留半成品 API Key；请重试或联系管理员。<br>Provisioning failed and the partial key was revoked.</div>'))
            return
        self.app.db.audit(record["email"], "verify.provision", record["user_id"], "success", self.client_address[0])
        with self.app.db.connect() as connection:
            user = connection.execute("SELECT * FROM users WHERE id=?", (record["user_id"],)).fetchone()
            raw_session = secrets.token_urlsafe(32)
            csrf = secrets.token_urlsafe(24)
            connection.execute("INSERT INTO sessions(token_hash,user_id,csrf_token,expires_at,created_at) VALUES(?,?,?,?,?)", (token_hash(raw_session), user["id"], csrf, now() + 7 * 86400, now()))
        secure = "; Secure" if self.app.config.cookie_secure else ""
        self.send_response(303)
        self.send_header("Location", "/?provisioned=1")
        # One-time key is carried in a short-lived HttpOnly cookie only to bridge the redirect.
        self.send_header("Set-Cookie", f"{SESSION_COOKIE}={raw_session}; Path=/; HttpOnly; SameSite=Lax; Max-Age=604800{secure}")
        self.send_header("Set-Cookie", f"{CSRF_COOKIE}={csrf}; Path=/; SameSite=Lax; Max-Age=604800{secure}")
        self.send_header("Set-Cookie", f"llm_key_once={urllib.parse.quote(raw_key)}; Path=/; HttpOnly; SameSite=Strict; Max-Age=30{secure}")
        self.end_headers()

    def handle_rotate(self, form: dict[str, str]) -> None:
        user, csrf = self.require_user()
        if not user:
            return
        if not self.verify_csrf(form, csrf) or user["role"] != "user":
            self.response(403, page("Forbidden", '<div class="card error">Forbidden</div>'))
            return
        old_id = user["api_key_id"]
        old_limit_id = user["token_limit_id"]
        try:
            new_id, raw_key = self.app.omni.create_user_key(user["id"], user["email"])
            limit_id = ""
            try:
                limit_id = self.app.omni.set_limit(new_id, user["quota_tokens"], user["quota_reset"], user["quota_reset_time"])
                if old_id:
                    self.app.omni.activate_key(old_id, False)
                with self.app.db.connect() as connection:
                    connection.execute("UPDATE users SET api_key_id=?,token_limit_id=? WHERE id=?", (new_id, limit_id, user["id"]))
            except Exception:
                with contextlib.suppress(Exception):
                    self.app.omni.delete_key_and_limit(new_id, limit_id)
                if old_id:
                    with contextlib.suppress(Exception):
                        self.app.omni.activate_key(old_id, True)
                raise
            if old_id:
                with contextlib.suppress(Exception):
                    self.app.omni.delete_key_and_limit(old_id, old_limit_id or "")
        except Exception as error:
            self.app.db.audit(user["email"], "key.rotate", user["id"], "failed", self.client_address[0], type(error).__name__)
            self.show_dashboard(user, message="密钥轮换失败，旧 Key 保持有效 / Rotation failed; old key remains valid")
            return
        self.app.db.audit(user["email"], "key.rotate", user["id"], "success", self.client_address[0])
        with self.app.db.connect() as connection:
            updated = connection.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
        self.show_dashboard(updated, raw_key=raw_key, message="密钥已轮换 / Key rotated")

    def handle_admin_settings(self, form: dict[str, str]) -> None:
        user, csrf = self.require_user(admin=True)
        if not user:
            return
        if not self.verify_csrf(form, csrf):
            self.response(403, page("Forbidden", '<div class="card error">CSRF validation failed</div>'))
            return
        try:
            domains = normalize_domains(form.get("domains", ""))
            quota = int(form.get("quota", "0"))
            reset = form.get("reset", "")
            reset_time = form.get("reset_time", "")
            enabled = form.get("enabled") == "1"
            if (enabled and not domains) or quota <= 0 or quota > 10**12 or reset not in {"daily", "weekly", "monthly"} or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", reset_time):
                raise ValueError("invalid registration settings")
            if enabled and (
                not self.app.config.public_url
                or not self.app.config.smtp_host
                or not self.app.config.smtp_from
            ):
                raise ValueError(
                    "public portal URL and SMTP must be configured before registration can be enabled"
                )
            values = {"registration_enabled": "1" if enabled else "0", "allowed_domains": ",".join(domains), "default_quota_tokens": str(quota), "default_quota_reset": reset, "default_quota_reset_time": reset_time}
            with self.app.db.connect() as connection:
                for key, value in values.items():
                    connection.execute("INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (key, value, now()))
        except ValueError as error:
            self.show_admin(str(error), True)
            return
        self.app.db.audit(user["email"], "settings.update", "registration", "success", self.client_address[0], values)
        self.show_admin("设置已保存 / Settings saved")

    def handle_admin_user(self, form: dict[str, str]) -> None:
        admin, csrf = self.require_user(admin=True)
        if not admin:
            return
        if not self.verify_csrf(form, csrf):
            self.response(403, page("Forbidden", '<div class="card error">CSRF validation failed</div>'))
            return
        try:
            quota = int(form.get("quota", "0"))
            reset, reset_time = form.get("reset", ""), form.get("reset_time", "")
            target_status = form.get("status", "")
            if quota <= 0 or quota > 10**12 or reset not in {"daily", "weekly", "monthly"} or target_status not in {"active", "disabled"} or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", reset_time):
                raise ValueError("invalid user settings")
            with self.app.db.connect() as connection:
                target = connection.execute("SELECT * FROM users WHERE id=? AND role='user'", (form.get("user_id", ""),)).fetchone()
            if not target or not target["api_key_id"]:
                raise ValueError("user is not provisioned")
            self.app.omni.activate_key(target["api_key_id"], target_status == "active")
            try:
                limit_id = self.app.omni.set_limit(target["api_key_id"], quota, reset, reset_time, target["token_limit_id"])
                with self.app.db.connect() as connection:
                    connection.execute("UPDATE users SET status=?,quota_tokens=?,quota_reset=?,quota_reset_time=?,token_limit_id=? WHERE id=?", (target_status, quota, reset, reset_time, limit_id, target["id"]))
            except Exception:
                with contextlib.suppress(Exception):
                    self.app.omni.activate_key(target["api_key_id"], target["status"] == "active")
                if target["token_limit_id"]:
                    with contextlib.suppress(Exception):
                        self.app.omni.set_limit(
                            target["api_key_id"],
                            target["quota_tokens"],
                            target["quota_reset"],
                            target["quota_reset_time"],
                            target["token_limit_id"],
                        )
                raise
        except Exception as error:
            self.app.db.audit(admin["email"], "user.update", form.get("user_id", ""), "failed", self.client_address[0], type(error).__name__)
            self.show_admin(str(error), True)
            return
        self.app.db.audit(admin["email"], "user.update", target["email"], "success", self.client_address[0], {"status": target_status, "quota": quota, "reset": reset})
        self.show_admin("用户设置已保存 / User settings saved")


class PortalHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class PortalServer:
    def __init__(self, config: Config):
        self.config = config
        self.db = Database(config)
        self.db.initialize()
        self.omni = OmniRouteClient(config)
        self.httpd = PortalHTTPServer((config.bind, config.port), PortalHandler)
        self.httpd.app = self  # type: ignore[attr-defined]

    def serve(self) -> None:
        print(f"[account-portal] listening on {self.config.bind}:{self.config.port}", flush=True)
        self.httpd.serve_forever(poll_interval=0.5)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        nargs="?",
        choices=("serve", "check-config", "reset-admin-password"),
        default="serve",
    )
    args = parser.parse_args()
    config = Config.from_env()
    if args.command == "check-config":
        print(json.dumps({"ok": True, "db": str(config.db_path), "registration": config.initial_registration}))
        return
    if args.command == "reset-admin-password":
        if not config.admin_password:
            raise SystemExit("ACCOUNT_ADMIN_PASSWORD is required")
        database = Database(config)
        database.initialize()
        with database.connect() as connection:
            changed = connection.execute(
                "UPDATE users SET password_hash=? WHERE role='admin'",
                (hash_password(config.admin_password),),
            ).rowcount
        if changed != 1:
            raise SystemExit("expected exactly one portal administrator")
        database.audit("system", "admin.password.reset", config.admin_email, "success", "local")
        print("portal administrator password updated")
        return
    PortalServer(config).serve()


if __name__ == "__main__":
    main()
