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
import io
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
import zipfile
from email.message import EmailMessage
from typing import Any
from xml.sax.saxutils import escape as xml_escape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


APP_VERSION = "3.6.1"
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
MODEL_REGISTRY_COMBO_MANAGED_DESCRIPTION = (
    "Managed by LLMCtl. Do not edit worker targets manually."
)
LLMCTL_MANAGED_COMBO_DESCRIPTIONS = frozenset(
    {PUBLIC_COMBO_MANAGED_DESCRIPTION, MODEL_REGISTRY_COMBO_MANAGED_DESCRIPTION}
)
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
    """规范化用户输入的登录标识，不把普通用户名误当成邮箱。"""
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
    """返回规范、可见且适合展示的 Unicode 用户组名称。"""
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
    """提取可展示的请求正文，不返回任意请求元数据。"""
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
    """从保留的 OpenAI 兼容响应制品中提取模型最终文本。"""
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
    """兼容不同 OmniRoute 数据版本，查找最终客户端响应制品。"""
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
    """按配置的服务时区返回下一个周期重置边界。"""
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
    """计算一次请求的费用，并保留历史重放所需的旧赠额算法。

    模型价格使用每百万 Token 的微美元表示。缓存、输入、输出和思考 Token
    可能使用不同价格，因此旧 Token 赠额不能直接按请求总量分摊。当前纯现金
    结算路径始终传入零；兼容分支只用于复现 3.0 之前的账本计算和迁移测试。
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
    """解析可选的正整数模型上限，非法输入必须明确失败。"""
    if value is None or str(value).strip() == "":
        return None
    try:
        result = int(str(value).strip())
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} 必须为正整数") from error
    if result <= 0 or result > maximum:
        raise ValueError(f"{label} 必须在 1-{maximum:,} 之间")
    return result


def max_output_tokens_limit() -> int:
    """读取门户与 vLLM 共用的单请求最大输出 Token。

    返回：
        当前控制面允许声明和保存的正整数输出上限。

    异常：
        ValueError: 环境配置不是正整数，或者超过平台固定的 32768 Token 边界。
    """

    return positive_int_or_none(
        os.environ.get("MAX_OUTPUT_TOKENS", "32768"),
        "MAX_OUTPUT_TOKENS",
        32768,
    ) or 32768


def portal_ui_url(value: str) -> str:
    """把门户公开来源规范为 Nginx 的 `/ui` 入口。"""
    value = value.rstrip("/")
    if value and not urllib.parse.urlsplit(value).path:
        return value + "/ui"
    return value


def normalize_public_origin(value: Any) -> str:
    """校验并规范化可选的外部发布来源地址。

    该设置刻意限定为来源地址而不是任意 URL，因为它会进入验证邮件和生成的
    API 示例。拒绝路径、凭据、控制字符、查询和片段，可以避免公网安装产生
    钓鱼链接或响应头注入风险。
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
    """规范化 OmniRoute 原生请求次数上限，零表示关闭限制。"""
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label}必须是整数") from error
    if not 0 <= result <= 10_000_000:
        raise ValueError(f"{label}必须在 0-10000000 之间")
    return result


def request_rate_limits(requests_per_minute: int, requests_per_day: int) -> list[dict[str, int]]:
    """构造 OmniRoute 按 Key 生效的固定窗口请求规则。

    该规则与 maxSessions 分开管理。maxSessions 表示会话共享信号，这里的规则
    限制实际 HTTP 请求次数。
    """
    rules: list[dict[str, int]] = []
    if requests_per_minute:
        rules.append({"limit": requests_per_minute, "window": 60})
    if requests_per_day:
        rules.append({"limit": requests_per_day, "window": 86_400})
    return rules


def effective_public_urls(config: "Config", settings: dict[str, str]) -> tuple[str, str]:
    """返回最终生效的门户地址和 API 来源地址。

    已配置的外部来源优先；空值继续使用现有访问地址，以兼容仅局域网部署和
    原地升级环境。
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

# 这些名称构成拆分后各模块共享的稳定基础契约；包含下划线前缀的兼容助手。
__all__ = [name for name in globals() if not name.startswith("__")]
