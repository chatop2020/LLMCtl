#!/usr/bin/env python3
"""LLMCtl OmniRoute 模式使用的轻量账户门户。

门户默认使用独立 SQLite，也可由管理员迁移到独立 MySQL 数据库；
不读取或修改 OmniRoute 的 SQLite schema；
所有网关操作均通过公开 HTTP API 完成。API Key 明文只返回一次，门户不持久化。
"""

from __future__ import annotations

import argparse
import base64
import csv
import contextlib
import dataclasses
import datetime
import hashlib
import hmac
import html
import http.cookies
import http.server
import importlib
import ipaddress
import json
import mimetypes
import os
import pathlib
import platform
import pwd
import re
import secrets
import shlex
import shutil
import signal
import smtplib
import socket
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from email.message import EmailMessage
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


APP_VERSION = "3.6.0"
SESSION_COOKIE = "llm_account_session"
CSRF_COOKIE = "llm_account_csrf"
MAX_FORM_BYTES = 64 * 1024
STRESS_CONCURRENCY_CHOICES = (1, 2, 4, 6, 8, 10, 15, 20, 25, 30, 40, 50, 60, 70, 80, 100)
STRESS_INPUT_TOKEN_CHOICES = (50, 100, 300, 800, 1500, 3000, 6000, 8000, 15000, 30000)
STRESS_OUTPUT_TOKEN_CHOICES = (64, 128, 256, 512, 1024)
STRESS_REQUEST_MULTIPLIER_CHOICES = (1, 2, 3, 4)
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+$")
DOMAIN_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
DUMMY_PASSWORD_HASH = "pbkdf2_sha256$600000$bGxtY3RsLWR1bW15LXNhbHQ$O5LpuYky-CKHcJaJEAX3-3B1rSxvRmdsFnyMXd5fUrg"
WORKFLOW_GATEWAY_NODE_NAME = "LLMCtl workflow data plane"
WORKFLOW_GATEWAY_CONNECTION_NAME = "LLMCtl workflow"
WORKFLOW_GATEWAY_PREFIX = "llmctl-workflow"
WORKFLOW_GATEWAY_MANAGED_DESCRIPTION = "Managed by LLMCtl workflow data plane"
PUBLIC_COMBO_MANAGED_DESCRIPTION = "Managed by LLMCtl account portal public model"
PUBLIC_COMBO_MIGRATION_NAME = "responses-native-public-combo-v2"
PUBLIC_COMBO_MIGRATION_SETTING = "public_combo_migration"
PUBLIC_COMBO_BACKUP_MAX_AGE_SECONDS = 15 * 60
PUBLIC_COMBO_MIGRATION_DELAY_SECONDS = 15
PUBLIC_COMBO_MIGRATION_RETRY_SECONDS = 60
PUBLIC_COMBO_AUDIT_SECONDS = 60
SYSTEM_MONITOR_CACHE_SECONDS = 1.0
SYSTEM_MONITOR_PROCESS_LIMIT = 200
SYSTEM_MONITOR_COMMAND_LIMIT = 240
SYSTEM_MONITOR_LOCAL_FILESYSTEMS = {
    "btrfs",
    "ext2",
    "ext3",
    "ext4",
    "exfat",
    "f2fs",
    "fuseblk",
    "ntfs",
    "ntfs3",
    "overlay",
    "vfat",
    "xfs",
    "zfs",
}
SYSTEM_MONITOR_SECRET_ARGUMENT = re.compile(
    r"(?i)(password|passwd|token|secret|api[_-]?key|authorization|credential|"
    r"database[_-]?url|dsn|smtp[_-]?(?:url|password))"
)
SYSTEM_MONITOR_CREDENTIAL_URL = re.compile(
    r"(?i)([a-z][a-z0-9+.-]*://[^/@:\s]+:)[^/@\s]+(@)"
)
MYSQL_CAPABILITY_VERSION = 1
MYSQL_SCHEMA_VERSION = 1
MYSQL_CONFIG_DIRECTORY = "Config"
MYSQL_CAPABILITY_FILE = "mysql-capability.json"
DATABASE_CONFIG_FILE = "database.json"
DATABASE_MIGRATION_FILE = "database-migration.json"
MYSQL_DATABASE_RE = re.compile(r"^[A-Za-z0-9_$-]{1,64}$")
MYSQL_HOST_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,255}$")


class DatabaseCapabilityError(RuntimeError):
    """数据库能力尚未激活或配置不完整。"""


class DatabaseMigrationError(RuntimeError):
    """数据库迁移在切换前失败。"""


class CompatRow(dict[str, Any]):
    """同时兼容 sqlite3.Row 的列名和数字下标读取方式。"""

    def __init__(self, values: dict[str, Any]):
        super().__init__(values)
        self._column_values = tuple(values.values())

    def __getitem__(self, key: str | int) -> Any:
        if isinstance(key, int):
            return self._column_values[key]
        return super().__getitem__(key)


class MySQLResult:
    """将 PyMySQL 游标适配为门户现有的 SQLite 结果接口。"""

    def __init__(self, cursor: Any):
        self._cursor = cursor
        self.rowcount = int(cursor.rowcount)
        self.lastrowid = cursor.lastrowid

    @staticmethod
    def _row(value: Any) -> CompatRow | None:
        return CompatRow(value) if isinstance(value, dict) else value

    def fetchone(self) -> CompatRow | None:
        return self._row(self._cursor.fetchone())

    def fetchall(self) -> list[CompatRow]:
        return [self._row(value) for value in self._cursor.fetchall()]


def _replace_sqlite_placeholders(statement: str) -> str:
    """只替换 SQL 字符串字面量之外的问号占位符。"""
    output: list[str] = []
    quote = ""
    index = 0
    while index < len(statement):
        character = statement[index]
        if quote:
            output.append(character)
            if character == quote:
                if index + 1 < len(statement) and statement[index + 1] == quote:
                    output.append(statement[index + 1])
                    index += 1
                else:
                    quote = ""
        elif character in {"'", '"'}:
            quote = character
            output.append(character)
        elif character == "?":
            output.append("%s")
        else:
            output.append(character)
        index += 1
    return "".join(output)


def mysql_compatible_sql(statement: str) -> str:
    """把门户使用的有限 SQLite DML 语法转换成 MySQL 语法。"""
    translated = statement.strip()
    insert_ignored = bool(re.match(r"(?is)^INSERT\s+OR\s+IGNORE\b", translated))
    translated = re.sub(
        r"(?is)^INSERT\s+OR\s+IGNORE\b", "INSERT IGNORE", translated
    )
    if re.search(r"(?is)\bON\s+CONFLICT\s*\([^)]*\)\s+DO\s+NOTHING\s*$", translated):
        translated = re.sub(
            r"(?is)\s+ON\s+CONFLICT\s*\([^)]*\)\s+DO\s+NOTHING\s*$",
            "",
            translated,
        )
        if not re.match(r"(?is)^INSERT\s+IGNORE\b", translated):
            translated = re.sub(r"(?is)^INSERT\b", "INSERT IGNORE", translated)
    conflict = re.search(
        r"(?is)\s+ON\s+CONFLICT\s*\([^)]*\)\s+DO\s+UPDATE\s+SET\s+(.+)$",
        translated,
    )
    if conflict:
        assignments = re.sub(
            r"(?i)\bexcluded\.([A-Za-z_][A-Za-z0-9_]*)",
            r"VALUES(\1)",
            conflict.group(1),
        )
        translated = translated[: conflict.start()] + " ON DUPLICATE KEY UPDATE " + assignments
    if insert_ignored and " ON DUPLICATE KEY UPDATE " in translated:
        translated = re.sub(r"(?is)^INSERT\s+IGNORE\b", "INSERT", translated)
    translated = re.sub(r"(?i)\s+COLLATE\s+NOCASE\b", "", translated)
    return _replace_sqlite_placeholders(translated)


class MySQLConnection:
    """为门户查询提供与 sqlite3.Connection 一致的最小接口。"""

    def __init__(self, connection: Any):
        self._connection = connection

    def execute(self, statement: str, parameters: tuple[Any, ...] | list[Any] = ()) -> MySQLResult:
        cursor = self._connection.cursor()
        cursor.execute(mysql_compatible_sql(statement), tuple(parameters))
        return MySQLResult(cursor)

    def executemany(self, statement: str, rows: list[tuple[Any, ...]]) -> MySQLResult:
        cursor = self._connection.cursor()
        cursor.executemany(mysql_compatible_sql(statement), rows)
        return MySQLResult(cursor)

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()


def _atomic_json_write(path: pathlib.Path, payload: dict[str, Any], mode: int = 0o600) -> None:
    """原子写入包含数据库状态或凭据的 JSON 文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class DatabaseRuntime:
    """管理 MySQL 能力标记、连接配置和迁移状态。"""

    def __init__(self, db_path: pathlib.Path):
        self.directory = db_path.parent / MYSQL_CONFIG_DIRECTORY
        self.capability_path = self.directory / MYSQL_CAPABILITY_FILE
        self.config_path = self.directory / DATABASE_CONFIG_FILE
        self.migration_path = self.directory / DATABASE_MIGRATION_FILE
        self._lock = threading.RLock()

    @staticmethod
    def _read(path: pathlib.Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        return value if isinstance(value, dict) else {}

    def capability(self) -> dict[str, Any]:
        value = self._read(self.capability_path)
        return {
            "enabled": bool(value.get("enabled")),
            "version": int(value.get("version") or 0),
            "runtime_python": str(value.get("runtime_python") or ""),
            "driver": str(value.get("driver") or ""),
            "activated_at": int(value.get("activated_at") or 0),
        }

    def config(self, include_password: bool = True) -> dict[str, Any]:
        value = self._read(self.config_path)
        result = {
            "active_backend": "mysql" if value.get("active_backend") == "mysql" else "sqlite",
            "host": str(value.get("host") or ""),
            "port": int(value.get("port") or 3306),
            "database": str(value.get("database") or ""),
            "username": str(value.get("username") or ""),
            "password": str(value.get("password") or ""),
            "tls_mode": str(value.get("tls_mode") or "preferred"),
            "ca_file": str(value.get("ca_file") or ""),
            "updated_at": int(value.get("updated_at") or 0),
        }
        if not include_password:
            result["password_configured"] = bool(result["password"])
            result.pop("password", None)
        return result

    def save_config(self, payload: dict[str, Any], keep_password: bool = True) -> dict[str, Any]:
        with self._lock:
            current = self.config(include_password=True)
            host = str(payload.get("host", current["host"])).strip()
            database = str(payload.get("database", current["database"])).strip()
            username = str(payload.get("username", current["username"])).strip()
            if not MYSQL_HOST_RE.fullmatch(host):
                raise ValueError("MySQL 主机名或 IP 地址无效")
            if not MYSQL_DATABASE_RE.fullmatch(database):
                raise ValueError("MySQL database 名称必须为 1-64 个安全字符")
            if not username or len(username) > 128 or any(ord(value) < 32 for value in username):
                raise ValueError("MySQL 用户名无效")
            try:
                port = int(payload.get("port", current["port"]))
            except (TypeError, ValueError) as error:
                raise ValueError("MySQL 端口必须是整数") from error
            if not 1 <= port <= 65535:
                raise ValueError("MySQL 端口必须在 1-65535 之间")
            tls_mode = str(payload.get("tls_mode", current["tls_mode"])).strip().lower()
            if tls_mode not in {"disabled", "preferred", "required", "verify_ca"}:
                raise ValueError("MySQL TLS 模式无效")
            ca_file = str(payload.get("ca_file", current["ca_file"])).strip()
            if tls_mode == "verify_ca" and (not ca_file or not pathlib.Path(ca_file).is_file()):
                raise ValueError("verify_ca 模式必须填写服务器上可读取的 CA 文件")
            password = str(payload.get("password") or "")
            if not password and keep_password:
                password = str(current.get("password") or "")
            if not password:
                raise ValueError("MySQL 密码不能为空")
            saved = {
                "active_backend": current["active_backend"],
                "host": host,
                "port": port,
                "database": database,
                "username": username,
                "password": password,
                "tls_mode": tls_mode,
                "ca_file": ca_file,
                "updated_at": now(),
            }
            _atomic_json_write(self.config_path, saved)
            return self.config(include_password=False)

    def set_active_backend(self, backend: str) -> None:
        with self._lock:
            if backend not in {"sqlite", "mysql"}:
                raise ValueError("数据库后端无效")
            value = self.config(include_password=True)
            value["active_backend"] = backend
            value["updated_at"] = now()
            _atomic_json_write(self.config_path, value)

    def migration(self) -> dict[str, Any]:
        value = self._read(self.migration_path)
        return value if value else {"status": "idle", "progress": 0, "stage": "未开始"}

    def save_migration(self, **values: Any) -> dict[str, Any]:
        with self._lock:
            current = self.migration()
            current.update(values)
            current["updated_at"] = now()
            _atomic_json_write(self.migration_path, current)
            return current


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


def normalize_login_name(value: str) -> str:
    """Accept a human login identifier without pretending it is an email address."""
    value = value.strip()
    if not value or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("登录名不能为空，也不能包含控制字符")
    return value


def login_candidates(value: str) -> list[str]:
    raw = normalize_login_name(value)
    candidates = [raw]
    try:
        email, _ = normalize_email(raw)
    except ValueError:
        pass
    else:
        if email not in candidates:
            candidates.append(email)
    return candidates


def find_user_by_login(
    connection: sqlite3.Connection, value: str
) -> tuple[sqlite3.Row | None, str]:
    try:
        candidates = login_candidates(value)
    except ValueError:
        return None, ""
    for candidate in candidates:
        user = connection.execute(
            "SELECT * FROM users WHERE login_name=? LIMIT 1", (candidate,)
        ).fetchone()
        if user:
            return user, candidate
    return None, candidates[0]


def user_identity(user: sqlite3.Row | dict[str, Any]) -> str:
    keys = set(user.keys())
    if "login_name" in keys:
        return str(user["login_name"] or user["email"] or user["id"])
    return str(user["email"])


def validate_password(password: str) -> None:
    if not 8 <= len(password) <= 200:
        raise ValueError("密码必须为 8-200 个字符")
    if password.strip().isdecimal():
        raise ValueError("密码不能全部由数字组成")


def validate_admin_password(password: str) -> None:
    if not password:
        raise ValueError("管理员密码不能为空")
    if any(character in "\r\n\x00" for character in password):
        raise ValueError("管理员密码不能包含换行或空字符")
    if password.isdecimal():
        raise ValueError("管理员密码不能全部由数字组成")


def hash_password(password: str, salt: bytes | None = None) -> str:
    validate_password(password)
    salt = salt or secrets.token_bytes(16)
    iterations = 600_000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations, dklen=32)
    return "pbkdf2_sha256$600000$%s$%s" % (
        base64.urlsafe_b64encode(salt).decode().rstrip("="),
        base64.urlsafe_b64encode(digest).decode().rstrip("="),
    )


def hash_admin_password(password: str, salt: bytes | None = None) -> str:
    validate_admin_password(password)
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


def normalize_group_name(value: str) -> str:
    """Return a canonical, visible Unicode group name suitable for display."""
    normalized = unicodedata.normalize("NFKC", str(value))
    visible: list[str] = []
    for character in normalized:
        category = unicodedata.category(character)
        if category.startswith("C") or category in {"Zl", "Zp"}:
            raise ValueError("用户组名称不能包含控制字符或不可见字符")
        visible.append(" " if character.isspace() else character)
    result = re.sub(r" +", " ", "".join(visible)).strip()
    if not 1 <= len(result) <= 80:
        raise ValueError("用户组名称必须为 1-80 个可见字符，支持中文")
    return result


def request_content_summary(request_body: Any, max_characters: int = 20_000) -> dict[str, Any]:
    """Extract displayable request text without returning arbitrary request metadata."""
    if isinstance(request_body, str):
        try:
            request_body = json.loads(request_body)
        except json.JSONDecodeError:
            request_body = {"prompt": request_body}
    if not isinstance(request_body, dict):
        return {"available": False, "messages": [], "truncated": False}

    def content_text(value: Any) -> str:
        if isinstance(value, str):
            return value
        if not isinstance(value, list):
            return ""
        parts: list[str] = []
        for block in value:
            if isinstance(block, str):
                parts.append(block)
                continue
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type", "")).lower()
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
            elif isinstance(block.get("content"), str):
                parts.append(str(block["content"]))
            elif "image" in block_type:
                parts.append("[图像内容]")
            elif "audio" in block_type:
                parts.append("[音频内容]")
            elif "file" in block_type:
                parts.append("[文件内容]")
        return "\n".join(part for part in parts if part)

    messages: list[dict[str, str]] = []
    raw_messages = request_body.get("messages")
    if not isinstance(raw_messages, list):
        raw_messages = request_body.get("input") if isinstance(request_body.get("input"), list) else []
    for item in raw_messages:
        if isinstance(item, str):
            messages.append({"role": "input", "content": item})
            continue
        if not isinstance(item, dict):
            continue
        text = content_text(item.get("content"))
        if not text and isinstance(item.get("text"), str):
            text = str(item["text"])
        if text:
            messages.append({"role": str(item.get("role", item.get("type", "input"))), "content": text})
    system = content_text(request_body.get("system"))
    if system:
        messages.insert(0, {"role": "system", "content": system})
    if not messages:
        prompt = request_body.get("prompt", request_body.get("input"))
        text = content_text(prompt) if isinstance(prompt, list) else str(prompt or "")
        if text:
            messages.append({"role": "input", "content": text})

    remaining = max_characters
    truncated = False
    result: list[dict[str, str]] = []
    for message in messages:
        content = message["content"]
        if remaining <= 0:
            truncated = True
            break
        if len(content) > remaining:
            content = content[:remaining] + "…"
            truncated = True
        result.append({"role": message["role"][:32], "content": content})
        remaining -= len(content)
    return {"available": bool(result), "messages": result, "truncated": truncated}


def response_content_summary(response_body: Any, max_characters: int = 40_000) -> dict[str, Any]:
    """Extract final model text from retained OpenAI-compatible response artifacts."""
    if isinstance(response_body, str):
        stripped = response_body.strip()
        if stripped.startswith("data:"):
            chunks: list[dict[str, Any]] = []
            for line in stripped.splitlines():
                line = line.strip()
                if not line.startswith("data:") or line[5:].strip() == "[DONE]":
                    continue
                try:
                    chunk = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                if isinstance(chunk, dict):
                    chunks.append(chunk)
            response_body = {"chunks": chunks}
        else:
            try:
                response_body = json.loads(stripped)
            except json.JSONDecodeError:
                response_body = {"content": stripped}
    if not isinstance(response_body, (dict, list)):
        return {"available": False, "messages": [], "truncated": False}

    messages: list[dict[str, str]] = []

    def add(role: str, value: Any) -> None:
        if isinstance(value, str):
            text = value
        elif isinstance(value, list):
            parts: list[str] = []
            for block in value:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict):
                    for key in ("text", "output_text", "content"):
                        if isinstance(block.get(key), str):
                            parts.append(str(block[key]))
                            break
            text = "\n".join(parts)
        else:
            text = ""
        if text:
            messages.append({"role": role, "content": text})

    def visit(value: Any, depth: int = 0) -> None:
        if depth > 6:
            return
        if isinstance(value, list):
            for item in value:
                visit(item, depth + 1)
            return
        if not isinstance(value, dict):
            return
        for key in ("reasoning_content", "reasoning_text", "reasoning"):
            if isinstance(value.get(key), (str, list)):
                add("reasoning", value[key])
        for key in ("output_text", "text"):
            if isinstance(value.get(key), str):
                add("assistant", value[key])
        if isinstance(value.get("content"), (str, list)):
            add(str(value.get("role") or "assistant"), value["content"])
        for key in (
            "message",
            "delta",
            "choices",
            "output",
            "candidates",
            "chunks",
            "response",
            "body",
            "data",
        ):
            if key in value:
                visit(value[key], depth + 1)

    visit(response_body)
    compact: list[dict[str, str]] = []
    for message in messages:
        if compact and compact[-1]["role"] == message["role"]:
            compact[-1]["content"] += message["content"]
        else:
            compact.append(dict(message))
    remaining = max_characters
    truncated = False
    result: list[dict[str, str]] = []
    for message in compact:
        if remaining <= 0:
            truncated = True
            break
        content = message["content"]
        if len(content) > remaining:
            content = content[:remaining] + "…"
            truncated = True
        result.append({"role": message["role"][:32], "content": content})
        remaining -= len(content)
    return {"available": bool(result), "messages": result, "truncated": truncated}


def retained_response_summary(detail: dict[str, Any]) -> dict[str, Any]:
    """Find the final client response artifact across OmniRoute schema versions."""
    preferred_keys = (
        "finalClientResponse",
        "finalClientResponseBody",
        "clientResponse",
        "clientResponseBody",
        "responseBody",
        "finalResponse",
        "providerResponseBody",
        "providerResponse",
    )

    def find(value: Any, depth: int = 0) -> Any:
        if depth > 4 or not isinstance(value, dict):
            return None
        for key in preferred_keys:
            if key in value and value[key] not in (None, "", {}, []):
                return value[key]
        for key, nested in value.items():
            if key in {"requestBody", "providerRequestBody", "translatedRequest"}:
                continue
            found = find(nested, depth + 1)
            if found is not None:
                return found
        return None

    payload = find(detail)
    summary = response_content_summary(payload)
    summary["retained"] = payload is not None or any(
        bool(detail.get(key))
        for key in ("hasResponseBody", "hasClientResponse", "hasProviderResponse")
    )
    return summary


def now() -> int:
    return int(time.time())


def next_reset_at(
    interval: str,
    current: int | None = None,
    reset_time: str = "00:00",
    timezone_name: str | None = None,
) -> int | None:
    """Return the next recurring boundary in the configured service timezone."""
    if interval == "none":
        return None
    if interval not in {"daily", "weekly", "monthly"} or not re.fullmatch(
        r"(?:[01]\d|2[0-3]):[0-5]\d", reset_time
    ):
        raise ValueError("invalid reset schedule")
    try:
        zone = ZoneInfo(timezone_name or os.environ.get("TZ", "Asia/Shanghai"))
    except ZoneInfoNotFoundError:
        zone = ZoneInfo("UTC")
    point = datetime.datetime.fromtimestamp(current or now(), zone)
    hour, minute = (int(value) for value in reset_time.split(":"))
    if interval == "daily":
        candidate = point.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= point:
            candidate += datetime.timedelta(days=1)
    elif interval == "weekly":
        candidate = (point - datetime.timedelta(days=point.weekday())).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        if candidate <= point:
            candidate += datetime.timedelta(days=7)
    else:
        candidate = point.replace(day=1, hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= point:
            year = candidate.year + (1 if candidate.month == 12 else 0)
            month = 1 if candidate.month == 12 else candidate.month + 1
            candidate = candidate.replace(year=year, month=month)
    return int(candidate.timestamp())


def money_to_micros(value: Any) -> int:
    try:
        text = str(value).strip()
        if not re.fullmatch(r"-?\d+(?:\.\d{1,6})?", text):
            raise ValueError
        negative = text.startswith("-")
        if negative:
            text = text[1:]
        whole, _, fraction = text.partition(".")
        result = int(whole) * 1_000_000 + int((fraction + "000000")[:6])
        return -result if negative else result
    except (TypeError, ValueError) as error:
        raise ValueError("invalid monetary amount") from error


def micros_to_money(value: int) -> str:
    sign = "-" if value < 0 else ""
    value = abs(int(value))
    return f"{sign}{value // 1_000_000}.{value % 1_000_000:06d}".rstrip("0").rstrip(".")


def tokens_to_money_micros(tokens: int, price_micros_per_million: int) -> int:
    """按保守单价把原始 Token 权益换算成现金。

    LLMCtl 旧赠额会优先抵扣价格最高的 Token 类型，因此一次性现金迁移采用
    当前最高模型单价，至少保留每笔剩余赠额的购买力。与请求计费一致，
    不足一微美元的部分向上取整。
    """
    tokens = max(0, int(tokens))
    price = max(0, int(price_micros_per_million))
    return (tokens * price + 999_999) // 1_000_000


def apply_welcome_credit(
    connection: sqlite3.Connection,
    user_id: str,
    settings: dict[str, str],
    stamp: int,
) -> tuple[int, str]:
    """以幂等方式写入配置的一次性欢迎余额。"""
    amount = money_to_micros(settings.get("default_welcome_balance", "0") or "0")
    if amount < 0:
        raise ValueError("default welcome balance cannot be negative")
    source_ref = f"welcome-credit:{user_id}"
    if amount == 0 or connection.execute(
        "SELECT 1 FROM balance_transactions WHERE source_ref=?", (source_ref,)
    ).fetchone():
        return 0, source_ref
    account = connection.execute(
        "SELECT balance_micros FROM billing_accounts WHERE user_id=?", (user_id,)
    ).fetchone()
    balance = int(account["balance_micros"] or 0) if account else 0
    after = balance + amount
    connection.execute(
        "INSERT INTO billing_accounts(user_id,balance_micros,suspended,updated_at) "
        "VALUES(?,?,0,?) ON CONFLICT(user_id) DO UPDATE SET "
        "balance_micros=excluded.balance_micros,updated_at=excluded.updated_at",
        (user_id, after, stamp),
    )
    connection.execute(
        """INSERT INTO balance_transactions(
             user_id,kind,amount_micros,balance_after_micros,actor,note,source_ref,created_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (
            user_id, "credit", amount, after, "system:registration",
            "Welcome balance", source_ref, stamp,
        ),
    )
    return amount, source_ref


def rollback_source_credit(
    connection: sqlite3.Connection, user_id: str, source_ref: str, stamp: int
) -> None:
    """外部 API Key 事务失败时撤销开户赠款。"""
    transaction = connection.execute(
        "SELECT amount_micros FROM balance_transactions WHERE user_id=? AND source_ref=?",
        (user_id, source_ref),
    ).fetchone()
    if not transaction:
        return
    amount = int(transaction["amount_micros"] or 0)
    connection.execute(
        "UPDATE billing_accounts SET balance_micros=balance_micros-?,updated_at=? "
        "WHERE user_id=?",
        (amount, stamp, user_id),
    )
    connection.execute(
        "DELETE FROM balance_transactions WHERE user_id=? AND source_ref=?",
        (user_id, source_ref),
    )


def price_usage(
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int,
    reasoning_tokens: int,
    prices: dict[str, Any] | sqlite3.Row,
    available_grant_tokens: int = 0,
) -> dict[str, int]:
    """Price one request, retaining legacy grant math for historical replay.

    Model prices are expressed in micro-USD per one million tokens. A raw
    legacy token grant cannot be prorated across the request total because
    cached, input, output and reasoning tokens can have different prices. The
    active cash-only settlement path always passes zero; the compatibility
    branch remains so pre-3.0 ledger calculations and migration tests are
    reproducible.
    """
    total_input = max(0, int(input_tokens))
    total_output = max(0, int(output_tokens))
    cached = min(total_input, max(0, int(cached_tokens)))
    reasoning = min(total_output, max(0, int(reasoning_tokens)))
    categories = [
        (total_input - cached, int(prices["input_price_micros"])),
        (cached, int(prices["cached_price_micros"])),
        (total_output - reasoning, int(prices["output_price_micros"])),
        (reasoning, int(prices["reasoning_price_micros"])),
    ]
    gross_numerator = sum(count * max(0, unit_price) for count, unit_price in categories)
    remaining_grant = max(0, int(available_grant_tokens))
    grant_numerator = 0
    granted_tokens = 0
    for count, unit_price in sorted(categories, key=lambda item: item[1], reverse=True):
        if remaining_grant <= 0 or count <= 0 or unit_price <= 0:
            continue
        take = min(count, remaining_grant)
        granted_tokens += take
        remaining_grant -= take
        grant_numerator += take * unit_price
    net_numerator = max(0, gross_numerator - grant_numerator)
    gross = (gross_numerator + 999_999) // 1_000_000
    amount = (net_numerator + 999_999) // 1_000_000
    return {
        "gross_amount_micros": gross,
        "grant_amount_micros": gross - amount,
        "amount_micros": amount,
        "granted_tokens": granted_tokens,
    }


def positive_int_or_none(value: Any, label: str, maximum: int = 10_000_000) -> int | None:
    """Parse an optional positive model limit without silently accepting bad input."""
    if value is None or str(value).strip() == "":
        return None
    try:
        result = int(str(value).strip())
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} 必须为正整数") from error
    if result <= 0 or result > maximum:
        raise ValueError(f"{label} 必须在 1-{maximum:,} 之间")
    return result


def portal_ui_url(value: str) -> str:
    """Canonicalize a public portal origin to the Nginx /ui entry point."""
    value = value.rstrip("/")
    if value and not urllib.parse.urlsplit(value).path:
        return value + "/ui"
    return value


def normalize_public_origin(value: Any) -> str:
    """Validate and canonicalize an optional externally published origin.

    The setting is deliberately an origin rather than a free-form URL.  It is
    used in verification mail and generated API examples, so accepting paths,
    credentials, control characters, queries, or fragments would create a
    phishing/header-injection footgun for a public installation.
    """
    origin = str(value or "").strip()
    if not origin:
        return ""
    if len(origin) > 2048 or any(ord(char) < 32 or ord(char) == 127 for char in origin):
        raise ValueError("对外发布地址无效")
    try:
        parsed = urllib.parse.urlsplit(origin)
        port = parsed.port
    except ValueError as error:
        raise ValueError("对外发布地址无效") from error
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("对外发布地址必须是无路径、无凭据的 http(s) 地址")
    hostname = parsed.hostname.lower().rstrip(".")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        try:
            hostname = normalize_domain(hostname)
        except ValueError as error:
            raise ValueError("对外发布地址主机名无效") from error
    host = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 80 if parsed.scheme == "http" else 443
    port_suffix = f":{port}" if port and port != default_port else ""
    return f"{parsed.scheme}://{host}{port_suffix}"


def normalize_portal_title(value: Any) -> str:
    title = unicodedata.normalize("NFKC", str(value or "")).strip()
    if (
        not 1 <= len(title) <= 40
        or any(unicodedata.category(char).startswith("C") for char in title)
    ):
        raise ValueError("门户品牌名称必须是 1-40 个可见字符")
    return title


def normalize_max_sessions(value: Any, label: str = "API Key 活跃会话上限") -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label}必须是整数") from error
    if not 0 <= result <= 10000:
        raise ValueError(f"{label}必须在 0-10000 之间")
    return result


def normalize_request_limit(value: Any, label: str) -> int:
    """Normalize a native OmniRoute request-count limit (0 disables it)."""
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label}必须是整数") from error
    if not 0 <= result <= 10_000_000:
        raise ValueError(f"{label}必须在 0-10000000 之间")
    return result


def request_rate_limits(requests_per_minute: int, requests_per_day: int) -> list[dict[str, int]]:
    """Build OmniRoute's per-key fixed-window request rules.

    Keep this separate from maxSessions: maxSessions is a conversation/session
    sharing signal, whereas these rules cap actual HTTP requests.
    """
    rules: list[dict[str, int]] = []
    if requests_per_minute:
        rules.append({"limit": requests_per_minute, "window": 60})
    if requests_per_day:
        rules.append({"limit": requests_per_day, "window": 86_400})
    return rules


def effective_public_urls(config: "Config", settings: dict[str, str]) -> tuple[str, str]:
    """Return the effective portal URL and API origin.

    A configured external origin wins.  Blank keeps the legacy/current access
    URLs, preserving existing LAN-only installations and upgrades.
    """
    try:
        published = normalize_public_origin(settings.get("published_origin", ""))
    except ValueError:
        # 手工损坏的 SQLite 值不能变成不安全链接。
        published = ""
    if published:
        return f"{published}/ui", published
    portal_url = portal_ui_url(settings.get("public_url", "") or config.public_url)
    api_url = (settings.get("api_public_url", "") or config.api_public_url).rstrip("/")
    return portal_url, api_url


@dataclasses.dataclass(frozen=True)
class Config:
    bind: str
    port: int
    db_path: pathlib.Path
    gateway_url: str
    gateway_manage_key: str
    public_url: str
    api_public_url: str
    admin_username: str
    admin_password: str
    initial_registration: bool
    initial_domains: list[str]
    initial_welcome_balance_micros: int
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
    static_dir: pathlib.Path = pathlib.Path(__file__).resolve().with_name("account_portal_ui")

    @classmethod
    def from_env(cls) -> "Config":
        bind = os.environ.get("ACCOUNT_BIND", "127.0.0.1").strip()
        if bind != "127.0.0.1":
            raise SystemExit(
                "ACCOUNT_BIND must be 127.0.0.1; publish the portal through Nginx"
            )
        public_url = (
            os.environ.get("ACCOUNT_PUBLIC_URL", "").strip()
            or f"http://127.0.0.1:{env_int('API_PORT', 8000)}/ui"
        ).rstrip("/")
        api_public = os.environ.get("ACCOUNT_API_PUBLIC_URL", "").rstrip("/")
        for name, value, allowed_paths in (
            ("ACCOUNT_PUBLIC_URL", public_url, {"", "/ui"}),
            ("ACCOUNT_API_PUBLIC_URL", api_public, {""}),
        ):
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
                    or parsed_value.path.rstrip("/") not in allowed_paths
                    or parsed_value.query
                    or parsed_value.fragment
                ):
                    raise SystemExit(f"{name} must be an http(s) origin without credentials or a path")
        public_url = portal_ui_url(public_url)
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
        encoded_admin_username = os.environ.get("ACCOUNT_ADMIN_USERNAME_B64", "").strip()
        if encoded_admin_username:
            try:
                admin_username = base64.b64decode(
                    encoded_admin_username, validate=True
                ).decode("utf-8")
            except (ValueError, UnicodeDecodeError) as error:
                raise SystemExit("ACCOUNT_ADMIN_USERNAME_B64 is invalid") from error
        else:
            admin_username = os.environ.get(
                "ACCOUNT_ADMIN_USERNAME",
                os.environ.get("ACCOUNT_ADMIN_EMAIL", "admin"),
            )
        try:
            admin_username = normalize_login_name(admin_username)
        except ValueError as error:
            raise SystemExit("ACCOUNT_ADMIN_USERNAME is invalid") from error
        port = env_int("ACCOUNT_PORT", 8001)
        smtp_port = env_int("SMTP_PORT", 587)
        initial_quota = env_int("ACCOUNT_DEFAULT_QUOTA_TOKENS", 0)
        try:
            initial_welcome_balance_micros = money_to_micros(
                os.environ.get("ACCOUNT_DEFAULT_WELCOME_BALANCE", "0")
            )
        except ValueError as error:
            raise SystemExit("ACCOUNT_DEFAULT_WELCOME_BALANCE must be a valid USD amount") from error
        verification_ttl = env_int("ACCOUNT_VERIFICATION_TTL", 86400)
        if not 1 <= port <= 65535:
            raise SystemExit("ACCOUNT_PORT must be 1-65535")
        if not 1 <= smtp_port <= 65535:
            raise SystemExit("SMTP_PORT must be 1-65535")
        if not 0 <= initial_quota <= 10**12:
            raise SystemExit("ACCOUNT_DEFAULT_QUOTA_TOKENS must be 0-1000000000000")
        if not 0 <= initial_welcome_balance_micros <= 10**15:
            raise SystemExit("ACCOUNT_DEFAULT_WELCOME_BALANCE must be 0-1000000000 USD")
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
            bind=bind,
            port=port,
            db_path=db_path,
            gateway_url=os.environ.get(
                "ACCOUNT_GATEWAY_LOCAL_URL",
                f"http://127.0.0.1:{env_int('GATEWAY_INTERNAL_PORT', 18000)}",
            ).rstrip("/"),
            gateway_manage_key=os.environ.get("GATEWAY_API_KEY", ""),
            public_url=public_url,
            api_public_url=api_public,
            admin_username=admin_username,
            admin_password=os.environ.get("ACCOUNT_ADMIN_PASSWORD", os.environ.get("UI_PASSWORD", "")),
            initial_registration=initial_registration,
            initial_domains=domains,
            initial_welcome_balance_micros=initial_welcome_balance_micros,
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
            static_dir=pathlib.Path(
                os.environ.get(
                    "ACCOUNT_STATIC_DIR",
                    str(pathlib.Path(__file__).resolve().with_name("account_portal_ui")),
                )
            ),
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
  login_name TEXT,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('admin','user')),
  status TEXT NOT NULL CHECK(status IN ('pending','active','disabled')),
  api_key_id TEXT,
  token_limit_id TEXT,
  quota_tokens INTEGER NOT NULL,
  quota_reset TEXT NOT NULL,
  quota_reset_time TEXT NOT NULL,
  max_sessions INTEGER NOT NULL DEFAULT 0,
  requests_per_minute INTEGER NOT NULL DEFAULT 0,
  requests_per_day INTEGER NOT NULL DEFAULT 0,
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
CREATE TABLE IF NOT EXISTS user_groups (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  description TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','disabled')),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS user_group_members (
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  group_id TEXT NOT NULL REFERENCES user_groups(id) ON DELETE CASCADE,
  created_at INTEGER NOT NULL,
  PRIMARY KEY(user_id, group_id)
);
CREATE TABLE IF NOT EXISTS free_resources (
  resource_key TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  model_id TEXT NOT NULL,
  display_name TEXT NOT NULL,
  free_type TEXT NOT NULL,
  monthly_tokens INTEGER,
  credit_tokens INTEGER,
  terms_status TEXT NOT NULL DEFAULT '',
  configured INTEGER NOT NULL DEFAULT 0,
  available INTEGER NOT NULL DEFAULT 0,
  native_visible INTEGER NOT NULL DEFAULT 1,
  test_status TEXT NOT NULL DEFAULT 'untested' CHECK(test_status IN ('untested','healthy','failed')),
  test_latency_ms INTEGER,
  test_error TEXT NOT NULL DEFAULT '',
  last_tested_at INTEGER,
  source_json TEXT NOT NULL,
  discovered_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(provider, model_id)
);
CREATE TABLE IF NOT EXISTS published_models (
  id TEXT PRIMARY KEY,
  public_model_id TEXT NOT NULL UNIQUE,
  display_name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  source_kind TEXT NOT NULL CHECK(source_kind IN ('combo','model','free')),
  source_ref TEXT NOT NULL DEFAULT '',
  source_provider TEXT NOT NULL DEFAULT '',
  source_model TEXT NOT NULL,
  capabilities_json TEXT NOT NULL DEFAULT '[]',
  context_window_tokens INTEGER,
  max_output_tokens INTEGER,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  metadata_sync_status TEXT NOT NULL DEFAULT 'unknown',
  metadata_sync_error TEXT NOT NULL DEFAULT '',
  metadata_synced_at INTEGER,
  input_price_micros INTEGER NOT NULL DEFAULT 0,
  output_price_micros INTEGER NOT NULL DEFAULT 0,
  cached_price_micros INTEGER NOT NULL DEFAULT 0,
  reasoning_price_micros INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'published' CHECK(status IN ('draft','published','disabled','error')),
  upstream_free INTEGER NOT NULL DEFAULT 0,
  mapping_kind TEXT NOT NULL DEFAULT '',
  mapping_id TEXT NOT NULL DEFAULT '',
  health_status TEXT NOT NULL DEFAULT 'unknown' CHECK(health_status IN ('unknown','healthy','failed')),
  health_latency_ms INTEGER,
  health_error TEXT NOT NULL DEFAULT '',
  last_health_at INTEGER,
  health_failures INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS model_price_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  model_id TEXT NOT NULL REFERENCES published_models(id) ON DELETE CASCADE,
  effective_at INTEGER NOT NULL,
  input_price_micros INTEGER NOT NULL,
  output_price_micros INTEGER NOT NULL,
  cached_price_micros INTEGER NOT NULL,
  reasoning_price_micros INTEGER NOT NULL,
  actor TEXT NOT NULL,
  UNIQUE(model_id, effective_at)
);
CREATE TABLE IF NOT EXISTS model_access (
  model_id TEXT NOT NULL REFERENCES published_models(id) ON DELETE CASCADE,
  subject_type TEXT NOT NULL CHECK(subject_type IN ('all','group','user')),
  subject_id TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL,
  PRIMARY KEY(model_id, subject_type, subject_id)
);
CREATE TABLE IF NOT EXISTS billing_accounts (
  user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  balance_micros INTEGER NOT NULL DEFAULT 0,
  suspended INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS token_grants (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  model_id TEXT REFERENCES published_models(id) ON DELETE CASCADE,
  label TEXT NOT NULL,
  tokens_initial INTEGER NOT NULL CHECK(tokens_initial > 0),
  tokens_remaining INTEGER NOT NULL CHECK(tokens_remaining >= 0),
  reset_interval TEXT NOT NULL DEFAULT 'none' CHECK(reset_interval IN ('none','daily','weekly','monthly')),
  reset_time TEXT NOT NULL DEFAULT '00:00',
  reset_at INTEGER,
  expires_at INTEGER,
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','disabled','expired')),
  converted_at INTEGER,
  converted_amount_micros INTEGER NOT NULL DEFAULT 0,
  conversion_rate_micros INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS usage_ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id TEXT NOT NULL UNIQUE,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  api_key_id TEXT NOT NULL,
  model_id TEXT REFERENCES published_models(id) ON DELETE SET NULL,
  public_model_id TEXT NOT NULL,
  provider TEXT NOT NULL,
  resolved_model TEXT NOT NULL,
  input_tokens INTEGER NOT NULL,
  output_tokens INTEGER NOT NULL,
  cached_tokens INTEGER NOT NULL,
  reasoning_tokens INTEGER NOT NULL,
  granted_tokens INTEGER NOT NULL,
  gross_amount_micros INTEGER NOT NULL DEFAULT 0,
  grant_amount_micros INTEGER NOT NULL DEFAULT 0,
  amount_micros INTEGER NOT NULL,
  price_snapshot_json TEXT NOT NULL,
  occurred_at INTEGER NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS balance_transactions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK(kind IN ('credit','debit','adjustment','refund')),
  amount_micros INTEGER NOT NULL,
  balance_after_micros INTEGER NOT NULL,
  actor TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  source_ref TEXT,
  usage_id INTEGER REFERENCES usage_ledger(id) ON DELETE SET NULL,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS permission_sync (
  user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  status TEXT NOT NULL CHECK(status IN ('pending','synced','failed')),
  error TEXT NOT NULL DEFAULT '',
  updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS stress_runs (
  id TEXT PRIMARY KEY,
  public_model_id TEXT NOT NULL,
  concurrency INTEGER NOT NULL,
  target_input_tokens INTEGER NOT NULL,
  max_output_tokens INTEGER NOT NULL,
  request_multiplier INTEGER NOT NULL,
  request_count INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('starting','running','canceling','completed','failed','canceled')),
  pid INTEGER,
  result_dir TEXT NOT NULL,
  metrics_json TEXT NOT NULL DEFAULT '{}',
  error TEXT NOT NULL DEFAULT '',
  created_by TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  started_at INTEGER,
  finished_at INTEGER,
  updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_free_status ON free_resources(available,test_status);
CREATE INDEX IF NOT EXISTS idx_models_status ON published_models(status,public_model_id);
CREATE INDEX IF NOT EXISTS idx_model_access_subject ON model_access(subject_type,subject_id);
CREATE INDEX IF NOT EXISTS idx_usage_user_time ON usage_ledger(user_id,occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_usage_user_time_v2 ON usage_ledger(user_id,occurred_at DESC,id DESC);
CREATE INDEX IF NOT EXISTS idx_usage_time ON usage_ledger(occurred_at DESC,id DESC);
CREATE INDEX IF NOT EXISTS idx_usage_model_time ON usage_ledger(public_model_id,occurred_at DESC,id DESC);
CREATE INDEX IF NOT EXISTS idx_usage_user_model_time ON usage_ledger(user_id,public_model_id,occurred_at DESC,id DESC);
CREATE INDEX IF NOT EXISTS idx_usage_model_fk ON usage_ledger(model_id,id);
CREATE INDEX IF NOT EXISTS idx_balance_user_time ON balance_transactions(user_id,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_grants_user_status ON token_grants(user_id,status);
CREATE INDEX IF NOT EXISTS idx_grants_active_balance ON token_grants(user_id,status,expires_at) WHERE tokens_remaining>0;
CREATE INDEX IF NOT EXISTS idx_stress_runs_created ON stress_runs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_stress_runs_status ON stress_runs(status,updated_at DESC);
"""


class Database:
    def __init__(self, config: Config):
        self.config = config
        self.runtime = DatabaseRuntime(config.db_path)
        self._connection_condition = threading.Condition()
        self._active_connections = 0
        self._migration_exclusive = False

    @staticmethod
    def _pymysql() -> Any:
        try:
            return importlib.import_module("pymysql")
        except ImportError as error:
            raise DatabaseCapabilityError(
                "MySQL 运行时未激活；请先在服务器执行 llmctl database enable-mysql"
            ) from error

    def _mysql_raw_connection(self, config: dict[str, Any] | None = None) -> Any:
        capability = self.runtime.capability()
        if not capability["enabled"]:
            raise DatabaseCapabilityError(
                "MySQL 能力尚未激活；请先执行 llmctl database enable-mysql"
            )
        mysql = self._pymysql()
        config = config or self.runtime.config(include_password=True)
        if not all(config.get(name) for name in ("host", "database", "username", "password")):
            raise DatabaseCapabilityError("MySQL 连接信息尚未配置完整")
        ssl_config: dict[str, Any] | None = None
        tls_mode = str(config.get("tls_mode") or "preferred")
        if tls_mode in {"preferred", "required"}:
            ssl_config = {"check_hostname": False}
        elif tls_mode == "verify_ca":
            ssl_config = {"ca": str(config.get("ca_file") or ""), "check_hostname": True}
        connect_options = {
            "host": str(config["host"]),
            "port": int(config["port"]),
            "user": str(config["username"]),
            "password": str(config["password"]),
            "database": str(config["database"]),
            "charset": "utf8mb4",
            "autocommit": False,
            "connect_timeout": 5,
            "read_timeout": 30,
            "write_timeout": 30,
            "ssl": ssl_config,
            "cursorclass": mysql.cursors.DictCursor,
        }
        try:
            return mysql.connect(**connect_options)
        except mysql.err.OperationalError as error:
            # preferred 模式只在服务器明确不支持 TLS（2026）时回退明文；
            # 认证、网络和证书错误不能被回退掩盖。
            if tls_mode != "preferred" or not error.args or error.args[0] != 2026:
                raise
            connect_options["ssl"] = None
            return mysql.connect(**connect_options)

    @contextlib.contextmanager
    def _sqlite_connect(self):
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

    @contextlib.contextmanager
    def connect(self):
        with self._connection_condition:
            if self._migration_exclusive:
                raise DatabaseMigrationError("数据库正在迁移，请稍后重试")
            self._active_connections += 1
        try:
            if self.runtime.config(include_password=False)["active_backend"] != "mysql":
                with self._sqlite_connect() as connection:
                    yield connection
                return
            raw_connection = self._mysql_raw_connection()
            connection = MySQLConnection(raw_connection)
            try:
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
        finally:
            with self._connection_condition:
                self._active_connections -= 1
                self._connection_condition.notify_all()

    @contextlib.contextmanager
    def migration_exclusive(self):
        """暂停新数据库请求，并等待已开始的事务结束。"""
        with self._connection_condition:
            if self._migration_exclusive:
                raise DatabaseMigrationError("已有数据库迁移正在执行")
            self._migration_exclusive = True
            while self._active_connections:
                self._connection_condition.wait(timeout=1)
        try:
            yield
        finally:
            with self._connection_condition:
                self._migration_exclusive = False
                self._connection_condition.notify_all()

    @property
    def is_mysql(self) -> bool:
        return self.runtime.config(include_password=False)["active_backend"] == "mysql"

    def is_integrity_error(self, error: BaseException) -> bool:
        if isinstance(error, sqlite3.IntegrityError):
            return True
        with contextlib.suppress(DatabaseCapabilityError):
            mysql = self._pymysql()
            return isinstance(error, mysql.err.IntegrityError)
        return False

    def initialize(self) -> None:
        if self.runtime.config(include_password=False)["active_backend"] == "mysql":
            self._initialize_mysql()
            return
        self._initialize_sqlite()

    def _initialize_mysql(self) -> None:
        with self.connect() as connection:
            marker = connection.execute(
                "SELECT schema_version FROM llmctl_database_meta WHERE id=1"
            ).fetchone()
            if not marker or int(marker["schema_version"] or 0) != MYSQL_SCHEMA_VERSION:
                raise SystemExit(
                    "MySQL schema 未通过 LLMCtl 迁移校验；已停止，避免使用不完整数据"
                )
            for table in ("users", "settings", "usage_ledger", "billing_accounts"):
                connection.execute(f"SELECT 1 FROM `{table}` LIMIT 1").fetchone()

    def _initialize_sqlite(self) -> None:
        self.config.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._sqlite_connect() as connection:
            connection.executescript(SCHEMA)
            had_welcome_balance = connection.execute(
                "SELECT 1 FROM settings WHERE key='default_welcome_balance'"
            ).fetchone() is not None
            defaults = {
                "registration_enabled": "1" if self.config.initial_registration else "0",
                "allowed_domains": ",".join(self.config.initial_domains),
                "default_quota_tokens": str(self.config.initial_quota),
                "default_quota_reset": self.config.initial_reset,
                "default_quota_reset_time": self.config.initial_reset_time,
                "default_welcome_balance": micros_to_money(
                    self.config.initial_welcome_balance_micros
                ),
                "default_max_sessions": "1",
                "default_requests_per_minute": "30",
                "default_requests_per_day": "2000",
                "portal_title": "LLMCtl",
                "published_origin": "",
                "public_url": self.config.public_url,
                "api_public_url": self.config.api_public_url,
                "smtp_host": self.config.smtp_host,
                "smtp_port": str(self.config.smtp_port),
                "smtp_security": self.config.smtp_security,
                "smtp_username": self.config.smtp_username,
                "smtp_password": self.config.smtp_password,
                "smtp_from": self.config.smtp_from,
                "currency": "USD",
            }
            for key, value in defaults.items():
                connection.execute(
                    "INSERT OR IGNORE INTO settings(key,value,updated_at) VALUES(?,?,?)",
                    (key, value, now()),
                )
            # 旧安装在注册初始关闭时会创建空公开来源；修复这些值，避免 SMTP
            # 和后续注册变更被无关空字段阻断。
            for key in ("public_url", "api_public_url"):
                connection.execute(
                    "UPDATE settings SET value=?,updated_at=? WHERE key=? AND TRIM(value)=''",
                    (defaults[key], now(), key),
                )
            user_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(users)")
            }
            if "login_name" not in user_columns:
                connection.execute("ALTER TABLE users ADD COLUMN login_name TEXT")
            if "max_sessions" not in user_columns:
                # 既有 Key 早于此控制，管理员明确修改前保持不限；新注册使用
                # default_max_sessions（默认 1）。
                connection.execute(
                    "ALTER TABLE users ADD COLUMN max_sessions INTEGER NOT NULL DEFAULT 0"
                )
            if "requests_per_minute" not in user_columns:
                connection.execute(
                    "ALTER TABLE users ADD COLUMN requests_per_minute INTEGER NOT NULL DEFAULT 0"
                )
            if "requests_per_day" not in user_columns:
                connection.execute(
                    "ALTER TABLE users ADD COLUMN requests_per_day INTEGER NOT NULL DEFAULT 0"
                )
            connection.execute(
                "UPDATE users SET login_name=email WHERE login_name IS NULL OR TRIM(login_name)=''"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_login_name "
                "ON users(login_name) WHERE login_name IS NOT NULL"
            )
            free_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(free_resources)")
            }
            if "native_visible" not in free_columns:
                connection.execute(
                    "ALTER TABLE free_resources ADD COLUMN native_visible INTEGER NOT NULL DEFAULT 1"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_free_native_visibility "
                "ON free_resources(native_visible,configured,available)"
            )
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(published_models)")
            }
            if "health_failures" not in columns:
                connection.execute(
                    "ALTER TABLE published_models ADD COLUMN health_failures INTEGER NOT NULL DEFAULT 0"
                )
            model_columns = {
                "context_window_tokens": "INTEGER",
                "max_output_tokens": "INTEGER",
                "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
                "metadata_sync_status": "TEXT NOT NULL DEFAULT 'unknown'",
                "metadata_sync_error": "TEXT NOT NULL DEFAULT ''",
                "metadata_synced_at": "INTEGER",
            }
            for name, declaration in model_columns.items():
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE published_models ADD COLUMN {name} {declaration}"
                    )
            grant_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(token_grants)")
            }
            if "reset_time" not in grant_columns:
                connection.execute(
                    "ALTER TABLE token_grants ADD COLUMN reset_time TEXT NOT NULL DEFAULT '00:00'"
                )
            for name, declaration in {
                "converted_at": "INTEGER",
                "converted_amount_micros": "INTEGER NOT NULL DEFAULT 0",
                "conversion_rate_micros": "INTEGER NOT NULL DEFAULT 0",
            }.items():
                if name not in grant_columns:
                    connection.execute(
                        f"ALTER TABLE token_grants ADD COLUMN {name} {declaration}"
                    )
            transaction_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(balance_transactions)")
            }
            if "source_ref" not in transaction_columns:
                connection.execute(
                    "ALTER TABLE balance_transactions ADD COLUMN source_ref TEXT"
                )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_balance_source_ref "
                "ON balance_transactions(source_ref) WHERE source_ref IS NOT NULL"
            )
            usage_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(usage_ledger)")
            }
            rebuild_usage_prices = False
            if "gross_amount_micros" not in usage_columns:
                connection.execute(
                    "ALTER TABLE usage_ledger ADD COLUMN gross_amount_micros INTEGER NOT NULL DEFAULT 0"
                )
                rebuild_usage_prices = True
            if "grant_amount_micros" not in usage_columns:
                connection.execute(
                    "ALTER TABLE usage_ledger ADD COLUMN grant_amount_micros INTEGER NOT NULL DEFAULT 0"
                )
                rebuild_usage_prices = True
            # 历史记录只保存钱包扣款；根据不可变价格快照重建标价费用，让升级后
            # 立即显示真实账单拆分。
            if rebuild_usage_prices:
                for usage in connection.execute(
                    "SELECT id,input_tokens,output_tokens,cached_tokens,reasoning_tokens,"
                    "amount_micros,price_snapshot_json FROM usage_ledger"
                ).fetchall():
                    try:
                        prices = json.loads(usage["price_snapshot_json"] or "{}")
                        priced = price_usage(
                            int(usage["input_tokens"]),
                            int(usage["output_tokens"]),
                            int(usage["cached_tokens"]),
                            int(usage["reasoning_tokens"]),
                            prices,
                        )
                        gross = priced["gross_amount_micros"]
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                        gross = int(usage["amount_micros"])
                    gross = max(gross, int(usage["amount_micros"]))
                    connection.execute(
                        "UPDATE usage_ledger SET gross_amount_micros=?,grant_amount_micros=? WHERE id=?",
                        (gross, gross - int(usage["amount_micros"]), usage["id"]),
                    )
            admin = connection.execute(
                "SELECT id FROM users WHERE role='admin' LIMIT 1"
            ).fetchone()
            if not admin:
                if not self.config.admin_password:
                    raise SystemExit("ACCOUNT_ADMIN_PASSWORD (or UI_PASSWORD) is required")
                connection.execute(
                    "INSERT INTO users(id,email,login_name,password_hash,role,status,quota_tokens,quota_reset,quota_reset_time,created_at,verified_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        str(uuid.uuid4()),
                        "admin@llmctl.local",
                        self.config.admin_username,
                        hash_admin_password(self.config.admin_password),
                        "admin",
                        "active",
                        0,
                        self.config.initial_reset,
                        self.config.initial_reset_time,
                        now(),
                        now(),
                    ),
                )
            stamp = now()
            connection.execute(
                "INSERT OR IGNORE INTO user_groups(id,name,description,status,created_at,updated_at) VALUES('default','default','Default LLMCtl users','active',?,?)",
                (stamp, stamp),
            )
            connection.execute(
                "UPDATE user_groups SET description=?,updated_at=? WHERE id='default' AND description='Default company users'",
                ("Default LLMCtl users", stamp),
            )
            connection.execute(
                "INSERT OR IGNORE INTO billing_accounts(user_id,balance_micros,suspended,updated_at) SELECT id,0,0,? FROM users",
                (stamp,),
            )
            connection.execute(
                "INSERT OR IGNORE INTO user_group_members(user_id,group_id,created_at) SELECT id,'default',? FROM users WHERE role='user'",
                (stamp,),
            )
            for row in connection.execute(
                "SELECT id,quota_tokens,quota_reset FROM users WHERE role='user' AND status='active'"
            ).fetchall():
                existing = connection.execute(
                    "SELECT 1 FROM token_grants WHERE user_id=? LIMIT 1", (row["id"],)
                ).fetchone()
                if not existing and int(row["quota_tokens"] or 0) > 0:
                    connection.execute(
                        "INSERT INTO token_grants(id,user_id,model_id,label,tokens_initial,tokens_remaining,reset_interval,reset_time,reset_at,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            str(uuid.uuid4()), row["id"], None, "Migrated recurring grant",
                            int(row["quota_tokens"]), int(row["quota_tokens"]), row["quota_reset"],
                            row["quota_reset_time"],
                            next_reset_at(row["quota_reset"], reset_time=row["quota_reset_time"]),
                            "active", stamp, stamp,
                        ),
                    )
            self._migrate_token_grants_to_cash(
                connection, had_welcome_balance=had_welcome_balance
            )
        os.chmod(self.config.db_path, 0o600)

    @staticmethod
    def _migrate_token_grants_to_cash(
        connection: sqlite3.Connection, had_welcome_balance: bool
    ) -> dict[str, int | str]:
        """把全部剩余赠送 Token 一次性折算为现金。

        迁移全程使用事务并采用失败关闭策略：任一有效赠额无法定价时，
        所有赠额均保持不变；唯一来源标识可防止初始化重试时重复入账。
        """
        stamp = now()
        grants = connection.execute(
            "SELECT * FROM token_grants WHERE status='active' AND tokens_remaining>0 "
            "ORDER BY created_at,id"
        ).fetchall()
        price_case = (
            "CASE WHEN input_price_micros>=output_price_micros "
            "AND input_price_micros>=cached_price_micros "
            "AND input_price_micros>=reasoning_price_micros THEN input_price_micros "
            "WHEN output_price_micros>=cached_price_micros "
            "AND output_price_micros>=reasoning_price_micros THEN output_price_micros "
            "WHEN cached_price_micros>=reasoning_price_micros THEN cached_price_micros "
            "ELSE reasoning_price_micros END"
        )
        public_rate_row = connection.execute(
            f"SELECT MAX({price_case}) rate FROM published_models "
            "WHERE status='published'"
        ).fetchone()
        public_rate = int(public_rate_row["rate"] or 0) if public_rate_row else 0
        legacy_default = connection.execute(
            "SELECT value FROM settings WHERE key='default_quota_tokens'"
        ).fetchone()
        legacy_tokens = int(legacy_default["value"] or 0) if legacy_default else 0
        conversions: list[tuple[sqlite3.Row, int, int]] = []
        blocked: list[str] = []
        for grant in grants:
            rate = public_rate
            if grant["model_id"]:
                model = connection.execute(
                    f"SELECT {price_case} rate FROM published_models WHERE id=?",
                    (grant["model_id"],),
                ).fetchone()
                rate = int(model["rate"] or 0) if model else 0
            if rate <= 0:
                blocked.append(str(grant["id"]))
                continue
            amount = tokens_to_money_micros(grant["tokens_remaining"], rate)
            conversions.append((grant, rate, amount))
        if legacy_tokens > 0 and public_rate <= 0:
            blocked.append("settings:default_quota_tokens")

        status = "complete"
        if blocked:
            status = "blocked:missing-price"
            conversions = []
        converted = credited = 0
        for grant, rate, amount in conversions:
            source_ref = f"token-grant-conversion:{grant['id']}"
            if connection.execute(
                "SELECT 1 FROM balance_transactions WHERE source_ref=?", (source_ref,)
            ).fetchone():
                continue
            account = connection.execute(
                "SELECT balance_micros FROM billing_accounts WHERE user_id=?",
                (grant["user_id"],),
            ).fetchone()
            balance = int(account["balance_micros"] or 0) if account else 0
            after = balance + amount
            connection.execute(
                "INSERT INTO billing_accounts(user_id,balance_micros,suspended,updated_at) "
                "VALUES(?,?,0,?) ON CONFLICT(user_id) DO UPDATE SET "
                "balance_micros=excluded.balance_micros,updated_at=excluded.updated_at",
                (grant["user_id"], after, stamp),
            )
            connection.execute(
                """INSERT INTO balance_transactions(
                     user_id,kind,amount_micros,balance_after_micros,actor,note,
                     source_ref,created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    grant["user_id"], "credit", amount, after, "system:migration",
                    f"Converted {int(grant['tokens_remaining'])} remaining tokens at "
                    f"{micros_to_money(rate)} USD/1M", source_ref, stamp,
                ),
            )
            connection.execute(
                "UPDATE token_grants SET tokens_remaining=0,status='disabled',"
                "converted_at=?,converted_amount_micros=?,conversion_rate_micros=?,"
                "updated_at=? WHERE id=?",
                (stamp, amount, rate, stamp, grant["id"]),
            )
            connection.execute(
                "INSERT INTO audit_events(created_at,actor,action,target,status,remote_addr,detail) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    stamp, "system:migration", "billing.grant-to-cash", grant["user_id"],
                    "success", "local",
                    json.dumps(
                        {
                            "grant_id": grant["id"],
                            "remaining_tokens": int(grant["tokens_remaining"]),
                            "rate_micros_per_million": rate,
                            "credited_micros": amount,
                        },
                        separators=(",", ":"),
                    ),
                ),
            )
            converted += 1
            credited += amount

        if not had_welcome_balance and legacy_tokens > 0 and not blocked:
            welcome = tokens_to_money_micros(legacy_tokens, public_rate)
            connection.execute(
                "UPDATE settings SET value=?,updated_at=? WHERE key='default_welcome_balance'",
                (micros_to_money(welcome), stamp),
            )
        elif blocked and not had_welcome_balance:
            # initialize() 为新设置写入了临时值；先删除它，使模型价格可用后的
            # 重试仍能识别并转换旧注册策略。
            connection.execute(
                "DELETE FROM settings WHERE key='default_welcome_balance'"
            )
        if not blocked:
            # 所有赠额完成估值后才停用旧来源。
            connection.execute(
                "UPDATE token_grants SET status='disabled',converted_at=COALESCE(converted_at,?),"
                "converted_amount_micros=COALESCE(converted_amount_micros,0),"
                "conversion_rate_micros=COALESCE(conversion_rate_micros,0),updated_at=? "
                "WHERE status='active' AND tokens_remaining<=0",
                (stamp, stamp),
            )
            connection.execute(
                "UPDATE settings SET value='0',updated_at=? WHERE key='default_quota_tokens'",
                (stamp,),
            )
            connection.execute("UPDATE users SET quota_tokens=0 WHERE role='user'")
        connection.execute(
            "INSERT INTO settings(key,value,updated_at) VALUES('token_grant_conversion_status',?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            (status, stamp),
        )
        return {
            "converted": converted,
            "credited_micros": credited,
            "status": status,
        }

    def finalize_legacy_billing_migration(self) -> dict[str, int | str]:
        """Retry conversion after the managed model and its prices are present."""
        with self.connect() as connection:
            had_welcome_balance = connection.execute(
                "SELECT 1 FROM settings WHERE key='default_welcome_balance'"
            ).fetchone() is not None
            return self._migrate_token_grants_to_cash(
                connection, had_welcome_balance=had_welcome_balance
            )

    def settings(self) -> dict[str, str]:
        with self.connect() as connection:
            return {row["key"]: row["value"] for row in connection.execute("SELECT key,value FROM settings")}

    def update_settings(self, values: dict[str, str]) -> None:
        with self.connect() as connection:
            for key, value in values.items():
                connection.execute(
                    "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                    (key, value, now()),
                )

    def recovery_inventory(self, show_secrets: bool = False) -> dict[str, Any]:
        """返回 ``llmctl info`` 所需的门户持久化状态。"""
        backend = "mysql" if self.is_mysql else "sqlite"
        if backend == "sqlite" and not self.config.db_path.is_file():
            raise RuntimeError(f"portal database does not exist: {self.config.db_path}")
        tables = (
            "users",
            "user_groups",
            "published_models",
            "free_resources",
            "usage_ledger",
            "balance_transactions",
            "audit_events",
        )
        with self.connect() as connection:
            settings = {
                row["key"]: row["value"]
                for row in connection.execute("SELECT key,value FROM settings ORDER BY key")
            }
            counts = {
                table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in tables
            }
            integrity = "connected"
            if backend == "sqlite":
                integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        if not show_secrets and settings.get("smtp_password"):
            settings["smtp_password"] = "<redacted>"
        database: dict[str, Any] = {
            "backend": backend,
            "quick_check": integrity,
        }
        if backend == "sqlite":
            stat = self.config.db_path.stat()
            database.update(
                {
                    "path": str(self.config.db_path),
                    "bytes": stat.st_size,
                    "mode": oct(stat.st_mode & 0o777),
                }
            )
        else:
            database.update(self.runtime.config(include_password=show_secrets))
        return {
            "version": APP_VERSION,
            "database": database,
            "settings": settings,
            "counts": counts,
        }

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


class DatabaseMigrationManager:
    """在后台执行 SQLite 到 MySQL 的可验证单向迁移。"""

    def __init__(self, database: Database):
        self.database = database
        self.runtime = database.runtime
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._progress_token = ""

    def snapshot(self) -> dict[str, Any]:
        capability = self.runtime.capability()
        config = self.runtime.config(include_password=False)
        migration = self.runtime.migration()
        thread_running = bool(self._thread and self._thread.is_alive())
        sqlite = {
            "path": str(self.database.config.db_path),
            "exists": self.database.config.db_path.is_file(),
            "bytes": (
                self.database.config.db_path.stat().st_size
                if self.database.config.db_path.is_file()
                else 0
            ),
        }
        return {
            "capability": capability,
            "config": config,
            "migration": migration,
            "sqlite": sqlite,
            "busy": thread_running or migration.get("status") == "running",
            "requirements": {
                "server": "MySQL 8.0+",
                "empty_database": True,
                "scope": "LLMCtl account portal only",
            },
        }

    def save_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.runtime.capability()["enabled"]:
            raise DatabaseCapabilityError(
                "MySQL 能力尚未激活；请先在服务器执行 llmctl database enable-mysql"
            )
        if self.database.is_mysql:
            raise DatabaseMigrationError(
                "当前已使用 MySQL；为避免运行中换库，请先回滚到 SQLite"
            )
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise DatabaseMigrationError("数据库迁移执行期间不能修改连接配置")
            return self.runtime.save_config(payload)

    def test(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if payload:
            self.save_config(payload)
        config = self.runtime.config(include_password=True)
        started = time.monotonic()
        connection = self.database._mysql_raw_connection(config)
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT VERSION() AS version, DATABASE() AS database_name")
                row = cursor.fetchone()
                cursor.execute("SHOW TABLES")
                tables = cursor.fetchall()
            version = str(row.get("version") or "")
            match = re.match(r"^(\d+)\.(\d+)", version)
            if not match or int(match.group(1)) < 8:
                raise DatabaseMigrationError(
                    f"需要 MySQL 8.0 或更高版本，当前为 {version or 'unknown'}"
                )
            return {
                "ok": True,
                "version": version,
                "database": str(row.get("database_name") or ""),
                "tables": len(tables),
                "empty": not tables,
                "latency_ms": round((time.monotonic() - started) * 1000, 1),
            }
        finally:
            connection.close()

    def start(self, actor: str) -> dict[str, Any]:
        if self.database.is_mysql:
            raise DatabaseMigrationError("门户当前已经使用 MySQL")
        if not self.database.config.db_path.is_file():
            raise DatabaseMigrationError("SQLite 源数据库不存在")
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise DatabaseMigrationError("已有数据库迁移正在执行")
            test_result = self.test()
            if not test_result["empty"]:
                raise DatabaseMigrationError(
                    "目标 MySQL database 必须为空；不会覆盖已有表"
                )
            migration_id = str(uuid.uuid4())
            self._progress_token = secrets.token_urlsafe(32)
            self.runtime.save_migration(
                id=migration_id,
                status="running",
                stage="准备冻结 SQLite 写入",
                progress=1,
                actor=actor,
                started_at=now(),
                finished_at=0,
                error="",
                backup_path="",
                source_backend="sqlite",
                target_backend="mysql",
                table_counts={},
            )
            self._thread = threading.Thread(
                target=self._run,
                args=(migration_id,),
                name="llmctl-database-migration",
                daemon=True,
            )
            self._thread.start()
        result = self.snapshot()
        result["progress_token"] = self._progress_token
        return result

    def progress(self, supplied_token: str) -> dict[str, Any]:
        """迁移期间不访问业务数据库，使用一次性随机令牌返回进度。"""
        if not self._progress_token or not hmac.compare_digest(
            self._progress_token, supplied_token
        ):
            raise PermissionError("迁移进度令牌无效")
        return {
            "migration": self.runtime.migration(),
            "busy": bool(self._thread and self._thread.is_alive()),
        }

    def rollback_to_sqlite(self, confirmation: str, actor: str) -> dict[str, Any]:
        if confirmation != "ROLLBACK_TO_SQLITE":
            raise ValueError("请输入 ROLLBACK_TO_SQLITE 确认回滚")
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise DatabaseMigrationError("数据库迁移执行期间不能回滚")
            if not self.database.is_mysql:
                raise DatabaseMigrationError("当前没有使用 MySQL")
            if not self.database.config.db_path.is_file():
                raise DatabaseMigrationError("原 SQLite 数据库不存在，不能回滚")
            with self.database._sqlite_connect() as source:
                quick_check = str(source.execute("PRAGMA quick_check").fetchone()[0])
            if quick_check != "ok":
                raise DatabaseMigrationError(
                    f"SQLite 完整性检查失败：{quick_check}"
                )
            self.runtime.set_active_backend("sqlite")
            self.runtime.save_migration(
                status="rolled_back",
                stage="已回滚到迁移时保留的 SQLite",
                progress=100,
                rolled_back_at=now(),
                rolled_back_by=actor,
                error="",
            )
        return self.snapshot()

    @staticmethod
    def _identifier(value: str) -> str:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            raise DatabaseMigrationError(f"发现不安全的数据库标识符：{value}")
        return f"`{value}`"

    @staticmethod
    def _sqlite_default(value: Any, mysql_type: str) -> str:
        if value is None:
            return ""
        raw = str(value).strip()
        if re.fullmatch(r"-?\d+(?:\.\d+)?", raw):
            return f" DEFAULT {raw}"
        if raw.upper() == "NULL":
            return " DEFAULT NULL"
        if len(raw) >= 2 and raw[0] in {"'", '"'} and raw[-1] == raw[0]:
            literal = raw[1:-1].replace(raw[0] * 2, raw[0])
            escaped = literal.replace("'", "''")
            if mysql_type in {"LONGTEXT", "LONGBLOB"}:
                return f" DEFAULT ('{escaped}')"
            return f" DEFAULT '{escaped}'"
        return ""

    def _mysql_table_definition(
        self, source: sqlite3.Connection, table: str
    ) -> tuple[str, list[dict[str, Any]]]:
        columns = [dict(row) for row in source.execute(f"PRAGMA table_info({self._identifier(table)})")]
        indexes = [dict(row) for row in source.execute(f"PRAGMA index_list({self._identifier(table)})")]
        foreign_keys = [
            dict(row)
            for row in source.execute(f"PRAGMA foreign_key_list({self._identifier(table)})")
        ]
        keyed: set[str] = {str(row["name"]) for row in columns if int(row["pk"] or 0)}
        for index in indexes:
            keyed.update(
                str(row["name"])
                for row in source.execute(
                    f"PRAGMA index_info({self._identifier(str(index['name']))})"
                )
            )
        keyed.update(str(row["from"]) for row in foreign_keys)
        keyed.update(str(row["to"]) for row in foreign_keys)
        primary = sorted(
            (row for row in columns if int(row["pk"] or 0)),
            key=lambda row: int(row["pk"]),
        )
        primary_names = {str(row["name"]) for row in primary}
        auto_primary = (
            len(primary) == 1
            and "INT" in str(primary[0]["type"] or "").upper()
        )
        definitions: list[str] = []
        for column in columns:
            declared = str(column["type"] or "TEXT").upper()
            name = str(column["name"])
            if "INT" in declared:
                mysql_type = "BIGINT"
            elif any(value in declared for value in ("REAL", "FLOA", "DOUB")):
                mysql_type = "DOUBLE"
            elif "BLOB" in declared:
                mysql_type = "LONGBLOB"
            elif name in keyed:
                mysql_type = "VARCHAR(191)"
            else:
                mysql_type = "LONGTEXT"
            item = f"{self._identifier(name)} {mysql_type}"
            if auto_primary and name == str(primary[0]["name"]):
                item += " NOT NULL AUTO_INCREMENT PRIMARY KEY"
            else:
                # SQLite 的复合主键列可能在 PRAGMA table_info 中仍显示
                # notnull=0；MySQL 要求主键的每一列都显式声明 NOT NULL。
                item += (
                    " NOT NULL"
                    if name in primary_names or int(column["notnull"] or 0)
                    else " NULL"
                )
                item += self._sqlite_default(column["dflt_value"], mysql_type)
            definitions.append(item)
        if primary and not auto_primary:
            definitions.append(
                "PRIMARY KEY ("
                + ",".join(self._identifier(str(row["name"])) for row in primary)
                + ")"
            )
        for foreign in foreign_keys:
            definitions.append(
                "FOREIGN KEY ("
                + self._identifier(str(foreign["from"]))
                + ") REFERENCES "
                + self._identifier(str(foreign["table"]))
                + " ("
                + self._identifier(str(foreign["to"]))
                + ") ON UPDATE "
                + str(foreign["on_update"] or "NO ACTION")
                + " ON DELETE "
                + str(foreign["on_delete"] or "NO ACTION")
            )
        statement = (
            f"CREATE TABLE {self._identifier(table)} ("
            + ",".join(definitions)
            + ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
        )
        return statement, indexes

    def _create_indexes(
        self,
        source: sqlite3.Connection,
        cursor: Any,
        table: str,
        indexes: list[dict[str, Any]],
    ) -> None:
        for index in indexes:
            if str(index.get("origin")) == "pk":
                continue
            name = str(index["name"])
            columns = [
                str(row["name"])
                for row in source.execute(
                    f"PRAGMA index_info({self._identifier(name)})"
                )
            ]
            if not columns:
                continue
            prefix = "CREATE UNIQUE INDEX" if int(index.get("unique") or 0) else "CREATE INDEX"
            cursor.execute(
                f"{prefix} {self._identifier(name[:64])} ON {self._identifier(table)} ("
                + ",".join(self._identifier(value) for value in columns)
                + ")"
            )

    @staticmethod
    def _digest_value(value: Any) -> Any:
        if isinstance(value, bytes):
            return {"base64": base64.b64encode(value).decode("ascii")}
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return str(value)

    def _table_digest(
        self, connection: Any, table: str, columns: list[str], primary: list[str]
    ) -> tuple[int, str]:
        order = primary or columns
        statement = (
            "SELECT "
            + ",".join(self._identifier(value) for value in columns)
            + f" FROM {self._identifier(table)} ORDER BY "
            + ",".join(self._identifier(value) for value in order)
        )
        digest = hashlib.sha256()
        count = 0
        cursor = connection.execute(statement) if isinstance(connection, sqlite3.Connection) else connection.cursor()
        if not isinstance(connection, sqlite3.Connection):
            cursor.execute(statement)
        while True:
            rows = cursor.fetchmany(500)
            if not rows:
                break
            for row in rows:
                values = [
                    self._digest_value(row[name] if hasattr(row, "keys") else row[index])
                    for index, name in enumerate(columns)
                ]
                digest.update(
                    json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode(
                        "utf-8"
                    )
                    + b"\n"
                )
                count += 1
        return count, digest.hexdigest()

    def _run(self, migration_id: str) -> None:
        source: sqlite3.Connection | None = None
        target: Any = None
        backup_path = ""
        created_tables: list[str] = []
        target_cleaned = False
        exclusive = self.database.migration_exclusive()
        exclusive_entered = False
        try:
            # 全程暂停门户数据库请求，防止校验完成后仍有请求写入旧 SQLite。
            # 管理页面通过独立的无数据库状态接口轮询进度。
            exclusive.__enter__()
            exclusive_entered = True
            self.runtime.save_migration(
                stage="已暂停门户数据库请求，正在创建 SQLite 备份", progress=5
            )
            source = sqlite3.connect(self.database.config.db_path, timeout=30)
            source.row_factory = sqlite3.Row
            source.execute("PRAGMA foreign_keys=ON")
            backup_directory = self.database.config.db_path.parent / "backups"
            backup_directory.mkdir(parents=True, exist_ok=True)
            backup = backup_directory / (
                "before-mysql-"
                + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                + ".db"
            )
            backup_connection = sqlite3.connect(backup)
            try:
                source.backup(backup_connection)
                check = str(backup_connection.execute("PRAGMA quick_check").fetchone()[0])
                if check != "ok":
                    raise DatabaseMigrationError(f"SQLite 备份完整性检查失败：{check}")
            finally:
                backup_connection.close()
            os.chmod(backup, 0o600)
            backup_path = str(backup)
            source.execute("BEGIN IMMEDIATE")
            self.runtime.save_migration(
                backup_path=backup_path,
                stage="SQLite 备份完成，正在创建 MySQL schema",
                progress=12,
            )

            target = self.database._mysql_raw_connection()
            target.autocommit(False)
            with target.cursor() as cursor:
                cursor.execute("SHOW TABLES")
                if cursor.fetchall():
                    raise DatabaseMigrationError(
                        "目标 MySQL database 在迁移开始后出现了表，已拒绝覆盖"
                    )
                cursor.execute("SET FOREIGN_KEY_CHECKS=0")
                tables = [
                    str(row["name"])
                    for row in source.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name NOT LIKE 'sqlite_%' ORDER BY rootpage"
                    )
                ]
                definitions: dict[str, list[dict[str, Any]]] = {}
                for table in tables:
                    ddl, indexes = self._mysql_table_definition(source, table)
                    cursor.execute(ddl)
                    created_tables.append(table)
                    definitions[table] = indexes
                table_counts: dict[str, int] = {}
                for table_index, table in enumerate(tables):
                    columns = [
                        str(row["name"])
                        for row in source.execute(
                            f"PRAGMA table_info({self._identifier(table)})"
                        )
                    ]
                    select_cursor = source.execute(
                        "SELECT "
                        + ",".join(self._identifier(value) for value in columns)
                        + f" FROM {self._identifier(table)}"
                    )
                    insert = (
                        f"INSERT INTO {self._identifier(table)} ("
                        + ",".join(self._identifier(value) for value in columns)
                        + ") VALUES ("
                        + ",".join("%s" for _ in columns)
                        + ")"
                    )
                    copied = 0
                    while True:
                        batch = select_cursor.fetchmany(500)
                        if not batch:
                            break
                        cursor.executemany(
                            insert,
                            [tuple(row[value] for value in columns) for row in batch],
                        )
                        copied += len(batch)
                    table_counts[table] = copied
                    self.runtime.save_migration(
                        stage=f"正在复制 {table}",
                        progress=15 + int((table_index + 1) * 55 / max(1, len(tables))),
                        table_counts=table_counts,
                    )
                for table in tables:
                    self._create_indexes(source, cursor, table, definitions[table])
                cursor.execute("SET FOREIGN_KEY_CHECKS=1")
            target.commit()
            self.runtime.save_migration(stage="正在逐表校验数量与 SHA-256", progress=75)
            validation: dict[str, Any] = {}
            for table_index, table in enumerate(tables):
                info = [
                    dict(row)
                    for row in source.execute(
                        f"PRAGMA table_info({self._identifier(table)})"
                    )
                ]
                columns = [str(row["name"]) for row in info]
                primary = [
                    str(row["name"])
                    for row in sorted(
                        (row for row in info if int(row["pk"] or 0)),
                        key=lambda value: int(value["pk"]),
                    )
                ]
                source_count, source_digest = self._table_digest(
                    source, table, columns, primary
                )
                target_count, target_digest = self._table_digest(
                    target, table, columns, primary
                )
                if source_count != target_count or source_digest != target_digest:
                    raise DatabaseMigrationError(
                        f"{table} 校验失败：SQLite={source_count}/{source_digest[:12]}，"
                        f"MySQL={target_count}/{target_digest[:12]}"
                    )
                validation[table] = {
                    "rows": source_count,
                    "sha256": source_digest,
                }
                self.runtime.save_migration(
                    stage=f"已校验 {table}",
                    progress=75 + int((table_index + 1) * 18 / max(1, len(tables))),
                )

            with target.cursor() as cursor:
                cursor.execute(
                    "CREATE TABLE llmctl_database_meta ("
                    "id BIGINT NOT NULL PRIMARY KEY,schema_version BIGINT NOT NULL,"
                    "source_backend VARCHAR(32) NOT NULL,migration_id VARCHAR(64) NOT NULL,"
                    "migrated_at BIGINT NOT NULL) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
                )
                created_tables.append("llmctl_database_meta")
                cursor.execute(
                    "INSERT INTO llmctl_database_meta VALUES (1,%s,'sqlite',%s,%s)",
                    (MYSQL_SCHEMA_VERSION, migration_id, now()),
                )
            target.commit()
            self.runtime.set_active_backend("mysql")
            source.commit()
            self.runtime.save_migration(
                status="completed",
                stage="迁移校验通过，门户已切换到 MySQL",
                progress=100,
                finished_at=now(),
                validation=validation,
                error="",
            )
        except Exception as error:
            with contextlib.suppress(Exception):
                if source:
                    source.rollback()
            with contextlib.suppress(Exception):
                if target:
                    target.rollback()
            # MySQL 的 CREATE TABLE 会隐式提交。目标库在开始时已经验证为空，
            # 因此失败时只删除本次迁移明确创建的表，使管理员修正问题后可重试；
            # 绝不扫描或删除迁移期间由其他系统新增的未知表。
            if target and created_tables:
                try:
                    with target.cursor() as cursor:
                        cursor.execute("SET FOREIGN_KEY_CHECKS=0")
                        for table in reversed(created_tables):
                            cursor.execute(
                                f"DROP TABLE IF EXISTS {self._identifier(table)}"
                            )
                        cursor.execute("SET FOREIGN_KEY_CHECKS=1")
                    target.commit()
                    target_cleaned = True
                except Exception as cleanup_error:
                    print(
                        f"[account-portal] MySQL migration cleanup failed: {cleanup_error}",
                        file=sys.stderr,
                        flush=True,
                    )
            self.runtime.set_active_backend("sqlite")
            self.runtime.save_migration(
                status="failed",
                stage=(
                    "迁移失败，已清理本次创建的 MySQL 表，门户继续使用 SQLite"
                    if target_cleaned
                    else "迁移失败，门户继续使用 SQLite"
                ),
                finished_at=now(),
                error=str(error)[:2000],
                backup_path=backup_path,
                target_cleaned=target_cleaned,
            )
            print(
                f"[account-portal] MySQL migration failed: {error}",
                file=sys.stderr,
                flush=True,
            )
        finally:
            if source:
                source.close()
            if target:
                target.close()
            if exclusive_entered:
                exclusive.__exit__(None, None, None)


class OmniRouteClient:
    def __init__(self, config: Config):
        if not config.gateway_manage_key:
            raise RuntimeError("GATEWAY_API_KEY is missing")
        self.base_url = config.gateway_url
        self.key = config.gateway_manage_key
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, method: str, path: str, payload: Any = None) -> Any:
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
        return parsed

    def create_user_key(
        self, user_id: str, email: str, max_sessions: int = 0
    ) -> tuple[str, str]:
        response = self.request(
            "POST",
            "/api/keys",
            {
                "name": f"portal:{user_id}:{email}",
                "scopes": ["self:usage"],
                # OmniRoute 把空 allowlist 解释为不限；先关闭，门户发布有效用户/
                # 用户组策略后才向用户返回 Key 明文。
                "allowedModels": ["__llmctl_no_models__"],
                "allowedCombos": ["__llmctl_no_combos__"],
                "streamDefaultMode": "json",
                "noLog": False,
                "maxSessions": max_sessions,
            },
        )
        key_id, raw_key = str(response.get("id", "")), str(response.get("key", ""))
        if not key_id or len(raw_key) < 16:
            raise RuntimeError("OmniRoute did not return the new API key")
        return key_id, raw_key

    def reveal_user_key(self, key_id: str) -> str:
        """Return the existing key; revealing must never rotate or replace it."""
        path = f"/api/keys/{urllib.parse.quote(key_id, safe='')}/reveal"
        try:
            response = self.request("GET", path)
        except RuntimeError as error:
            # 旧 LLMCtl 安装启动 OmniRoute 时关闭了 Key 展示；原生开关可热加载，
            # 升级门户无需重启网关或 GPU Worker 即可修复。
            if "HTTP 403" not in str(error) or "reveal is disabled" not in str(error):
                raise
            self.request(
                "PUT",
                "/api/settings/feature-flags",
                {"key": "ALLOW_API_KEY_REVEAL", "value": "true"},
            )
            response = self.request("GET", path)
        raw_key = str(response.get("key", "")) if isinstance(response, dict) else ""
        if len(raw_key) < 16:
            raise RuntimeError("OmniRoute did not return the existing API key")
        return raw_key

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

    def set_key_max_sessions(self, key_id: str, max_sessions: int) -> None:
        self.request(
            "PATCH",
            f"/api/keys/{urllib.parse.quote(key_id, safe='')}",
            {"maxSessions": max_sessions},
        )

    def usage(self, key_id: str) -> dict[str, Any]:
        return self.request(
            "GET", f"/api/usage/token-limits?apiKeyId={urllib.parse.quote(key_id, safe='')}"
        )

    def models(self) -> list[dict[str, Any]]:
        response = self.request("GET", "/v1/models")
        if not isinstance(response, dict):
            return []
        data = response.get("data", [])
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    @staticmethod
    def items(response: Any, *keys: str) -> list[dict[str, Any]]:
        if isinstance(response, list):
            return [item for item in response if isinstance(item, dict)]
        if not isinstance(response, dict):
            return []
        for key in keys:
            value = response.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return []

    def combos(self) -> list[dict[str, Any]]:
        return self.items(self.request("GET", "/api/combos?limit=1000"), "combos")

    def upsert_combo(
        self, combo_id: str, payload: dict[str, Any], active: bool = True
    ) -> tuple[str, bool]:
        """Create or update a native OmniRoute combo and return (id, created)."""
        if combo_id:
            response = self.request(
                "PUT",
                f"/api/combos/{urllib.parse.quote(combo_id, safe='')}",
                {**payload, "isActive": active},
            )
            created = False
        else:
            response = self.request("POST", "/api/combos", payload)
            created = True
        combo = response.get("combo", response) if isinstance(response, dict) else {}
        result = str(combo.get("id", combo_id)) if isinstance(combo, dict) else combo_id
        if not result:
            # 不同 OmniRoute 版本会返回原始或包装后的 Combo；按名称重新获取可
            # 同时兼容两种形式。
            result = str(
                next(
                    (
                        item.get("id", "")
                        for item in self.combos()
                        if str(item.get("name", "")) == str(payload.get("name", ""))
                    ),
                    "",
                )
            )
        if not result:
            raise RuntimeError("OmniRoute did not return the combo id")
        if created and not active:
            self.request(
                "PUT",
                f"/api/combos/{urllib.parse.quote(result, safe='')}",
                {"isActive": False},
            )
        return result, created

    def set_combo_active(self, combo_id: str, active: bool) -> None:
        self.request(
            "PUT",
            f"/api/combos/{urllib.parse.quote(combo_id, safe='')}",
            {"isActive": active},
        )

    def delete_combo(self, combo_id: str) -> None:
        self.request("DELETE", f"/api/combos/{urllib.parse.quote(combo_id, safe='')}")

    def sync_workflow_routes(
        self, workflow_config: dict[str, Any], workflow_secret: str
    ) -> dict[str, Any]:
        """Publish enabled Go workflow routes as an explicit OmniRoute provider.

        This is intentionally opt-in.  Saving a workflow configuration never
        changes the production gateway, and upgrades never call this method.
        Public aliases such as ``gdn-inside`` remain owned by the portal's
        normal model publishing screen; this method creates collision-free
        ``llmctl-workflow-*`` combo targets for an administrator to select.
        """
        if len(workflow_secret) < 24:
            raise ValueError("工作流共享密钥尚未配置")
        raw_base_url = str(workflow_config.get("gateway_base_url", "")).strip().rstrip("/")
        parsed = urllib.parse.urlparse(raw_base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("工作流 gateway_base_url 必须是无内嵌凭据的 HTTP(S) URL")
        routes = {
            str(route_id).strip(): route
            for route_id, route in dict(workflow_config.get("models") or {}).items()
            if str(route_id).strip()
            and isinstance(route, dict)
            and route.get("enabled") is True
        }
        if not routes:
            raise ValueError("没有已启用的工作流模型路由")

        # 若管理员已占用确定性 Combo 名称，在修改 Provider 状态前失败。这样显式
        # 同步从运维视角保持事务性，命名冲突不会残留新 Node/Connection/Model。
        combos = self.combos()
        existing_combos = {
            str(item.get("name", "")): item for item in combos if item.get("name")
        }
        for route_id in sorted(routes):
            combo_name = f"llmctl-workflow-{route_id}"
            existing_combo = existing_combos.get(combo_name)
            if (
                existing_combo
                and existing_combo.get("description")
                != WORKFLOW_GATEWAY_MANAGED_DESCRIPTION
            ):
                raise RuntimeError(
                    f"OmniRoute 路由组合 {combo_name!r} 已存在且不由 LLMCtl 管理"
                )

        nodes = self.items(
            self.request("GET", "/api/provider-nodes?limit=1000"), "nodes"
        )
        matches = [item for item in nodes if item.get("name") == WORKFLOW_GATEWAY_NODE_NAME]
        node_payload = {
            "name": WORKFLOW_GATEWAY_NODE_NAME,
            "prefix": WORKFLOW_GATEWAY_PREFIX,
            "apiType": "chat",
            "type": "openai-compatible",
            "baseUrl": raw_base_url,
            "chatPath": "/chat/completions",
            "modelsPath": "/models",
        }
        if matches:
            node_id = str(matches[0].get("id", ""))
            response = self.request(
                "PUT",
                f"/api/provider-nodes/{urllib.parse.quote(node_id, safe='')}",
                node_payload,
            )
            node = response.get("node", matches[0]) if isinstance(response, dict) else matches[0]
        else:
            response = self.request("POST", "/api/provider-nodes", node_payload)
            node = response.get("node", {}) if isinstance(response, dict) else {}
        node_id = str(node.get("id", ""))
        if not node_id:
            raise RuntimeError("OmniRoute 未返回工作流 Provider Node ID")

        connections = self.items(
            self.request("GET", "/api/providers?limit=1000"), "connections"
        )
        connection_matches = [item for item in connections if item.get("provider") == node_id]
        first_model = sorted(routes)[0]
        connection_payload = {
            "name": WORKFLOW_GATEWAY_CONNECTION_NAME,
            "apiKey": workflow_secret,
            "priority": 1,
            "maxConcurrent": 512,
            "defaultModel": first_model,
            "isActive": True,
            "testStatus": "success",
        }
        if connection_matches:
            connection_id = str(connection_matches[0].get("id", ""))
            response = self.request(
                "PUT",
                f"/api/providers/{urllib.parse.quote(connection_id, safe='')}",
                connection_payload,
            )
            connection = (
                response.get("connection", connection_matches[0])
                if isinstance(response, dict)
                else connection_matches[0]
            )
        else:
            response = self.request(
                "POST", "/api/providers", {"provider": node_id, **connection_payload}
            )
            connection = response.get("connection", {}) if isinstance(response, dict) else {}
        connection_id = str(connection.get("id", ""))
        if not connection_id:
            raise RuntimeError("OmniRoute 未返回工作流 Connection ID")

        existing_models = self.items(
            self.request(
                "GET",
                f"/api/provider-models?provider={urllib.parse.quote(node_id, safe='')}",
            ),
            "models",
        )
        existing_model_ids = {
            str(item.get("modelId") or item.get("id") or "") for item in existing_models
        }
        for route_id in sorted(routes):
            model_payload = {
                "provider": node_id,
                "modelId": route_id,
                "modelName": route_id,
                "source": WORKFLOW_GATEWAY_MANAGED_DESCRIPTION,
                "apiFormat": "chat-completions",
                "supportedEndpoints": ["chat"],
                "targetFormat": "openai",
            }
            self.request(
                "PUT" if route_id in existing_model_ids else "POST",
                "/api/provider-models",
                model_payload,
            )

        published: list[dict[str, str]] = []
        for route_id in sorted(routes):
            combo_name = f"llmctl-workflow-{route_id}"
            existing_combo = existing_combos.get(combo_name)
            combo_payload = {
                "name": combo_name,
                "description": WORKFLOW_GATEWAY_MANAGED_DESCRIPTION,
                "models": [
                    {
                        "kind": "model",
                        "provider": node_id,
                        "model": route_id,
                        "connectionId": connection_id,
                        "label": WORKFLOW_GATEWAY_CONNECTION_NAME,
                    }
                ],
                "strategy": "round-robin",
                "config": {
                    "disableSessionStickiness": True,
                    "stickyRoundRobinLimit": 1,
                    "healthCheckEnabled": True,
                    "maxRetries": 1,
                    "failoverBeforeRetry": True,
                },
            }
            if existing_combo:
                combo_id = str(existing_combo.get("id", ""))
                self.request(
                    "PUT",
                    f"/api/combos/{urllib.parse.quote(combo_id, safe='')}",
                    combo_payload,
                )
            else:
                self.request("POST", "/api/combos", combo_payload)
            published.append({"route_model": route_id, "combo": combo_name})
        return {
            "ok": True,
            "gateway_base_url": raw_base_url,
            "provider_node": node_id,
            "connection": connection_id,
            "published": published,
            "next_step": "在模型、映射与定价中把所需公开模型 ID 指向生成的工作流路由组合",
        }

    def combo_builder_options(self) -> dict[str, Any]:
        response = self.request("GET", "/api/combos/builder/options")
        return response if isinstance(response, dict) else {}

    def alias_metadata(self, alias: str) -> dict[str, Any]:
        response = self.request(
            "GET", f"/api/models/alias?{urllib.parse.urlencode({'alias': alias})}"
        )
        return response if isinstance(response, dict) else {}

    def set_context_window_override(
        self, provider: str, model_id: str, value: int
    ) -> None:
        self.request(
            "PUT",
            "/api/provider-models",
            {
                "provider": provider,
                "modelId": model_id,
                "contextWindowOverride": value,
            },
        )

    def set_max_output_override(self, provider: str, model_id: str, value: int) -> None:
        self.request(
            "PATCH",
            "/api/model-capability-overrides",
            {"target": f"{provider}/{model_id}", "key": "max_token", "value": value},
        )

    def free_models(self) -> list[dict[str, Any]]:
        return self.items(self.request("GET", "/api/free-models"), "models")

    def hidden_provider_models(self, provider: str) -> set[str]:
        response = self.request(
            "GET",
            f"/api/provider-models?{urllib.parse.urlencode({'provider': provider})}",
        )
        if not isinstance(response, dict):
            raise RuntimeError("OmniRoute returned invalid provider model visibility")
        hidden: set[str] = set()
        for key in ("models", "modelCompatOverrides"):
            values = response.get(key, [])
            if not isinstance(values, list):
                continue
            for item in values:
                if not isinstance(item, dict) or not (
                    item.get("isHidden") is True or item.get("isDeleted") is True
                ):
                    continue
                model_id = str(item.get("id", "")).strip()
                if not model_id:
                    continue
                hidden.add(model_id)
                prefix = f"{provider}/"
                if model_id.startswith(prefix):
                    hidden.add(model_id[len(prefix) :])
        return hidden

    def free_rankings(self, available_only: bool = False) -> list[dict[str, Any]]:
        available = "&availableOnly=1" if available_only else ""
        return self.items(
            self.request(
                "GET",
                f"/api/free-provider-rankings?configuredOnly=1{available}&limit=100",
            ),
            "rankings",
        )

    def call_logs(self, key_id: str, limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({"apiKey": key_id, "limit": limit, "offset": offset})
        return self.items(self.request("GET", f"/api/usage/call-logs?{query}"), "logs")

    def call_log(self, request_id: str) -> dict[str, Any]:
        response = self.request(
            "GET", f"/api/usage/call-logs/{urllib.parse.quote(request_id, safe='')}"
        )
        if not isinstance(response, dict):
            raise RuntimeError("OmniRoute returned an invalid call-log detail")
        return response

    def patch_key_permissions(
        self,
        key_id: str,
        allowed_models: list[str],
        allowed_combos: list[str],
        active: bool,
        max_sessions: int = 0,
        requests_per_minute: int = 0,
        requests_per_day: int = 0,
    ) -> None:
        self.request(
            "PATCH",
            f"/api/keys/{urllib.parse.quote(key_id, safe='')}",
            {
                "allowedModels": allowed_models or ["__llmctl_no_models__"],
                "allowedCombos": allowed_combos or ["__llmctl_no_combos__"],
                "isActive": active,
                "noLog": False,
                "maxSessions": max_sessions,
                "rateLimits": request_rate_limits(
                    requests_per_minute, requests_per_day
                ),
            },
        )

    def set_combo_mapping(
        self, pattern: str, combo_id: str, mapping_id: str = "", enabled: bool = True
    ) -> str:
        payload = {
            "pattern": pattern,
            "comboId": combo_id,
            "priority": 100,
            "enabled": enabled,
            "description": "Managed by LLMCtl account portal",
        }
        if mapping_id:
            response = self.request(
                "PUT", f"/api/model-combo-mappings/{urllib.parse.quote(mapping_id, safe='')}", payload
            )
        else:
            response = self.request("POST", "/api/model-combo-mappings", payload)
        mapping = response.get("mapping", {}) if isinstance(response, dict) else {}
        result = str(mapping.get("id", mapping_id)) if isinstance(mapping, dict) else mapping_id
        if not result:
            raise RuntimeError("OmniRoute did not return the model-combo mapping id")
        return result

    def delete_combo_mapping(self, mapping_id: str) -> None:
        self.request(
            "DELETE", f"/api/model-combo-mappings/{urllib.parse.quote(mapping_id, safe='')}"
        )

    def set_model_alias(self, public_id: str, source_model: str) -> str:
        self.request("PUT", "/api/models/alias", {"model": source_model, "alias": public_id})
        return public_id

    def delete_model_alias(self, public_id: str) -> None:
        self.request(
            "DELETE", f"/api/models/alias?{urllib.parse.urlencode({'alias': public_id})}"
        )

    def test_model(self, model_id: str) -> tuple[int, str]:
        payload = {
            "model": model_id,
            "stream": False,
            "max_tokens": 32,
            "temperature": 0,
            # 只有思考内容的输出在 OpenAI 兼容网关看来可能为空。健康检查需要
            # 简短最终答案而非推理轨迹，因此显式使用两种受支持的关闭思考控制。
            "reasoning_effort": "none",
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "user", "content": "Reply with exactly OK"}],
        }
        started = time.monotonic()
        data = json.dumps(payload, separators=(",", ":")).encode()
        request = urllib.request.Request(
            f"{self.base_url}/v1/chat/completions",
            data=data,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.key}",
            },
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=60) as response:
                raw = response.read().decode(errors="replace")
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")[:500]
            raise RuntimeError(f"model test HTTP {error.code}: {detail}") from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"model test failed: {error.reason}") from error
        content = ""
        try:
            parsed = json.loads(raw)
            content = str(parsed.get("choices", [{}])[0].get("message", {}).get("content", ""))
        except (json.JSONDecodeError, IndexError, AttributeError):
            for line in raw.splitlines():
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                with contextlib.suppress(json.JSONDecodeError, IndexError, AttributeError):
                    event = json.loads(line[6:])
                    content += str(event.get("choices", [{}])[0].get("delta", {}).get("content", ""))
        if not content.strip():
            raise RuntimeError("model test returned no assistant content")
        return int((time.monotonic() - started) * 1000), content.strip()[:200]

    def test_provider_model(self, provider: str, model_id: str) -> tuple[int, str]:
        """Run the same provider-aware probe used by OmniRoute's native UI."""
        response = self.request(
            "POST",
            "/api/models/test",
            {"providerId": provider, "modelId": model_id},
        )
        if not isinstance(response, dict):
            raise RuntimeError("OmniRoute returned an invalid model-test response")
        if response.get("status") != "ok":
            detail = str(response.get("error", "Unknown model-test error")).strip()
            raise RuntimeError(detail or "Unknown model-test error")
        try:
            latency = max(0, int(response.get("latencyMs", 0) or 0))
        except (TypeError, ValueError):
            latency = 0
        content = str(response.get("responseText", "")).strip()
        return latency, content[:200] or "OK"


def effective_mail_config(config: Config, settings: dict[str, str]) -> Config:
    try:
        smtp_port = int(settings.get("smtp_port", str(config.smtp_port)))
    except ValueError:
        smtp_port = config.smtp_port
    public_url, api_public_url = effective_public_urls(config, settings)
    return dataclasses.replace(
        config,
        public_url=public_url,
        api_public_url=api_public_url,
        smtp_host=settings.get("smtp_host", config.smtp_host),
        smtp_port=smtp_port,
        smtp_security=settings.get("smtp_security", config.smtp_security),
        smtp_username=settings.get("smtp_username", config.smtp_username),
        smtp_password=settings.get("smtp_password", config.smtp_password),
        smtp_from=settings.get("smtp_from", config.smtp_from),
    )


def send_verification_email(config: Config, recipient: str, raw_token: str) -> None:
    if not config.smtp_host or not config.smtp_from:
        raise RuntimeError("SMTP is not configured")
    verify_url = f"{config.public_url}/#/verify?token={urllib.parse.quote(raw_token)}"
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


def send_test_email(config: Config, recipient: str) -> None:
    recipient, _ = normalize_email(recipient)
    if not config.smtp_host or not config.smtp_from:
        raise RuntimeError("SMTP is not configured")
    message = EmailMessage()
    message["Subject"] = "LLMCtl SMTP test / 邮件服务测试"
    message["From"] = config.smtp_from
    message["To"] = recipient
    message.set_content("LLMCtl SMTP configuration works.\nLLMCtl 邮件配置测试成功。\n")
    context = ssl.create_default_context()
    client: smtplib.SMTP
    if config.smtp_security == "ssl":
        client = smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, timeout=20, context=context)
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
            f'<span class="muted">{html.escape(user_identity(user))}</span> '
            '<form class="inline" method="post" action="/logout"><input type="hidden" name="csrf" value="__CSRF__"><button class="secondary">退出 / Sign out</button></form>'
        )
    else:
        auth = '<a href="/login">登录 / Sign in</a>'
    return f"""<!doctype html><html lang="{lang}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)} · LLMCtl</title><style>{STYLE}</style></head><body><main class="shell"><nav class="nav"><div><a class="brand" href="/">LLMCtl Model Service Portal</a><div class="sub">Models, API keys, quotas, and usage</div></div><div class="row">{auth}</div></nav>{body}</main><script>function cp(id){{navigator.clipboard.writeText(document.getElementById(id).innerText).then(()=>{{const b=document.querySelector('[data-copy="'+id+'"]');if(b)b.innerText='已复制 / Copied'}})}}document.querySelectorAll('[data-copy]').forEach(b=>b.onclick=()=>cp(b.dataset.copy));</script></body></html>"""


class SystemMonitor:
    """Low-overhead, read-only host telemetry for authenticated administrators.

    Sampling is request-driven and shared across browser sessions.  A short
    cache prevents multiple open tabs from multiplying /proc scans or
    ``nvidia-smi`` processes.  No caller-controlled value reaches a command
    invocation.
    """

    def __init__(
        self,
        proc_root: pathlib.Path | str = "/proc",
        *,
        cache_seconds: float = SYSTEM_MONITOR_CACHE_SECONDS,
        monotonic=time.monotonic,
        command_runner=None,
    ) -> None:
        self.proc_root = pathlib.Path(proc_root)
        self.cache_seconds = max(0.0, float(cache_seconds))
        self.monotonic = monotonic
        self.command_runner = command_runner
        self.lock = threading.Lock()
        self.cached: dict[str, Any] | None = None
        self.cached_at = 0.0
        self.previous_cpu: tuple[int, int] | None = None
        self.previous_network: tuple[float, dict[str, tuple[int, int]]] | None = None
        self.previous_processes: dict[tuple[int, int], int] = {}
        self.user_names: dict[int, str] = {}
        try:
            self.clock_ticks = int(os.sysconf("SC_CLK_TCK"))
        except (AttributeError, OSError, ValueError):
            self.clock_ticks = 100
        try:
            self.page_size = int(os.sysconf("SC_PAGE_SIZE"))
        except (AttributeError, OSError, ValueError):
            self.page_size = 4096

    @staticmethod
    def _number(value: str, *, integer: bool = False) -> int | float | None:
        value = value.strip()
        if not value or value.lower() in {"n/a", "[n/a]", "not supported"}:
            return None
        try:
            return int(float(value)) if integer else float(value)
        except ValueError:
            return None

    @staticmethod
    def _percent(used: int | float, total: int | float) -> float | None:
        if total <= 0:
            return None
        return round(max(0.0, min(100.0, float(used) * 100.0 / float(total))), 1)

    @staticmethod
    def _decode_mount(value: str) -> str:
        return re.sub(
            r"\\([0-7]{3})",
            lambda match: chr(int(match.group(1), 8)),
            value,
        )

    @staticmethod
    def redact_command_line(arguments: list[str]) -> str:
        """Return a useful command summary without exposing common secrets."""
        redacted: list[str] = []
        redact_next = False
        for argument in arguments:
            argument = SYSTEM_MONITOR_CREDENTIAL_URL.sub(r"\1<redacted>\2", argument)
            if redact_next:
                redacted.append("<redacted>")
                redact_next = False
                continue
            lowered = argument.lower()
            if lowered == "bearer":
                redacted.append(argument)
                redact_next = True
                continue
            if re.match(r"(?i)^sk-[a-z0-9_-]{12,}$", argument):
                redacted.append("<redacted>")
                continue
            if SYSTEM_MONITOR_SECRET_ARGUMENT.search(argument):
                if "=" in argument:
                    name, _value = argument.split("=", 1)
                    redacted.append(f"{name}=<redacted>")
                elif ":" in argument and not argument.startswith(("http://", "https://")):
                    name, _value = argument.split(":", 1)
                    redacted.append(f"{name}:<redacted>")
                else:
                    redacted.append(argument)
                    redact_next = True
                continue
            redacted.append(argument)
        result = shlex.join(redacted)
        return result[:SYSTEM_MONITOR_COMMAND_LIMIT]

    def _read_text(self, relative: str) -> str:
        return (self.proc_root / relative).read_text(errors="replace")

    def _cpu(self) -> tuple[dict[str, Any], int]:
        cpu_line = next(
            line for line in self._read_text("stat").splitlines() if line.startswith("cpu ")
        )
        counters = [int(value) for value in cpu_line.split()[1:]]
        total = sum(counters)
        idle = counters[3] + (counters[4] if len(counters) > 4 else 0)
        percent = None
        total_delta = 0
        if self.previous_cpu:
            previous_total, previous_idle = self.previous_cpu
            total_delta = max(0, total - previous_total)
            idle_delta = max(0, idle - previous_idle)
            if total_delta:
                percent = round(
                    max(0.0, min(100.0, (total_delta - idle_delta) * 100.0 / total_delta)),
                    1,
                )
        self.previous_cpu = (total, idle)
        return {
            "usage_percent": percent,
            "logical_cpus": os.cpu_count() or 1,
        }, total_delta

    def _memory(self) -> dict[str, Any]:
        values: dict[str, int] = {}
        for line in self._read_text("meminfo").splitlines():
            if ":" not in line:
                continue
            name, raw = line.split(":", 1)
            parts = raw.split()
            if parts:
                values[name] = int(parts[0]) * 1024
        total = values.get("MemTotal", 0)
        available = values.get("MemAvailable", values.get("MemFree", 0))
        used = max(0, total - available)
        swap_total = values.get("SwapTotal", 0)
        swap_free = values.get("SwapFree", 0)
        return {
            "total_bytes": total,
            "available_bytes": available,
            "used_bytes": used,
            "used_percent": self._percent(used, total),
            "cached_bytes": values.get("Cached", 0) + values.get("SReclaimable", 0),
            "buffers_bytes": values.get("Buffers", 0),
            "swap_total_bytes": swap_total,
            "swap_used_bytes": max(0, swap_total - swap_free),
            "swap_used_percent": self._percent(max(0, swap_total - swap_free), swap_total),
        }

    def _network(self, sampled_at: float) -> dict[str, Any]:
        counters: dict[str, tuple[int, int]] = {}
        for line in self._read_text("net/dev").splitlines()[2:]:
            if ":" not in line:
                continue
            interface, raw = line.split(":", 1)
            fields = raw.split()
            if len(fields) >= 9:
                counters[interface.strip()] = (int(fields[0]), int(fields[8]))
        previous_at = sampled_at
        previous: dict[str, tuple[int, int]] = {}
        if self.previous_network:
            previous_at, previous = self.previous_network
        elapsed = sampled_at - previous_at
        interfaces: list[dict[str, Any]] = []
        for name, (received, transmitted) in sorted(counters.items()):
            before = previous.get(name)
            rx_rate = tx_rate = None
            if before and elapsed > 0:
                rx_rate = max(0.0, (received - before[0]) / elapsed)
                tx_rate = max(0.0, (transmitted - before[1]) / elapsed)
            interfaces.append(
                {
                    "name": name,
                    "rx_bytes": received,
                    "tx_bytes": transmitted,
                    "rx_bytes_per_second": round(rx_rate, 1) if rx_rate is not None else None,
                    "tx_bytes_per_second": round(tx_rate, 1) if tx_rate is not None else None,
                    "loopback": name == "lo",
                }
            )
        self.previous_network = (sampled_at, counters)
        external = [item for item in interfaces if not item["loopback"]]
        return {
            "interfaces": interfaces,
            "rx_bytes_per_second": (
                round(sum(item["rx_bytes_per_second"] or 0 for item in external), 1)
                if elapsed > 0
                else None
            ),
            "tx_bytes_per_second": (
                round(sum(item["tx_bytes_per_second"] or 0 for item in external), 1)
                if elapsed > 0
                else None
            ),
        }

    def _user_name(self, uid: int) -> str:
        if uid not in self.user_names:
            try:
                self.user_names[uid] = pwd.getpwuid(uid).pw_name
            except KeyError:
                self.user_names[uid] = str(uid)
        return self.user_names[uid]

    def _processes(
        self, total_cpu_delta: int, memory_total: int
    ) -> tuple[dict[str, int], list[dict[str, Any]]]:
        records: list[dict[str, Any]] = []
        current_ticks: dict[tuple[int, int], int] = {}
        states: dict[str, int] = {}
        logical_cpus = os.cpu_count() or 1
        for entry in self.proc_root.iterdir():
            if not entry.name.isdecimal():
                continue
            try:
                raw = (entry / "stat").read_text(errors="replace")
                right = raw.rfind(")")
                left = raw.find("(")
                if left < 0 or right <= left:
                    continue
                fields = raw[right + 2 :].split()
                if len(fields) < 22:
                    continue
                pid = int(entry.name)
                command = raw[left + 1 : right]
                state = fields[0]
                ticks = int(fields[11]) + int(fields[12])
                start_time = int(fields[19])
                rss_bytes = max(0, int(fields[21])) * self.page_size
            except (FileNotFoundError, PermissionError, OSError, ValueError):
                continue
            key = (pid, start_time)
            current_ticks[key] = ticks
            previous_ticks = self.previous_processes.get(key, ticks)
            cpu_percent = 0.0
            if total_cpu_delta > 0:
                cpu_percent = max(
                    0.0,
                    (ticks - previous_ticks) * logical_cpus * 100.0 / total_cpu_delta,
                )
            states[state] = states.get(state, 0) + 1
            records.append(
                {
                    "pid": pid,
                    "command": command,
                    "state": state,
                    "cpu_percent": round(cpu_percent, 1),
                    "memory_percent": round(rss_bytes * 100.0 / memory_total, 2)
                    if memory_total
                    else 0.0,
                    "rss_bytes": rss_bytes,
                    "proc_path": entry,
                }
            )
        self.previous_processes = current_ticks
        records.sort(
            key=lambda item: (item["cpu_percent"], item["rss_bytes"]), reverse=True
        )
        result: list[dict[str, Any]] = []
        for record in records[:SYSTEM_MONITOR_PROCESS_LIMIT]:
            entry = record.pop("proc_path")
            try:
                record["user"] = self._user_name(entry.stat().st_uid)
            except (FileNotFoundError, PermissionError, OSError):
                record["user"] = "?"
            try:
                arguments = [
                    value.decode(errors="replace")
                    for value in (entry / "cmdline").read_bytes().split(b"\0")
                    if value
                ]
            except (FileNotFoundError, PermissionError, OSError):
                arguments = []
            record["command_line"] = (
                self.redact_command_line(arguments) if arguments else record["command"]
            )
            result.append(record)
        summary = {
            "total": len(records),
            "running": states.get("R", 0),
            "sleeping": states.get("S", 0),
            "uninterruptible": states.get("D", 0),
            "zombie": states.get("Z", 0),
            "returned": len(result),
        }
        return summary, result

    def _disks(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[tuple[str, int]] = set()
        for line in self._read_text("self/mountinfo").splitlines():
            before, separator, after = line.partition(" - ")
            if not separator:
                continue
            fields, trailing = before.split(), after.split()
            if len(fields) < 6 or len(trailing) < 2:
                continue
            mount_point = self._decode_mount(fields[4])
            filesystem, source = trailing[0], self._decode_mount(trailing[1])
            if filesystem not in SYSTEM_MONITOR_LOCAL_FILESYSTEMS:
                continue
            try:
                stats = os.statvfs(mount_point)
            except (FileNotFoundError, PermissionError, OSError):
                continue
            total = stats.f_blocks * stats.f_frsize
            available = stats.f_bavail * stats.f_frsize
            used = max(0, (stats.f_blocks - stats.f_bfree) * stats.f_frsize)
            identity = (source, total)
            if identity in seen or total <= 0:
                continue
            seen.add(identity)
            rows.append(
                {
                    "source": source,
                    "mount_point": mount_point,
                    "filesystem": filesystem,
                    "total_bytes": total,
                    "used_bytes": used,
                    "available_bytes": available,
                    "used_percent": self._percent(used, used + available),
                }
            )
        rows.sort(key=lambda item: (item["mount_point"] != "/", item["mount_point"]))
        return rows

    def _run_command(self, arguments: list[str]) -> str:
        if self.command_runner is not None:
            result = self.command_runner(list(arguments))
            return result.stdout if hasattr(result, "stdout") else str(result)
        completed = subprocess.run(
            arguments,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=2,
            env={**os.environ, "LC_ALL": "C"},
        )
        return completed.stdout

    def _gpus(self) -> dict[str, Any]:
        executable = shutil.which("nvidia-smi")
        if self.command_runner is not None and not executable:
            executable = "nvidia-smi"
        if not executable:
            return {"available": False, "error": "nvidia-smi 未安装或不在 PATH 中", "gpus": []}
        query = (
            "index,name,uuid,pci.bus_id,driver_version,temperature.gpu,"
            "utilization.gpu,utilization.memory,memory.used,memory.total,"
            "power.draw,power.limit,pstate"
        )
        try:
            raw = self._run_command(
                [executable, f"--query-gpu={query}", "--format=csv,noheader,nounits"]
            )
        except (OSError, subprocess.SubprocessError) as error:
            return {"available": False, "error": f"nvidia-smi 读取失败：{error}", "gpus": []}
        gpu_rows: list[dict[str, Any]] = []
        by_uuid: dict[str, dict[str, Any]] = {}
        for fields in csv.reader(raw.splitlines()):
            if len(fields) < 13:
                continue
            index = self._number(fields[0], integer=True)
            memory_used = self._number(fields[8])
            memory_total = self._number(fields[9])
            item = {
                "index": index,
                "name": fields[1].strip(),
                "pci_bus_id": fields[3].strip(),
                "driver_version": fields[4].strip(),
                "temperature_c": self._number(fields[5]),
                "utilization_percent": self._number(fields[6]),
                "memory_utilization_percent": self._number(fields[7]),
                "memory_used_bytes": int(memory_used * 1024 * 1024) if memory_used is not None else None,
                "memory_total_bytes": int(memory_total * 1024 * 1024) if memory_total is not None else None,
                "power_watts": self._number(fields[10]),
                "power_limit_watts": self._number(fields[11]),
                "pstate": fields[12].strip(),
                "process_count": 0,
                "process_memory_bytes": 0,
            }
            gpu_rows.append(item)
            by_uuid[fields[2].strip()] = item
        process_error = ""
        try:
            processes = self._run_command(
                [
                    executable,
                    "--query-compute-apps=gpu_uuid,pid,process_name,used_gpu_memory",
                    "--format=csv,noheader,nounits",
                ]
            )
            for fields in csv.reader(processes.splitlines()):
                if len(fields) < 4 or fields[0].strip() not in by_uuid:
                    continue
                item = by_uuid[fields[0].strip()]
                used = self._number(fields[3])
                item["process_count"] += 1
                if used is not None:
                    item["process_memory_bytes"] += int(used * 1024 * 1024)
        except (OSError, subprocess.SubprocessError) as error:
            process_error = str(error)
        return {
            "available": bool(gpu_rows),
            "error": "" if gpu_rows else "nvidia-smi 未返回 GPU",
            "process_error": process_error,
            "gpus": sorted(gpu_rows, key=lambda item: item["index"] if item["index"] is not None else 9999),
        }

    def _cpu_model(self) -> str:
        try:
            for line in self._read_text("cpuinfo").splitlines():
                if line.lower().startswith(("model name", "hardware")) and ":" in line:
                    return line.split(":", 1)[1].strip()
        except (FileNotFoundError, PermissionError, OSError):
            pass
        return platform.processor() or "unknown"

    def _collect(self, sampled_at: float) -> dict[str, Any]:
        errors: dict[str, str] = {}
        uptime_seconds = 0.0
        load = [0.0, 0.0, 0.0]
        try:
            uptime_seconds = float(self._read_text("uptime").split()[0])
            load = [float(value) for value in self._read_text("loadavg").split()[:3]]
        except (FileNotFoundError, PermissionError, OSError, ValueError) as error:
            errors["system"] = str(error)
        try:
            cpu, total_cpu_delta = self._cpu()
        except (FileNotFoundError, PermissionError, OSError, StopIteration, ValueError) as error:
            cpu, total_cpu_delta = {"usage_percent": None, "logical_cpus": os.cpu_count() or 1}, 0
            errors["cpu"] = str(error)
        try:
            memory = self._memory()
        except (FileNotFoundError, PermissionError, OSError, ValueError) as error:
            memory = {"total_bytes": 0, "used_bytes": 0, "used_percent": None}
            errors["memory"] = str(error)
        try:
            network = self._network(sampled_at)
        except (FileNotFoundError, PermissionError, OSError, ValueError) as error:
            network = {"interfaces": [], "rx_bytes_per_second": None, "tx_bytes_per_second": None}
            errors["network"] = str(error)
        try:
            process_summary, processes = self._processes(
                total_cpu_delta, int(memory.get("total_bytes") or 0)
            )
        except (FileNotFoundError, PermissionError, OSError, ValueError) as error:
            process_summary, processes = {"total": 0, "returned": 0}, []
            errors["processes"] = str(error)
        try:
            disks = self._disks()
        except (FileNotFoundError, PermissionError, OSError, ValueError) as error:
            disks = []
            errors["disks"] = str(error)
        gpus = self._gpus()
        if gpus.get("error"):
            errors["gpu"] = str(gpus["error"])
        return {
            "sampled_at": int(time.time()),
            "sample_interval_seconds": 2,
            "host": {
                "hostname": socket.gethostname(),
                "kernel": platform.release(),
                "cpu_model": self._cpu_model(),
                "logical_cpus": os.cpu_count() or 1,
                "uptime_seconds": round(uptime_seconds, 1),
                "booted_at": int(time.time() - uptime_seconds) if uptime_seconds else None,
                "load_average": load,
            },
            "cpu": cpu,
            "memory": memory,
            "network": network,
            "disks": disks,
            "gpu": gpus,
            "process_summary": process_summary,
            "processes": processes,
            "errors": errors,
        }

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            sampled_at = self.monotonic()
            if self.cached is not None and sampled_at - self.cached_at < self.cache_seconds:
                return self.cached
            self.cached = self._collect(sampled_at)
            self.cached_at = sampled_at
            return self.cached


class PortalHandler(http.server.BaseHTTPRequestHandler):
    server_version = "LLMCtlAccountPortal/2"

    @property
    def app(self) -> "PortalServer":
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        # 永不输出查询字符串，验证码不得进入 journal。
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

    def secure_cookie_suffix(self) -> str:
        """Apply the installation-time cookie policy only.

        ``published_origin`` is link metadata for mail, curl examples and the UI.
        It must never change how an existing LAN or reverse-proxy session works.
        Deployments that require Secure cookies can opt in explicitly with
        ``ACCOUNT_COOKIE_SECURE`` during installation/configuration.
        """
        return "; Secure" if self.app.config.cookie_secure else ""

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
            secure = self.secure_cookie_suffix()
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
        if not self.csrf_token():
            csrf = secrets.token_urlsafe(24)
            secure = self.secure_cookie_suffix()
            self.send_header(
                "Set-Cookie", f"{CSRF_COOKIE}={csrf}; Path=/; SameSite=Lax; Max-Age=604800{secure}"
            )
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def json_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("invalid content length") from error
        if length <= 0 or length > MAX_FORM_BYTES:
            raise ValueError("invalid JSON body size")
        try:
            value = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("invalid JSON body") from error
        if not isinstance(value, dict):
            raise ValueError("JSON body must be an object")
        return value

    def remote_addr(self) -> str:
        forwarded = self.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
        return forwarded if forwarded and self.client_address[0] in {"127.0.0.1", "::1"} else self.client_address[0]

    def api_require(self, admin: bool = False) -> tuple[sqlite3.Row | None, str]:
        user, csrf = self.current_session()
        if not user or (admin and user["role"] != "admin"):
            self.json_response(401 if not user else 403, {"error": "authentication required" if not user else "administrator required"})
            return None, ""
        return user, csrf

    def api_csrf_valid(self, expected: str = "") -> bool:
        supplied = self.headers.get("X-CSRF-Token", "")
        expected = expected or self.csrf_token()
        return bool(supplied and expected and hmac.compare_digest(supplied, expected))

    def serve_vue(self, path: str) -> None:
        relative = path[len("/ui/") :] if path.startswith("/ui/") else ""
        target = self.app.config.static_dir / (relative or "index.html")
        try:
            resolved = target.resolve(strict=True)
            root = self.app.config.static_dir.resolve(strict=True)
            resolved.relative_to(root)
            if not resolved.is_file():
                raise FileNotFoundError
        except (FileNotFoundError, ValueError):
            resolved = self.app.config.static_dir / "index.html"
            if not resolved.is_file():
                self.json_response(503, {"error": "Vue portal assets are not installed"})
                return
        raw = resolved.read_bytes()
        content_type = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; "
            "img-src 'self' data:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'",
        )
        self.send_header("Cache-Control", "no-cache" if resolved.name == "index.html" else "public,max-age=31536000,immutable")
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
        if parsed.path == "/ui":
            self.redirect("/ui/")
            return
        if parsed.path.startswith("/ui/"):
            self.serve_vue(parsed.path)
            return
        if parsed.path.startswith("/portal-api/"):
            self.handle_api_get(parsed.path, parsed.query)
            return
        if parsed.path == "/health":
            try:
                with self.app.db.connect() as connection:
                    connection.execute("SELECT 1").fetchone()
                self.json_response(200, {"status": "ok", "version": APP_VERSION})
            except Exception as error:
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
                    secure = self.secure_cookie_suffix()
                    self.extra_response_cookies = [
                        f"llm_key_once=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0{secure}"
                    ]
                self.show_dashboard(user, raw_key=raw_key)
            return
        self.response(404, page("Not found", '<div class="card"><h1>404</h1></div>'))

    def do_POST(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/portal-api/"):
            try:
                payload = self.json_body()
            except ValueError as error:
                self.json_response(400, {"error": str(error)})
                return
            self.handle_api_post(path, payload)
            return
        try:
            form = self.form()
        except ValueError:
            self.response(400, page("Bad request", '<div class="card error">Invalid request</div>'))
            return
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

    def handle_api_get(self, path: str, query: str = "") -> None:
        if path == "/portal-api/database-migration-progress":
            try:
                result = self.app.database_migration.progress(
                    str(self.headers.get("X-LLMCtl-Migration-Token") or "")
                )
            except PermissionError as error:
                self.json_response(403, {"error": str(error)})
                return
            self.json_response(200, result)
            return
        if path == "/portal-api/public":
            settings = self.app.db.settings()
            portal_url, api_url = effective_public_urls(self.app.config, settings)
            self.json_response(
                200,
                {
                    "version": APP_VERSION,
                    "registration_enabled": settings.get("registration_enabled") == "1",
                    "allowed_domains": normalize_domains(settings.get("allowed_domains", "")),
                    "portal_title": settings.get("portal_title", "LLMCtl"),
                    "published_origin": settings.get("published_origin", ""),
                    "portal_public_url": portal_url,
                    "api_public_url": api_url,
                },
            )
            return
        if path == "/portal-api/session":
            user, _ = self.current_session()
            self.json_response(
                200,
                {
                    "authenticated": bool(user),
                    "user": {
                        "id": user["id"],
                        "email": user["email"] if user["role"] == "user" else "",
                        "login_name": user_identity(user),
                        "role": user["role"],
                    }
                    if user
                    else None,
                },
            )
            return
        if path == "/portal-api/dashboard":
            user, _ = self.api_require()
            if user:
                self.json_response(200, self.app.control.user_dashboard(user["id"]))
            return
        if path == "/portal-api/usage-page":
            user, _ = self.api_require()
            if user:
                try:
                    page, page_size = self.page_parameters(query)
                    filters = self.usage_filter_parameters(query)
                    self.json_response(
                        200,
                        self.app.control.usage_page(
                            owner_user_id=user["id"],
                            model_id=filters["model"],
                            page=page,
                            page_size=page_size,
                        ),
                    )
                except ValueError as error:
                    self.json_response(400, {"error": str(error)})
            return
        if path.startswith("/portal-api/usage/"):
            user, _ = self.api_require()
            if not user:
                return
            request_id = urllib.parse.unquote(path.removeprefix("/portal-api/usage/"))
            try:
                detail = self.app.control.user_request_detail(user["id"], request_id)
            except ValueError as error:
                self.json_response(404, {"error": str(error)})
                return
            except RuntimeError as error:
                self.json_response(502, {"error": str(error)})
                return
            self.app.db.audit(user_identity(user), "usage.detail.view", request_id, "success", self.remote_addr())
            self.json_response(200, detail)
            return
        if path.startswith("/portal-api/admin/usage/"):
            user, _ = self.api_require(admin=True)
            if not user:
                return
            request_id = urllib.parse.unquote(
                path.removeprefix("/portal-api/admin/usage/")
            )
            try:
                detail = self.app.control.admin_request_detail(request_id)
            except ValueError as error:
                self.json_response(404, {"error": str(error)})
                return
            except RuntimeError as error:
                self.json_response(502, {"error": str(error)})
                return
            self.app.db.audit(
                user_identity(user), "admin.usage.detail.view", request_id, "success", self.remote_addr()
            )
            self.json_response(200, detail)
            return
        if path == "/portal-api/admin/usage-page":
            user, _ = self.api_require(admin=True)
            if user:
                try:
                    page, page_size = self.page_parameters(query)
                    filters = self.usage_filter_parameters(query)
                    self.json_response(
                        200,
                        self.app.control.usage_page(
                            filter_user_id=filters["user"],
                            model_id=filters["model"],
                            page=page,
                            page_size=page_size,
                        ),
                    )
                except ValueError as error:
                    self.json_response(400, {"error": str(error)})
            return
        if path == "/portal-api/admin/analytics":
            user, _ = self.api_require(admin=True)
            if user:
                try:
                    values = urllib.parse.parse_qs(query, keep_blank_values=True)
                    self.json_response(
                        200,
                        self.app.control.admin_analytics(
                            range_key=values.get("range", ["today"])[-1].strip(),
                            model_id=values.get("model", [""])[-1].strip(),
                            selected_user_id=values.get("user", [""])[-1].strip(),
                            active_page=int(values.get("active_page", ["1"])[-1]),
                            active_page_size=int(values.get("active_page_size", ["10"])[-1]),
                        ),
                    )
                except (TypeError, ValueError) as error:
                    self.json_response(400, {"error": str(error)})
            return
        if path == "/portal-api/admin/system-monitor":
            user, _ = self.api_require(admin=True)
            if user:
                try:
                    self.json_response(200, self.app.monitor.snapshot())
                except Exception as error:
                    self.json_response(
                        503,
                        {
                            "error": f"系统监控采集失败：{error}",
                            "available": False,
                        },
                    )
            return
        if path == "/portal-api/admin/stress":
            user, _ = self.api_require(admin=True)
            if user:
                try:
                    values = urllib.parse.parse_qs(query, keep_blank_values=True)
                    run_id = values.get("id", [""])[-1].strip()
                    result = (
                        self.app.control.sync_stress_run(run_id)
                        if run_id
                        else {"runs": self.app.control.stress_runs()}
                    )
                    self.json_response(200, result)
                except ValueError as error:
                    self.json_response(404, {"error": str(error)})
                except Exception as error:
                    self.json_response(502, {"error": str(error)})
            return
        if path == "/portal-api/admin/workflow":
            user, _ = self.api_require(admin=True)
            if user:
                try:
                    self.json_response(200, self.app.workflow.config())
                except Exception as error:
                    self.json_response(
                        503,
                        {
                            "error": str(error),
                            "available": False,
                            "setup_command": "llmctl workflow status",
                            "recovery_commands": [
                                "llmctl workflow status",
                                "llmctl workflow init",
                                "llmctl workflow model enable <公开ID>",
                                "llmctl workflow check",
                                "llmctl workflow enable",
                            ],
                        },
                    )
            return
        if path == "/portal-api/admin/model-deployments":
            user, _ = self.api_require(admin=True)
            if user:
                try:
                    self.json_response(200, self.app.models.snapshot())
                except Exception as error:
                    self.json_response(
                        503,
                        {
                            "error": str(error),
                            "available": False,
                            "setup_command": "llmctl model init",
                            "recovery_commands": [
                                "llmctl model init",
                                "llmctl model status",
                                "llmctl logs model",
                            ],
                        },
                    )
            return
        if path == "/portal-api/admin/database":
            user, _ = self.api_require(admin=True)
            if user:
                self.json_response(200, self.app.database_migration.snapshot())
            return
        if path == "/portal-api/admin":
            user, _ = self.api_require(admin=True)
            if user:
                try:
                    self.json_response(200, self.app.control.admin_snapshot())
                except Exception as error:
                    self.json_response(502, {"error": str(error)})
            return
        self.json_response(404, {"error": "not found"})

    @staticmethod
    def page_parameters(query: str) -> tuple[int, int]:
        values = urllib.parse.parse_qs(query, keep_blank_values=True)
        try:
            page = int(values.get("page", ["1"])[-1])
            page_size = int(values.get("page_size", ["20"])[-1])
        except ValueError as error:
            raise ValueError("分页参数必须为整数") from error
        if page < 1 or page_size < 1 or page_size > 100:
            raise ValueError("分页范围无效：page >= 1，page_size 为 1-100")
        return page, page_size

    @staticmethod
    def usage_filter_parameters(query: str) -> dict[str, str]:
        values = urllib.parse.parse_qs(query, keep_blank_values=True)
        result = {
            "user": values.get("user", [""])[-1].strip(),
            "model": values.get("model", [""])[-1].strip(),
        }
        if any(len(value) > 200 for value in result.values()):
            raise ValueError("筛选条件过长")
        return result

    def handle_api_post(self, path: str, payload: dict[str, Any]) -> None:
        if path == "/portal-api/auth/login":
            self.api_login(payload)
            return
        if path == "/portal-api/auth/register":
            self.api_register(payload)
            return
        if path == "/portal-api/auth/verify":
            self.api_verify(payload)
            return
        if path == "/portal-api/auth/logout":
            user, csrf = self.api_require()
            if not user:
                return
            if not self.api_csrf_valid(csrf):
                self.json_response(403, {"error": "CSRF validation failed"})
                return
            morsel = self.cookies().get(SESSION_COOKIE)
            if morsel:
                with self.app.db.connect() as connection:
                    connection.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(morsel.value),))
            secure = self.secure_cookie_suffix()
            self.extra_response_cookies = [f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0{secure}"]
            self.json_response(200, {"ok": True})
            return
        user, csrf = self.api_require(admin=path.startswith("/portal-api/admin/"))
        if not user:
            return
        if not self.api_csrf_valid(csrf):
            self.json_response(403, {"error": "CSRF validation failed"})
            return
        try:
            if path == "/portal-api/key/reveal":
                result = self.api_reveal_key(user)
            elif path == "/portal-api/key/rotate":
                result = self.api_rotate_key(user)
            elif path == "/portal-api/admin/free/discover":
                result = self.app.control.discover_free_resources()
            elif path == "/portal-api/admin/free/test":
                result = self.app.control.test_free_resource(str(payload.get("resource_key", "")))
            elif path == "/portal-api/admin/models/save":
                result = self.app.control.save_model(payload, user_identity(user))
            elif path == "/portal-api/admin/models/inspect":
                result = self.app.control.inspect_model(payload)
            elif path == "/portal-api/admin/models/test":
                result = self.app.control.test_published_model(str(payload.get("model_id", "")))
            elif path == "/portal-api/admin/users/update":
                self.app.control.update_user(payload, user_identity(user))
                result = {"ok": True}
            elif path == "/portal-api/admin/users/bulk-policy":
                result = self.app.control.bulk_update_user_policies(
                    payload, user_identity(user)
                )
            elif path == "/portal-api/admin/groups/save":
                # save_group 自己负责故障关闭的静默/修改/重同步周期；此处重复会
                # 无意义地禁用每个用户 Key 两次。
                result = {"id": self.app.control.save_group(payload)}
            elif path == "/portal-api/admin/permissions/reconcile":
                result = self.app.control.sync_all_users()
            elif path == "/portal-api/usage/reconcile":
                if user["role"] != "user":
                    raise ValueError("only a user account can refresh its own usage")
                result = self.app.control.reconcile_usage(
                    user_id=str(user["id"]), min_interval=10
                )
            elif path == "/portal-api/admin/billing/reconcile":
                result = self.app.control.reconcile_usage()
            elif path == "/portal-api/admin/stress/start":
                result = self.app.control.start_stress_run(payload, user_identity(user))
            elif path == "/portal-api/admin/stress/cancel":
                result = self.app.control.cancel_stress_run(str(payload.get("id", "")))
            elif path == "/portal-api/admin/workflow/config":
                result = self.app.workflow.replace_config(payload)
            elif path == "/portal-api/admin/workflow/publish":
                workflow = self.app.workflow.config()
                result = self.app.omni.sync_workflow_routes(
                    workflow["config"], self.app.workflow.secret
                )
            elif path == "/portal-api/admin/model-deployments/plan":
                result = self.app.models.request("plan", payload)
            elif path == "/portal-api/admin/model-deployments/submit":
                result = self.app.models.request("submit", payload)
            elif path == "/portal-api/admin/model-deployments/job":
                result = self.app.models.request(
                    "job", {"id": str(payload.get("id", ""))}
                )
            elif path == "/portal-api/admin/model-deployments/cancel":
                result = self.app.models.request(
                    "cancel", {"id": str(payload.get("id", ""))}
                )
            elif path == "/portal-api/admin/model-deployments/rollback":
                result = self.app.models.request(
                    "rollback", {"id": str(payload.get("id", ""))}
                )
            elif path == "/portal-api/admin/database/config":
                result = self.app.database_migration.save_config(payload)
            elif path == "/portal-api/admin/database/test":
                result = self.app.database_migration.test(payload)
            elif path == "/portal-api/admin/database/migrate":
                if str(payload.get("confirmation") or "") != "MIGRATE_TO_MYSQL":
                    raise ValueError("请输入 MIGRATE_TO_MYSQL 确认迁移")
                result = self.app.database_migration.start(user_identity(user))
            elif path == "/portal-api/admin/database/rollback":
                result = self.app.database_migration.rollback_to_sqlite(
                    str(payload.get("confirmation") or ""), user_identity(user)
                )
            elif path == "/portal-api/admin/settings":
                result = self.api_update_settings(payload)
            elif path == "/portal-api/admin/smtp/test":
                config = self.smtp_config_from_payload(payload)
                send_test_email(config, str(payload.get("recipient", "")))
                result = {"ok": True}
            else:
                self.json_response(404, {"error": "not found"})
                return
        except Exception as error:
            with contextlib.suppress(Exception):
                self.app.db.audit(user_identity(user), path.removeprefix("/portal-api/"), str(payload.get("id", "")), "failed", self.remote_addr(), str(error))
            status = 409 if isinstance(error, (DatabaseMigrationError, DatabaseCapabilityError)) else 400 if isinstance(error, ValueError) else 502
            self.json_response(status, {"error": str(error)})
            return
        # 审计账本不持久化凭据明文；事件只证明发生展示/轮换，不复制秘密。
        audit_result = (
            {"ok": bool(result.get("ok"))}
            if path in {"/portal-api/key/reveal", "/portal-api/key/rotate"}
            and isinstance(result, dict)
            else result
        )
        audit_status = (
            "partial"
            if path == "/portal-api/admin/users/bulk-policy"
            and isinstance(result, dict)
            and result.get("failed")
            else "success"
        )
        if path != "/portal-api/admin/database/migrate":
            self.app.db.audit(
                user_identity(user),
                path.removeprefix("/portal-api/"),
                str(payload.get("id", "")),
                audit_status,
                self.remote_addr(),
                audit_result,
            )
        self.json_response(200, result)

    def api_login(self, payload: dict[str, Any]) -> None:
        if not self.api_csrf_valid():
            self.json_response(403, {"error": "CSRF validation failed"})
            return
        remote = self.remote_addr()
        supplied_identity = str(payload.get("identity", payload.get("email", "")))
        try:
            normalized_identity = normalize_login_name(supplied_identity)
        except ValueError:
            normalized_identity = ""
        identity = token_hash(f"{remote}|{normalized_identity}")
        with self.app.db.connect() as connection:
            failure = connection.execute("SELECT * FROM login_failures WHERE identity_hash=?", (identity,)).fetchone()
            if failure and failure["locked_until"] > now():
                self.json_response(429, {"error": "too many attempts; try again later"})
                return
            user, matched_identity = find_user_by_login(connection, supplied_identity)
            candidate_hash = user["password_hash"] if user else DUMMY_PASSWORD_HASH
            password_valid = verify_password(str(payload.get("password", "")), candidate_hash)
            valid = bool(user and user["status"] == "active" and password_valid)
            if not valid:
                current = now()
                attempts = 1 if not failure or current - failure["window_started_at"] > 900 else int(failure["attempts"]) + 1
                connection.execute(
                    "INSERT INTO login_failures(identity_hash,attempts,window_started_at,locked_until) VALUES(?,?,?,?) ON CONFLICT(identity_hash) DO UPDATE SET attempts=excluded.attempts,window_started_at=excluded.window_started_at,locked_until=excluded.locked_until",
                    (identity, attempts, current if attempts == 1 else failure["window_started_at"], current + 900 if attempts >= 5 else 0),
                )
            else:
                connection.execute("DELETE FROM login_failures WHERE identity_hash=?", (identity,))
                raw_session, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
                connection.execute("DELETE FROM sessions WHERE user_id=? OR expires_at<=?", (user["id"], now()))
                connection.execute(
                    "INSERT INTO sessions(token_hash,user_id,csrf_token,expires_at,created_at) VALUES(?,?,?,?,?)",
                    (token_hash(raw_session), user["id"], csrf, now() + 7 * 86400, now()),
                )
                connection.execute("UPDATE users SET last_login_at=? WHERE id=?", (now(), user["id"]))
        if not valid:
            self.app.db.audit("anonymous", "login.failed", normalized_identity or "invalid", "denied", remote)
            self.json_response(401, {"error": "invalid credentials or account status"})
            return
        secure = self.secure_cookie_suffix()
        self.extra_response_cookies = [
            f"{SESSION_COOKIE}={raw_session}; Path=/; HttpOnly; SameSite=Lax; Max-Age=604800{secure}",
            f"{CSRF_COOKIE}={csrf}; Path=/; SameSite=Lax; Max-Age=604800{secure}",
        ]
        login_name = user_identity(user)
        self.app.db.audit(login_name, "login.success", user["id"], "success", remote)
        self.json_response(
            200,
            {
                "user": {
                    "id": user["id"],
                    "email": user["email"] if user["role"] == "user" else "",
                    "login_name": login_name,
                    "role": user["role"],
                }
            },
        )

    def api_register(self, payload: dict[str, Any]) -> None:
        if not self.api_csrf_valid():
            self.json_response(403, {"error": "CSRF validation failed"})
            return
        settings = self.app.db.settings()
        if settings.get("registration_enabled") != "1":
            self.json_response(403, {"error": "registration is closed"})
            return
        try:
            email, domain = normalize_email(str(payload.get("email", "")))
            if domain not in normalize_domains(settings.get("allowed_domains", "")):
                raise ValueError("email domain is not allowed")
            password = str(payload.get("password", ""))
            if password != str(payload.get("confirm", "")):
                raise ValueError("passwords do not match")
            password_hash = hash_password(password)
        except ValueError as error:
            self.json_response(400, {"error": str(error)})
            return
        raw_token, stamp = secrets.token_urlsafe(40), now()
        ignored_reason = ""
        default_max_sessions = normalize_max_sessions(
            settings.get("default_max_sessions", "1"), "默认活跃会话上限"
        )
        default_rpm = normalize_request_limit(
            settings.get("default_requests_per_minute", "30"), "默认每分钟请求数"
        )
        default_rpd = normalize_request_limit(
            settings.get("default_requests_per_day", "2000"), "默认每日请求数"
        )
        with self.app.db.connect() as connection:
            user = connection.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            if user and user["status"] != "pending":
                ignored_reason = "duplicate"
            elif user:
                user_id = user["id"]
                latest = connection.execute(
                    "SELECT created_at FROM verification_tokens WHERE user_id=? ORDER BY created_at DESC LIMIT 1",
                    (user_id,),
                ).fetchone()
                if latest and stamp - int(latest["created_at"]) < 60:
                    ignored_reason = "throttled"
            else:
                user_id = str(uuid.uuid4())
            if not ignored_reason and user:
                connection.execute(
                    "UPDATE users SET login_name=?,password_hash=?,max_sessions=?,requests_per_minute=?,requests_per_day=? WHERE id=?",
                    (email, password_hash, default_max_sessions, default_rpm, default_rpd, user_id),
                )
                connection.execute("DELETE FROM verification_tokens WHERE user_id=?", (user_id,))
            elif not ignored_reason:
                connection.execute(
                    "INSERT INTO users(id,email,login_name,password_hash,role,status,quota_tokens,quota_reset,quota_reset_time,max_sessions,requests_per_minute,requests_per_day,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (user_id, email, email, password_hash, "user", "pending", 0, settings["default_quota_reset"], settings["default_quota_reset_time"], default_max_sessions, default_rpm, default_rpd, stamp),
                )
            if not ignored_reason:
                connection.execute(
                    "INSERT INTO verification_tokens(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)",
                    (token_hash(raw_token), user_id, stamp + self.app.config.verification_ttl, stamp),
                )
        if ignored_reason:
            self.app.db.audit(
                "anonymous", f"register.{ignored_reason}", email, "ignored", self.remote_addr()
            )
            self.json_response(
                200, {"ok": True, "message": "If eligible, a verification email was sent"}
            )
            return
        try:
            send_verification_email(effective_mail_config(self.app.config, settings), email, raw_token)
        except Exception as error:
            with self.app.db.connect() as connection:
                connection.execute("DELETE FROM verification_tokens WHERE token_hash=?", (token_hash(raw_token),))
            self.app.db.audit(
                "anonymous", "register.email", email, "failed", self.remote_addr(), type(error).__name__
            )
            self.json_response(502, {"error": f"email delivery failed: {error}"})
            return
        self.app.db.audit("anonymous", "register.email", email, "success", self.remote_addr())
        self.json_response(200, {"ok": True, "message": "Verification email sent"})

    def api_verify(self, payload: dict[str, Any]) -> None:
        if not self.api_csrf_valid():
            self.json_response(403, {"error": "CSRF validation failed"})
            return
        raw_token = str(payload.get("token", ""))
        with self.app.db.connect() as connection:
            record = connection.execute(
                "SELECT v.*,u.* FROM verification_tokens v JOIN users u ON u.id=v.user_id WHERE v.token_hash=? AND v.used_at IS NULL AND v.expires_at>?",
                (token_hash(raw_token), now()),
            ).fetchone()
        if not record:
            self.json_response(410, {"error": "verification link expired"})
            return
        settings = self.app.db.settings()
        key_id = ""
        welcome_source_ref = ""
        try:
            _, domain = normalize_email(record["email"])
            if domain not in normalize_domains(settings.get("allowed_domains", "")):
                raise ValueError("email domain is no longer allowed")
            key_id, raw_key = self.app.omni.create_user_key(
                record["user_id"], record["email"], int(record["max_sessions"])
            )
            stamp = now()
            with self.app.db.connect() as connection:
                changed = connection.execute(
                    "UPDATE verification_tokens SET used_at=? WHERE token_hash=? AND used_at IS NULL",
                    (stamp, token_hash(raw_token)),
                ).rowcount
                if changed != 1:
                    raise RuntimeError("verification token already consumed")
                connection.execute("UPDATE users SET status='active',verified_at=?,api_key_id=?,token_limit_id=NULL WHERE id=?", (stamp, key_id, record["user_id"]))
                connection.execute("INSERT OR IGNORE INTO billing_accounts(user_id,balance_micros,suspended,updated_at) VALUES(?,0,0,?)", (record["user_id"], stamp))
                connection.execute("INSERT OR IGNORE INTO user_group_members(user_id,group_id,created_at) VALUES(?,'default',?)", (record["user_id"], stamp))
                _credited, welcome_source_ref = apply_welcome_credit(
                    connection, record["user_id"], settings, stamp
                )
            self.app.control.sync_user(record["user_id"])
        except Exception as error:
            if key_id:
                with contextlib.suppress(Exception):
                    self.app.omni.delete_key(key_id)
            # 开通跨越门户数据库和 OmniRoute；后续权限同步失败时恢复待注册状态，
            # 避免临时网关错误消耗验证链接或留下绑定已删除 Key 的活动账户。
            with self.app.db.connect() as connection:
                if welcome_source_ref:
                    rollback_source_credit(
                        connection, record["user_id"], welcome_source_ref, now()
                    )
                connection.execute("DELETE FROM permission_sync WHERE user_id=?", (record["user_id"],))
                connection.execute("DELETE FROM user_group_members WHERE user_id=?", (record["user_id"],))
                connection.execute(
                    "DELETE FROM billing_accounts WHERE user_id=? AND NOT EXISTS "
                    "(SELECT 1 FROM balance_transactions WHERE user_id=?) AND NOT EXISTS "
                    "(SELECT 1 FROM usage_ledger WHERE user_id=?)",
                    (record["user_id"], record["user_id"], record["user_id"]),
                )
                connection.execute(
                    "UPDATE users SET status='pending',verified_at=NULL,api_key_id=NULL,token_limit_id=NULL WHERE id=?",
                    (record["user_id"],),
                )
                connection.execute(
                    "UPDATE verification_tokens SET used_at=NULL WHERE token_hash=?",
                    (token_hash(raw_token),),
                )
            self.json_response(502, {"error": f"provisioning failed: {error}"})
            return
        raw_session, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        with self.app.db.connect() as connection:
            connection.execute(
                "INSERT INTO sessions(token_hash,user_id,csrf_token,expires_at,created_at) VALUES(?,?,?,?,?)",
                (token_hash(raw_session), record["user_id"], csrf, now() + 7 * 86400, now()),
            )
        secure = self.secure_cookie_suffix()
        self.extra_response_cookies = [
            f"{SESSION_COOKIE}={raw_session}; Path=/; HttpOnly; SameSite=Lax; Max-Age=604800{secure}",
            f"{CSRF_COOKIE}={csrf}; Path=/; SameSite=Lax; Max-Age=604800{secure}",
        ]
        self.json_response(200, {"ok": True, "api_key": raw_key})

    def api_reveal_key(self, user: sqlite3.Row) -> dict[str, Any]:
        if user["role"] != "user":
            raise ValueError("only user API keys can be revealed here")
        key_id = str(user["api_key_id"] or "")
        if not key_id:
            raise ValueError("账户尚未配置 API Key")
        return {"ok": True, "api_key": self.app.omni.reveal_user_key(key_id)}

    def api_rotate_key(self, user: sqlite3.Row) -> dict[str, Any]:
        if user["role"] != "user":
            raise ValueError("only user API keys can be rotated here")
        old_id = str(user["api_key_id"] or "")
        old_limit_id = str(user["token_limit_id"] or "")
        new_id, raw_key = self.app.omni.create_user_key(
            user["id"], user["email"], int(user["max_sessions"])
        )
        try:
            with self.app.db.connect() as connection:
                connection.execute("UPDATE users SET api_key_id=?,token_limit_id=NULL WHERE id=?", (new_id, user["id"]))
            self.app.control.sync_user(user["id"])
            if old_id:
                self.app.omni.delete_key_and_limit(old_id, old_limit_id)
        except Exception:
            with self.app.db.connect() as connection:
                connection.execute("UPDATE users SET api_key_id=? WHERE id=?", (old_id or None, user["id"]))
            with contextlib.suppress(Exception):
                self.app.omni.delete_key(new_id)
            raise
        return {"ok": True, "api_key": raw_key}

    def smtp_config_from_payload(self, payload: dict[str, Any]) -> Config:
        current = effective_mail_config(self.app.config, self.app.db.settings())
        try:
            smtp_port = int(payload.get("smtp_port", current.smtp_port))
        except (TypeError, ValueError) as error:
            raise ValueError("SMTP 端口必须是 1-65535 之间的整数") from error
        smtp_security = str(payload.get("smtp_security", current.smtp_security)).strip().lower()
        smtp_host = str(payload.get("smtp_host", current.smtp_host)).strip()
        smtp_from = str(payload.get("smtp_from", current.smtp_from)).strip()
        smtp_username = str(payload.get("smtp_username", current.smtp_username)).strip()
        smtp_password = str(payload.get("smtp_password", "")) or current.smtp_password
        if smtp_security not in {"starttls", "ssl", "plain"} or not 1 <= smtp_port <= 65535:
            raise ValueError("SMTP 安全协议或端口无效")
        if not smtp_host or len(smtp_host) > 253 or re.search(r"\s", smtp_host):
            raise ValueError("SMTP 主机无效")
        if not smtp_from:
            raise ValueError("请填写 SMTP 发件人")
        normalize_email(smtp_from)
        return dataclasses.replace(
            current,
            smtp_host=smtp_host,
            smtp_port=smtp_port,
            smtp_security=smtp_security,
            smtp_username=smtp_username,
            smtp_password=smtp_password,
            smtp_from=smtp_from,
        )

    def api_update_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        scope = str(payload.get("scope", "all")).strip().lower()
        if scope not in {"all", "publishing", "registration", "smtp"}:
            raise ValueError("invalid settings scope")
        current = self.app.db.settings()
        if scope == "publishing":
            portal_title = normalize_portal_title(
                payload.get("portal_title", current.get("portal_title", "LLMCtl"))
            )
            published_origin = normalize_public_origin(
                payload.get("published_origin", current.get("published_origin", ""))
            )
            self.app.db.update_settings(
                {"portal_title": portal_title, "published_origin": published_origin}
            )
            portal_url, api_url = effective_public_urls(
                self.app.config,
                current | {"published_origin": published_origin},
            )
            return {
                "ok": True,
                "scope": scope,
                "portal_title": portal_title,
                "published_origin": published_origin,
                "portal_public_url": portal_url,
                "api_public_url": api_url,
            }
        smtp_values: dict[str, str] = {}
        if scope in {"all", "smtp"}:
            smtp = self.smtp_config_from_payload(payload)
            smtp_values = {
                "smtp_host": smtp.smtp_host,
                "smtp_port": str(smtp.smtp_port),
                "smtp_security": smtp.smtp_security,
                "smtp_username": smtp.smtp_username,
                "smtp_password": smtp.smtp_password,
                "smtp_from": smtp.smtp_from,
            }
            if scope == "smtp":
                self.app.db.update_settings(smtp_values)
                return {"ok": True, "scope": scope}

        enabled_value = payload.get(
            "registration_enabled", current.get("registration_enabled", "0")
        )
        enabled = enabled_value is True or str(enabled_value).strip().lower() in {"1", "true", "yes", "on"}
        domains = normalize_domains(str(payload.get("allowed_domains", current.get("allowed_domains", ""))))
        published_origin = normalize_public_origin(
            payload.get("published_origin", current.get("published_origin", ""))
        )
        public_url = str(
            payload.get("public_url", current.get("public_url") or self.app.config.public_url)
        ).rstrip("/")
        api_url = str(
            payload.get("api_public_url", current.get("api_public_url") or self.app.config.api_public_url)
        ).rstrip("/")
        welcome_balance_micros = money_to_micros(
            payload.get(
                "default_welcome_balance",
                current.get("default_welcome_balance", "0"),
            )
        )
        default_max_sessions = normalize_max_sessions(
            payload.get(
                "default_max_sessions", current.get("default_max_sessions", "1")
            ),
            "默认活跃会话上限",
        )
        default_rpm = normalize_request_limit(
            payload.get(
                "default_requests_per_minute",
                current.get("default_requests_per_minute", "30"),
            ),
            "默认每分钟请求数",
        )
        default_rpd = normalize_request_limit(
            payload.get(
                "default_requests_per_day",
                current.get("default_requests_per_day", "2000"),
            ),
            "默认每日请求数",
        )
        currency = str(payload.get("currency", current.get("currency", "USD"))).strip().upper()
        if enabled and not domains:
            raise ValueError("开放注册前请至少配置一个允许的邮箱域名")
        if not 0 <= welcome_balance_micros <= 1_000_000_000_000_000:
            raise ValueError("默认赠送金额必须在 0-1000000000 之间")
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError("货币代码必须是三个大写字母")
        for label, url, allow_ui in (("门户公开 URL", public_url, True), ("API 公开 URL", api_url, False)):
            try:
                parsed = urllib.parse.urlsplit(url)
                parsed.port
            except ValueError as error:
                raise ValueError(f"{label} 无效") from error
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(f"{label} 无效")
            if (allow_ui and parsed.path.rstrip("/") not in {"", "/ui"}) or (not allow_ui and parsed.path.rstrip("/")):
                raise ValueError(f"{label} 路径无效")
        public_url = portal_ui_url(public_url)
        mail = smtp if scope == "all" else effective_mail_config(self.app.config, self.app.db.settings())
        if enabled and (not public_url or not mail.smtp_host or not mail.smtp_from):
            raise ValueError("开放注册前必须配置门户公开 URL、SMTP 主机和发件人")
        self.app.db.update_settings(
            smtp_values
            | {
                "registration_enabled": "1" if enabled else "0",
                "allowed_domains": ",".join(domains),
                "default_welcome_balance": micros_to_money(welcome_balance_micros),
                "default_quota_tokens": "0",
                "default_max_sessions": str(default_max_sessions),
                "default_requests_per_minute": str(default_rpm),
                "default_requests_per_day": str(default_rpd),
                "published_origin": published_origin,
                "public_url": public_url,
                "api_public_url": api_url,
                "currency": currency,
            }
        )
        return {"ok": True, "scope": scope}

    def show_landing(self) -> None:
        settings = self.app.db.settings()
        registration = settings.get("registration_enabled") == "1"
        register = '<a class="button" href="/register">注册 / Register</a>' if registration else '<span class="muted">注册已关闭 / Registration is closed</span>'
        body = f'<section class="card"><h1>LLMCtl 模型服务门户</h1><p class="muted">LLMCtl model service portal</p><p>验证允许的邮箱后获得个人 API Key、预付余额、用量和可调用模型。</p><div class="row"><a class="button" href="/login">登录 / Sign in</a>{register}</div></section>'
        self.response(200, page("Account portal", body))

    def show_login(self, message: str = "", error: bool = False) -> None:
        flash = f'<div class="flash {"error" if error else ""}">{html.escape(message)}</div>' if message else ""
        body = f'''{flash}<section class="card" style="max-width:520px;margin:auto"><h1>登录 / Sign in</h1><form method="post" action="/login"><input type="hidden" name="csrf" value="__CSRF__"><label>登录名或邮箱 / Username or email</label><input name="identity" type="text" autocomplete="username" required><label>密码 / Password</label><input name="password" type="password" autocomplete="current-password" required><p><button>登录 / Sign in</button></p></form><a href="/register">注册新账户 / Create account</a></section>'''
        self.response(200, page("Sign in", body))

    def show_register(self, message: str = "", error: bool = False) -> None:
        settings = self.app.db.settings()
        if settings.get("registration_enabled") != "1":
            self.response(403, page("Registration closed", '<div class="card notice">注册已关闭 / Registration is closed.</div>'))
            return
        domains = "、".join(
            f"@{domain}" for domain in normalize_domains(settings.get("allowed_domains", ""))
        )
        flash = f'<div class="flash {"error" if error else ""}">{html.escape(message)}</div>' if message else ""
        body = f'''{flash}<section class="card" style="max-width:560px;margin:auto"><h1>注册 / Register</h1><p class="muted">允许注册邮箱：{html.escape(domains or "管理员尚未配置")}</p><form method="post" action="/register"><input type="hidden" name="csrf" value="__CSRF__"><label>邮箱 / Email</label><input name="email" type="email" required><label>密码 / Password</label><input name="password" type="password" minlength="8" maxlength="200" required><label>确认密码 / Confirm</label><input name="confirm" type="password" minlength="8" maxlength="200" required><p class="small muted">8-200 个字符，不能为纯数字；收到邮件后请点击其中的验证链接。</p><button>发送验证邮件 / Send verification</button></form></section>'''
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
        body = f'''<section class="card" style="max-width:620px;margin:auto"><h1>确认邮箱 / Confirm email</h1><p>{html.escape(row["email"])}</p><p class="muted">确认后将创建个人 API Key，并一次性入账管理员设置的注册赠款。邮件扫描器访问此页面不会自动开通账户。</p><form method="post" action="/verify"><input type="hidden" name="csrf" value="__CSRF__"><input type="hidden" name="token" value="{html.escape(raw_token)}"><button>确认并创建 API Key / Verify &amp; create key</button></form></section>'''
        self.response(200, page("Verify email", body))

    def show_dashboard(self, user: sqlite3.Row, raw_key: str = "", message: str = "") -> None:
        gateway_error = ""
        models: list[dict[str, Any]] = []
        try:
            models = self.app.omni.models()
        except RuntimeError as error:
            gateway_error = str(error)
        with self.app.db.connect() as connection:
            account = connection.execute(
                "SELECT balance_micros FROM billing_accounts WHERE user_id=?",
                (user["id"],),
            ).fetchone()
        balance = int(account["balance_micros"] or 0) if account else 0
        key_box = ""
        if raw_key:
            key_box = f'''<div class="card wide notice"><h2>请立即复制 API Key / Copy now</h2><p>明文只显示这一次；门户不会保存它。</p><div id="new-key" class="key">{html.escape(raw_key)}</div><p><button data-copy="new-key">复制 / Copy</button></p></div>'''
        model_rows = []
        for index, model in enumerate(models[:500]):
            model_id = str(model.get("id", ""))
            if not model_id:
                continue
            owned = str(model.get("owned_by", model.get("provider", "LLMCtl")))
            capabilities = model.get("capabilities", model.get("input_modalities", []))
            caps = json.dumps(capabilities, ensure_ascii=False) if capabilities else "chat"
            if self.app.config.supports_ocr and model_id == os.environ.get("SERVED_MODEL_NAME", ""):
                caps += ", vision, OCR"
            dom_id = f"model-{index}"
            model_rows.append(f'''<div class="model"><div><code id="{dom_id}">{html.escape(model_id)}</code><div><span class="tag">{html.escape(owned)}</span><span class="tag">{html.escape(caps[:120])}</span></div></div><button class="secondary" data-copy="{dom_id}">复制 ID / Copy</button></div>''')
        sample_model = str(models[0].get("id", "MODEL_ID")) if models else "MODEL_ID"
        sample_payload = json.dumps(
            {
                "model": sample_model,
                "stream": False,
                "messages": [{"role": "user", "content": "你好"}],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        _portal_url, api_public_url = effective_public_urls(
            self.app.config, self.app.db.settings()
        )
        curl = f'''curl {shlex.quote(api_public_url + "/v1/chat/completions")} \\
  -H 'Authorization: Bearer YOUR_API_KEY' \\
  -H 'Accept: application/json' \\
  -H 'Content-Type: application/json' \\
  -d {shlex.quote(sample_payload)}'''
        flash = f'<div class="flash">{html.escape(message)}</div>' if message else ""
        error = '<div class="flash error">AI gateway unavailable; see LLMCtl account logs.</div>' if gateway_error else ""
        body = f'''{flash}{error}<div class="grid">{key_box}<section class="card"><h2>可用余额 / Balance</h2><div class="stat">${html.escape(micros_to_money(balance))}</div><p class="muted">付费调用按模型实际 Token 用量扣款；余额耗尽后停止付费模型权限。</p></section><section class="card"><h2>API 地址 / Endpoint</h2><div id="api-base" class="key">{html.escape(api_public_url)}/v1</div><p><button class="secondary" data-copy="api-base">复制 / Copy</button></p></section><section class="card wide"><h2>调用示例 / curl demo</h2><pre id="curl-demo">{html.escape(curl)}</pre><button class="secondary" data-copy="curl-demo">复制示例 / Copy demo</button></section><section class="card wide"><div class="row"><h2>开放模型 / Available models</h2><span class="spacer"></span><span class="muted">{len(model_rows)} models</span></div><div class="models">{''.join(model_rows) or '<p class="muted">No models are currently available.</p>'}</div></section><section class="card wide"><h2>密钥安全 / Key security</h2><p class="muted">轮换会先创建并验证新 Key，再停用旧 Key。新 Key 仍只显示一次。</p><form method="post" action="/rotate-key"><input type="hidden" name="csrf" value="__CSRF__"><button class="danger">轮换 API Key / Rotate key</button></form></section></div>'''
        self.response(200, page("Dashboard", body, user), user)

    def show_admin(self, message: str = "", error: bool = False) -> None:
        user, _ = self.require_user(admin=True)
        if not user:
            return
        settings = self.app.db.settings()
        portal_public_url, api_public_url = effective_public_urls(
            self.app.config, settings
        )
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
                f'''<form method="post" action="/admin/user"><input type="hidden" name="csrf" value="__CSRF__"><input type="hidden" name="user_id" value="{html.escape(item["id"])}"><label>Balance adjustment (USD)</label><input name="balance_delta" value="0" inputmode="decimal"><label>Adjustment note</label><input name="note" value="Legacy admin adjustment"><label>API Key active sessions (0 = unlimited)</label><input name="max_sessions" type="number" min="0" max="10000" value="{item["max_sessions"]}"><label>Requests per minute (0 = unlimited)</label><input name="requests_per_minute" type="number" min="0" max="10000000" value="{item["requests_per_minute"]}"><label>Requests per day (0 = unlimited)</label><input name="requests_per_day" type="number" min="0" max="10000000" value="{item["requests_per_day"]}"><label>Status</label><select name="status">{status_options}</select><p><button class="secondary">保存 / Save</button></p></form>'''
                if provisioned
                else '<span class="muted">等待邮箱验证 / Pending email verification</span>'
            )
            rows.append(f'''<tr><td>{html.escape(item["email"])}</td><td>{html.escape(item["status"])}</td><td>{controls}</td></tr>''')
        audit_rows = "".join(f'<tr><td>{time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(a["created_at"]))}</td><td>{html.escape(a["actor"])}</td><td>{html.escape(a["action"])}</td><td>{html.escape(a["status"])}</td><td>{html.escape(a["detail"])}</td></tr>' for a in audits)
        checked = "checked" if settings.get("registration_enabled") == "1" else ""
        flash = f'<div class="flash {"error" if error else ""}">{html.escape(message)}</div>' if message else ""
        registration = f'''<section class="card"><h2>注册策略 / Registration</h2><form method="post" action="/admin/settings"><input type="hidden" name="csrf" value="__CSRF__"><label><input style="width:auto" type="checkbox" name="enabled" value="1" {checked}> 允许新用户注册</label><label>允许注册的邮箱后缀（逗号分隔）</label><input name="domains" value="{html.escape(settings.get("allowed_domains", ""))}"><label>默认 API Key 活跃会话上限（0 = 不限制）</label><input name="max_sessions" type="number" min="0" max="10000" value="{html.escape(settings.get("default_max_sessions", "1"))}"><label>默认每分钟请求数（0 = 不限制）</label><input name="requests_per_minute" type="number" min="0" max="10000000" value="{html.escape(settings.get("default_requests_per_minute", "30"))}"><label>默认每日请求数（0 = 不限制）</label><input name="requests_per_day" type="number" min="0" max="10000000" value="{html.escape(settings.get("default_requests_per_day", "2000"))}"><label>新用户一次性赠送金额（USD）</label><input name="welcome_balance" value="{html.escape(settings.get("default_welcome_balance", "0"))}" inputmode="decimal"><p><button>保存策略 / Save</button></p></form><p class="small muted">新用户只获得一次性现金余额；每次调用按模型 Token 单价扣款，余额耗尽后停止模型权限。</p></section>'''
        endpoints = f'''<section class="card"><h2>服务入口</h2><p>API: <a href="{html.escape(api_public_url)}">{html.escape(api_public_url)}</a></p><p>LLMCtl: {html.escape(portal_public_url)}</p><p class="muted">账户策略由 LLMCtl 统一管理并同步到当前 AI 接入层。</p></section>'''
        users_section = f'''<section class="card wide"><h2>用户 / Users</h2><div style="overflow:auto"><table><tr><th>Email</th><th>Status</th><th>Quota / status</th></tr>{''.join(rows) or '<tr><td colspan="3">暂无用户</td></tr>'}</table></div></section>'''
        audit_section = f'''<section class="card wide"><h2>门户审计 / Portal audit</h2><div style="overflow:auto"><table><tr><th>Time</th><th>Actor</th><th>Action</th><th>Status</th><th>Detail</th></tr>{audit_rows}</table></div></section>'''
        body = f'''{flash}<div class="grid">{registration}{endpoints}{users_section}{audit_section}</div>'''
        self.response(200, page("Admin", body, user), user)

    def handle_login(self, form: dict[str, str]) -> None:
        if not self.verify_csrf(form):
            self.response(403, page("Forbidden", '<div class="card error">CSRF validation failed</div>'))
            return
        remote = self.client_address[0]
        supplied_identity = form.get("identity", form.get("email", ""))
        try:
            normalized_identity = normalize_login_name(supplied_identity)
        except ValueError:
            normalized_identity = ""
        identity = token_hash(f"{remote}|{normalized_identity}")
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
            if not locked:
                user, _ = find_user_by_login(connection, supplied_identity)
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
                    audit_action, audit_target = (
                        "login.failed",
                        normalized_identity or "invalid",
                    )
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
                self.show_login("登录名、密码或账户状态无效 / Invalid credentials or account", True)
            return
        self.app.db.audit(user_identity(user), "login.success", user["id"], "success", remote)
        secure = self.secure_cookie_suffix()
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
        secure = self.secure_cookie_suffix()
        self.redirect("/", [f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0{secure}"])

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
        default_max_sessions = normalize_max_sessions(
            settings.get("default_max_sessions", "1"), "默认活跃会话上限"
        )
        default_rpm = normalize_request_limit(
            settings.get("default_requests_per_minute", "30"), "默认每分钟请求数"
        )
        default_rpd = normalize_request_limit(
            settings.get("default_requests_per_day", "2000"), "默认每日请求数"
        )
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
                    connection.execute(
                        "UPDATE users SET login_name=?,password_hash=?,max_sessions=?,requests_per_minute=?,requests_per_day=? WHERE id=?",
                        (email, password_hash, default_max_sessions, default_rpm, default_rpd, user_id),
                    )
                    connection.execute("DELETE FROM verification_tokens WHERE user_id=?", (user_id,))
            else:
                user_id = str(uuid.uuid4())
                connection.execute("INSERT INTO users(id,email,login_name,password_hash,role,status,quota_tokens,quota_reset,quota_reset_time,max_sessions,requests_per_minute,requests_per_day,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (user_id, email, email, password_hash, "user", "pending", 0, settings["default_quota_reset"], settings["default_quota_reset_time"], default_max_sessions, default_rpm, default_rpd, now()))
            if not duplicate_active and not throttled:
                connection.execute("INSERT INTO verification_tokens(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)", (token_hash(raw_token), user_id, now() + self.app.config.verification_ttl, now()))
        if duplicate_active or throttled:
            self.app.db.audit("anonymous", "register.duplicate" if duplicate_active else "register.throttled", email, "ignored", remote)
            self.show_register("若该邮箱可注册，验证邮件已经发送 / If eligible, a verification email was sent")
            return
        try:
            send_verification_email(effective_mail_config(self.app.config, settings), email, raw_token)
        except Exception as error:
            with self.app.db.connect() as connection:
                connection.execute("DELETE FROM verification_tokens WHERE token_hash=?", (token_hash(raw_token),))
            self.app.db.audit("anonymous", "register.email", email, "failed", remote, type(error).__name__)
            self.show_register("验证邮件发送失败，请联系管理员 / Email delivery failed", True)
            return
        self.app.db.audit("anonymous", "register.email", email, "success", remote)
        self.show_register("验证邮件已发送，请点击邮件中的链接完成验证 / Open the link in the email to verify")

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
        key_id = ""
        welcome_source_ref = ""
        try:
            _, domain = normalize_email(record["email"])
            if domain not in normalize_domains(settings.get("allowed_domains", "")):
                raise ValueError("email domain is no longer allowed")
            key_id, raw_key = self.app.omni.create_user_key(
                record["user_id"], record["email"], int(record["max_sessions"])
            )
            stamp = now()
            with self.app.db.connect() as connection:
                changed = connection.execute(
                    "UPDATE verification_tokens SET used_at=? "
                    "WHERE token_hash=? AND used_at IS NULL",
                    (stamp, token_hash(raw_token)),
                ).rowcount
                if changed != 1:
                    raise RuntimeError("verification token was already consumed")
                connection.execute(
                    "UPDATE users SET status='active',verified_at=?,api_key_id=?,"
                    "token_limit_id=NULL WHERE id=?",
                    (stamp, key_id, record["user_id"]),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO billing_accounts"
                    "(user_id,balance_micros,suspended,updated_at) VALUES(?,0,0,?)",
                    (record["user_id"], stamp),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO user_group_members"
                    "(user_id,group_id,created_at) VALUES(?,'default',?)",
                    (record["user_id"], stamp),
                )
                _credited, welcome_source_ref = apply_welcome_credit(
                    connection, record["user_id"], settings, stamp
                )
            self.app.control.sync_user(record["user_id"])
        except Exception as error:
            if key_id:
                with contextlib.suppress(Exception):
                    self.app.omni.delete_key(key_id)
            with self.app.db.connect() as connection:
                if welcome_source_ref:
                    rollback_source_credit(
                        connection, record["user_id"], welcome_source_ref, now()
                    )
                connection.execute(
                    "DELETE FROM permission_sync WHERE user_id=?", (record["user_id"],)
                )
                connection.execute(
                    "DELETE FROM user_group_members WHERE user_id=?", (record["user_id"],)
                )
                connection.execute(
                    "DELETE FROM billing_accounts WHERE user_id=? AND NOT EXISTS "
                    "(SELECT 1 FROM balance_transactions WHERE user_id=?) AND NOT EXISTS "
                    "(SELECT 1 FROM usage_ledger WHERE user_id=?)",
                    (record["user_id"], record["user_id"], record["user_id"]),
                )
                connection.execute(
                    "UPDATE users SET status='pending',verified_at=NULL,api_key_id=NULL,"
                    "token_limit_id=NULL WHERE id=?",
                    (record["user_id"],),
                )
                connection.execute(
                    "UPDATE verification_tokens SET used_at=NULL WHERE token_hash=?",
                    (token_hash(raw_token),),
                )
            self.app.db.audit(record["email"], "verify.provision", record["user_id"], "failed", self.client_address[0], type(error).__name__)
            self.response(503, page("Provisioning failed", '<div class="card error">账户开通失败，未保留半成品 API Key；请重试或联系管理员。<br>Provisioning failed and the partial key was revoked.</div>'))
            return
        self.app.db.audit(record["email"], "verify.provision", record["user_id"], "success", self.client_address[0])
        with self.app.db.connect() as connection:
            user = connection.execute("SELECT * FROM users WHERE id=?", (record["user_id"],)).fetchone()
            raw_session = secrets.token_urlsafe(32)
            csrf = secrets.token_urlsafe(24)
            connection.execute("INSERT INTO sessions(token_hash,user_id,csrf_token,expires_at,created_at) VALUES(?,?,?,?,?)", (token_hash(raw_session), user["id"], csrf, now() + 7 * 86400, now()))
        secure = self.secure_cookie_suffix()
        self.send_response(303)
        self.send_header("Location", "/?provisioned=1")
        # 一次性 Key 只通过短期 HttpOnly Cookie 跨越重定向。
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
            new_id, raw_key = self.app.omni.create_user_key(
                user["id"], user["email"], int(user["max_sessions"])
            )
            try:
                if old_id:
                    self.app.omni.activate_key(old_id, False)
                with self.app.db.connect() as connection:
                    connection.execute("UPDATE users SET api_key_id=?,token_limit_id=NULL WHERE id=?", (new_id, user["id"]))
                self.app.control.sync_user(user["id"])
            except Exception:
                with contextlib.suppress(Exception):
                    self.app.omni.delete_key(new_id)
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
            welcome_balance = money_to_micros(form.get("welcome_balance", "0"))
            default_max_sessions = normalize_max_sessions(
                form.get("max_sessions", "1"), "默认活跃会话上限"
            )
            default_rpm = normalize_request_limit(
                form.get("requests_per_minute", "30"), "默认每分钟请求数"
            )
            default_rpd = normalize_request_limit(
                form.get("requests_per_day", "2000"), "默认每日请求数"
            )
            enabled = form.get("enabled") == "1"
            if (enabled and not domains) or welcome_balance < 0 or welcome_balance > 1_000_000_000_000_000:
                raise ValueError("invalid registration settings")
            if enabled and (
                not self.app.config.public_url
                or not self.app.config.smtp_host
                or not self.app.config.smtp_from
            ):
                raise ValueError(
                    "public portal URL and SMTP must be configured before registration can be enabled"
                )
            values = {"registration_enabled": "1" if enabled else "0", "allowed_domains": ",".join(domains), "default_welcome_balance": micros_to_money(welcome_balance), "default_quota_tokens": "0", "default_max_sessions": str(default_max_sessions), "default_requests_per_minute": str(default_rpm), "default_requests_per_day": str(default_rpd)}
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
            target_status = form.get("status", "")
            max_sessions = normalize_max_sessions(form.get("max_sessions", "0"))
            rpm = normalize_request_limit(
                form.get("requests_per_minute", "0"), "每分钟请求数"
            )
            rpd = normalize_request_limit(
                form.get("requests_per_day", "0"), "每日请求数"
            )
            if target_status not in {"active", "disabled"}:
                raise ValueError("invalid user settings")
            with self.app.db.connect() as connection:
                target = connection.execute("SELECT * FROM users WHERE id=? AND role='user'", (form.get("user_id", ""),)).fetchone()
                group_ids = [
                    str(row["group_id"])
                    for row in connection.execute(
                        "SELECT group_id FROM user_group_members WHERE user_id=?",
                        (form.get("user_id", ""),),
                    )
                ]
            if not target or not target["api_key_id"]:
                raise ValueError("user is not provisioned")
            self.app.control.update_user(
                {
                    "user_id": target["id"],
                    "status": target_status,
                    "max_sessions": max_sessions,
                    "requests_per_minute": rpm,
                    "requests_per_day": rpd,
                    "group_ids": group_ids,
                    "balance_delta": form.get("balance_delta", "0"),
                    "note": form.get("note", "Legacy admin adjustment"),
                },
                str(admin["email"]),
            )
        except Exception as error:
            self.app.db.audit(admin["email"], "user.update", form.get("user_id", ""), "failed", self.client_address[0], type(error).__name__)
            self.show_admin(str(error), True)
            return
        self.app.db.audit(admin["email"], "user.update", target["email"], "success", self.client_address[0], {"status": target_status, "balance_delta": form.get("balance_delta", "0"), "max_sessions": max_sessions, "requests_per_minute": rpm, "requests_per_day": rpd})
        self.show_admin("用户设置已保存 / User settings saved")


class WorkflowClient:
    """Local, authenticated control client for the optional Go data plane."""

    def __init__(self) -> None:
        self.base_url = os.environ.get(
            "LLM_WORKFLOW_CONTROL_URL", "http://127.0.0.1:18100"
        ).strip().rstrip("/")
        self.secret = os.environ.get("LLM_WORKFLOW_SHARED_SECRET", "").strip()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, method: str, path: str, payload: Any = None) -> Any:
        if len(self.secret) < 24:
            raise RuntimeError(
                "工作流尚未初始化；请先运行 llmctl workflow init"
            )
        data = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.secret}",
        }
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
            if len(data) > 2 << 20:
                raise ValueError("工作流配置超过 2 MiB")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=data, headers=headers, method=method
        )
        try:
            with self.opener.open(request, timeout=10) as response:
                raw = response.read(3 << 20)
        except urllib.error.HTTPError as error:
            detail = error.read(2000).decode(errors="replace")
            try:
                parsed = json.loads(detail)
                detail = str(parsed.get("error", {}).get("message", detail))
            except (AttributeError, json.JSONDecodeError):
                pass
            raise RuntimeError(
                f"工作流控制接口 HTTP {error.code}: {detail[:1000]}"
            ) from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"工作流控制接口不可用：{error.reason}") from error
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError as error:
            raise RuntimeError("工作流控制接口返回了无效 JSON") from error

    def config(self) -> dict[str, Any]:
        result = self.request("GET", "/admin/config")
        if not isinstance(result, dict) or not isinstance(result.get("config"), dict):
            raise RuntimeError("工作流控制接口缺少配置")
        return result

    def replace_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        revision = str(payload.get("revision", "")).strip()
        config = payload.get("config")
        if len(revision) != 64 or not isinstance(config, dict):
            raise ValueError("工作流配置或版本号无效，请刷新后重试")
        return self.request(
            "PUT", "/admin/config", {"revision": revision, "config": config}
        )


class ModelDeploymentClient:
    """通过本机 Unix Socket 调用具备 root 权限的模型部署控制服务。"""

    def __init__(self) -> None:
        self.socket_path = pathlib.Path(
            os.environ.get(
                "LLM_MODEL_CONTROL_SOCKET",
                "/run/llm-cluster/model-control.sock",
            )
        )

    def request(
        self, operation: str, payload: dict[str, Any] | None = None
    ) -> Any:
        """发送白名单操作；门户进程不执行 systemctl、Docker 或文件写入。"""

        if operation not in {
            "snapshot",
            "plan",
            "submit",
            "job",
            "cancel",
            "rollback",
        }:
            raise ValueError("不支持的模型部署操作")
        encoded = json.dumps(
            {"operation": operation, "payload": payload or {}},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode() + b"\n"
        if len(encoded) > 2 << 20:
            raise ValueError("模型部署请求超过 2 MiB")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(30)
                client.connect(str(self.socket_path))
                client.sendall(encoded)
                response = b""
                while not response.endswith(b"\n"):
                    chunk = client.recv(65536)
                    if not chunk:
                        break
                    response += chunk
                    if len(response) > 2 << 20:
                        raise RuntimeError("模型部署响应超过 2 MiB")
        except FileNotFoundError as error:
            raise RuntimeError(
                "模型部署控制服务尚未注册；请运行 llmctl model init。该操作不会重启 Router 或 Worker"
            ) from error
        except PermissionError as error:
            raise RuntimeError(
                "账户门户无权访问模型部署控制服务，请检查 llm-account 用户组"
            ) from error
        except (ConnectionRefusedError, TimeoutError, socket.timeout) as error:
            raise RuntimeError(
                "模型部署控制服务不可用；请先运行 llmctl model init，再用 llmctl logs model 查看日志"
            ) from error
        if not response:
            raise RuntimeError("模型部署控制服务未返回数据")
        try:
            result = json.loads(response)
        except json.JSONDecodeError as error:
            raise RuntimeError("模型部署控制服务返回了无效 JSON") from error
        if not isinstance(result, dict) or not result.get("ok"):
            raise RuntimeError(str(result.get("error", "模型部署操作失败")))
        return result.get("result")

    def snapshot(self) -> dict[str, Any]:
        """读取部署注册表、GPU 状态和后台任务。"""

        result = self.request("snapshot")
        if not isinstance(result, dict):
            raise RuntimeError("模型部署快照结构无效")
        return result


class PortalControlPlane:
    """Business policy layered on OmniRoute's native routing and enforcement APIs."""

    def __init__(self, config: Config, db: Database, omni: OmniRouteClient):
        self.config, self.db, self.omni = config, db, omni
        self.lock = threading.RLock()
        self.usage_reconciled_at: dict[str, int] = {}
        self.free_visibility_reconciled_at = 0
        self.public_combo_backup_dir: pathlib.Path | None = None

    @staticmethod
    def _sqlite_online_backup(source: pathlib.Path, destination: pathlib.Path) -> None:
        """Create a standalone SQLite snapshot while the source may be live."""
        if not source.is_file():
            raise RuntimeError(f"SQLite source does not exist: {source}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(source), timeout=30) as source_db:
            with sqlite3.connect(str(destination), timeout=30) as backup_db:
                source_db.backup(backup_db)
                result = backup_db.execute("PRAGMA quick_check").fetchone()
                if not result or result[0] != "ok":
                    raise RuntimeError(f"SQLite backup quick_check failed: {destination}")
        os.chmod(destination, 0o600)

    def _migration_backup_directory(self) -> pathlib.Path:
        configured = os.environ.get("LLMCTL_BACKUP_ROOT", "").strip()
        production = str(self.config.db_path).startswith("/var/lib/llm-cluster/")
        root = pathlib.Path(
            configured or ("/var/backups/llmctl" if production else self.config.db_path.parent / "backups")
        )
        root.mkdir(parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        candidates = sorted(
            (path for path in root.glob("control-plane-*") if path.is_dir()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if candidates and time.time() - candidates[0].stat().st_mtime <= PUBLIC_COMBO_BACKUP_MAX_AGE_SECONDS:
            return candidates[0]
        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = root / f"routing-migration-{timestamp}"
        destination.mkdir(mode=0o700)
        return destination

    def prepare_public_combo_migration_backup(self) -> pathlib.Path | None:
        """Snapshot both SQLite control stores before the current route migration."""
        if self.public_combo_backup_dir is not None:
            return self.public_combo_backup_dir
        if self.db.settings().get(PUBLIC_COMBO_MIGRATION_SETTING) == PUBLIC_COMBO_MIGRATION_NAME:
            return None
        with self.db.connect() as connection:
            legacy_rows = self.rows(
                connection.execute(
                    "SELECT id,public_model_id,source_ref,source_model,mapping_kind,"
                    "mapping_id,status FROM published_models WHERE source_kind='combo' "
                    "ORDER BY created_at,id"
                ).fetchall()
            )
        if not legacy_rows:
            return None

        backup_dir = self._migration_backup_directory()
        data_dir = backup_dir / "runtime-data"
        manifest_path = data_dir / "runtime-data.json"
        if manifest_path.is_file():
            try:
                existing_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                existing_manifest = {}
            if existing_manifest.get("migration") == PUBLIC_COMBO_MIGRATION_NAME:
                self.public_combo_backup_dir = backup_dir
                return backup_dir
            # 即使处于控制面升级验收窗口，也不覆盖另一迁移的回滚快照。
            timestamp = datetime.datetime.now(datetime.timezone.utc).strftime(
                "%Y%m%dT%H%M%SZ"
            )
            backup_dir = backup_dir.parent / f"routing-migration-{timestamp}"
            suffix = 0
            while backup_dir.exists():
                suffix += 1
                backup_dir = backup_dir.parent / f"routing-migration-{timestamp}-{suffix}"
            backup_dir.mkdir(mode=0o700)
            data_dir = backup_dir / "runtime-data"
            manifest_path = data_dir / "runtime-data.json"
        data_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(data_dir, 0o700)
        entries: list[dict[str, Any]] = []

        portal_backup = data_dir / "portal" / self.config.db_path.name
        self._sqlite_online_backup(self.config.db_path, portal_backup)
        entries.append(
            {
                "role": "account-portal",
                "source": str(self.config.db_path),
                "backup": str(portal_backup.relative_to(backup_dir)),
                "mode": oct(self.config.db_path.stat().st_mode & 0o777),
            }
        )

        gateway_dir = self.config.db_path.parent.parent / "gateway"
        gateway_databases = sorted(
            {
                path
                for pattern in ("*.sqlite", "*.db")
                for path in gateway_dir.glob(pattern)
                if path.is_file()
            }
        )
        production = str(self.config.db_path).startswith("/var/lib/llm-cluster/")
        if production and not gateway_databases:
            raise RuntimeError(f"OmniRoute SQLite database not found under {gateway_dir}")
        for source in gateway_databases:
            backup = data_dir / "gateway" / source.name
            self._sqlite_online_backup(source, backup)
            entries.append(
                {
                    "role": "omniroute",
                    "source": str(source),
                    "backup": str(backup.relative_to(backup_dir)),
                    "mode": oct(source.stat().st_mode & 0o777),
                }
            )

        temporary = manifest_path.with_suffix(".json.new")
        temporary.write_text(
            json.dumps(
                {
                    "version": 1,
                    "migration": PUBLIC_COMBO_MIGRATION_NAME,
                    "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "databases": entries,
                    "legacy_routes": legacy_rows,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(manifest_path)
        self.public_combo_backup_dir = backup_dir
        print(
            f"[account-portal] pre-migration SQLite snapshot: {backup_dir}",
            flush=True,
        )
        return backup_dir

    @staticmethod
    def rows(rows: Any) -> list[dict[str, Any]]:
        return [dict(row) for row in rows]

    @staticmethod
    def _qualified_target(value: str, provider: str = "") -> tuple[str, str]:
        value, provider = str(value).strip(), str(provider).strip()
        if provider and value.startswith(provider + "/"):
            value = value[len(provider) + 1 :]
        if not provider and "/" in value:
            provider, value = value.split("/", 1)
        return provider, value

    def _resolve_model_targets(self, payload: dict[str, Any]) -> list[dict[str, str]]:
        source_kind = str(payload.get("source_kind", "model"))
        source_ref = str(payload.get("source_ref", ""))
        source_provider = str(payload.get("source_provider", ""))
        source_model = str(payload.get("source_model", ""))
        if source_kind != "combo":
            provider, model = self._qualified_target(source_model, source_provider)
            return [{"provider": provider, "model": model}] if model else []

        combos = self.omni.combos()
        by_id = {str(item.get("id", "")): item for item in combos}
        by_name = {str(item.get("name", "")): item for item in combos}
        selected = by_id.get(source_ref) or by_name.get(source_model)
        if not selected:
            return []
        targets: list[dict[str, str]] = []
        visiting: set[str] = set()

        def visit(combo: dict[str, Any]) -> None:
            combo_id = str(combo.get("id", combo.get("name", "")))
            if combo_id in visiting:
                return
            visiting.add(combo_id)
            members = combo.get("models", combo.get("targets", []))
            if not isinstance(members, list):
                return
            for member in members:
                if isinstance(member, str):
                    provider, model = self._qualified_target(member)
                elif isinstance(member, dict):
                    kind = str(member.get("kind", ""))
                    if kind in {"combo", "combo-ref", "combo_ref"}:
                        nested_id = str(
                            member.get(
                                "comboName",
                                member.get(
                                    "comboId", member.get("combo_id", member.get("id", ""))
                                ),
                            )
                        )
                        nested = by_id.get(nested_id) or by_name.get(nested_id)
                        if nested:
                            visit(nested)
                        continue
                    provider = str(member.get("provider", member.get("providerId", "")))
                    model_value = str(
                        member.get(
                            "model",
                            member.get(
                                "modelId", member.get("qualifiedModel", member.get("id", ""))
                            ),
                        )
                    )
                    provider, model = self._qualified_target(model_value, provider)
                else:
                    continue
                if model and {"provider": provider, "model": model} not in targets:
                    targets.append({"provider": provider, "model": model})

        visit(selected)
        return targets

    def inspect_model(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Read gateway-native metadata for every concrete target behind a publication."""
        if payload.get("id") and not payload.get("source_model"):
            with self.db.connect() as connection:
                row = connection.execute(
                    "SELECT * FROM published_models WHERE id=?", (str(payload["id"]),)
                ).fetchone()
            if not row:
                raise ValueError("model not found")
            payload = dict(row)
        targets = self._resolve_model_targets(payload)
        if not targets:
            raise ValueError("无法从当前接入层解析底层模型，请检查来源模型或路由组合")

        options = self.omni.combo_builder_options()
        providers = options.get("providers", []) if isinstance(options, dict) else []
        lookup: dict[tuple[str, str], dict[str, Any]] = {}
        if isinstance(providers, list):
            for provider_entry in providers:
                if not isinstance(provider_entry, dict):
                    continue
                provider_id = str(
                    provider_entry.get("providerId", provider_entry.get("id", ""))
                )
                for model_entry in provider_entry.get("models", []) or []:
                    if not isinstance(model_entry, dict):
                        continue
                    model_id = str(model_entry.get("id", model_entry.get("model", "")))
                    qualified = str(model_entry.get("qualifiedModel", ""))
                    q_provider, q_model = self._qualified_target(qualified, provider_id)
                    lookup[(q_provider or provider_id, q_model or model_id)] = model_entry

        enriched: list[dict[str, Any]] = []
        capabilities: set[str] = set()
        for target in targets:
            provider, model_id = target["provider"], target["model"]
            option = lookup.get((provider, model_id), {})
            qualified = f"{provider}/{model_id}" if provider else model_id
            alias: dict[str, Any] = {}
            with contextlib.suppress(RuntimeError):
                alias = self.omni.alias_metadata(qualified)
            resolved = alias.get("resolved", {}) if isinstance(alias, dict) else {}
            if isinstance(resolved, dict):
                provider = str(
                    resolved.get("provider", resolved.get("providerAlias", provider))
                )
                model_id = str(resolved.get("model", model_id))
                qualified = str(
                    resolved.get(
                        "qualifiedId", f"{provider}/{model_id}" if provider else model_id
                    )
                )
            native = alias.get("metadata", {}) if isinstance(alias.get("metadata"), dict) else {}
            limits = native.get("limits", {}) if isinstance(native.get("limits"), dict) else {}
            native_caps = (
                native.get("capabilities", {})
                if isinstance(native.get("capabilities"), dict)
                else {}
            )
            context_window = option.get(
                "contextLength", limits.get("contextWindow", limits.get("maxInputTokens"))
            )
            max_output = option.get("outputTokenLimit", limits.get("maxOutputTokens"))
            for key, portal_cap in {
                "vision": "vision",
                "toolCalling": "tools",
                "supportsTools": "tools",
                "reasoning": "reasoning",
                "supportsThinking": "reasoning",
            }.items():
                if native_caps.get(key):
                    capabilities.add(portal_cap)
            capabilities.add("chat")
            enriched.append(
                {
                    "provider": provider,
                    "model": model_id,
                    "qualified_id": qualified,
                    "context_window_tokens": int(context_window) if context_window else None,
                    "max_output_tokens": int(max_output) if max_output else None,
                    "family": str(
                        (native.get("metadata") or {}).get("family", "")
                        if isinstance(native.get("metadata"), dict)
                        else ""
                    ),
                    "modalities": native.get("modalities", {}),
                    "capabilities": native_caps,
                }
            )
        contexts = [item["context_window_tokens"] for item in enriched if item["context_window_tokens"]]
        outputs = [item["max_output_tokens"] for item in enriched if item["max_output_tokens"]]
        return {
            "targets": enriched,
            "target_count": len(enriched),
            "context_known_count": len(contexts),
            "output_known_count": len(outputs),
            "context_window_tokens": min(contexts) if contexts else None,
            "max_output_tokens": min(outputs) if outputs else None,
            "capabilities": sorted(capabilities),
            "native_sync_supported": all(item["provider"] for item in enriched),
            "read_at": now(),
        }

    def _sync_model_limits(
        self,
        metadata: dict[str, Any],
        context_window: int | None,
        max_output: int | None,
        sync_context: bool,
        sync_output: bool,
    ) -> tuple[str, str]:
        errors: list[str] = []
        attempted = 0
        for target in metadata.get("targets", []):
            provider, model = str(target.get("provider", "")), str(target.get("model", ""))
            if not provider or not model:
                if sync_context or sync_output:
                    errors.append(f"{target.get('qualified_id', model)} 缺少供应商标识")
                continue
            if sync_context and context_window:
                attempted += 1
                try:
                    self.omni.set_context_window_override(provider, model, context_window)
                except Exception as error:
                    errors.append(f"{provider}/{model} 上下文：{error}")
            if sync_output and max_output:
                attempted += 1
                try:
                    self.omni.set_max_output_override(provider, model, max_output)
                except Exception as error:
                    errors.append(f"{provider}/{model} 最大输出：{error}")
        if not attempted and not errors:
            return "read", ""
        if errors and attempted > len(errors):
            return "partial", "; ".join(errors)[:2000]
        if errors:
            return "failed", "; ".join(errors)[:2000]
        return "synced", ""

    def seed_managed_model(self) -> None:
        model_name = os.environ.get("SERVED_MODEL_NAME", "").strip()
        if not model_name:
            return
        combos = self.omni.combos()
        combo = next((item for item in combos if str(item.get("name", "")) == model_name), None)
        if not combo:
            return
        capabilities = ["chat"]
        if env_bool("SUPPORTS_IMAGE_INPUT"):
            capabilities.append("vision")
        if env_bool("SUPPORTS_OCR"):
            capabilities.append("ocr")
        if env_bool("SUPPORTS_TOOL_CALLING"):
            capabilities.append("tools")
        if env_bool("SUPPORTS_REASONING"):
            capabilities.append("reasoning")
        stamp = now()
        with self.db.connect() as connection:
            existing = connection.execute(
                "SELECT id FROM published_models WHERE public_model_id=?", (model_name,)
            ).fetchone()
            if existing:
                connection.execute(
                    "UPDATE published_models SET source_kind='combo',source_ref=?,source_model=?,capabilities_json=?,health_status='healthy',health_failures=0,updated_at=? WHERE id=?",
                    (str(combo.get("id", "")), model_name, json.dumps(capabilities), stamp, existing["id"]),
                )
                model_id = existing["id"]
            else:
                model_id = str(uuid.uuid4())
                connection.execute(
                    "INSERT INTO published_models(id,public_model_id,display_name,description,source_kind,source_ref,source_model,capabilities_json,status,health_status,last_health_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        model_id, model_name, model_name, "LLMCtl local vLLM cluster", "combo",
                        str(combo.get("id", "")), model_name, json.dumps(capabilities), "published",
                        "healthy", stamp, stamp, stamp,
                    ),
                )
                connection.execute(
                    "INSERT INTO model_price_versions(model_id,effective_at,input_price_micros,output_price_micros,cached_price_micros,reasoning_price_micros,actor) VALUES(?,?,?,?,?,?,?)",
                    (model_id, stamp, 0, 0, 0, 0, "system"),
                )
            connection.execute(
                "INSERT OR IGNORE INTO model_access(model_id,subject_type,subject_id,created_at) VALUES(?, 'all', '', ?)",
                (model_id, stamp),
            )

    @staticmethod
    def _source_combo(
        combos: list[dict[str, Any]], source_ref: str, source_model: str
    ) -> dict[str, Any] | None:
        return next(
            (
                combo
                for combo in combos
                if (source_ref and str(combo.get("id", "")) == source_ref)
                or (source_model and str(combo.get("name", "")) == source_model)
            ),
            None,
        )

    @staticmethod
    def _combo_member_signatures(combo: dict[str, Any] | None) -> list[str]:
        """Return schema-tolerant identities for the direct members of a combo."""
        if not combo:
            return []
        members = combo.get("models", combo.get("targets", []))
        if not isinstance(members, list):
            return []
        signatures: list[str] = []
        for member in members:
            if isinstance(member, str):
                identity: dict[str, Any] = {"member": member}
            elif isinstance(member, dict):
                identity = {
                    "kind": str(member.get("kind", "model")),
                    "provider": str(
                        member.get("provider", member.get("providerId", ""))
                    ),
                    "model": str(
                        member.get(
                            "model",
                            member.get(
                                "modelId",
                                member.get("qualifiedModel", member.get("id", "")),
                            ),
                        )
                    ),
                    "connection": str(
                        member.get(
                            "connectionId",
                            member.get("providerConnectionId", member.get("connection", "")),
                        )
                    ),
                    "combo": str(
                        member.get(
                            "comboName",
                            member.get("comboId", member.get("combo_id", "")),
                        )
                    ),
                }
            else:
                continue
            signatures.append(json.dumps(identity, sort_keys=True, separators=(",", ":")))
        return sorted(signatures)

    @classmethod
    def _public_combo_mirrors_source(
        cls, public_combo: dict[str, Any] | None, source_combo: dict[str, Any] | None
    ) -> bool:
        public_members = cls._combo_member_signatures(public_combo)
        source_members = cls._combo_member_signatures(source_combo)
        if not public_members or public_members != source_members:
            return False
        public_strategy = str((public_combo or {}).get("strategy", "round-robin"))
        source_strategy = str((source_combo or {}).get("strategy", "round-robin"))
        if public_strategy != source_strategy:
            return False
        source_config = (source_combo or {}).get("config", {})
        public_config = (public_combo or {}).get("config", {})
        if isinstance(source_config, dict):
            if not isinstance(public_config, dict):
                return False
            for key, value in source_config.items():
                if public_config.get(key) != value:
                    return False
        return True

    def ensure_public_combo_route(
        self,
        public_id: str,
        source_ref: str,
        source_model: str,
        published: bool,
    ) -> dict[str, Any]:
        """Expose a combo-backed public ID as an actual native combo.

        OmniRoute's Responses API resolves bare model names before applying
        model-to-combo mappings. A mapping-only public ID can therefore be
        rewritten to a provider-qualified model and rejected by key policy.
        The public combo mirrors the source combo's direct members and routing
        configuration. OmniRoute 3.8.x accepts nested combo references but does
        not preserve the nested round-robin cursor, which concentrates traffic
        on one Worker under load.
        """
        combos = self.omni.combos()
        source = self._source_combo(combos, source_ref, source_model)
        if not source:
            raise ValueError("combo id is required")
        source_id = str(source.get("id", ""))
        source_name = str(source.get("name", ""))
        if not source_id or not source_name:
            raise RuntimeError("OmniRoute returned an invalid source combo")
        if public_id == source_name:
            return {
                "mapping_kind": "source-combo",
                "mapping_id": source_id,
                "source_ref": source_id,
                "source_model": source_name,
                "created": False,
            }

        existing = next(
            (combo for combo in combos if str(combo.get("name", "")) == public_id),
            None,
        )
        if existing and str(existing.get("description", "")) != PUBLIC_COMBO_MANAGED_DESCRIPTION:
            raise RuntimeError(
                f"OmniRoute combo {public_id!r} already exists and is not managed by LLMCtl"
            )
        source_models = source.get("models", source.get("targets", []))
        if not isinstance(source_models, list) or not source_models:
            raise RuntimeError("source combo has no routable members")
        payload: dict[str, Any] = {
            "name": public_id,
            "description": PUBLIC_COMBO_MANAGED_DESCRIPTION,
            # JSON 往返提供符合 schema 的深拷贝，不与来源响应共享可变成员/配置。
            "models": json.loads(json.dumps(source_models)),
            "strategy": str(source.get("strategy", "round-robin")) or "round-robin",
        }
        source_config = source.get("config")
        if isinstance(source_config, dict):
            payload["config"] = json.loads(json.dumps(source_config))
        context_length = source.get("context_length")
        if isinstance(context_length, int) and context_length >= 1000:
            payload["context_length"] = context_length
        combo_id, created = self.omni.upsert_combo(
            str(existing.get("id", "")) if existing else "",
            payload,
            active=published,
        )
        return {
            "mapping_kind": "native-combo",
            "mapping_id": combo_id,
            "source_ref": source_id,
            "source_model": source_name,
            "created": created,
            "updated": bool(existing) and not self._public_combo_mirrors_source(
                existing, source
            ),
        }

    def _delete_published_route(
        self, mapping_kind: str, mapping_id: str, public_id: str
    ) -> None:
        if mapping_kind == "combo" and mapping_id:
            self.omni.delete_combo_mapping(mapping_id)
        elif mapping_kind == "native-combo" and mapping_id:
            self.omni.delete_combo(mapping_id)
        elif mapping_kind == "alias" and public_id:
            self.omni.delete_model_alias(public_id)

    def reconcile_public_combo_routes(self) -> dict[str, int]:
        """Idempotently migrate legacy combo mappings without stopping traffic."""
        self.prepare_public_combo_migration_backup()
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM published_models WHERE source_kind='combo' "
                "ORDER BY created_at,id"
            ).fetchall()
        migrated = unchanged = failed = 0
        for row in rows:
            old_kind = str(row["mapping_kind"] or "")
            old_id = str(row["mapping_id"] or "")
            public_id = str(row["public_model_id"] or "")
            try:
                route = self.ensure_public_combo_route(
                    public_id,
                    str(row["source_ref"] or ""),
                    str(row["source_model"] or ""),
                    str(row["status"] or "") == "published",
                )
                new_kind = str(route["mapping_kind"])
                new_id = str(route["mapping_id"])
                changed = (
                    old_kind != new_kind
                    or old_id != new_id
                    or str(row["source_ref"] or "") != str(route["source_ref"])
                    or str(row["source_model"] or "") != str(route["source_model"])
                    or bool(route.get("updated"))
                )
                # 原生路由已上线；只停用被替代的门户路由，用户 Key 继续使用相同的
                # allowlist Combo ID。
                if old_kind in {"combo", "native-combo", "alias"} and (
                    old_kind != new_kind or old_id != new_id
                ):
                    try:
                        self._delete_published_route(old_kind, old_id, public_id)
                    except RuntimeError as error:
                        if "HTTP 404" not in str(error):
                            raise
                with self.db.connect() as connection:
                    connection.execute(
                        "UPDATE published_models SET source_ref=?,source_model=?,"
                        "mapping_kind=?,mapping_id=?,updated_at=? WHERE id=?",
                        (
                            route["source_ref"],
                            route["source_model"],
                            new_kind,
                            new_id,
                            now(),
                            row["id"],
                        ),
                    )
                migrated += 1 if changed else 0
                unchanged += 0 if changed else 1
            except Exception as error:
                failed += 1
                print(
                    f"[account-portal] public combo reconciliation warning "
                    f"(model={public_id}): {error}",
                    file=sys.stderr,
                    flush=True,
                )
        if failed == 0:
            self.db.update_settings(
                {PUBLIC_COMBO_MIGRATION_SETTING: PUBLIC_COMBO_MIGRATION_NAME}
            )
        return {"migrated": migrated, "unchanged": unchanged, "failed": failed}

    def public_combo_route_status(self) -> dict[str, Any]:
        """Compare portal-published combo IDs with OmniRoute's live combos."""
        combos = self.omni.combos()
        by_name = {str(item.get("name", "")): item for item in combos}
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT public_model_id,source_ref,source_model,mapping_kind,"
                "mapping_id,status FROM published_models WHERE source_kind='combo' "
                "ORDER BY public_model_id"
            ).fetchall()
        routes: list[dict[str, Any]] = []
        for row in rows:
            public_id = str(row["public_model_id"] or "")
            mapping_kind = str(row["mapping_kind"] or "")
            mapping_id = str(row["mapping_id"] or "")
            live = by_name.get(public_id)
            live_id = str(live.get("id", "")) if live else ""
            description = str(live.get("description", "")) if live else ""
            source = self._source_combo(
                combos,
                str(row["source_ref"] or ""),
                str(row["source_model"] or ""),
            )
            identity_route = (
                public_id == str(row["source_model"] or "")
                and live_id == str(row["source_ref"] or "")
            )
            managed_route = description == PUBLIC_COMBO_MANAGED_DESCRIPTION
            mirrored_route = self._public_combo_mirrors_source(live, source)
            ready = bool(live) and (
                (
                    mapping_kind == "native-combo"
                    and managed_route
                    and mirrored_route
                    and mapping_id == live_id
                )
                or (mapping_kind == "source-combo" and identity_route and mapping_id == live_id)
            )
            if not live:
                reason = "native combo missing"
            elif mapping_kind not in {"native-combo", "source-combo"}:
                reason = f"legacy mapping kind: {mapping_kind or 'unset'}"
            elif mapping_id != live_id:
                reason = "portal mapping id does not match live combo"
            elif mapping_kind == "native-combo" and not managed_route:
                reason = "live combo is not managed by LLMCtl"
            elif mapping_kind == "native-combo" and not mirrored_route:
                reason = "native combo does not mirror source members"
            elif mapping_kind == "source-combo" and not identity_route:
                reason = "source combo identity does not match"
            else:
                reason = "ready"
            routes.append(
                {
                    "public_model_id": public_id,
                    "published": str(row["status"] or "") == "published",
                    "mapping_kind": mapping_kind or "legacy",
                    "mapping_id": mapping_id,
                    "live_combo_id": live_id,
                    "source_model": str(row["source_model"] or ""),
                    "member_count": len(self._combo_member_signatures(live)),
                    "source_member_count": len(self._combo_member_signatures(source)),
                    "ready": ready,
                    "reason": reason,
                }
            )
        ready_count = sum(1 for route in routes if route["ready"])
        return {
            "ready": ready_count == len(routes),
            "ready_count": ready_count,
            "total": len(routes),
            "routes": routes,
        }

    def effective_models(self, user_id: str) -> list[dict[str, Any]]:
        with self.db.connect() as connection:
            account = connection.execute(
                "SELECT * FROM billing_accounts WHERE user_id=?", (user_id,)
            ).fetchone()
            models = self.rows(
                connection.execute(
                    """SELECT DISTINCT p.* FROM published_models p
                       JOIN model_access a ON a.model_id=p.id
                       WHERE p.status='published' AND p.health_status!='failed' AND (
                         a.subject_type='all' OR
                         (a.subject_type='user' AND a.subject_id=?) OR
                         (a.subject_type='group' AND a.subject_id IN
                           (SELECT m.group_id FROM user_group_members m
                            JOIN user_groups g ON g.id=m.group_id
                            WHERE m.user_id=? AND g.status='active'))
                       ) ORDER BY p.public_model_id""",
                    (user_id, user_id),
                ).fetchall()
            )
        balance = int(account["balance_micros"]) if account else 0
        suspended = bool(account["suspended"]) if account else False
        result = []
        for model in models:
            paid = any(
                int(model[key]) > 0
                for key in (
                    "input_price_micros", "output_price_micros", "cached_price_micros",
                    "reasoning_price_micros",
                )
            )
            if not suspended and (not paid or balance > 0):
                result.append(model)
        return result

    def sync_user(self, user_id: str) -> None:
        # 权限发布、用量结算和管理员策略变更共用一把锁，避免维护 tick 在故障关闭
        # 更新期间用旧策略重新启用 Key。
        with self.lock:
            self._sync_user(user_id)

    def _sync_user(self, user_id: str) -> None:
        with self.db.connect() as connection:
            user = connection.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not user or not user["api_key_id"]:
            return
        # 旧 LLMCtl 会把促销赠额镜像为 OmniRoute 全局 Token 限制，赠额耗尽后即使
        # 有现金余额也无法使用。发布当前计费策略前移除该旧硬限制；删除或后续
        # 策略同步任一步失败，Key 都保持禁用。
        if user["token_limit_id"]:
            self.omni.activate_key(str(user["api_key_id"]), False)
            try:
                self.omni.delete_limit(str(user["token_limit_id"]))
            except RuntimeError as error:
                if "HTTP 404" not in str(error):
                    raise
            with self.db.connect() as connection:
                connection.execute(
                    "UPDATE users SET token_limit_id=NULL WHERE id=?",
                    (user_id,),
                )
            with self.db.connect() as connection:
                user = connection.execute(
                    "SELECT * FROM users WHERE id=?", (user_id,)
                ).fetchone()
        models = self.effective_models(user_id) if user["status"] == "active" else []
        allowed_models: list[str] = []
        allowed_combos: list[str] = []
        for model in models:
            # 只授权公开 ID。OmniRoute 在 API Key 策略检查后解析门户所有的 Combo
            # 映射/模型别名；若加入来源 ID，用户可直接调用底层路由绕过公开命名
            # 与访问策略。
            public_id = str(model["public_model_id"])
            target = allowed_combos if model["source_kind"] == "combo" else allowed_models
            if public_id and public_id not in target:
                target.append(public_id)
        try:
            self.omni.patch_key_permissions(
                user["api_key_id"], allowed_models, allowed_combos,
                user["status"] == "active" and bool(models),
                int(user["max_sessions"]),
                int(user["requests_per_minute"]),
                int(user["requests_per_day"]),
            )
            sync_status, error = "synced", ""
        except Exception as exc:
            sync_status, error = "failed", str(exc)[:500]
        with self.db.connect() as connection:
            connection.execute(
                "INSERT INTO permission_sync(user_id,status,error,updated_at) VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET status=excluded.status,error=excluded.error,updated_at=excluded.updated_at",
                (user_id, sync_status, error, now()),
            )
        if error:
            raise RuntimeError(error)

    def quiesce_all_users(self) -> int:
        """Fail closed before changing a policy shared by multiple API keys."""
        with self.db.connect() as connection:
            keys = [
                str(row["api_key_id"])
                for row in connection.execute(
                    "SELECT api_key_id FROM users WHERE role='user' AND api_key_id IS NOT NULL"
                )
            ]
        disabled: list[str] = []
        try:
            for key_id in keys:
                self.omni.activate_key(key_id, False)
                disabled.append(key_id)
        except Exception:
            # 静默操作无法完成时恢复最后提交的有效策略，此时请求的策略修改尚未开始。
            with contextlib.suppress(Exception):
                self.sync_all_users()
            raise RuntimeError("could not safely quiesce all user API keys")
        return len(disabled)

    def sync_all_users(self) -> dict[str, int]:
        with self.db.connect() as connection:
            ids = [row["id"] for row in connection.execute("SELECT id FROM users WHERE role='user'")]
        success = failed = 0
        for user_id in ids:
            try:
                self.sync_user(user_id)
                success += 1
            except RuntimeError:
                failed += 1
        return {"synced": success, "failed": failed}

    def discover_free_resources(self) -> dict[str, int]:
        catalog = self.omni.free_models()
        rankings = self.omni.free_rankings()
        available_rankings = self.omni.free_rankings(available_only=True)
        configured = {str(item.get("id", "")): item for item in rankings}
        available = {str(item.get("id", "")): item for item in available_rankings}
        stamp = now()
        seen: set[str] = set()
        with self.db.connect() as connection:
            for item in catalog:
                provider = str(item.get("provider", "")).strip()
                model_id = str(item.get("modelId", "")).strip()
                if not provider or not model_id:
                    continue
                resource_key = f"{provider}:{model_id}"
                seen.add(resource_key)
                provider_state = configured.get(provider)
                source = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
                connection.execute(
                    """INSERT INTO free_resources(resource_key,provider,model_id,display_name,free_type,monthly_tokens,credit_tokens,terms_status,configured,available,source_json,discovered_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(resource_key) DO UPDATE SET
                       display_name=excluded.display_name,free_type=excluded.free_type,monthly_tokens=excluded.monthly_tokens,
                       credit_tokens=excluded.credit_tokens,terms_status=excluded.terms_status,configured=excluded.configured,
                       available=excluded.available,source_json=excluded.source_json,updated_at=excluded.updated_at""",
                    (
                        resource_key, provider, model_id, str(item.get("displayName", model_id)),
                        str(item.get("freeType", "unknown")), item.get("monthlyTokens"), item.get("creditTokens"),
                        str(item.get("tos", "")), 1 if provider_state else 0, 1 if provider in available else 0,
                        source, stamp, stamp,
                    ),
                )
            if seen:
                placeholders = ",".join("?" for _ in seen)
                connection.execute(
                    f"UPDATE free_resources SET available=0,updated_at=? WHERE resource_key NOT IN ({placeholders})",
                    (stamp, *sorted(seen)),
                )
        visibility = self.refresh_free_resource_visibility()
        self.free_visibility_reconciled_at = now()
        return {
            "catalog": len(catalog),
            "configured_providers": len(configured),
            "available_providers": len(available),
            "resources": len(seen),
            "hidden_resources": visibility["hidden"],
        }

    def refresh_free_resource_visibility(self) -> dict[str, int]:
        """Mirror OmniRoute's native eye-hidden state without owning that state."""
        with self.db.connect() as connection:
            providers = [
                str(row["provider"])
                for row in connection.execute(
                    "SELECT DISTINCT provider FROM free_resources WHERE configured=1"
                )
            ]
        hidden_total = reconciled = failed = 0
        for provider in providers:
            try:
                hidden = self.omni.hidden_provider_models(provider)
            except RuntimeError:
                failed += 1
                continue
            with self.db.connect() as connection:
                rows = connection.execute(
                    "SELECT resource_key,model_id FROM free_resources WHERE provider=?",
                    (provider,),
                ).fetchall()
                for row in rows:
                    visible = 0 if str(row["model_id"]) in hidden else 1
                    hidden_total += 1 - visible
                    connection.execute(
                        "UPDATE free_resources SET native_visible=?,updated_at=? WHERE resource_key=?",
                        (visible, now(), row["resource_key"]),
                    )
            reconciled += 1
        return {"providers": reconciled, "failed": failed, "hidden": hidden_total}

    def test_free_resource(self, resource_key: str) -> dict[str, Any]:
        with self.db.connect() as connection:
            resource = connection.execute(
                "SELECT * FROM free_resources WHERE resource_key=?", (resource_key,)
            ).fetchone()
        if not resource:
            raise ValueError("free resource not found")
        if not resource["configured"]:
            error = f"请先配置并启用该免费资源对应的供应商：{resource['provider']}"
            with self.db.connect() as connection:
                connection.execute(
                    "UPDATE free_resources SET test_status='failed',test_error=?,available=0,last_tested_at=?,updated_at=? WHERE resource_key=?",
                    (error, now(), now(), resource_key),
                )
            raise ValueError(error)
        try:
            # 免费 Provider 可能要求流式、专用端点、能力协商或由 OmniRoute 选择
            # Connection。复用原生仪表盘探针，让门户与原生 UI 共享同一测试契约，
            # 避免两套略有差异的适配器。
            latency, content = self.omni.test_provider_model(
                str(resource["provider"]), str(resource["model_id"])
            )
            status, available, error = "healthy", 1, ""
        except Exception as exc:
            latency, content, status, available, error = None, "", "failed", 0, str(exc)[:500]
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE free_resources SET test_status=?,test_latency_ms=?,test_error=?,available=?,last_tested_at=?,updated_at=? WHERE resource_key=?",
                (status, latency, error, available, now(), now(), resource_key),
            )
        if error:
            raise RuntimeError(error)
        return {"status": status, "latency_ms": latency, "response": content}

    def save_model(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        public_id = str(payload.get("public_model_id", "")).strip()
        source_kind = str(payload.get("source_kind", "")).strip()
        source_model = str(payload.get("source_model", "")).strip()
        source_ref = str(payload.get("source_ref", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9._:/-]{1,200}", public_id):
            raise ValueError("invalid public model id")
        if source_kind not in {"combo", "model", "free"} or not source_model:
            raise ValueError("invalid source model")
        capabilities = payload.get("capabilities", ["chat"])
        if not isinstance(capabilities, list) or any(
            not isinstance(item, str) or not re.fullmatch(r"[a-z0-9_-]{1,32}", item)
            for item in capabilities
        ):
            raise ValueError("invalid capabilities")
        status = str(payload.get("status", "published"))
        if status not in {"draft", "published", "disabled"}:
            raise ValueError("invalid model status")
        access = payload.get("access") or [{"type": "all", "id": ""}]
        if not isinstance(access, list):
            raise ValueError("invalid model access")
        prices = {
            key: money_to_micros(payload.get(key.replace("_micros", ""), "0"))
            for key in (
                "input_price_micros", "output_price_micros", "cached_price_micros",
                "reasoning_price_micros",
            )
        }
        if any(value < 0 for value in prices.values()):
            raise ValueError("model prices cannot be negative")
        requested_context = positive_int_or_none(
            payload.get("context_window_tokens"), "最大上下文"
        )
        requested_output = positive_int_or_none(
            payload.get("max_output_tokens"), "最大输出 Token"
        )
        sync_context = bool(payload.get("sync_context_window"))
        sync_output = bool(payload.get("sync_max_output_tokens"))
        if source_kind == "free":
            with self.db.connect() as connection:
                free = connection.execute(
                    "SELECT * FROM free_resources WHERE resource_key=?", (source_ref,)
                ).fetchone()
            if not free or free["test_status"] != "healthy" or not free["available"]:
                raise ValueError("free resource must pass a live test before publishing")
        with self.db.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM published_models WHERE id=? OR public_model_id=? LIMIT 1",
                (str(payload.get("id", "")), public_id),
            ).fetchone()
        self.quiesce_all_users()
        old_mapping_id = str(existing["mapping_id"] or "") if existing else ""
        old_mapping_kind = str(existing["mapping_kind"] or "") if existing else ""
        old_public_id = str(existing["public_model_id"] or "") if existing else ""
        old_source_ref = str(existing["source_ref"] or "") if existing else ""
        old_source_model = str(existing["source_model"] or "") if existing else ""
        old_status = str(existing["status"] or "") if existing else ""
        mapping_id = ""
        mapping_kind = ""
        created_mapping = False
        mutated_mapping = False
        try:
            if source_kind == "combo":
                route = self.ensure_public_combo_route(
                    public_id,
                    source_ref,
                    source_model,
                    status == "published",
                )
                mapping_id = str(route["mapping_id"])
                mapping_kind = str(route["mapping_kind"])
                source_ref = str(route["source_ref"])
                source_model = str(route["source_model"])
                created_mapping = bool(route["created"])
                mutated_mapping = bool(
                    existing
                    and not created_mapping
                    and old_mapping_kind == "native-combo"
                    and old_mapping_id == mapping_id
                    and old_public_id == public_id
                )
            elif public_id != source_model:
                mapping_id = self.omni.set_model_alias(public_id, source_model)
                mapping_kind = "alias"
                mutated_mapping = old_mapping_kind == "alias" and old_public_id == public_id
                created_mapping = not mutated_mapping
            if status == "published":
                latency, _ = self.omni.test_model(public_id)
            else:
                latency = None
            inspect_payload = dict(payload)
            inspect_payload.update(
                {
                    "source_kind": source_kind,
                    "source_ref": source_ref,
                    "source_model": source_model,
                }
            )
            metadata: dict[str, Any]
            metadata_error = ""
            try:
                metadata = self.inspect_model(inspect_payload)
            except Exception as error:
                metadata = {"targets": [], "read_at": now()}
                metadata_error = str(error)[:2000]
            context_window = requested_context or metadata.get("context_window_tokens")
            max_output = requested_output or metadata.get("max_output_tokens")
            if (sync_context or sync_output) and metadata_error:
                metadata_status = "failed"
            else:
                metadata_status, sync_error = self._sync_model_limits(
                    metadata,
                    context_window,
                    max_output,
                    sync_context,
                    sync_output,
                )
                metadata_error = sync_error or metadata_error
            stamp = now()
            model_id = str(existing["id"]) if existing else str(uuid.uuid4())
            with self.db.connect() as connection:
                connection.execute(
                    """INSERT INTO published_models(id,public_model_id,display_name,description,source_kind,source_ref,source_provider,source_model,capabilities_json,input_price_micros,output_price_micros,cached_price_micros,reasoning_price_micros,status,upstream_free,mapping_kind,mapping_id,health_status,health_latency_ms,last_health_at,health_failures,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                       public_model_id=excluded.public_model_id,display_name=excluded.display_name,description=excluded.description,
                       source_kind=excluded.source_kind,source_ref=excluded.source_ref,source_provider=excluded.source_provider,
                       source_model=excluded.source_model,capabilities_json=excluded.capabilities_json,input_price_micros=excluded.input_price_micros,
                       output_price_micros=excluded.output_price_micros,cached_price_micros=excluded.cached_price_micros,
                       reasoning_price_micros=excluded.reasoning_price_micros,status=excluded.status,upstream_free=excluded.upstream_free,
                       mapping_kind=excluded.mapping_kind,mapping_id=excluded.mapping_id,health_status=excluded.health_status,
                       health_latency_ms=excluded.health_latency_ms,last_health_at=excluded.last_health_at,health_failures=0,updated_at=excluded.updated_at""",
                    (
                        model_id, public_id, str(payload.get("display_name", public_id)), str(payload.get("description", "")),
                        source_kind, source_ref, str(payload.get("source_provider", "")), source_model,
                        json.dumps(capabilities, ensure_ascii=False), prices["input_price_micros"], prices["output_price_micros"],
                        prices["cached_price_micros"], prices["reasoning_price_micros"], status,
                        1 if source_kind == "free" else 0, mapping_kind, mapping_id,
                        "healthy" if status == "published" else "unknown", latency, stamp if latency else None,
                        0, int(existing["created_at"]) if existing else stamp, stamp,
                    ),
                )
                connection.execute(
                    """UPDATE published_models
                       SET context_window_tokens=?,max_output_tokens=?,metadata_json=?,
                           metadata_sync_status=?,metadata_sync_error=?,metadata_synced_at=?,updated_at=?
                       WHERE id=?""",
                    (
                        context_window,
                        max_output,
                        json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
                        metadata_status,
                        metadata_error,
                        stamp,
                        stamp,
                        model_id,
                    ),
                )
                connection.execute("DELETE FROM model_access WHERE model_id=?", (model_id,))
                for item in access:
                    subject_type = str(item.get("type", "")) if isinstance(item, dict) else ""
                    subject_id = str(item.get("id", "")) if isinstance(item, dict) else ""
                    if subject_type not in {"all", "group", "user"}:
                        raise ValueError("invalid access subject")
                    if subject_type == "group" and not connection.execute(
                        "SELECT 1 FROM user_groups WHERE id=?", (subject_id,)
                    ).fetchone():
                        raise ValueError("access group does not exist")
                    if subject_type == "user" and not connection.execute(
                        "SELECT 1 FROM users WHERE id=? AND role='user'", (subject_id,)
                    ).fetchone():
                        raise ValueError("access user does not exist")
                    connection.execute(
                        "INSERT INTO model_access(model_id,subject_type,subject_id,created_at) VALUES(?,?,?,?)",
                        (model_id, subject_type, subject_id if subject_type != "all" else "", stamp),
                    )
                previous = connection.execute(
                    "SELECT input_price_micros,output_price_micros,cached_price_micros,reasoning_price_micros FROM model_price_versions WHERE model_id=? ORDER BY effective_at DESC LIMIT 1",
                    (model_id,),
                ).fetchone()
                current_prices = tuple(prices[key] for key in prices)
                if not previous or tuple(previous) != current_prices:
                    connection.execute(
                        "INSERT INTO model_price_versions(model_id,effective_at,input_price_micros,output_price_micros,cached_price_micros,reasoning_price_micros,actor) VALUES(?,?,?,?,?,?,?)",
                        (model_id, stamp, *current_prices, actor),
                    )
            permission_sync = self.sync_all_users()
            # 新 OmniRoute 路由、门户策略和用户权限全部提交后才删除旧别名/映射。
            if old_mapping_kind in {"combo", "native-combo", "alias"} and (
                old_mapping_kind != mapping_kind
                or old_mapping_id != mapping_id
                or old_public_id != public_id
            ):
                with contextlib.suppress(Exception):
                    self._delete_published_route(
                        old_mapping_kind, old_mapping_id, old_public_id
                    )
            return {
                "id": model_id,
                "public_model_id": public_id,
                "latency_ms": latency,
                "permission_sync": permission_sync,
                "metadata_sync": {
                    "status": metadata_status,
                    "error": metadata_error,
                    "targets": len(metadata.get("targets", [])),
                },
            }
        except Exception:
            if created_mapping:
                with contextlib.suppress(Exception):
                    self._delete_published_route(
                        mapping_kind, mapping_id, public_id
                    )
            elif mutated_mapping and existing:
                # 在线测试、策略校验或 SQLite 持久化失败时恢复最后提交的 OmniRoute 映射。
                with contextlib.suppress(Exception):
                    if old_mapping_kind == "native-combo":
                        self.ensure_public_combo_route(
                            old_public_id,
                            old_source_ref,
                            old_source_model,
                            old_status == "published",
                        )
                    elif old_mapping_kind == "alias":
                        self.omni.set_model_alias(old_public_id, old_source_model)
            with contextlib.suppress(Exception):
                self.sync_all_users()
            raise

    def test_published_model(self, model_id: str) -> dict[str, Any]:
        with self.db.connect() as connection:
            model = connection.execute("SELECT * FROM published_models WHERE id=?", (model_id,)).fetchone()
        if not model:
            raise ValueError("model not found")
        try:
            latency, response = self.omni.test_model(model["public_model_id"])
            failures, health, error = 0, "healthy", ""
        except Exception as exc:
            latency, response, failures, error = None, "", int(model["health_failures"] or 0) + 1, str(exc)[:500]
            health = "failed" if failures >= 3 else "unknown"
        withdraw = bool(model["upstream_free"]) and health == "failed"
        if withdraw:
            # 免费上游可能随时消失；撤回共享路由前先停用所有门户 Key，避免请求在
            # 健康状态变化与权限刷新之间穿透。
            self.quiesce_all_users()
            try:
                if model["mapping_kind"] == "combo" and model["mapping_id"]:
                    self.omni.set_combo_mapping(
                        model["public_model_id"], model["source_ref"], model["mapping_id"], False
                    )
                elif model["mapping_kind"] == "native-combo" and model["mapping_id"]:
                    self.omni.set_combo_active(str(model["mapping_id"]), False)
                elif model["mapping_kind"] == "alias":
                    self.omni.delete_model_alias(model["public_model_id"])
            except Exception:
                # 无法确认原生撤回成功时 Key 按设计保持禁用，由管理员修复并重同步。
                with self.db.connect() as connection:
                    connection.execute(
                        "UPDATE published_models SET status='error',health_status='failed',health_error=?,last_health_at=?,health_failures=?,updated_at=? WHERE id=?",
                        (error or "native route withdrawal failed", now(), failures, now(), model_id),
                    )
                raise
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE published_models SET health_status=?,health_latency_ms=?,health_error=?,last_health_at=?,health_failures=?,status=CASE WHEN ?='failed' AND upstream_free=1 THEN 'error' ELSE status END,updated_at=? WHERE id=?",
                (health, latency, error, now(), failures, health, now(), model_id),
            )
        if withdraw:
            self.sync_all_users()
        if error:
            raise RuntimeError(error)
        return {"status": health, "latency_ms": latency, "response": response}

    @staticmethod
    def parse_timestamp(value: Any) -> int:
        try:
            return int(datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
        except (ValueError, TypeError):
            return now()

    def reset_due_grants(self) -> int:
        """Reset grants without requiring a request to pass through a disabled key."""
        stamp = now()
        with self.db.connect() as connection:
            due = connection.execute(
                "SELECT id,user_id,reset_interval,reset_time FROM token_grants "
                "WHERE status='active' AND reset_interval!='none' AND reset_at IS NOT NULL AND reset_at<=?",
                (stamp,),
            ).fetchall()
            user_ids = {str(row["user_id"]) for row in due}
            for grant in due:
                reset_time = str(grant["reset_time"] or "00:00")
                connection.execute(
                    "UPDATE token_grants SET tokens_remaining=tokens_initial,reset_at=?,updated_at=? WHERE id=?",
                    (
                        next_reset_at(
                            str(grant["reset_interval"]), stamp, reset_time=reset_time
                        ),
                        stamp,
                        grant["id"],
                    ),
                )
        for user_id in user_ids:
            try:
                self.sync_user(user_id)
            except RuntimeError as error:
                print(
                    f"[account-portal] grant reset permission sync warning "
                    f"(user={user_id}): {error}",
                    file=sys.stderr,
                    flush=True,
                )
        return len(due)

    def reconcile_usage(
        self, user_id: str | None = None, min_interval: int = 0
    ) -> dict[str, int]:
        # 管理员触发的同步可能与维护线程同时运行，序列化完整的获取/账本/Key 同步周期。
        with self.lock:
            throttle_key = user_id or "*"
            stamp = now()
            if min_interval > 0 and stamp - self.usage_reconciled_at.get(
                throttle_key, 0
            ) < min_interval:
                return {
                    "processed": 0,
                    "skipped": 0,
                    "users": 0,
                    "policy_updates": 0,
                    "sync_failed": 0,
                    "relabeled": 0,
                    "throttled": 1,
                }
            result = self._reconcile_usage(user_id=user_id)
            self.usage_reconciled_at[throttle_key] = now()
            return result

    @staticmethod
    def _usage_model_identities(item: dict[str, Any]) -> list[str]:
        identities: list[str] = []
        for key in (
            "comboName",
            "combo",
            "requestedModel",
            "requested_model",
            "model",
        ):
            value = str(item.get(key, "") or "").strip()
            if value and value not in identities:
                identities.append(value)
        return identities

    def _resolve_usage_model(
        self,
        connection: sqlite3.Connection,
        user_id: str,
        item: dict[str, Any],
    ) -> sqlite3.Row | None:
        """Resolve a gateway log to the public model the user was allowed to call."""
        identities = self._usage_model_identities(item)
        if not identities:
            return None
        eligible = connection.execute(
            """SELECT DISTINCT p.* FROM published_models p
               JOIN model_access a ON a.model_id=p.id
               WHERE p.status='published' AND (
                 a.subject_type='all' OR
                 (a.subject_type='user' AND a.subject_id=?) OR
                 (a.subject_type='group' AND a.subject_id IN
                   (SELECT m.group_id FROM user_group_members m
                    JOIN user_groups g ON g.id=m.group_id
                    WHERE m.user_id=? AND g.status='active'))
               ) ORDER BY p.public_model_id""",
            (user_id, user_id),
        ).fetchall()
        for identity in identities:
            exact = [row for row in eligible if row["public_model_id"] == identity]
            if len(exact) == 1:
                return exact[0]
        for identity in identities:
            mapped = [
                row
                for row in eligible
                if identity in {str(row["mapping_id"] or ""), str(row["source_ref"] or "")}
            ]
            if len(mapped) == 1:
                return mapped[0]
        for identity in identities:
            sourced = [
                row for row in eligible if str(row["source_model"] or "") == identity
            ]
            if len(sourced) == 1:
                return sourced[0]
        # 旧记录可能引用已停用且无替代的模型；保留精确历史身份，不虚构公开别名。
        for identity in identities:
            exact = connection.execute(
                "SELECT * FROM published_models WHERE public_model_id=? LIMIT 1",
                (identity,),
            ).fetchone()
            if exact:
                return exact
        return None

    def _repair_usage_model_ids(
        self, connection: sqlite3.Connection, user_id: str | None = None
    ) -> int:
        parameters: list[Any] = []
        user_clause = ""
        if user_id:
            user_clause = " AND l.user_id=?"
            parameters.append(user_id)
        rows = connection.execute(
            """SELECT l.* FROM usage_ledger l
               LEFT JOIN published_models current ON current.id=l.model_id
               WHERE (current.id IS NULL OR current.status!='published')"""
            + user_clause
            + " ORDER BY l.id",
            parameters,
        ).fetchall()
        repaired = 0
        for row in rows:
            resolved = self._resolve_usage_model(
                connection,
                str(row["user_id"]),
                {
                    "requestedModel": row["public_model_id"],
                    "model": row["resolved_model"],
                },
            )
            if (
                resolved
                and resolved["status"] == "published"
                and int(resolved["created_at"] or 0) <= int(row["occurred_at"] or 0)
                and (
                    resolved["id"] != row["model_id"]
                    or resolved["public_model_id"] != row["public_model_id"]
                )
            ):
                # 金额和不可变价格快照永不重新定价；此处只修复公开模型归属。
                connection.execute(
                    "UPDATE usage_ledger SET model_id=?,public_model_id=? WHERE id=?",
                    (resolved["id"], resolved["public_model_id"], row["id"]),
                )
                repaired += 1
        return repaired

    def _reconcile_usage(self, user_id: str | None = None) -> dict[str, int]:
        processed = skipped = 0
        settled_users: set[str] = set()
        policy_users: set[str] = set()
        with self.db.connect() as connection:
            if user_id:
                users = connection.execute(
                    "SELECT id,api_key_id FROM users WHERE id=? AND role='user' AND api_key_id IS NOT NULL",
                    (user_id,),
                ).fetchall()
            else:
                users = connection.execute(
                    "SELECT id,api_key_id FROM users WHERE role='user' AND api_key_id IS NOT NULL"
                ).fetchall()
            relabeled = self._repair_usage_model_ids(connection, user_id=user_id)
        key_ids_by_user = {
            str(user["id"]): str(user["api_key_id"]) for user in users
        }
        for user in users:
            logs: list[dict[str, Any]] = []
            reached_checkpoint = False
            for offset in range(0, 100_000, 200):
                page = self.omni.call_logs(user["api_key_id"], 200, offset)
                page_ids = [str(item.get("id", "")) for item in page if item.get("id")]
                known: set[str] = set()
                if page_ids:
                    placeholders = ",".join("?" for _ in page_ids)
                    with self.db.connect() as connection:
                        known = {
                            str(row["request_id"])
                            for row in connection.execute(
                                f"SELECT request_id FROM usage_ledger WHERE request_id IN ({placeholders})",
                                page_ids,
                            )
                        }
                for item in page:
                    if str(item.get("id", "")) in known:
                        reached_checkpoint = True
                        break
                    logs.append(item)
                if reached_checkpoint:
                    break
                if len(page) < 200:
                    break
            else:
                raise RuntimeError(
                    f"usage reconciliation backlog exceeds 100000 calls for API key {user['api_key_id']}"
                )

            billable_logs = []
            for item in logs:
                status_code = int(item.get("status", 0) or 0)
                detail_state = str(item.get("detailState", "")).lower()
                if (
                    item.get("id")
                    and 200 <= status_code < 400
                    and not item.get("active")
                    and detail_state != "in-memory"
                ):
                    billable_logs.append(item)
            if not billable_logs:
                skipped += len(logs)
                continue

            for item in reversed(billable_logs):
                request_id = str(item.get("id", ""))
                status_code = int(item.get("status", 0) or 0)
                if not request_id or status_code < 200 or status_code >= 400:
                    skipped += 1
                    continue
                tokens = item.get("tokens", {}) if isinstance(item.get("tokens"), dict) else {}
                input_tokens = int(tokens.get("in", 0) or 0)
                output_tokens = int(tokens.get("out", 0) or 0)
                cached_tokens = int(tokens.get("cacheRead", 0) or 0)
                reasoning_tokens = int(tokens.get("reasoning", 0) or 0)
                occurred = self.parse_timestamp(item.get("timestamp"))
                with self.db.connect() as connection:
                    if connection.execute(
                        "SELECT 1 FROM usage_ledger WHERE request_id=?", (request_id,)
                    ).fetchone():
                        continue
                    model = self._resolve_usage_model(
                        connection, str(user["id"]), item
                    )
                    if not model:
                        skipped += 1
                        continue
                    price = connection.execute(
                        "SELECT * FROM model_price_versions WHERE model_id=? AND effective_at<=? ORDER BY effective_at DESC LIMIT 1",
                        (model["id"], occurred),
                    ).fetchone() or model
                    stamp = now()
                    priced = price_usage(
                        input_tokens,
                        output_tokens,
                        cached_tokens,
                        reasoning_tokens,
                        price,
                        0,
                    )
                    gross = priced["gross_amount_micros"]
                    grant_amount = priced["grant_amount_micros"]
                    amount = priced["amount_micros"]
                    granted = priced["granted_tokens"]
                    account = connection.execute(
                        "SELECT * FROM billing_accounts WHERE user_id=?", (user["id"],)
                    ).fetchone()
                    balance = int(account["balance_micros"]) if account else 0
                    balance_after = balance - amount
                    connection.execute(
                        "INSERT OR IGNORE INTO billing_accounts(user_id,balance_micros,suspended,updated_at) VALUES(?,?,0,?)",
                        (user["id"], balance, stamp),
                    )
                    cursor = connection.execute(
                        """INSERT INTO usage_ledger(request_id,user_id,api_key_id,model_id,public_model_id,provider,resolved_model,input_tokens,output_tokens,cached_tokens,reasoning_tokens,granted_tokens,gross_amount_micros,grant_amount_micros,amount_micros,price_snapshot_json,occurred_at,created_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            request_id, user["id"], user["api_key_id"], model["id"], model["public_model_id"],
                            str(item.get("provider", "")), str(item.get("model", "")), input_tokens,
                            output_tokens, cached_tokens, reasoning_tokens, granted, gross,
                            grant_amount, amount,
                            json.dumps({key: int(price[key]) for key in ("input_price_micros", "output_price_micros", "cached_price_micros", "reasoning_price_micros")}),
                            occurred, stamp,
                        ),
                    )
                    if amount:
                        connection.execute(
                            "UPDATE billing_accounts SET balance_micros=?,updated_at=? WHERE user_id=?",
                            (balance_after, stamp, user["id"]),
                        )
                        connection.execute(
                            "INSERT INTO balance_transactions(user_id,kind,amount_micros,balance_after_micros,actor,note,usage_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
                            (user["id"], "debit", -amount, balance_after, "system", request_id, cursor.lastrowid, stamp),
                        )
                processed += 1
                settled_users.add(str(user["id"]))
                # 正余额结算不改变有效权限，因此不能切换外部可见 Key。扣款耗尽余额时，
                # sync_user 通过一次 OmniRoute PATCH 发布缩减后的 allowlist 和状态。
                if amount > 0 and balance_after <= 0:
                    policy_users.add(str(user["id"]))
        sync_failed = 0
        for user_id in policy_users:
            try:
                self.sync_user(user_id)
            except RuntimeError as error:
                sync_failed += 1
                fail_closed = "applied"
                try:
                    self.omni.activate_key(key_ids_by_user[user_id], False)
                except Exception as disable_error:
                    fail_closed = f"failed: {disable_error}"
                print(
                    f"[account-portal] post-billing permission sync warning "
                    f"(user={user_id}, fail_closed={fail_closed}): {error}",
                    file=sys.stderr,
                    flush=True,
                )
        return {
            "processed": processed,
            "skipped": skipped,
            "users": len(settled_users),
            "policy_updates": len(policy_users),
            "sync_failed": sync_failed,
            "relabeled": relabeled,
            "throttled": 0,
        }

    def user_dashboard(self, user_id: str) -> dict[str, Any]:
        models = self.effective_models(user_id)
        _portal_url, api_public_url = effective_public_urls(
            self.config, self.db.settings()
        )
        with self.db.connect() as connection:
            key_record = connection.execute(
                "SELECT api_key_id FROM users WHERE id=?", (user_id,)
            ).fetchone()
            account = connection.execute("SELECT * FROM billing_accounts WHERE user_id=?", (user_id,)).fetchone()
            grants = self.rows(connection.execute("SELECT * FROM token_grants WHERE user_id=? ORDER BY created_at DESC", (user_id,)).fetchall())
            transactions = self.rows(connection.execute("SELECT * FROM balance_transactions WHERE user_id=? ORDER BY created_at DESC LIMIT 200", (user_id,)).fetchall())
            total_spent_micros = int(
                connection.execute(
                    "SELECT COALESCE(SUM(amount_micros),0) FROM usage_ledger WHERE user_id=?",
                    (user_id,),
                ).fetchone()[0]
            )
        usage_page = self.usage_page(owner_user_id=user_id)
        result_models = []
        for model in models:
            item = dict(model)
            item["capabilities"] = json.loads(item.pop("capabilities_json") or "[]")
            item["metadata"] = json.loads(item.pop("metadata_json", "{}") or "{}")
            for key in ("input", "output", "cached", "reasoning"):
                item[f"{key}_price"] = micros_to_money(item[f"{key}_price_micros"])
            result_models.append(item)
        return {
            "balance": micros_to_money(int(account["balance_micros"]) if account else 0),
            "total_spent_micros": total_spent_micros,
            "suspended": bool(account["suspended"]) if account else False,
            "has_api_key": bool(key_record and key_record["api_key_id"]),
            "models": result_models,
            "grants": grants,
            "usage": usage_page["items"],
            "usage_pagination": {key: usage_page[key] for key in ("page", "page_size", "pages", "total")},
            "transactions": transactions,
            "api_base": api_public_url.rstrip("/") + "/v1",
        }

    def usage_page(
        self,
        owner_user_id: str | None = None,
        filter_user_id: str = "",
        model_id: str = "",
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        if page < 1 or page_size < 1 or page_size > 100:
            raise ValueError("invalid usage page")
        conditions: list[str] = []
        parameters: list[Any] = []
        if owner_user_id:
            conditions.append("l.user_id=?")
            parameters.append(owner_user_id)
        elif filter_user_id:
            conditions.append("l.user_id=?")
            parameters.append(filter_user_id)
        if model_id:
            conditions.append("l.public_model_id=?")
            parameters.append(model_id)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        with self.db.connect() as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM usage_ledger l{where}", parameters
                ).fetchone()[0]
            )
            pages = max(1, (total + page_size - 1) // page_size)
            effective_page = min(page, pages)
            offset = (effective_page - 1) * page_size
            if owner_user_id:
                query = (
                    f"SELECT l.* FROM usage_ledger l{where} "
                    "ORDER BY l.occurred_at DESC,l.id DESC LIMIT ? OFFSET ?"
                )
            else:
                query = (
                    "SELECT l.*,u.email user_email FROM usage_ledger l "
                    f"JOIN users u ON u.id=l.user_id{where} "
                    "ORDER BY l.occurred_at DESC,l.id DESC LIMIT ? OFFSET ?"
                )
            items = self.rows(
                connection.execute(
                    query, [*parameters, page_size, offset]
                ).fetchall()
            )
        return {
            "items": items,
            "page": effective_page,
            "page_size": page_size,
            "pages": pages,
            "total": total,
        }

    @staticmethod
    def analytics_window(
        range_key: str, stamp: int | None = None
    ) -> tuple[ZoneInfo, str, str, list[dict[str, Any]]]:
        """Build local-calendar buckets without relying on SQLite's host timezone.

        Epoch boundaries keep the SQL indexable and remain correct in named
        timezones with daylight-saving transitions. The last bucket is partial
        and ends at the dashboard generation time.
        """
        ranges = {
            "today": ("今日", "hour"),
            "7d": ("近 7 天", "day"),
            "30d": ("近 30 天", "day"),
            "12m": ("近 12 个月", "month"),
        }
        if range_key not in ranges:
            raise ValueError("统计范围无效")
        timezone_name = os.environ.get("TZ", "Asia/Shanghai")
        try:
            zone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            zone, timezone_name = ZoneInfo("UTC"), "UTC"
        generated_at = stamp if stamp is not None else now()
        current = datetime.datetime.fromtimestamp(generated_at, zone)
        label, grain = ranges[range_key]
        if range_key == "today":
            cursor = current.replace(hour=0, minute=0, second=0, microsecond=0)
            count = current.hour + 1

            def advance(value: datetime.datetime) -> datetime.datetime:
                return value + datetime.timedelta(hours=1)

            def bucket_label(value: datetime.datetime) -> str:
                return value.strftime("%H:00")

        elif range_key in {"7d", "30d"}:
            count = 7 if range_key == "7d" else 30
            cursor = current.replace(hour=0, minute=0, second=0, microsecond=0)
            cursor -= datetime.timedelta(days=count - 1)

            def advance(value: datetime.datetime) -> datetime.datetime:
                return value + datetime.timedelta(days=1)

            def bucket_label(value: datetime.datetime) -> str:
                return value.strftime("%m/%d")

        else:
            count = 12
            month_index = current.year * 12 + current.month - 1 - (count - 1)
            cursor = current.replace(
                year=month_index // 12,
                month=month_index % 12 + 1,
                day=1,
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )

            def advance(value: datetime.datetime) -> datetime.datetime:
                next_index = value.year * 12 + value.month
                return value.replace(
                    year=next_index // 12, month=next_index % 12 + 1
                )

            def bucket_label(value: datetime.datetime) -> str:
                return value.strftime("%Y/%m")

        buckets: list[dict[str, Any]] = []
        dashboard_end = generated_at + 1
        for index in range(count):
            next_cursor = advance(cursor)
            start_at = int(cursor.timestamp())
            end_at = min(int(next_cursor.timestamp()), dashboard_end)
            buckets.append(
                {
                    "index": index,
                    "label": bucket_label(cursor),
                    "start_at": start_at,
                    "end_at": max(start_at + 1, end_at),
                }
            )
            cursor = next_cursor
        return zone, timezone_name, label, buckets

    @staticmethod
    def _usage_conditions(
        start_at: int,
        end_at: int,
        model_id: str = "",
        user_id: str = "",
    ) -> tuple[str, list[Any]]:
        conditions = ["l.occurred_at>=?", "l.occurred_at<?"]
        parameters: list[Any] = [start_at, end_at]
        if model_id:
            conditions.append("l.public_model_id=?")
            parameters.append(model_id)
        if user_id:
            conditions.append("l.user_id=?")
            parameters.append(user_id)
        return " AND ".join(conditions), parameters

    def _usage_summary(
        self,
        connection: sqlite3.Connection,
        start_at: int,
        end_at: int,
        model_id: str = "",
        user_id: str = "",
    ) -> dict[str, Any]:
        where, parameters = self._usage_conditions(
            start_at, end_at, model_id, user_id
        )
        row = connection.execute(
            f"""SELECT COUNT(*) requests,
                       COUNT(DISTINCT l.user_id) active_users,
                       COALESCE(SUM(l.input_tokens),0) input_tokens,
                       COALESCE(SUM(l.output_tokens),0) output_tokens,
                       COALESCE(SUM(l.input_tokens+l.output_tokens),0) total_tokens,
                       COALESCE(SUM(l.cached_tokens),0) cached_tokens,
                       COALESCE(SUM(l.reasoning_tokens),0) reasoning_tokens,
                       COALESCE(SUM(l.amount_micros),0) amount_micros,
                       MAX(l.occurred_at) last_activity_at
                FROM usage_ledger l WHERE {where}""",
            parameters,
        ).fetchone()
        result = dict(row)
        requests = int(result["requests"] or 0)
        result["average_tokens_per_request"] = (
            round(int(result["total_tokens"] or 0) / requests, 2)
            if requests
            else 0
        )
        return result

    def _usage_series(
        self,
        connection: sqlite3.Connection,
        buckets: list[dict[str, Any]],
        model_id: str = "",
        user_id: str = "",
    ) -> list[dict[str, Any]]:
        values = ",".join("(?,?,?)" for _ in buckets)
        parameters: list[Any] = []
        for bucket in buckets:
            parameters.extend(
                (bucket["index"], bucket["start_at"], bucket["end_at"])
            )
        join_conditions = [
            "l.occurred_at>=b.start_at",
            "l.occurred_at<b.end_at",
        ]
        if model_id:
            join_conditions.append("l.public_model_id=?")
            parameters.append(model_id)
        if user_id:
            join_conditions.append("l.user_id=?")
            parameters.append(user_id)
        if self.db.is_mysql:
            bucket_source = " UNION ALL ".join(
                "SELECT ? AS bucket_index,? AS start_at,? AS end_at"
                for _ in buckets
            )
            bucket_cte = f"WITH buckets AS ({bucket_source})"
        else:
            bucket_cte = (
                f"WITH buckets(bucket_index,start_at,end_at) AS (VALUES {values})"
            )
        rows = connection.execute(
            f"""{bucket_cte}
                SELECT b.bucket_index,
                       COUNT(l.id) requests,
                       COUNT(DISTINCT l.user_id) active_users,
                       COALESCE(SUM(l.input_tokens),0) input_tokens,
                       COALESCE(SUM(l.output_tokens),0) output_tokens,
                       COALESCE(SUM(l.input_tokens+l.output_tokens),0) total_tokens,
                       COALESCE(SUM(l.cached_tokens),0) cached_tokens,
                       COALESCE(SUM(l.reasoning_tokens),0) reasoning_tokens,
                       COALESCE(SUM(l.amount_micros),0) amount_micros
                FROM buckets b
                LEFT JOIN usage_ledger l ON {' AND '.join(join_conditions)}
                GROUP BY b.bucket_index ORDER BY b.bucket_index""",
            parameters,
        ).fetchall()
        by_index = {int(row["bucket_index"]): dict(row) for row in rows}
        return [
            {
                **by_index.get(bucket["index"], {}),
                "bucket_index": bucket["index"],
                "label": bucket["label"],
                "start_at": bucket["start_at"],
                "end_at": bucket["end_at"],
            }
            for bucket in buckets
        ]

    def admin_analytics(
        self,
        range_key: str = "today",
        model_id: str = "",
        selected_user_id: str = "",
        active_page: int = 1,
        active_page_size: int = 10,
    ) -> dict[str, Any]:
        if any(len(value) > 200 for value in (model_id, selected_user_id)):
            raise ValueError("筛选条件过长")
        if active_page < 1 or not 1 <= active_page_size <= 50:
            raise ValueError("活跃用户分页范围无效")
        generated_at = now()
        _zone, timezone_name, range_label, buckets = self.analytics_window(
            range_key, generated_at
        )
        start_at, end_at = buckets[0]["start_at"], generated_at + 1
        with self.db.connect() as connection:
            selected_user = None
            if selected_user_id:
                user = connection.execute(
                    "SELECT id,email,status FROM users WHERE id=? AND role='user'",
                    (selected_user_id,),
                ).fetchone()
                if not user:
                    raise ValueError("用户不存在")
                selected_user = {
                    **dict(user),
                    "summary": self._usage_summary(
                        connection,
                        start_at,
                        end_at,
                        model_id,
                        selected_user_id,
                    ),
                    "timeseries": self._usage_series(
                        connection, buckets, model_id, selected_user_id
                    ),
                }
            summary = self._usage_summary(
                connection, start_at, end_at, model_id
            )
            timeseries = self._usage_series(connection, buckets, model_id)
            where, parameters = self._usage_conditions(
                start_at, end_at, model_id
            )
            top_users = self.rows(
                connection.execute(
                    f"""SELECT u.id user_id,u.email,
                               COUNT(l.id) requests,
                               SUM(l.input_tokens) input_tokens,
                               SUM(l.output_tokens) output_tokens,
                               SUM(l.input_tokens+l.output_tokens) total_tokens,
                               SUM(l.amount_micros) amount_micros,
                               MAX(l.occurred_at) last_activity_at
                        FROM usage_ledger l JOIN users u ON u.id=l.user_id
                        WHERE {where}
                        GROUP BY u.id,u.email
                        ORDER BY total_tokens DESC,last_activity_at DESC
                        LIMIT 10""",
                    parameters,
                ).fetchall()
            )
            active_total = int(
                connection.execute(
                    f"SELECT COUNT(DISTINCT l.user_id) FROM usage_ledger l WHERE {where}",
                    parameters,
                ).fetchone()[0]
            )
            active_pages = max(
                1, (active_total + active_page_size - 1) // active_page_size
            )
            effective_page = min(active_page, active_pages)
            active_users = self.rows(
                connection.execute(
                    f"""SELECT u.id user_id,u.email,u.status,
                               COUNT(l.id) requests,
                               SUM(l.input_tokens+l.output_tokens) total_tokens,
                               SUM(l.amount_micros) amount_micros,
                               MAX(l.occurred_at) last_activity_at
                        FROM usage_ledger l JOIN users u ON u.id=l.user_id
                        WHERE {where}
                        GROUP BY u.id,u.email,u.status
                        ORDER BY last_activity_at DESC,u.email
                        LIMIT ? OFFSET ?""",
                    [
                        *parameters,
                        active_page_size,
                        (effective_page - 1) * active_page_size,
                    ],
                ).fetchall()
            )
        total_tokens = max(1, int(summary["total_tokens"] or 0))
        for row in top_users:
            row["share_percent"] = round(
                int(row["total_tokens"] or 0) * 100 / total_tokens, 2
            )
        return {
            "generated_at": generated_at,
            "timezone": timezone_name,
            "source": "usage_ledger",
            "settlement_lag_seconds": 2,
            "token_definition": "input_tokens + output_tokens",
            "range": {
                "key": range_key,
                "label": range_label,
                "grain": "hour" if range_key == "today" else (
                    "month" if range_key == "12m" else "day"
                ),
                "start_at": start_at,
                "end_at": generated_at,
            },
            "filters": {"model": model_id},
            "summary": summary,
            "timeseries": timeseries,
            "top_users": top_users,
            "active_users": active_users,
            "active_pagination": {
                "page": effective_page,
                "page_size": active_page_size,
                "pages": active_pages,
                "total": active_total,
            },
            "selected_user": selected_user,
        }

    def user_request_detail(self, user_id: str, request_id: str) -> dict[str, Any]:
        if not request_id or len(request_id) > 200:
            raise ValueError("请求记录不存在")
        with self.db.connect() as connection:
            owned = connection.execute(
                "SELECT 1 FROM usage_ledger WHERE request_id=? AND user_id=?",
                (request_id, user_id),
            ).fetchone()
        if not owned:
            raise ValueError("请求记录不存在")
        detail = self.omni.call_log(request_id)
        summary = request_content_summary(detail.get("requestBody"))
        summary.update(
            {
                "request_id": request_id,
                "detail_state": str(detail.get("detailState", "")),
                "retained": bool(detail.get("hasRequestBody")) or summary["available"],
            }
        )
        return summary

    def admin_request_detail(self, request_id: str) -> dict[str, Any]:
        if not request_id or len(request_id) > 200:
            raise ValueError("请求记录不存在")
        with self.db.connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM usage_ledger WHERE request_id=?", (request_id,)
            ).fetchone()
        if not exists:
            raise ValueError("请求记录不存在")
        detail = self.omni.call_log(request_id)
        summary = request_content_summary(detail.get("requestBody"))
        response_summary = retained_response_summary(detail)
        summary.update(
            {
                "request_id": request_id,
                "detail_state": str(detail.get("detailState", "")),
                "retained": bool(detail.get("hasRequestBody")) or summary["available"],
                "response_available": response_summary["available"],
                "response_messages": response_summary["messages"],
                "response_truncated": response_summary["truncated"],
                "response_retained": response_summary["retained"],
            }
        )
        return summary

    @property
    def stress_root(self) -> pathlib.Path:
        return self.config.db_path.parent / "stress"

    @staticmethod
    def process_alive(pid: int | None) -> bool:
        if not pid or pid <= 1:
            return False
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    @staticmethod
    def stress_process_matches(pid: int, run_id: str) -> bool:
        """Do not signal a recycled PID that no longer belongs to this run."""
        try:
            arguments = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\x00")
        except OSError:
            return False
        decoded = [value.decode("utf-8", errors="replace") for value in arguments]
        return run_id in decoded and any(
            pathlib.Path(value).name == "llm_benchmark.py" for value in decoded
        )

    def stress_route_map(self, published: sqlite3.Row) -> dict[str, str]:
        """Resolve OmniRoute response identifiers to human-readable Worker names.

        Route metadata is emitted by OmniRoute, while the labels live on the
        combo targets that LLMCtl manages.  Keep this lookup best-effort so an
        unavailable builder-options endpoint never prevents a benchmark from
        running; the raw provider/connection identifier remains observable.
        """
        if str(published["source_kind"] or "") != "combo":
            return {}
        source_ref = str(published["source_ref"] or "").strip()
        source_model = str(published["source_model"] or "").strip()
        try:
            combos = self.omni.combos()
        except RuntimeError:
            return {}
        combo = next(
            (
                item
                for item in combos
                if source_ref in {str(item.get("id", "")), str(item.get("name", ""))}
                or source_model == str(item.get("name", ""))
            ),
            None,
        )
        if not combo:
            return {}
        models = combo.get("models", [])
        if not isinstance(models, list):
            return {}

        route_map: dict[str, str] = {}
        target_provider_labels: dict[str, set[str]] = {}
        target_connection_labels: dict[str, str] = {}
        for index, target in enumerate(models):
            if not isinstance(target, dict):
                continue
            label = str(target.get("label") or f"Worker {index}").strip()
            provider = str(target.get("providerId") or target.get("provider") or "").strip()
            connection = str(target.get("connectionId") or "").strip()
            if provider:
                target_provider_labels.setdefault(provider, set()).add(label)
            if connection:
                target_connection_labels[connection] = label
                route_map[connection] = label

        # Provider 标识只在唯一指向一个 Combo 目标时才可安全标注；共享 Provider
        # 必须通过 Connection 区分。
        for provider, labels in target_provider_labels.items():
            if len(labels) == 1:
                route_map[provider] = next(iter(labels))

        try:
            options = self.omni.combo_builder_options()
        except RuntimeError:
            options = {}
        providers = options.get("providers", []) if isinstance(options, dict) else []
        if isinstance(providers, list):
            for provider in providers:
                if not isinstance(provider, dict):
                    continue
                provider_id = str(
                    provider.get("providerId") or provider.get("id") or ""
                ).strip()
                labels = target_provider_labels.get(provider_id, set())
                if len(labels) == 1:
                    label = next(iter(labels))
                    for key in ("providerId", "id", "alias", "prefix"):
                        value = str(provider.get(key) or "").strip()
                        if value:
                            route_map[value] = label
                connections = provider.get("connections", [])
                if not isinstance(connections, list):
                    continue
                for connection in connections:
                    if not isinstance(connection, dict):
                        continue
                    connection_id = str(connection.get("id") or "").strip()
                    if connection_id in target_connection_labels:
                        route_map[connection_id] = target_connection_labels[connection_id]
        return route_map

    def sync_stress_run(self, run_id: str) -> dict[str, Any]:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM stress_runs WHERE id=?", (run_id,)
            ).fetchone()
        if not row:
            raise ValueError("压测任务不存在")
        record = dict(row)
        status_path = pathlib.Path(record["result_dir"]) / "status.json"
        status_document: dict[str, Any] = {}
        if status_path.is_file():
            try:
                value = json.loads(status_path.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    status_document = value
            except (OSError, json.JSONDecodeError):
                pass
        stored_status = str(record["status"])
        file_status = str(status_document.get("status", stored_status))
        terminal_statuses = {"completed", "failed", "canceled"}
        if stored_status in terminal_statuses and file_status not in terminal_statuses:
            status = stored_status
        elif stored_status == "canceling" and file_status not in terminal_statuses:
            status = "canceling"
        else:
            status = file_status
        if status not in {"starting", "running", "canceling", "completed", "failed", "canceled"}:
            status = "failed"
        pid = int(record.get("pid") or 0)
        if status == "canceling" and not self.process_alive(pid):
            status = "canceled"
        elif status in {"starting", "running"} and not self.process_alive(pid):
            grace_expired = now() - int(record["created_at"]) > 5
            if grace_expired:
                status = "failed"
                status_document["error"] = status_document.get("error") or "压测执行器意外退出"
        metrics = status_document.get("metrics", {})
        if not isinstance(metrics, dict):
            metrics = {}
        finished_at = (
            int(status_document.get("updated_at", now()))
            if status in {"completed", "failed", "canceled"}
            else None
        )
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE stress_runs SET status=?,metrics_json=?,error=?,started_at=COALESCE(started_at,?),finished_at=COALESCE(?,finished_at),updated_at=? WHERE id=?",
                (
                    status,
                    json.dumps(metrics, ensure_ascii=False, separators=(",", ":")),
                    str(status_document.get("error", ""))[:500],
                    status_document.get("started_at"),
                    finished_at,
                    now(),
                    run_id,
                ),
            )
            current = connection.execute(
                "SELECT * FROM stress_runs WHERE id=?", (run_id,)
            ).fetchone()
        result = dict(current)
        result["metrics"] = json.loads(result.pop("metrics_json") or "{}")
        result["progress"] = float(status_document.get("progress", 0) or 0)
        result["elapsed_seconds"] = float(status_document.get("elapsed_seconds", 0) or 0)
        gpu = status_document.get("gpu", {})
        result["gpu"] = gpu if isinstance(gpu, dict) else {}
        event_path = pathlib.Path(result["result_dir"]) / "events.jsonl"
        events: list[dict[str, Any]] = []
        if event_path.is_file():
            try:
                lines = event_path.read_text(encoding="utf-8").splitlines()[-20:]
                for line in lines:
                    value = json.loads(line)
                    if isinstance(value, dict):
                        events.append(value)
            except (OSError, json.JSONDecodeError):
                events = []
        result["recent_requests"] = events
        result.pop("result_dir", None)
        result.pop("pid", None)
        return result

    def stress_runs(self, limit: int = 30) -> list[dict[str, Any]]:
        with self.db.connect() as connection:
            identifiers = [
                str(row["id"])
                for row in connection.execute(
                    "SELECT id FROM stress_runs ORDER BY created_at DESC LIMIT ?",
                    (max(1, min(limit, 100)),),
                )
            ]
        return [self.sync_stress_run(identifier) for identifier in identifiers]

    def start_stress_run(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        try:
            concurrency = int(payload.get("concurrency", 1))
            input_tokens = int(payload.get("input_tokens", 50))
            output_tokens = int(payload.get("output_tokens", 128))
            request_multiplier = int(payload.get("request_multiplier", 2))
        except (TypeError, ValueError) as error:
            raise ValueError("压测参数必须为整数") from error
        if concurrency not in STRESS_CONCURRENCY_CHOICES:
            raise ValueError("不支持该并发档位")
        if input_tokens not in STRESS_INPUT_TOKEN_CHOICES:
            raise ValueError("不支持该提示词 Token 档位")
        if output_tokens not in STRESS_OUTPUT_TOKEN_CHOICES:
            raise ValueError("不支持该最大输出 Token 档位")
        if request_multiplier not in STRESS_REQUEST_MULTIPLIER_CHOICES:
            raise ValueError("每个并发槽位的请求数必须为 1-4")
        if (concurrency >= 20 or input_tokens >= 8000) and payload.get("risk_confirmed") is not True:
            raise ValueError("高负载压测必须确认风险")
        model = str(payload.get("model", "")).strip()
        with self.lock:
            with self.db.connect() as connection:
                published = connection.execute(
                    "SELECT * FROM published_models WHERE public_model_id=? AND status='published'",
                    (model,),
                ).fetchone()
                active = connection.execute(
                    "SELECT id FROM stress_runs WHERE status IN ('starting','running','canceling') ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
            if not published:
                raise ValueError("只能压测已发布的公开模型 ID")
            if active:
                current = self.sync_stress_run(str(active["id"]))
                if current["status"] in {"starting", "running", "canceling"}:
                    raise ValueError("已有压测任务正在运行，请等待完成或先停止")
            runner = pathlib.Path(__file__).resolve().with_name("llm_benchmark.py")
            if not runner.is_file():
                raise RuntimeError("缺少后台压测执行器 llm_benchmark.py")
            run_id = str(uuid.uuid4())
            run_dir = self.stress_root / run_id
            run_dir.mkdir(parents=True, exist_ok=False)
            os.chmod(run_dir, 0o700)
            route_map_path = run_dir / "route-map.json"
            route_map_path.write_text(
                json.dumps(
                    self.stress_route_map(published),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            os.chmod(route_map_path, 0o600)
            command = [
                sys.executable,
                str(runner),
                "--run-id", run_id,
                "--base-url", self.config.gateway_url,
                "--model", model,
                "--concurrency", str(concurrency),
                "--input-tokens", str(input_tokens),
                "--output-tokens", str(output_tokens),
                "--request-multiplier", str(request_multiplier),
                "--result-dir", str(run_dir),
                "--route-map", str(route_map_path),
            ]
            environment = os.environ.copy()
            environment["LLMCTL_BENCHMARK_API_KEY"] = self.config.gateway_manage_key
            log_file = (run_dir / "runner.log").open("ab", buffering=0)
            try:
                process = subprocess.Popen(  # noqa: S603 - fixed local executable and validated arguments
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    env=environment,
                    start_new_session=True,
                    close_fds=True,
                )
            finally:
                log_file.close()
            stamp = now()
            try:
                with self.db.connect() as connection:
                    connection.execute(
                        """INSERT INTO stress_runs(id,public_model_id,concurrency,target_input_tokens,max_output_tokens,request_multiplier,request_count,status,pid,result_dir,created_by,created_at,updated_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            run_id, model, concurrency, input_tokens, output_tokens,
                            request_multiplier, concurrency * request_multiplier,
                            "starting", process.pid, str(run_dir), actor, stamp, stamp,
                        ),
                    )
            except Exception:
                # Popen 成功但持久运行记录失败时，不能让无人跟踪的压测继续在后台
                # 消耗网关/GPU。
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.killpg(process.pid, signal.SIGKILL)
                raise
            threading.Thread(
                target=process.wait,
                name=f"stress-reaper-{run_id[:8]}",
                daemon=True,
            ).start()
        return self.sync_stress_run(run_id)

    def cancel_stress_run(self, run_id: str) -> dict[str, Any]:
        current = self.sync_stress_run(run_id)
        if current["status"] not in {"starting", "running", "canceling"}:
            return current
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT pid FROM stress_runs WHERE id=?", (run_id,)
            ).fetchone()
            connection.execute(
                "UPDATE stress_runs SET status='canceling',updated_at=? WHERE id=?",
                (now(), run_id),
            )
        pid = int(row["pid"] or 0)
        if self.process_alive(pid) and self.stress_process_matches(pid, run_id):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pid, signal.SIGTERM)

            def enforce_stop() -> None:
                time.sleep(10)
                with self.db.connect() as connection:
                    latest = connection.execute(
                        "SELECT pid,status FROM stress_runs WHERE id=?", (run_id,)
                    ).fetchone()
                if not latest or latest["status"] != "canceling":
                    return
                latest_pid = int(latest["pid"] or 0)
                if not (
                    self.process_alive(latest_pid)
                    and self.stress_process_matches(latest_pid, run_id)
                ):
                    return
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(latest_pid, signal.SIGKILL)
                with self.db.connect() as connection:
                    connection.execute(
                        "UPDATE stress_runs SET status='canceled',error=?,finished_at=?,updated_at=? WHERE id=? AND status='canceling'",
                        ("停止等待超过 10 秒，已强制结束后台压测进程", now(), now(), run_id),
                    )

            threading.Thread(
                target=enforce_stop,
                name=f"stress-stop-{run_id[:8]}",
                daemon=True,
            ).start()
        return self.sync_stress_run(run_id)

    def admin_snapshot(self) -> dict[str, Any]:
        stamp = now()
        if stamp - self.free_visibility_reconciled_at >= 30:
            self.refresh_free_resource_visibility()
            self.free_visibility_reconciled_at = stamp
        with self.db.connect() as connection:
            users = self.rows(
                connection.execute(
                    """SELECT u.*,b.balance_micros,b.suspended,
                              p.status permission_status,p.error permission_error
                       FROM users u
                       LEFT JOIN billing_accounts b ON b.user_id=u.id
                       LEFT JOIN permission_sync p ON p.user_id=u.id
                       ORDER BY u.created_at DESC""",
                ).fetchall()
            )
            groups = self.rows(connection.execute("SELECT g.*,COUNT(m.user_id) member_count FROM user_groups g LEFT JOIN user_group_members m ON m.group_id=g.id GROUP BY g.id ORDER BY g.name").fetchall())
            memberships = self.rows(connection.execute("SELECT * FROM user_group_members").fetchall())
            models = self.rows(connection.execute("SELECT * FROM published_models ORDER BY public_model_id").fetchall())
            access = self.rows(connection.execute("SELECT * FROM model_access").fetchall())
            free = self.rows(connection.execute("SELECT * FROM free_resources ORDER BY available DESC,test_status,provider,model_id").fetchall())
            audits = self.rows(connection.execute("SELECT * FROM audit_events ORDER BY id DESC LIMIT 300").fetchall())
            grants = self.rows(connection.execute("SELECT * FROM token_grants ORDER BY created_at DESC LIMIT 500").fetchall())
            transactions = self.rows(connection.execute("SELECT t.*,u.email user_email FROM balance_transactions t JOIN users u ON u.id=t.user_id ORDER BY t.id DESC LIMIT 500").fetchall())
        usage_page = self.usage_page()
        for user in users:
            user["balance"] = micros_to_money(int(user.get("balance_micros") or 0))
            user.pop("password_hash", None)
        for model in models:
            model["capabilities"] = json.loads(model.pop("capabilities_json") or "[]")
            model["metadata"] = json.loads(model.pop("metadata_json", "{}") or "{}")
            model["access"] = [item for item in access if item["model_id"] == model["id"]]
            for key in ("input", "output", "cached", "reasoning"):
                model[f"{key}_price"] = micros_to_money(model[f"{key}_price_micros"])
        settings = self.db.settings()
        portal_public_url, api_public_url = effective_public_urls(self.config, settings)
        settings["effective_public_url"] = portal_public_url
        settings["effective_api_public_url"] = api_public_url
        if settings.get("smtp_password"):
            settings["smtp_password_configured"] = "1"
            settings["smtp_password"] = ""
        gateway_models: list[dict[str, Any]] = []
        combos: list[dict[str, Any]] = []
        gateway_error = ""
        try:
            gateway_models = self.omni.models()
            combos = self.omni.combos()
        except RuntimeError as error:
            # 数据面临时不可用时，控制面仍可用于账户、SMTP、账本和审计恢复。
            gateway_error = str(error)
        stress_runs = self.stress_runs()
        return {
            "users": users, "groups": groups, "memberships": memberships, "models": models,
            "free_resources": free, "settings": settings, "audit": audits, "grants": grants,
            "usage": usage_page["items"],
            "usage_pagination": {key: usage_page[key] for key in ("page", "page_size", "pages", "total")},
            "transactions": transactions, "gateway_models": gateway_models, "combos": combos,
            "gateway_error": gateway_error, "stress_runs": stress_runs,
        }

    def bulk_update_user_policies(
        self, payload: dict[str, Any], actor: str
    ) -> dict[str, Any]:
        with self.lock:
            return self._bulk_update_user_policies(payload, actor)

    def _bulk_update_user_policies(
        self, payload: dict[str, Any], actor: str
    ) -> dict[str, Any]:
        """Atomically update selected local policies, then publish each key.

        The caller must send explicit user IDs. This prevents a stale browser
        filter from silently turning a targeted change into a global one.
        Keys are disabled before the SQLite commit and only re-enabled by a
        successful sync of the newly committed policy.
        """
        raw_ids = payload.get("user_ids", [])
        if not isinstance(raw_ids, list):
            raise ValueError("批量用户范围无效")
        user_ids = list(dict.fromkeys(str(value).strip() for value in raw_ids if str(value).strip()))
        if not user_ids or len(user_ids) > 2000:
            raise ValueError("请选择 1-2000 个用户")
        requested: dict[str, int] = {}
        field_specs = {
            "max_sessions": (normalize_max_sessions, "API Key 活跃会话上限"),
            "requests_per_minute": (normalize_request_limit, "每分钟请求数"),
            "requests_per_day": (normalize_request_limit, "每日请求数"),
        }
        for field, (normalizer, label) in field_specs.items():
            if field not in payload or payload.get(field) is None:
                continue
            requested[field] = normalizer(payload[field], label)
        if not requested:
            raise ValueError("至少选择一个要修改的调用策略")
        placeholders = ",".join("?" for _ in user_ids)
        with self.db.connect() as connection:
            rows = connection.execute(
                f"SELECT id,email,api_key_id FROM users WHERE role='user' AND id IN ({placeholders})",
                user_ids,
            ).fetchall()
        if len(rows) != len(user_ids):
            raise ValueError("批量范围包含不存在的用户，请刷新页面后重试")
        disabled_ids: list[str] = []
        try:
            for row in rows:
                if row["api_key_id"]:
                    self.omni.activate_key(str(row["api_key_id"]), False)
                disabled_ids.append(str(row["id"]))
        except Exception as error:
            for user_id in disabled_ids:
                with contextlib.suppress(Exception):
                    self.sync_user(user_id)
            raise RuntimeError("无法安全停用全部目标 Key；本次批量修改未执行") from error
        assignments = ",".join(f"{field}=?" for field in requested)
        try:
            with self.db.connect() as connection:
                connection.execute(
                    f"UPDATE users SET {assignments} WHERE role='user' AND id IN ({placeholders})",
                    [*requested.values(), *user_ids],
                )
        except Exception:
            for user_id in disabled_ids:
                with contextlib.suppress(Exception):
                    self.sync_user(user_id)
            raise
        failed: list[dict[str, str]] = []
        for row in rows:
            try:
                self.sync_user(str(row["id"]))
            except RuntimeError as error:
                failed.append(
                    {"user_id": str(row["id"]), "email": str(row["email"]), "error": str(error)}
                )
        return {
            "updated": len(rows),
            "synced": len(rows) - len(failed),
            "failed": failed,
            "changes": requested,
            "actor": actor,
        }

    def update_user(self, payload: dict[str, Any], actor: str) -> None:
        user_id = str(payload.get("user_id", ""))
        status = str(payload.get("status", "active"))
        if status not in {"active", "disabled"}:
            raise ValueError("invalid user status")
        balance_delta = money_to_micros(payload.get("balance_delta", "0"))
        raw_group_ids = payload.get("group_ids", [])
        if not isinstance(raw_group_ids, list):
            raise ValueError("invalid groups")
        group_ids = list(dict.fromkeys(str(item) for item in raw_group_ids))
        grant_tokens = int(payload.get("grant_tokens", 0) or 0)
        disable_active_grants = payload.get("disable_active_grants") is True
        grant_reset = str(payload.get("grant_reset", "none"))
        if grant_tokens:
            raise ValueError("Token 赠额已停用，请直接调整用户金额余额")
        if grant_reset not in {"none", "daily", "weekly", "monthly"}:
            raise ValueError("invalid token grant")
        model_id = str(payload.get("grant_model_id", "")) or None
        grant_reset_time = str(
            payload.get(
                "grant_reset_time",
                self.db.settings().get("default_quota_reset_time", "00:00"),
            )
        )
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", grant_reset_time):
            raise ValueError("invalid grant reset time")
        stamp = now()
        with self.db.connect() as connection:
            user = connection.execute("SELECT * FROM users WHERE id=? AND role='user'", (user_id,)).fetchone()
            if not user:
                raise ValueError("user not found")
            max_sessions = normalize_max_sessions(
                payload.get("max_sessions", user["max_sessions"])
            )
            rpm = normalize_request_limit(
                payload.get("requests_per_minute", user["requests_per_minute"]),
                "每分钟请求数",
            )
            rpd = normalize_request_limit(
                payload.get("requests_per_day", user["requests_per_day"]),
                "每日请求数",
            )
            if group_ids:
                placeholders = ",".join("?" for _ in group_ids)
                found = int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM user_groups WHERE id IN ({placeholders})", group_ids
                    ).fetchone()[0]
                )
                if found != len(group_ids):
                    raise ValueError("group does not exist")
            if model_id and not connection.execute(
                "SELECT 1 FROM published_models WHERE id=?", (model_id,)
            ).fetchone():
                raise ValueError("grant model does not exist")
        if user["api_key_id"]:
            # 修改状态、用户组、赠额和余额期间让旧 Key 保持故障关闭；重同步失败后
            # 继续禁用。
            self.omni.activate_key(str(user["api_key_id"]), False)
        try:
            with self.db.connect() as connection:
                connection.execute(
                    "UPDATE users SET status=?,max_sessions=?,requests_per_minute=?,requests_per_day=? WHERE id=?",
                    (status, max_sessions, rpm, rpd, user_id),
                )
                connection.execute("DELETE FROM user_group_members WHERE user_id=?", (user_id,))
                for group_id in group_ids:
                    connection.execute(
                        "INSERT INTO user_group_members(user_id,group_id,created_at) VALUES(?,?,?)",
                        (user_id, group_id, stamp),
                    )
                account = connection.execute("SELECT * FROM billing_accounts WHERE user_id=?", (user_id,)).fetchone()
                balance = int(account["balance_micros"]) if account else 0
                after = balance + balance_delta
                connection.execute(
                    "INSERT INTO billing_accounts(user_id,balance_micros,suspended,updated_at) VALUES(?,?,0,?) ON CONFLICT(user_id) DO UPDATE SET balance_micros=excluded.balance_micros,updated_at=excluded.updated_at",
                    (user_id, after, stamp),
                )
                if balance_delta:
                    connection.execute(
                        "INSERT INTO balance_transactions(user_id,kind,amount_micros,balance_after_micros,actor,note,created_at) VALUES(?,?,?,?,?,?,?)",
                        (user_id, "adjustment", balance_delta, after, actor, str(payload.get("note", "Admin adjustment")), stamp),
                    )
                if disable_active_grants:
                    connection.execute(
                        "UPDATE token_grants SET status='disabled',updated_at=? "
                        "WHERE user_id=? AND status='active'",
                        (stamp, user_id),
                    )
        except Exception:
            # SQLite 已回滚，应恢复最后提交的策略，不能让配置正确的用户保持禁用。
            with contextlib.suppress(Exception):
                self.sync_user(user_id)
            raise
        self.sync_user(user_id)

    def save_group(self, payload: dict[str, Any]) -> str:
        group_id = str(payload.get("id", "")).strip() or str(uuid.uuid4())
        name = normalize_group_name(str(payload.get("name", "")))
        status = str(payload.get("status", "active"))
        if status not in {"active", "disabled"}:
            raise ValueError("invalid group status")
        with self.db.connect() as connection:
            existing = connection.execute(
                "SELECT id,name FROM user_groups WHERE id<>?", (group_id,)
            ).fetchall()
        if any(normalize_group_name(row["name"]).casefold() == name.casefold() for row in existing):
            raise ValueError("用户组名称已存在")
        stamp = now()
        self.quiesce_all_users()
        try:
            with self.db.connect() as connection:
                connection.execute(
                    "INSERT INTO user_groups(id,name,description,status,created_at,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,description=excluded.description,status=excluded.status,updated_at=excluded.updated_at",
                    (group_id, name, str(payload.get("description", "")), status, stamp, stamp),
                )
        except Exception as error:
            if not self.db.is_integrity_error(error):
                with contextlib.suppress(Exception):
                    self.sync_all_users()
                raise
            with contextlib.suppress(Exception):
                self.sync_all_users()
            if "user_groups.name" in str(error) or "name" in str(error).lower():
                raise ValueError("用户组名称已存在") from error
            raise
        self.sync_all_users()
        return group_id

    def background_tick(self) -> None:
        # 同时迁移旧 OmniRoute Token 限制；该限制曾错误地把促销赠额与现金计费耦合。
        try:
            self.sync_all_users()
        except Exception as error:
            print(
                f"[account-portal] permission sync warning: {error}",
                file=sys.stderr,
                flush=True,
            )
        with self.db.connect() as connection:
            due = connection.execute(
                "SELECT id FROM published_models WHERE status='published' AND upstream_free=1 AND (last_health_at IS NULL OR last_health_at<?)",
                (now() - 900,),
            ).fetchall()
        for row in due:
            try:
                self.test_published_model(row["id"])
            except Exception as error:
                print(
                    f"[account-portal] free-model health warning "
                    f"(model={row['id']}): {error}",
                    file=sys.stderr,
                    flush=True,
                )


class PortalHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class PortalServer:
    def __init__(self, config: Config):
        self.config = config
        self.db = Database(config)
        self.db.initialize()
        self.database_migration = DatabaseMigrationManager(self.db)
        self.omni = OmniRouteClient(config)
        self.workflow = WorkflowClient()
        self.models = ModelDeploymentClient()
        self.control = PortalControlPlane(config, self.db, self.omni)
        self.monitor = SystemMonitor()
        # 首次升级进程可能仍是上一版本升级器；修改 OmniRoute 或任一 SQLite
        # 数据库前，先等待其健康验收和文件回滚窗口结束，避免控制面升级失败后
        # 留下只完成一半的 Responses API 路由迁移。
        self.route_migration_due = (
            time.monotonic() + PUBLIC_COMBO_MIGRATION_DELAY_SECONDS
        )
        self.route_migration_finished = False
        try:
            self.control.prepare_public_combo_migration_backup()
            self.control.seed_managed_model()
            self.db.finalize_legacy_billing_migration()
        except Exception as error:
            print(
                f"[account-portal] managed-model seed warning: {error}",
                file=sys.stderr,
                flush=True,
            )
        self.stop_event = threading.Event()
        self.httpd = PortalHTTPServer((config.bind, config.port), PortalHandler)
        self.httpd.app = self  # type: ignore[attr-defined]

    def reconcile_public_routes_after_acceptance(self) -> None:
        current = time.monotonic()
        if current < self.route_migration_due:
            return
        if self.route_migration_finished:
            try:
                status = self.control.public_combo_route_status()
            except Exception as error:
                self.route_migration_due = current + PUBLIC_COMBO_MIGRATION_RETRY_SECONDS
                print(
                    f"[account-portal] public combo audit warning: {error}",
                    file=sys.stderr,
                    flush=True,
                )
                return
            if status["ready"]:
                self.route_migration_due = current + PUBLIC_COMBO_AUDIT_SECONDS
                return
            # 来源 Worker 集合或路由策略在上次成功迁移后变化；无需等待再次升级，
            # 立即修复公开镜像。
            self.route_migration_finished = False
        try:
            route_migration = self.control.reconcile_public_combo_routes()
        except Exception as error:
            # 保留旧路由继续服务并稍后重试；备份过程会在修改任何网关路由前故障关闭。
            self.route_migration_due = (
                time.monotonic() + PUBLIC_COMBO_MIGRATION_RETRY_SECONDS
            )
            print(
                f"[account-portal] delayed public combo migration warning: {error}",
                file=sys.stderr,
                flush=True,
            )
            return
        # 单模型失败不代表迁移完成。若在此标记全部完成，/v1/responses 会永久把
        # 缺失的裸 Combo 重写为 codex/<public-id>。门户在有限延迟后重试期间，
        # Chat 流量继续走旧路由。
        self.route_migration_finished = route_migration["failed"] == 0
        self.route_migration_due = time.monotonic() + (
            PUBLIC_COMBO_AUDIT_SECONDS
            if self.route_migration_finished
            else PUBLIC_COMBO_MIGRATION_RETRY_SECONDS
        )
        if route_migration["migrated"] or route_migration["failed"]:
            print(
                "[account-portal] public combo reconciliation: "
                f"migrated={route_migration['migrated']}, "
                f"unchanged={route_migration['unchanged']}, "
                f"failed={route_migration['failed']}",
                flush=True,
            )

    def maintenance_loop(self) -> None:
        if self.stop_event.wait(2):
            return
        while not self.stop_event.is_set():
            try:
                self.reconcile_public_routes_after_acceptance()
                self.control.background_tick()
            except Exception as error:
                print(f"[account-portal] maintenance warning: {error}", file=sys.stderr, flush=True)
            self.stop_event.wait(5 if not self.route_migration_finished else 60)

    def billing_loop(self) -> None:
        """Settle completed calls promptly while `/v1` stays gateway-direct."""
        interval = max(1, min(30, env_int("ACCOUNT_BILLING_INTERVAL", 2)))
        if self.stop_event.wait(interval):
            return
        while not self.stop_event.is_set():
            try:
                self.control.reconcile_usage()
            except Exception as error:
                print(
                    f"[account-portal] usage reconciliation warning: {error}",
                    file=sys.stderr,
                    flush=True,
                )
            self.stop_event.wait(interval)

    def serve(self) -> None:
        print(f"[account-portal] listening on {self.config.bind}:{self.config.port}", flush=True)
        threading.Thread(target=self.maintenance_loop, name="portal-maintenance", daemon=True).start()
        threading.Thread(target=self.billing_loop, name="portal-billing", daemon=True).start()
        try:
            self.httpd.serve_forever(poll_interval=0.5)
        finally:
            self.stop_event.set()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        nargs="?",
        choices=(
            "serve",
            "check-config",
            "set-admin-username",
            "reset-admin-password",
            "dump-config",
            "public-route-status",
            "reconcile-public-routes",
        ),
        default="serve",
    )
    parser.add_argument(
        "--show-secrets",
        action="store_true",
        help="include persisted SMTP credentials in dump-config output",
    )
    args = parser.parse_args()
    config = Config.from_env()
    if args.command == "check-config":
        print(json.dumps({"ok": True, "db": str(config.db_path), "registration": config.initial_registration}))
        return
    if args.command == "set-admin-username":
        database = Database(config)
        database.initialize()
        with database.connect() as connection:
            changed = connection.execute(
                "UPDATE users SET login_name=? WHERE role='admin'",
                (config.admin_username,),
            ).rowcount
        if changed != 1:
            raise SystemExit("expected exactly one portal administrator")
        database.audit(
            "system",
            "admin.username.updated",
            config.admin_username,
            "success",
            "local",
        )
        print("portal administrator username updated")
        return
    if args.command == "reset-admin-password":
        if not config.admin_password:
            raise SystemExit("ACCOUNT_ADMIN_PASSWORD is required")
        database = Database(config)
        database.initialize()
        with database.connect() as connection:
            changed = connection.execute(
                "UPDATE users SET password_hash=? WHERE role='admin'",
                (hash_admin_password(config.admin_password),),
            ).rowcount
        if changed != 1:
            raise SystemExit("expected exactly one portal administrator")
        database.audit(
            "system",
            "admin.password.reset",
            config.admin_username,
            "success",
            "local",
        )
        print("portal administrator password updated")
        return
    if args.command == "dump-config":
        database = Database(config)
        print(
            json.dumps(
                database.recovery_inventory(show_secrets=args.show_secrets),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return
    if args.command in {"public-route-status", "reconcile-public-routes"}:
        database = Database(config)
        database.initialize()
        control = PortalControlPlane(config, database, OmniRouteClient(config))
        result: dict[str, Any] = {}
        if args.command == "reconcile-public-routes":
            # stdout 保持供 llmctl 读取的机器格式，快照路径和逐模型诊断写入 stderr。
            with contextlib.redirect_stdout(sys.stderr):
                result["reconciliation"] = control.reconcile_public_combo_routes()
                if result["reconciliation"]["failed"] == 0:
                    result["permission_sync"] = control.sync_all_users()
                else:
                    result["permission_sync"] = {"synced": 0, "failed": 0}
        result["status"] = control.public_combo_route_status()
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        failed = int(result.get("reconciliation", {}).get("failed", 0))
        permission_failed = int(result.get("permission_sync", {}).get("failed", 0))
        if failed or permission_failed or not result["status"]["ready"]:
            raise SystemExit(2)
        return
    PortalServer(config).serve()


if __name__ == "__main__":
    main()
