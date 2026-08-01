#!/usr/bin/env python3
"""Render and reconcile LLMCtl gateway configurations.

The renderer is deliberately dependency-free so it can run on a minimal
Ubuntu host. Secrets stay in the root-only environment file; generated
configuration refers to environment variables rather than embedding them.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import pathlib
import tempfile
import urllib.error
import urllib.request
from typing import Any, Iterable


MANAGED_TAG = "llmctl-managed"
MANAGED_TOKEN = "llmctl-default"


def required_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(f"missing required environment variable: {name}")
    return value


def parse_worker_ids(value: str) -> list[int]:
    result: list[int] = []
    seen: set[int] = set()
    for raw in value.split(","):
        raw = raw.strip()
        if not raw:
            continue
        if not raw.isdigit():
            raise SystemExit(f"invalid worker id: {raw}")
        worker_id = int(raw)
        if worker_id not in seen:
            result.append(worker_id)
            seen.add(worker_id)
    if not result:
        raise SystemExit("at least one worker id is required")
    return result


def atomic_write(path: pathlib.Path, content: str, mode: int = 0o640) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def litellm_config(worker_ids: Iterable[int]) -> str:
    model = required_env("SERVED_MODEL_NAME")
    base_port = int(required_env("WORKER_BASE_PORT"))
    max_seqs = int(required_env("MAX_NUM_SEQS"))
    max_len = int(required_env("MAX_MODEL_LEN"))
    strategy = required_env("ROUTING_STRATEGY")
    lines = ["model_list:"]
    for worker_id in worker_ids:
        lines.extend(
            [
                f'  - model_name: "{model}"',
                "    litellm_params:",
                f'      model: "hosted_vllm/{model}"',
                f'      api_base: "http://127.0.0.1:{base_port + worker_id}/v1"',
                '      api_key: "os.environ/BACKEND_API_KEY"',
                f"      max_parallel_requests: {max_seqs}",
                "      timeout: 7200",
                "    model_info:",
                f'      id: "llm-instance-{worker_id}"',
                '      mode: "chat"',
                f"      max_input_tokens: {max_len}",
                f"      max_output_tokens: {max_len}",
            ]
        )
    lines.extend(
        [
            "router_settings:",
            f'  routing_strategy: "{strategy}"',
            "  num_retries: 1",
            "  timeout: 7200",
            "  allowed_fails: 1",
            "  cooldown_time: 30",
            "  retry_after: 1",
            "litellm_settings:",
            "  drop_params: false",
            "  turn_off_message_logging: true",
            "  request_timeout: 7200",
            "general_settings:",
            '  master_key: "os.environ/LITELLM_MASTER_KEY"',
            '  database_url: "os.environ/DATABASE_URL"',
            "  store_model_in_db: false",
            "  disable_spend_logs: false",
        ]
    )
    return "\n".join(lines) + "\n"


def postgres_store(max_idle: int, max_open: int) -> dict[str, Any]:
    return {
        "enabled": True,
        "type": "postgres",
        "config": {
            "host": "127.0.0.1",
            "port": required_env("GATEWAY_DB_PORT"),
            "user": "env.POSTGRES_USER",
            "password": "env.POSTGRES_PASSWORD",
            "db_name": required_env("POSTGRES_DB"),
            "ssl_mode": "disable",
            "max_idle_conns": max_idle,
            "max_open_conns": max_open,
        },
    }


def bifrost_config(worker_ids: Iterable[int]) -> str:
    model = required_env("SERVED_MODEL_NAME")
    base_port = int(required_env("WORKER_BASE_PORT"))
    keys = []
    for worker_id in worker_ids:
        keys.append(
            {
                "id": f"llmctl-worker-{worker_id}",
                "name": f"LLMCtl worker {worker_id}",
                "value": "env.BACKEND_API_KEY",
                "models": [model],
                "weight": 1.0,
                "vllm_key_config": {
                    "url": f"http://127.0.0.1:{base_port + worker_id}",
                    "model_name": model,
                },
            }
        )
    config: dict[str, Any] = {
        "$schema": "https://www.getbifrost.ai/schema",
        "version": 2,
        "source_of_truth": "config.json",
        "client": {
            "allowed_origins": ["*"],
            "disable_content_logging": True,
            "drop_excess_requests": False,
            "enable_logging": True,
            "enforce_auth_on_inference": True,
            "initial_pool_size": 64,
            "log_retention_days": 30,
            "max_request_body_size_mb": 100,
        },
        "encryption_key": "env.BIFROST_ENCRYPTION_KEY",
        "config_store": postgres_store(5, 50),
        "logs_store": {
            **postgres_store(10, 100),
            "retention_days": 30,
        },
        "providers": {"vllm": {"keys": keys}},
        "governance": {
            "auth_config": {
                "admin_username": "env.UI_USERNAME",
                "admin_password": "env.UI_PASSWORD",
                "is_enabled": True,
                # Authorization remains available for the OpenAI-style VK.
                "disable_auth_on_inference": True,
            },
            "virtual_keys": [
                {
                    "id": MANAGED_TOKEN,
                    "name": "LLMCtl default API key",
                    "value": "env.GATEWAY_API_KEY",
                    "is_active": True,
                    "provider_configs": [
                        {
                            "provider": "vllm",
                            "key_ids": ["*"],
                            "allowed_models": [model],
                            "weight": 1.0,
                        }
                    ],
                }
            ],
        },
        "plugins": [
            {
                "name": "governance",
                "enabled": True,
                "config": {"is_vk_mandatory": True},
            }
        ],
    }
    return json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def newapi_plan(worker_ids: Iterable[int]) -> str:
    """Persist an auditable, non-secret desired-state snapshot."""
    model = required_env("SERVED_MODEL_NAME")
    base_port = int(required_env("WORKER_BASE_PORT"))
    data = {
        "gateway": "newapi",
        "managed_tag": MANAGED_TAG,
        "model": model,
        "retry_times": 1,
        "channels": [
            {
                "name": f"LLMCtl worker {worker_id}",
                "base_url": f"http://127.0.0.1:{base_port + worker_id}",
                "weight": 100,
                "priority": 0,
            }
            for worker_id in worker_ids
        ],
    }
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


class NewAPIClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.access_token = ""
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=data, headers=headers, method=method
        )
        try:
            with self.opener.open(request, timeout=20) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            raw = error.read()
            detail = raw.decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"New API {method} {path} returned HTTP {error.code}: {detail}") from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"New API {method} {path} failed: {error.reason}") from error
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as error:
            raise RuntimeError(f"New API {method} {path} returned invalid JSON") from error
        if not isinstance(parsed, dict):
            raise RuntimeError(f"New API {method} {path} returned a non-object response")
        if parsed.get("success") is False:
            raise RuntimeError(
                f"New API {method} {path} failed: {parsed.get('message', 'unknown error')}"
            )
        return parsed

    def setup(self) -> None:
        status = self.request("GET", "/api/setup")
        data = status.get("data") or {}
        if isinstance(data, dict) and data.get("status"):
            return
        password = required_env("UI_PASSWORD")
        self.request(
            "POST",
            "/api/setup",
            {
                "username": required_env("UI_USERNAME"),
                "password": password,
                "confirmPassword": password,
                "SelfUseModeEnabled": True,
                "DemoSiteEnabled": False,
            },
        )

    def login(self) -> None:
        response = self.request(
            "POST",
            "/api/user/login",
            {
                "username": required_env("UI_USERNAME"),
                "password": required_env("UI_PASSWORD"),
            },
        )
        data = response.get("data") or {}
        token = data.get("access_token") if isinstance(data, dict) else ""
        if not isinstance(token, str) or not token:
            raise RuntimeError("New API login did not return an access token")
        self.access_token = token


def response_items(response: dict[str, Any]) -> list[dict[str, Any]]:
    data: Any = response.get("data", [])
    if isinstance(data, dict):
        for key in ("items", "data", "records"):
            candidate = data.get(key)
            if isinstance(candidate, list):
                data = candidate
                break
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def update_env_file(path: pathlib.Path, updates: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        remaining = dict(updates)
        output: list[str] = []
        for line in lines:
            if "=" not in line or line.lstrip().startswith("#"):
                output.append(line)
                continue
            key = line.split("=", 1)[0]
            if key in remaining:
                output.append(f"{key}={remaining.pop(key)}")
            else:
                output.append(line)
        output.extend(f"{key}={value}" for key, value in remaining.items())
        content = "\n".join(output) + "\n"
        atomic_write(path, content, 0o600)
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def ensure_newapi_token(client: NewAPIClient, secrets_file: pathlib.Path, rotate: bool) -> str:
    tokens = response_items(client.request("GET", "/api/token/?p=0&page_size=100"))
    managed = [item for item in tokens if item.get("name") == MANAGED_TOKEN]
    if rotate or not managed:
        client.request(
            "POST",
            "/api/token/",
            {
                "name": MANAGED_TOKEN,
                "expired_time": -1,
                "unlimited_quota": True,
                "model_limits_enabled": True,
                "model_limits": required_env("SERVED_MODEL_NAME"),
                "group": "default",
            },
        )
        tokens = response_items(client.request("GET", "/api/token/?p=0&page_size=100"))
        managed = [item for item in tokens if item.get("name") == MANAGED_TOKEN]
    managed_ids = sorted(
        item["id"] for item in managed if isinstance(item.get("id"), int)
    )
    if not managed_ids:
        raise RuntimeError("New API managed token is missing")
    # New API token names are not unique. Selecting the newest ID lets an
    # interrupted rotation recover without deleting the last working token.
    token_id = managed_ids[-1]
    response = client.request("POST", f"/api/token/{token_id}/key")
    data = response.get("data") or {}
    key = data.get("key") if isinstance(data, dict) else ""
    if not isinstance(key, str) or len(key) < 16:
        raise RuntimeError("New API did not return the managed token key")
    if not key.startswith("sk-"):
        key = f"sk-{key}"
    update_env_file(
        secrets_file,
        {
            "GATEWAY_API_KEY": key,
            "LITELLM_MASTER_KEY": key,
            "NEWAPI_MANAGED_TOKEN_ID": str(token_id),
        },
    )
    for stale_id in managed_ids[:-1]:
        client.request("DELETE", f"/api/token/{stale_id}")
    return key


def reconcile_newapi(client: NewAPIClient, worker_ids: list[int], secrets_file: pathlib.Path) -> None:
    client.setup()
    client.login()
    client.request("PUT", "/api/option/", {"key": "RetryTimes", "value": 1})
    channels = response_items(client.request("GET", "/api/channel/?p=0&page_size=100"))
    previous_managed_ids = [
        channel["id"]
        for channel in channels
        if channel.get("tag") == MANAGED_TAG and isinstance(channel.get("id"), int)
    ]

    model = required_env("SERVED_MODEL_NAME")
    base_port = int(required_env("WORKER_BASE_PORT"))
    backend_key = required_env("BACKEND_API_KEY")
    for worker_id in worker_ids:
        client.request(
            "POST",
            "/api/channel/",
            {
                "mode": "single",
                "channel": {
                    "type": 1,
                    "key": backend_key,
                    "status": 1,
                    "name": f"LLMCtl worker {worker_id}",
                    "weight": 100,
                    "base_url": f"http://127.0.0.1:{base_port + worker_id}",
                    "models": model,
                    "group": "default",
                    "priority": 0,
                    "auto_ban": 0,
                    "tag": MANAGED_TAG,
                },
            },
        )
    # Preserve the last known-good routes until every replacement exists.
    for channel_id in previous_managed_ids:
        client.request("DELETE", f"/api/channel/{channel_id}")
    ensure_newapi_token(client, secrets_file, rotate=False)


def newapi_client() -> NewAPIClient:
    return NewAPIClient(required_env("GATEWAY_LOCAL_URL"))


def command_render(args: argparse.Namespace) -> None:
    workers = parse_worker_ids(args.worker_ids)
    output = pathlib.Path(args.output)
    if args.gateway == "litellm":
        content = litellm_config(workers)
    elif args.gateway == "bifrost":
        content = bifrost_config(workers)
    else:
        content = newapi_plan(workers)
    atomic_write(output, content)


def command_reconcile_newapi(args: argparse.Namespace) -> None:
    reconcile_newapi(
        newapi_client(), parse_worker_ids(args.worker_ids), pathlib.Path(args.secrets_file)
    )


def command_rotate_newapi(args: argparse.Namespace) -> None:
    client = newapi_client()
    client.login()
    ensure_newapi_token(client, pathlib.Path(args.secrets_file), rotate=True)


def command_newapi_admin(args: argparse.Namespace) -> None:
    client = newapi_client()
    client.login()
    payload: dict[str, Any] = {
        "username": required_env("UI_USERNAME"),
        "display_name": required_env("UI_USERNAME"),
    }
    updates: dict[str, str] = {}
    if args.action == "set-password":
        new_password = required_env("LLMCTL_NEW_PASSWORD")
        payload.update(
            {
                "original_password": required_env("UI_PASSWORD"),
                "password": new_password,
            }
        )
        updates["UI_PASSWORD"] = new_password
    else:
        new_username = required_env("LLMCTL_NEW_USERNAME")
        payload.update({"username": new_username, "display_name": new_username})
        updates["UI_USERNAME"] = new_username
    client.request("PUT", "/api/user/self", payload)
    update_env_file(pathlib.Path(args.secrets_file), updates)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    render = subparsers.add_parser("render")
    render.add_argument("--gateway", choices=("newapi", "litellm", "bifrost"), required=True)
    render.add_argument("--worker-ids", required=True)
    render.add_argument("--output", required=True)
    render.set_defaults(handler=command_render)

    reconcile = subparsers.add_parser("reconcile-newapi")
    reconcile.add_argument("--worker-ids", required=True)
    reconcile.add_argument("--secrets-file", required=True)
    reconcile.set_defaults(handler=command_reconcile_newapi)

    rotate = subparsers.add_parser("rotate-newapi-token")
    rotate.add_argument("--secrets-file", required=True)
    rotate.set_defaults(handler=command_rotate_newapi)

    admin = subparsers.add_parser("newapi-admin")
    admin.add_argument("action", choices=("set-password", "set-username"))
    admin.add_argument("--secrets-file", required=True)
    admin.set_defaults(handler=command_newapi_admin)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        args.handler(args)
    except RuntimeError as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
