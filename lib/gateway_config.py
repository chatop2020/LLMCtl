#!/usr/bin/env python3
"""Render and reconcile LLMCtl gateway configurations.

The renderer is deliberately dependency-free so it can run on a minimal
Ubuntu host. Secrets stay in the root-only environment file; generated
configuration refers to environment variables rather than embedding them.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import http.cookiejar
import json
import os
import pathlib
import shlex
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterable


MANAGED_TAG = "llmctl-managed"
MANAGED_TOKEN = "llmctl-default"
OMNIROUTE_MANAGED_KEY = "llmctl-management"
OMNIROUTE_DESCRIPTION = "Managed by LLMCtl. Do not edit worker targets manually."
# Keep OmniRoute in the routing plane and leave request scheduling to vLLM.  The
# upstream default queues behind a saturated Combo member for 30 seconds before
# trying the next member.  That produces a deterministic 30-second stall for
# multimodal requests even while another worker is idle.  OmniRoute currently
# accepts at most 20 in-flight requests per target and a minimum queue timeout of
# one second, so use those explicit bounds for every LLMCtl-owned connection and
# Combo.  vLLM's MAX_NUM_SEQS remains the active-sequence limit; extra in-flight
# requests wait in vLLM's scheduler instead of OmniRoute's opaque semaphore.
OMNIROUTE_INFLIGHT_PER_WORKER = 20
OMNIROUTE_QUEUE_TIMEOUT_MS = 1000


def worker_origin(worker_id: int) -> str:
    """Return the origin visible from the selected gateway container."""
    base_port = int(required_env("WORKER_BASE_PORT"))
    host = f"llm-worker-{worker_id}" if os.environ.get("DOCKER_NETWORK") else "127.0.0.1"
    return f"http://{host}:{base_port + worker_id}"


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
                f'      api_base: "{worker_origin(worker_id)}/v1"',
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
            "host": "llm-database" if os.environ.get("DOCKER_NETWORK") else "127.0.0.1",
            "port": "5432" if os.environ.get("DOCKER_NETWORK") else required_env("GATEWAY_DB_PORT"),
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
                    "url": worker_origin(worker_id),
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
                "base_url": worker_origin(worker_id),
                "weight": 100,
                "priority": 0,
            }
            for worker_id in worker_ids
        ],
    }
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def omniroute_plan(worker_ids: Iterable[int]) -> str:
    """Persist the desired OmniRoute graph without embedding credentials."""
    model = required_env("SERVED_MODEL_NAME")
    base_port = int(required_env("WORKER_BASE_PORT"))
    # MAX_NUM_SEQS limits active vLLM sequences, not HTTP requests admitted by
    # the gateway.  Capping OmniRoute at MAX_NUM_SEQS makes its own semaphore
    # queue first and invokes the upstream 30-second failover timeout.
    concurrency_per_worker = OMNIROUTE_INFLIGHT_PER_WORKER
    data = {
        "gateway": "omniroute",
        "managed_tag": MANAGED_TAG,
        "model": model,
        "strategy": "round-robin",
        # OmniRoute has a separate sticky round-robin batch setting whose
        # global default is greater than one.  It must be explicitly disabled
        # for independent vLLM replicas or a simultaneous burst will queue on
        # the first target before rotation advances.
        "sticky_round_robin_limit": 1,
        "concurrency_per_worker": concurrency_per_worker,
        "queue_timeout_ms": OMNIROUTE_QUEUE_TIMEOUT_MS,
        "supports_vision": os.environ.get("SUPPORTS_IMAGE_INPUT", "0") == "1",
        "workers": [
            {
                "id": worker_id,
                "node_name": f"LLMCtl worker {worker_id}",
                "prefix": f"llmctl-w{worker_id}",
                "base_url": f"{worker_origin(worker_id)}/v1",
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


class OmniRouteClient:
    """Minimal dashboard/API client with cookie isolation and no proxy use."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(self.cookies)
        )

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        bearer: str = "",
    ) -> dict[str, Any]:
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=data, headers=headers, method=method
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:800]
            raise RuntimeError(
                f"OmniRoute {method} {path} returned HTTP {error.code}: {detail}"
            ) from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"OmniRoute {method} {path} failed: {error.reason}") from error
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as error:
            raise RuntimeError(f"OmniRoute {method} {path} returned invalid JSON") from error
        if not isinstance(parsed, dict):
            raise RuntimeError(f"OmniRoute {method} {path} returned a non-object response")
        return parsed

    def login(self) -> None:
        self.request("POST", "/api/auth/login", {"password": required_env("UI_PASSWORD")})
        if not any(cookie.name == "auth_token" for cookie in self.cookies):
            raise RuntimeError("OmniRoute login did not establish a dashboard session")

    def management_key_works(self, key: str) -> bool:
        if not key:
            return False
        probe = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request(
            f"{self.base_url}/api/keys?limit=1",
            headers={"Accept": "application/json", "Authorization": f"Bearer {key}"},
        )
        try:
            with probe.open(request, timeout=10) as response:
                return response.status == 200
        except (urllib.error.HTTPError, urllib.error.URLError):
            return False


def response_list(response: dict[str, Any], *keys: str) -> list[dict[str, Any]]:
    for key in keys:
        value = response.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def ensure_omniroute_management_key(
    client: OmniRouteClient, secrets_file: pathlib.Path, rotate: bool = False
) -> str:
    current = os.environ.get("GATEWAY_API_KEY", "")
    if not rotate and client.management_key_works(current):
        return current
    existing = response_list(client.request("GET", "/api/keys?limit=1000"), "keys")
    created = client.request(
        "POST", "/api/keys", {"name": OMNIROUTE_MANAGED_KEY, "scopes": ["manage"]}
    )
    key_id, raw_key = str(created.get("id", "")), str(created.get("key", ""))
    if not key_id or len(raw_key) < 16 or not client.management_key_works(raw_key):
        if key_id:
            with contextlib.suppress(RuntimeError):
                client.request("DELETE", f"/api/keys/{urllib.parse.quote(key_id, safe='')}")
        raise RuntimeError("OmniRoute did not create a working LLMCtl management key")
    update_env_file(
        secrets_file,
        {
            "GATEWAY_API_KEY": raw_key,
            "LITELLM_MASTER_KEY": raw_key,
            "OMNIROUTE_MANAGED_KEY_ID": key_id,
        },
    )
    for item in existing:
        stale_id = str(item.get("id", ""))
        if item.get("name") == OMNIROUTE_MANAGED_KEY and stale_id and stale_id != key_id:
            with contextlib.suppress(RuntimeError):
                client.request("DELETE", f"/api/keys/{urllib.parse.quote(stale_id, safe='')}")
    return raw_key


def reconcile_omniroute(
    client: OmniRouteClient, worker_ids: list[int], secrets_file: pathlib.Path
) -> None:
    """Reconcile compatible nodes, connections, model metadata, and routing combo."""
    client.login()
    ensure_omniroute_management_key(client, secrets_file)
    model = required_env("SERVED_MODEL_NAME")
    base_port = int(required_env("WORKER_BASE_PORT"))
    backend_key = required_env("BACKEND_API_KEY")
    supports_vision = os.environ.get("SUPPORTS_IMAGE_INPUT", "0") == "1"
    concurrency_per_worker = OMNIROUTE_INFLIGHT_PER_WORKER

    nodes = response_list(client.request("GET", "/api/provider-nodes?limit=1000"), "nodes")
    connections = response_list(client.request("GET", "/api/providers?limit=1000"), "connections")
    desired_node_ids: set[str] = set()
    stale_connection_ids: set[str] = set()
    combo_models: list[dict[str, Any]] = []
    for worker_id in worker_ids:
        name = f"LLMCtl worker {worker_id}"
        prefix = f"llmctl-w{worker_id}"
        base_url = f"{worker_origin(worker_id)}/v1"
        matches = [node for node in nodes if node.get("name") == name]
        node: dict[str, Any]
        if matches:
            node = client.request(
                "PUT",
                f"/api/provider-nodes/{urllib.parse.quote(str(matches[0]['id']), safe='')}",
                {
                    "name": name,
                    "prefix": prefix,
                    "apiType": "chat",
                    "baseUrl": base_url,
                    "chatPath": "/chat/completions",
                    "modelsPath": "/models",
                },
            ).get("node", matches[0])
        else:
            response = client.request(
                "POST",
                "/api/provider-nodes",
                {
                    "name": name,
                    "prefix": prefix,
                    "apiType": "chat",
                    "type": "openai-compatible",
                    "baseUrl": base_url,
                    "chatPath": "/chat/completions",
                    "modelsPath": "/models",
                },
            )
            node = response.get("node", {})
        node_id = str(node.get("id", ""))
        if not node_id:
            raise RuntimeError(f"OmniRoute did not return the provider node for worker {worker_id}")
        desired_node_ids.add(node_id)

        connection_matches = [item for item in connections if item.get("provider") == node_id]
        if connection_matches:
            connection_id = str(connection_matches[0].get("id", ""))
            if not connection_id:
                raise RuntimeError(
                    f"OmniRoute returned an invalid connection for worker {worker_id}"
                )
            response = client.request(
                "PUT",
                f"/api/providers/{urllib.parse.quote(connection_id, safe='')}",
                {
                    "name": name,
                    "apiKey": backend_key,
                    "priority": 1,
                    "maxConcurrent": concurrency_per_worker,
                    "defaultModel": model,
                    "isActive": True,
                    "testStatus": "success",
                },
            )
            connection = response.get("connection", connection_matches[0])
            for duplicate in connection_matches[1:]:
                duplicate_id = str(duplicate.get("id", ""))
                if duplicate_id:
                    stale_connection_ids.add(duplicate_id)
        else:
            response = client.request(
                "POST",
                "/api/providers",
                {
                    "provider": node_id,
                    "apiKey": backend_key,
                    "name": name,
                    "priority": 1,
                    "maxConcurrent": concurrency_per_worker,
                    "testStatus": "success",
                    "defaultModel": model,
                },
            )
            connection = response.get("connection", {})
        connection_id = str(connection.get("id", ""))
        if not connection_id:
            raise RuntimeError(f"OmniRoute did not return the connection for worker {worker_id}")

        models = response_list(
            client.request(
                "GET", f"/api/provider-models?provider={urllib.parse.quote(node_id, safe='')}"
            ),
            "models",
        )
        existing_model = next(
            (item for item in models if item.get("id") == model or item.get("modelId") == model),
            None,
        )
        model_payload: dict[str, Any] = {
            "provider": node_id,
            "modelId": model,
            "modelName": model,
            "source": MANAGED_TAG,
            "apiFormat": "chat-completions",
            "supportedEndpoints": ["chat"],
            "targetFormat": "openai",
            "max_input_tokens": int(required_env("MAX_MODEL_LEN")),
            "max_output_tokens": int(required_env("MAX_MODEL_LEN")),
            "contextWindowOverride": int(required_env("MAX_MODEL_LEN")),
            "supportsVision": supports_vision,
        }
        if existing_model:
            # Reconcile mutable capability/context metadata as the selected
            # model plan changes; a stale manual row must not outlive it.
            client.request("PUT", "/api/provider-models", model_payload)
        else:
            client.request(
                "POST",
                "/api/provider-models",
                model_payload,
            )
        combo_models.append(
            {
                "kind": "model",
                "provider": node_id,
                "model": model,
                "connectionId": connection_id,
                "label": name,
            }
        )

    combos = response_list(client.request("GET", "/api/combos?limit=1000"), "combos")
    existing_combo = next((item for item in combos if item.get("name") == model), None)
    combo_payload: dict[str, Any] = {
        "name": model,
        "description": OMNIROUTE_DESCRIPTION,
        "models": combo_models,
        "strategy": "round-robin",
        "config": {
            "disableSessionStickiness": True,
            # This is distinct from conversation/session stickiness.  The
            # OmniRoute global default batches several successful requests on
            # one target.  Under a simultaneous benchmark burst that leaves
            # the RR counter unchanged until responses finish, concentrating
            # work on one or two GPUs.  One request per target restores true
            # eager rotation across every healthy LLMCtl worker.
            "stickyRoundRobinLimit": 1,
            "concurrencyPerModel": concurrency_per_worker,
            # OmniRoute 3.8.x defaults to 30 seconds.  Its API strips the old
            # queueDepth field on update, but queueTimeoutMs is supported and
            # keeps a saturated target from hiding an idle fallback worker.
            "queueTimeoutMs": OMNIROUTE_QUEUE_TIMEOUT_MS,
            "healthCheckEnabled": True,
            "maxRetries": 1,
            "failoverBeforeRetry": True,
        },
        "context_length": int(required_env("MAX_MODEL_LEN")),
    }
    if existing_combo:
        if existing_combo.get("description") != OMNIROUTE_DESCRIPTION:
            raise RuntimeError(
                f"OmniRoute combo '{model}' already exists and is not managed by LLMCtl"
            )
        combo_id = str(existing_combo.get("id", ""))
        client.request(
            "PUT", f"/api/combos/{urllib.parse.quote(combo_id, safe='')}", combo_payload
        )
    else:
        client.request("POST", "/api/combos", combo_payload)

    # Remove only LLMCtl-owned duplicates after the replacement combo is
    # committed.  This ordering preserves the last known-good route if any
    # earlier reconciliation step fails.
    for connection_id in sorted(stale_connection_ids):
        client.request(
            "DELETE", f"/api/providers/{urllib.parse.quote(connection_id, safe='')}"
        )
    for node in nodes:
        node_id = str(node.get("id", ""))
        if (
            str(node.get("name", "")).startswith("LLMCtl worker ")
            and node_id
            and node_id not in desired_node_ids
        ):
            client.request(
                "DELETE", f"/api/provider-nodes/{urllib.parse.quote(node_id, safe='')}"
            )


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
                output.append(f"{key}={shlex.quote(remaining.pop(key))}")
            else:
                output.append(line)
        output.extend(f"{key}={shlex.quote(value)}" for key, value in remaining.items())
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
                    "base_url": worker_origin(worker_id),
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
    elif args.gateway == "omniroute":
        content = omniroute_plan(workers)
    else:
        content = newapi_plan(workers)
    atomic_write(output, content)


def command_reconcile_newapi(args: argparse.Namespace) -> None:
    reconcile_newapi(
        newapi_client(), parse_worker_ids(args.worker_ids), pathlib.Path(args.secrets_file)
    )


def command_reconcile_omniroute(args: argparse.Namespace) -> None:
    reconcile_omniroute(
        OmniRouteClient(required_env("GATEWAY_LOCAL_URL")),
        parse_worker_ids(args.worker_ids),
        pathlib.Path(args.secrets_file),
    )


def command_rotate_omniroute(args: argparse.Namespace) -> None:
    client = OmniRouteClient(required_env("GATEWAY_LOCAL_URL"))
    client.login()
    ensure_omniroute_management_key(client, pathlib.Path(args.secrets_file), rotate=True)


def command_omniroute_admin(args: argparse.Namespace) -> None:
    client = OmniRouteClient(required_env("GATEWAY_LOCAL_URL"))
    client.login()
    if args.action != "set-password":
        raise RuntimeError("OmniRoute dashboard authentication does not use a username")
    new_password = required_env("LLMCTL_NEW_PASSWORD")
    client.request(
        "PATCH",
        "/api/settings",
        {
            "currentPassword": required_env("UI_PASSWORD"),
            "newPassword": new_password,
        },
    )
    update_env_file(
        pathlib.Path(args.secrets_file),
        {"UI_PASSWORD": new_password},
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
    render.add_argument(
        "--gateway", choices=("newapi", "litellm", "bifrost", "omniroute"), required=True
    )
    render.add_argument("--worker-ids", required=True)
    render.add_argument("--output", required=True)
    render.set_defaults(handler=command_render)

    reconcile = subparsers.add_parser("reconcile-newapi")
    reconcile.add_argument("--worker-ids", required=True)
    reconcile.add_argument("--secrets-file", required=True)
    reconcile.set_defaults(handler=command_reconcile_newapi)

    reconcile_omni = subparsers.add_parser("reconcile-omniroute")
    reconcile_omni.add_argument("--worker-ids", required=True)
    reconcile_omni.add_argument("--secrets-file", required=True)
    reconcile_omni.set_defaults(handler=command_reconcile_omniroute)

    rotate_omni = subparsers.add_parser("rotate-omniroute-key")
    rotate_omni.add_argument("--secrets-file", required=True)
    rotate_omni.set_defaults(handler=command_rotate_omniroute)

    omni_admin = subparsers.add_parser("omniroute-admin")
    omni_admin.add_argument("action", choices=("set-password",))
    omni_admin.add_argument("--secrets-file", required=True)
    omni_admin.set_defaults(handler=command_omniroute_admin)

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
