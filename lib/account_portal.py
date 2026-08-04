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
import datetime
import hashlib
import hmac
import html
import http.cookies
import http.server
import ipaddress
import json
import mimetypes
import os
import pathlib
import re
import secrets
import shlex
import signal
import smtplib
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


APP_VERSION = "3.3.0"
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
    """Convert a raw token entitlement to cash at one conservative unit price.

    Existing LLMCtl grants were consumed against the most expensive token class
    first.  Using the highest current model price therefore preserves at least
    the purchasing power of every remaining grant during the one-time cash
    migration.  As with request billing, fractional micro-dollars round up.
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
    """Credit the configured one-time welcome balance idempotently."""
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
    """Undo a provisioning credit when the external API-key transaction fails."""
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
        # A manually corrupted SQLite value must not become an unsafe link.
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
            # Older installs created blank public origins whenever registration
            # started disabled. Repair those values so SMTP and later
            # registration changes are not blocked by unrelated empty fields.
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
                # Existing keys predate this control and remain unlimited until
                # an administrator explicitly changes them. New registrations
                # use default_max_sessions (1 by default).
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
            # Historical rows only stored the wallet debit. Reconstruct the
            # list-price charge from the immutable price snapshot so upgraded
            # installations immediately show an honest billing breakdown.
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
        """Convert all remaining promotional tokens to cash exactly once.

        The migration is deliberately transactional and fail-closed.  If any
        active grant cannot be valued, no grant is consumed.  A unique source
        reference protects balances if initialization is retried.
        """
        stamp = now()
        grants = connection.execute(
            "SELECT * FROM token_grants WHERE status='active' AND tokens_remaining>0 "
            "ORDER BY created_at,id"
        ).fetchall()
        public_rate_row = connection.execute(
            "SELECT MAX(MAX(input_price_micros,output_price_micros,"
            "cached_price_micros,reasoning_price_micros)) rate "
            "FROM published_models WHERE status='published'"
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
                    "SELECT MAX(input_price_micros,output_price_micros,"
                    "cached_price_micros,reasoning_price_micros) rate "
                    "FROM published_models WHERE id=?",
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
            # initialize() inserted a provisional value for the new setting.
            # Remove it so a later retry can still recognize and convert the
            # legacy registration policy after model pricing becomes available.
            connection.execute(
                "DELETE FROM settings WHERE key='default_welcome_balance'"
            )
        if not blocked:
            # Retire every legacy source only after all grants were valued.
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
        """Return the current persisted portal state for `llmctl info`.

        This intentionally reads the portal-owned SQLite database instead of
        repeating installation-time environment defaults: administrators can
        change SMTP and registration settings from the Vue portal later.
        """
        if not self.config.db_path.is_file():
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
            integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        if not show_secrets and settings.get("smtp_password"):
            settings["smtp_password"] = "<redacted>"
        stat = self.config.db_path.stat()
        return {
            "version": APP_VERSION,
            "database": {
                "path": str(self.config.db_path),
                "bytes": stat.st_size,
                "mode": oct(stat.st_mode & 0o777),
                "quick_check": integrity,
            },
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
                # OmniRoute interprets empty allowlists as unrestricted. Start
                # closed and let the portal publish the effective user/group
                # policy before the plaintext key is returned to the user.
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
            # Older LLMCtl installations started OmniRoute with key reveal
            # disabled. The native flag is hot-reloadable, so an upgraded
            # portal can repair that state without restarting the gateway or
            # any GPU worker.
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

        # Fail before mutating provider state if an administrator already owns
        # one of the deterministic combo names.  This keeps an explicit sync
        # transactional from the operator's point of view: a naming conflict
        # cannot leave a new node/connection/model set behind.
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
            # Thinking-only output can look empty to an OpenAI-compatible
            # gateway. Health checks need a short final answer, not a reasoning
            # trace, so explicitly use both supported thinking-off controls.
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


class PortalHandler(http.server.BaseHTTPRequestHandler):
    server_version = "LLMCtlAccountPortal/2"

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
                            "setup_command": "llmctl workflow init",
                        },
                    )
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
                # save_group owns the fail-closed quiesce/mutate/resync cycle.
                # Repeating it here needlessly disables every user key twice.
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
            self.app.db.audit(user_identity(user), path.removeprefix("/portal-api/"), str(payload.get("id", "")), "failed", self.remote_addr(), str(error))
            self.json_response(400 if isinstance(error, ValueError) else 502, {"error": str(error)})
            return
        # Never persist plaintext credentials in the audit ledger. The event
        # proves that a reveal/rotation happened without copying the secret.
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
            # Provisioning spans the portal DB and OmniRoute. Restore the
            # pending registration state if any later permission sync fails so
            # a transient gateway error never consumes the verification link
            # or leaves an active account backed by a deleted key.
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


class PortalControlPlane:
    """Business policy layered on OmniRoute's native routing and enforcement APIs."""

    def __init__(self, config: Config, db: Database, omni: OmniRouteClient):
        self.config, self.db, self.omni = config, db, omni
        self.lock = threading.RLock()
        self.usage_reconciled_at: dict[str, int] = {}
        self.free_visibility_reconciled_at = 0

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
                            member.get("comboId", member.get("combo_id", member.get("id", "")))
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
        # Permission publication, usage settlement, and administrator policy
        # changes share one lock so a maintenance tick cannot re-enable a key
        # with a stale policy during a fail-closed update.
        with self.lock:
            self._sync_user(user_id)

    def _sync_user(self, user_id: str) -> None:
        with self.db.connect() as connection:
            user = connection.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not user or not user["api_key_id"]:
            return
        # Older LLMCtl releases mirrored promotional grants into an OmniRoute
        # global token limit. That made a positive cash balance unusable as soon
        # as the grant was exhausted. Retire that legacy hard limit before
        # publishing the current LLMCtl billing policy. The key stays disabled
        # if either deletion or the following policy sync fails.
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
            # Only authorize the public ID. OmniRoute resolves the portal-owned
            # combo mapping/model alias after its API-key policy check. Adding
            # source IDs here would let users bypass the administrator's public
            # naming and access policy by calling the underlying route directly.
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
            # Restore the last committed effective policy when quiescing cannot
            # complete. The requested policy mutation has not started yet.
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
            # Free providers can require streaming, provider-specific endpoints,
            # capability negotiation, or a connection selected by OmniRoute.
            # Reuse its native dashboard probe so the portal and native UI have
            # one testing contract instead of two subtly different adapters.
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
                if not source_ref:
                    combo = next(
                        (item for item in self.omni.combos() if str(item.get("name", "")) == source_model),
                        None,
                    )
                    source_ref = str(combo.get("id", "")) if combo else ""
                if not source_ref:
                    raise ValueError("combo id is required")
                reuse_id = old_mapping_id if old_mapping_kind == "combo" else ""
                mapping_id = self.omni.set_combo_mapping(
                    public_id, source_ref, reuse_id, status == "published"
                )
                mapping_kind = "combo"
                mutated_mapping = bool(reuse_id)
                created_mapping = not mutated_mapping
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
            # Remove a superseded alias/mapping only after the new OmniRoute
            # route, portal policy and user permissions have all committed.
            if old_mapping_kind == "combo" and old_mapping_id and (
                mapping_kind != "combo" or mapping_id != old_mapping_id
            ):
                with contextlib.suppress(Exception):
                    self.omni.delete_combo_mapping(old_mapping_id)
            elif old_mapping_kind == "alias" and old_public_id and (
                mapping_kind != "alias" or old_public_id != public_id
            ):
                with contextlib.suppress(Exception):
                    self.omni.delete_model_alias(old_public_id)
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
                    if mapping_kind == "combo":
                        self.omni.delete_combo_mapping(mapping_id)
                    elif mapping_kind == "alias":
                        self.omni.delete_model_alias(public_id)
            elif mutated_mapping and existing:
                # Restore the last committed OmniRoute mapping when live test,
                # policy validation, or SQLite persistence fails.
                with contextlib.suppress(Exception):
                    if old_mapping_kind == "combo":
                        self.omni.set_combo_mapping(
                            old_public_id,
                            old_source_ref,
                            old_mapping_id,
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
            # Free upstreams can disappear without notice. Stop every portal
            # key before withdrawing the shared route so no request can slip
            # through between the health transition and permission refresh.
            self.quiesce_all_users()
            try:
                if model["mapping_kind"] == "combo" and model["mapping_id"]:
                    self.omni.set_combo_mapping(
                        model["public_model_id"], model["source_ref"], model["mapping_id"], False
                    )
                elif model["mapping_kind"] == "alias":
                    self.omni.delete_model_alias(model["public_model_id"])
            except Exception:
                # Keys intentionally remain disabled when native withdrawal
                # cannot be proven. An administrator can repair and resync.
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
        # Admin-triggered reconciliation and the maintenance thread may run at
        # the same time. Serialize the complete fetch/ledger/key-sync cycle.
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
        # Legacy rows may refer to a model that has since been disabled and has
        # no active replacement. Keep that exact historical identity rather
        # than inventing a public alias.
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
                # Financial amounts and the immutable price snapshot are never
                # repriced; this only repairs the public model attribution.
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
                # Positive-balance settlement does not change effective
                # permissions, so it must never toggle the externally visible
                # key. If the debit exhausts the balance, sync_user publishes
                # the reduced allowlist and active state in one OmniRoute PATCH.
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
        rows = connection.execute(
            f"""WITH buckets(bucket_index,start_at,end_at) AS (VALUES {values})
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

        # A provider identifier is safe to label only when it identifies one
        # combo target.  Shared providers must be distinguished by connection.
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
                # Popen succeeded but the durable run record did not.  Never leave an
                # untracked benchmark consuming the gateway/GPU in the background.
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
            # The control plane remains useful for account, SMTP, ledger, and
            # audit recovery while the data plane is temporarily unavailable.
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
            # Keep the old key fail-closed while status, groups, grants and
            # balance are changed. A failed resync leaves it disabled.
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
            # SQLite rolled back; restore the last committed policy rather than
            # leaving a correctly configured user disabled.
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
        except sqlite3.IntegrityError as error:
            with contextlib.suppress(Exception):
                self.sync_all_users()
            if "user_groups.name" in str(error):
                raise ValueError("用户组名称已存在") from error
            raise
        except Exception:
            with contextlib.suppress(Exception):
                self.sync_all_users()
            raise
        self.sync_all_users()
        return group_id

    def background_tick(self) -> None:
        # This also migrates legacy OmniRoute token limits that incorrectly
        # coupled promotional grants to the cash billing path.
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
        self.omni = OmniRouteClient(config)
        self.workflow = WorkflowClient()
        self.control = PortalControlPlane(config, self.db, self.omni)
        try:
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

    def maintenance_loop(self) -> None:
        if self.stop_event.wait(2):
            return
        while not self.stop_event.is_set():
            try:
                self.control.background_tick()
            except Exception as error:
                print(f"[account-portal] maintenance warning: {error}", file=sys.stderr, flush=True)
            self.stop_event.wait(60)

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
    PortalServer(config).serve()


if __name__ == "__main__":
    main()
