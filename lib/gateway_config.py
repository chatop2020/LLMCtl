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
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterable


MANAGED_TAG = "llmctl-managed"
MANAGED_TOKEN = "llmctl-default"
OMNIROUTE_MANAGED_KEY = "llmctl-management"
OMNIROUTE_DESCRIPTION = "Managed by LLMCtl. Do not edit worker targets manually."
OMNIROUTE_REASONING_RULE_DESCRIPTION = (
    "Managed by LLMCtl reasoning effort compatibility. Do not edit manually."
)
PORTAL_PUBLIC_COMBO_DESCRIPTION = "Managed by LLMCtl account portal public model"
LLMCTL_MANAGED_COMBO_DESCRIPTIONS = frozenset(
    {OMNIROUTE_DESCRIPTION, PORTAL_PUBLIC_COMBO_DESCRIPTION}
)
# OmniRoute 只负责路由，具体请求调度交给 vLLM。上游默认会在已饱和的 Combo
# 成员后排队 30 秒才尝试下一个成员，即使其他 Worker 空闲，多模态请求也会
# 稳定卡住 30 秒。OmniRoute 当前每个目标最多接受 20 个在途请求，队列超时
# 最低为 1 秒，因此对 LLMCtl 管理的 Connection 和 Combo 显式使用这两个边界。
# vLLM 的 MAX_NUM_SEQS 仍是活跃序列上限；额外请求在 vLLM 调度器内等待，
# 不进入 OmniRoute 不透明的 semaphore 队列。
OMNIROUTE_INFLIGHT_PER_WORKER = 20
OMNIROUTE_QUEUE_TIMEOUT_MS = 1000
MAX_OUTPUT_TOKENS_CEILING = 32768
OMNIROUTE_PAGE_LIMIT = 200
OMNIROUTE_REASONING_EFFORTS = frozenset(
    {"none", "low", "medium", "high", "xhigh", "max", "ultra"}
)
QWEN38_REASONING_EFFORT_ALIASES = {"high": "xhigh"}


class OmniRouteFeatureUnavailable(RuntimeError):
    """表示运行镜像缺少 LLMCtl 当前操作所需的可选 OmniRoute API。"""


def is_llmctl_managed_combo(combo: dict[str, Any] | None) -> bool:
    """判断 Combo 是否由模型注册表或账户门户任一 LLMCtl 控制面管理。"""

    return str((combo or {}).get("description", "")) in LLMCTL_MANAGED_COMBO_DESCRIPTIONS


def deployment_reasoning_effort_aliases(
    deployment_id: str, deployment: dict[str, Any]
) -> dict[str, str]:
    """返回单个部署需要由 OmniRoute 执行的推理等级兼容映射。

    注册表可以显式提供 `reasoning_effort_aliases`。既有 Qwen3.8 部署在升级前
    尚无该字段，因此还会根据稳定的部署 ID、模型 ID 和服务名补齐
    `high -> xhigh`。映射只作用于该部署的公开模型，不会改变 Ornith 或其他
    模型对 `high` 的原生含义。

    参数：
        deployment_id: 部署注册表中的稳定部署 ID。
        deployment: 包含模型身份、运行参数和公开 ID 的部署记录。

    返回：
        已校验且去除恒等项的来源等级到目标等级映射。

    异常：
        RuntimeError: 显式映射不是对象，或包含 OmniRoute 不支持的等级。
    """

    runtime = deployment.get("runtime", {})
    if not isinstance(runtime, dict):
        runtime = {}
    raw_aliases = runtime.get("reasoning_effort_aliases")
    if raw_aliases is None:
        identifiers = (
            deployment_id,
            str(deployment.get("model_id", "")),
            str(deployment.get("served_model_name", "")),
        )
        is_qwen38 = any(
            "qwen38flashnext"
            in "".join(character for character in value.lower() if character.isalnum())
            for value in identifiers
        )
        raw_aliases = QWEN38_REASONING_EFFORT_ALIASES if is_qwen38 else {}
    if not isinstance(raw_aliases, dict):
        raise RuntimeError("reasoning_effort_aliases 必须是对象")

    aliases: dict[str, str] = {}
    for raw_source, raw_target in raw_aliases.items():
        source = str(raw_source).strip().lower()
        target = str(raw_target).strip().lower()
        if source not in OMNIROUTE_REASONING_EFFORTS:
            raise RuntimeError(f"OmniRoute 不支持来源推理等级：{source or '<empty>'}")
        if target not in OMNIROUTE_REASONING_EFFORTS:
            raise RuntimeError(f"OmniRoute 不支持目标推理等级：{target or '<empty>'}")
        if source != target:
            aliases[source] = target
    return aliases


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


def managed_output_token_limit(max_model_len: int) -> int:
    """返回 LLMCtl 受管模型对外声明的单请求输出硬上限。

    参数：
        max_model_len: 当前模型实际启用的输入与输出总上下文长度。

    返回：
        不超过模型上下文和 32768 Token 平台安全边界的输出上限。

    异常：
        SystemExit: 配置不是整数，或者管理员尝试把上限提高到平台边界之外。
    """

    raw_value = os.environ.get("MAX_OUTPUT_TOKENS", str(MAX_OUTPUT_TOKENS_CEILING))
    try:
        configured = int(raw_value)
    except ValueError as error:
        raise SystemExit("MAX_OUTPUT_TOKENS must be an integer") from error
    if configured < 1 or configured > MAX_OUTPUT_TOKENS_CEILING:
        raise SystemExit(
            f"MAX_OUTPUT_TOKENS must be between 1 and {MAX_OUTPUT_TOKENS_CEILING}"
        )
    return min(max_model_len, configured)


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
    """为旧版 LiteLLM 接入层生成逐 Worker 路由和真实模型能力上限。"""

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
                f"      max_output_tokens: {managed_output_token_limit(max_len)}",
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
                # OpenAI 风格的 VK 仍可携带 Authorization。
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
    # MAX_NUM_SEQS 限制 vLLM 活跃序列，不限制网关接收的 HTTP 请求。若用它
    # 限制 OmniRoute，会先触发网关自己的 semaphore 队列和 30 秒故障转移。
    concurrency_per_worker = OMNIROUTE_INFLIGHT_PER_WORKER
    data = {
        "gateway": "omniroute",
        "managed_tag": MANAGED_TAG,
        "model": model,
        "strategy": "round-robin",
        # OmniRoute 另有默认大于 1 的粘性轮询批次设置。独立 vLLM 副本必须
        # 显式关闭该行为，否则突发并发会在轮询前集中排到第一个目标。
        "sticky_round_robin_limit": 1,
        "concurrency_per_worker": concurrency_per_worker,
        "queue_timeout_ms": OMNIROUTE_QUEUE_TIMEOUT_MS,
        "supports_vision": os.environ.get("SUPPORTS_IMAGE_INPUT", "0") == "1",
        # OmniRoute 3.8.x 检查 Combo 成员时无法从自定义 Provider 模型解析
        # supportsVision，默认启用的 Vision Bridge 会先等待
        # openai/gpt-4o-mini 30 秒，才保留原图并调用原生视觉 LLMCtl Worker。
        # 已验证为原生多模态的模型应关闭该桥接。
        "vision_bridge_enabled": False
        if os.environ.get("SUPPORTS_IMAGE_INPUT", "0") == "1"
        else None,
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
    existing = response_list(
        client.request("GET", f"/api/keys?limit={OMNIROUTE_PAGE_LIMIT}"), "keys"
    )
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
    """同步兼容节点、连接、真实模型能力元数据和路由组合。"""
    client.login()
    ensure_omniroute_management_key(client, secrets_file)
    model = required_env("SERVED_MODEL_NAME")
    base_port = int(required_env("WORKER_BASE_PORT"))
    backend_key = required_env("BACKEND_API_KEY")
    supports_vision = os.environ.get("SUPPORTS_IMAGE_INPUT", "0") == "1"
    concurrency_per_worker = OMNIROUTE_INFLIGHT_PER_WORKER

    if supports_vision:
        # 下方自定义模型会持久化 supportsVision=true，但 OmniRoute 的
        # VisionBridgeGuardrail 通过静态/同步注册表检查 Combo 目标，会忽略
        # 自定义 Provider 元数据。因此每张图片都会调用默认
        # openai/gpt-4o-mini 桥接并等待完整 30 秒，之后才把原图交给 vLLM。
        # LLMCtl 已验证目标模型的原生图像能力，此桥接既多余又有害。OmniRoute
        # 会热加载 PATCH，不需要重启 Worker 或 Router。
        bridge_settings = client.request(
            "PATCH", "/api/settings", {"visionBridgeEnabled": False}
        )
        if bridge_settings.get("visionBridgeEnabled") is not False:
            raise RuntimeError(
                "OmniRoute did not persist visionBridgeEnabled=false for the native-vision cluster"
            )

    nodes = response_list(
        client.request("GET", f"/api/provider-nodes?limit={OMNIROUTE_PAGE_LIMIT}"),
        "nodes",
    )
    connections = response_list(
        client.request("GET", f"/api/providers?limit={OMNIROUTE_PAGE_LIMIT}"),
        "connections",
    )
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
        max_model_len = int(required_env("MAX_MODEL_LEN"))
        model_payload: dict[str, Any] = {
            "provider": node_id,
            "modelId": model,
            "modelName": model,
            "source": MANAGED_TAG,
            "apiFormat": "chat-completions",
            "supportedEndpoints": ["chat"],
            "targetFormat": "openai",
            "max_input_tokens": max_model_len,
            "max_output_tokens": managed_output_token_limit(max_model_len),
            "contextWindowOverride": max_model_len,
            "supportsVision": supports_vision,
        }
        if existing_model:
            # 模型计划变化时同步可变的能力和上下文元数据，避免旧手工记录残留。
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

    combos = response_list(
        client.request("GET", f"/api/combos?limit={OMNIROUTE_PAGE_LIMIT}"), "combos"
    )
    existing_combo = next((item for item in combos if item.get("name") == model), None)
    combo_payload: dict[str, Any] = {
        "name": model,
        "description": OMNIROUTE_DESCRIPTION,
        "models": combo_models,
        "strategy": "round-robin",
        "config": {
            "disableSessionStickiness": True,
            # 该设置不同于会话粘性。OmniRoute 全局默认会在同一目标批量处理
            # 多个成功请求；并发压测时，响应完成前 RR 计数不变化，任务会集中
            # 到一两张 GPU。每目标一个请求可恢复所有健康 Worker 的即时轮询。
            "stickyRoundRobinLimit": 1,
            "concurrencyPerModel": concurrency_per_worker,
            # OmniRoute 3.8.x 默认 30 秒。API 更新时会丢弃旧 queueDepth，
            # 但支持 queueTimeoutMs，可避免饱和目标遮蔽空闲后备 Worker。
            "queueTimeoutMs": OMNIROUTE_QUEUE_TIMEOUT_MS,
            "healthCheckEnabled": True,
            "maxRetries": 1,
            "failoverBeforeRetry": True,
        },
        "context_length": int(required_env("MAX_MODEL_LEN")),
    }
    if existing_combo:
        if not is_llmctl_managed_combo(existing_combo):
            raise RuntimeError(
                f"OmniRoute combo '{model}' already exists and is not managed by LLMCtl"
            )
        combo_id = str(existing_combo.get("id", ""))
        client.request(
            "PUT", f"/api/combos/{urllib.parse.quote(combo_id, safe='')}", combo_payload
        )
    else:
        client.request("POST", "/api/combos", combo_payload)

    # 替代 Combo 提交后才删除 LLMCtl 所有的重复项；如果之前任何同步步骤失败，
    # 该顺序仍能保留最后一条已知可用路由。
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


def registry_omniroute_specs(registry_file: pathlib.Path) -> list[dict[str, Any]]:
    """从部署注册表生成 OmniRoute 所需的多模型目标清单。"""

    registry = json.loads(registry_file.read_text(encoding="utf-8"))
    deployments = registry.get("deployments", {})
    aliases = registry.get("legacy_aliases", {})
    if not isinstance(deployments, dict) or not isinstance(aliases, dict):
        raise RuntimeError("LLMCtl 部署注册表结构无效")
    alias_targets: dict[str, list[str]] = {}
    for alias, target in aliases.items():
        alias_targets.setdefault(str(target), []).append(str(alias))
    specs: list[dict[str, Any]] = []
    for deployment_id, deployment in deployments.items():
        if (
            not isinstance(deployment, dict)
            or not deployment.get("enabled", True)
            or not deployment.get("publish_requested", True)
        ):
            continue
        runtime = deployment.get("runtime", {})
        served_model = str(deployment.get("served_model_name", ""))
        public_ids = [str(item) for item in deployment.get("public_model_ids", [])]
        public_ids.extend(alias_targets.get(served_model, []))
        public_ids.extend(
            alias
            for target, aliases_for_target in alias_targets.items()
            if target in public_ids
            for alias in aliases_for_target
        )
        targets: list[dict[str, Any]] = []
        for instance in deployment.get("instances", []):
            if not isinstance(instance, dict) or not instance.get("enabled", True):
                continue
            if instance.get("kind") == "remote":
                base_url = str(instance.get("base_url", "")).rstrip("/")
                if not base_url.endswith("/v1"):
                    base_url += "/v1"
                targets.append(
                    {
                        "id": str(instance.get("id", "remote")),
                        "base_url": base_url,
                        "api_key_env": str(instance.get("api_key_env", "BACKEND_API_KEY")),
                    }
                )
            elif instance.get("kind") == "local":
                worker_id = int(instance["worker_id"])
                host = (
                    f"llm-worker-{worker_id}"
                    if os.environ.get("DOCKER_NETWORK")
                    else "127.0.0.1"
                )
                targets.append(
                    {
                        "id": f"worker-{worker_id}",
                        "worker_id": worker_id,
                        "base_url": f"http://{host}:{int(instance['port'])}/v1",
                        "api_key_env": "BACKEND_API_KEY",
                    }
                )
        if targets:
            specs.append(
                {
                    "deployment_id": str(deployment_id),
                    "served_model_name": served_model,
                    "public_model_ids": list(dict.fromkeys(public_ids)),
                    "max_model_len": int(runtime.get("max_model_len", 32768)),
                    "supports_vision": bool(runtime.get("supports_image_input", False)),
                    "reasoning_effort_aliases": deployment_reasoning_effort_aliases(
                        str(deployment_id), deployment
                    ),
                    "targets": targets,
                }
            )
    if not specs:
        raise RuntimeError("部署注册表中没有可发布的模型实例")
    return specs


def reasoning_rule_payloads(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把活动部署的推理等级别名转换为 OmniRoute 模型级规则。

    参数：
        specs: `registry_omniroute_specs` 生成的活动公开部署清单。

    返回：
        可直接提交到 OmniRoute 推理规则 API 的确定性、去重规则列表。
        未配置兼容别名的模型不会产生规则。
    """

    payloads: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for spec in specs:
        aliases = spec.get("reasoning_effort_aliases", {})
        if not isinstance(aliases, dict):
            raise RuntimeError("OmniRoute 部署清单中的推理等级映射无效")
        for public_model_id in spec.get("public_model_ids", []):
            model = str(public_model_id).strip()
            if not model:
                continue
            for source, target in sorted(aliases.items()):
                identity = (model, str(source), str(target))
                if identity in seen:
                    continue
                seen.add(identity)
                payloads.append(
                    {
                        "name": f"LLMCtl {model} reasoning {source} to {target}"[:200],
                        "description": OMNIROUTE_REASONING_RULE_DESCRIPTION,
                        "scope": "model",
                        "modelPattern": model,
                        "sourceEffort": source,
                        "requestTags": [],
                        "tagMatchMode": "any",
                        "effortMode": "force",
                        "targetEffort": target,
                        "targetKind": "keep",
                        "budgetAction": "preserve",
                        "priority": 900_000,
                        "enabled": True,
                    }
                )
    return payloads


def omniroute_reasoning_rules(client: OmniRouteClient) -> list[dict[str, Any]]:
    """读取 OmniRoute 推理路由规则，并把旧运行版本转成可操作错误。

    参数：
        client: 已登录且具有管理权限的 OmniRoute 客户端。

    返回：
        当前全部推理路由规则；不存在规则时返回空列表。

    异常：
        RuntimeError: 当前运行镜像缺少规则 API 时给出版本核对与恢复入口；
            其他网络、鉴权或服务端错误保持原始错误。
    """

    path = "/api/settings/reasoning-routing-rules"
    try:
        return response_list(client.request("GET", path), "rules")
    except RuntimeError as error:
        detail = str(error)
        if f"GET {path} returned HTTP 404" not in detail:
            raise
        raise OmniRouteFeatureUnavailable(
            "当前实际运行的 OmniRoute 缺少推理路由规则 API，无法安装 "
            "Qwen3.8 的 high→xhigh 兼容规则；尚未修改 Worker、Combo 或路由。"
            "请先运行 `llmctl omniroute status`：若 running_image 与 "
            "configured_image 不一致，执行 `llmctl router restart`；若两者一致但 "
            "不是推荐镜像，执行 `llmctl omniroute update "
            "diegosouzapw/omniroute:3.8.49 --yes`。"
        ) from error


def reconcile_omniroute_reasoning_rules(
    client: OmniRouteClient,
    specs: list[dict[str, Any]],
    existing_rules: list[dict[str, Any]] | None = None,
) -> None:
    """原子对账 LLMCtl 管理的模型级推理等级兼容规则。

    先创建或更新活动模型规则，再删除重复项和已退役模型规则。任何写入失败都会
    保留此前已知可用的规则；函数只识别专用描述，不修改管理员手工规则。

    参数：
        client: 已登录且具有管理权限的 OmniRoute 客户端。
        specs: 当前部署注册表生成的活动公开部署清单。
        existing_rules: 可选的同步前规则快照；提供后不再重复调用读取 API。

    异常：
        RuntimeError: OmniRoute API 返回缺失 ID、拒绝规则或网络调用失败。
    """

    desired = reasoning_rule_payloads(specs)
    existing = (
        list(existing_rules)
        if existing_rules is not None
        else omniroute_reasoning_rules(client)
    )
    managed = [
        rule
        for rule in existing
        if rule.get("description") == OMNIROUTE_REASONING_RULE_DESCRIPTION
    ]
    managed_by_name: dict[str, list[dict[str, Any]]] = {}
    for rule in managed:
        managed_by_name.setdefault(str(rule.get("name", "")), []).append(rule)

    retained_ids: set[str] = set()
    for payload in desired:
        matches = managed_by_name.get(str(payload["name"]), [])
        current = matches.pop(0) if matches else None
        if current:
            rule_id = str(current.get("id", ""))
            if not rule_id:
                raise RuntimeError("OmniRoute 返回了缺少 ID 的 LLMCtl 推理规则")
            client.request(
                "PATCH",
                "/api/settings/reasoning-routing-rules/"
                + urllib.parse.quote(rule_id, safe=""),
                payload,
            )
            retained_ids.add(rule_id)
        else:
            created = client.request(
                "POST", "/api/settings/reasoning-routing-rules", payload
            ).get("rule", {})
            if not isinstance(created, dict) or not str(created.get("id", "")):
                raise RuntimeError("OmniRoute 未返回新建推理兼容规则的 ID")

    for rule in managed:
        rule_id = str(rule.get("id", ""))
        if rule_id and rule_id not in retained_ids:
            client.request(
                "DELETE",
                "/api/settings/reasoning-routing-rules/"
                + urllib.parse.quote(rule_id, safe=""),
            )


def reconcile_omniroute_registry(
    client: OmniRouteClient, registry_file: pathlib.Path, secrets_file: pathlib.Path
) -> None:
    """把全部已启用部署一次性同步到 OmniRoute，避免模型间相互清理。"""

    specs = registry_omniroute_specs(registry_file)
    client.login()
    ensure_omniroute_management_key(client, secrets_file)
    # 在修改 Vision Bridge、节点和 Combo 之前验证运行镜像能力。旧镜像缺少规则
    # API 时失败关闭，避免路由已局部同步后才暴露版本不一致。
    reasoning_rules_available = True
    try:
        existing_reasoning_rules = omniroute_reasoning_rules(client)
    except OmniRouteFeatureUnavailable:
        if os.environ.get("LLMCTL_ALLOW_LEGACY_OMNIROUTE") != "1":
            raise
        reasoning_rules_available = False
        existing_reasoning_rules = []
        print(
            "[gateway-config] WARNING: 恢复流程正在兼容旧 OmniRoute；"
            "跳过 Qwen 推理等级规则，不影响现有模型路由。",
            file=sys.stderr,
        )
    if any(spec["supports_vision"] for spec in specs):
        settings = client.request("PATCH", "/api/settings", {"visionBridgeEnabled": False})
        if settings.get("visionBridgeEnabled") is not False:
            raise RuntimeError("OmniRoute 未能关闭会造成多模态请求延迟的 Vision Bridge")

    nodes = response_list(
        client.request("GET", f"/api/provider-nodes?limit={OMNIROUTE_PAGE_LIMIT}"),
        "nodes",
    )
    connections = response_list(
        client.request("GET", f"/api/providers?limit={OMNIROUTE_PAGE_LIMIT}"),
        "connections",
    )
    combos = response_list(
        client.request("GET", f"/api/combos?limit={OMNIROUTE_PAGE_LIMIT}"),
        "combos",
    )
    desired_node_ids: set[str] = set()
    desired_combo_names: set[str] = set()
    stale_connection_ids: set[str] = set()

    for spec in specs:
        combo_targets: list[dict[str, Any]] = []
        for target_index, target in enumerate(spec["targets"]):
            worker_suffix = target.get("worker_id", target["id"])
            node_name = f"LLMCtl {spec['deployment_id']} worker {worker_suffix}"
            node_prefix = f"llmctl-{spec['deployment_id']}-w{target_index}"
            matching_nodes = [item for item in nodes if item.get("name") == node_name]
            node_payload = {
                "name": node_name,
                "prefix": node_prefix,
                "apiType": "chat",
                "baseUrl": target["base_url"],
                "chatPath": "/chat/completions",
                "modelsPath": "/models",
            }
            if matching_nodes:
                node_id = str(matching_nodes[0]["id"])
                node = client.request(
                    "PUT",
                    f"/api/provider-nodes/{urllib.parse.quote(node_id, safe='')}",
                    node_payload,
                ).get("node", matching_nodes[0])
            else:
                node = client.request(
                    "POST",
                    "/api/provider-nodes",
                    {**node_payload, "type": "openai-compatible"},
                ).get("node", {})
            node_id = str(node.get("id", ""))
            if not node_id:
                raise RuntimeError(f"OmniRoute 未返回节点：{node_name}")
            desired_node_ids.add(node_id)

            api_key_env = str(target.get("api_key_env", "BACKEND_API_KEY"))
            backend_key = os.environ.get(api_key_env, "")
            if not backend_key:
                raise RuntimeError(f"远程实例密钥环境变量不存在：{api_key_env}")
            matching_connections = [
                item for item in connections if item.get("provider") == node_id
            ]
            connection_payload = {
                "name": node_name,
                "apiKey": backend_key,
                "priority": 1,
                "maxConcurrent": OMNIROUTE_INFLIGHT_PER_WORKER,
                "defaultModel": spec["served_model_name"],
                "isActive": True,
                "testStatus": "success",
            }
            if matching_connections:
                connection_id = str(matching_connections[0].get("id", ""))
                connection = client.request(
                    "PUT",
                    f"/api/providers/{urllib.parse.quote(connection_id, safe='')}",
                    connection_payload,
                ).get("connection", matching_connections[0])
                for duplicate in matching_connections[1:]:
                    duplicate_id = str(duplicate.get("id", ""))
                    if duplicate_id:
                        stale_connection_ids.add(duplicate_id)
            else:
                connection = client.request(
                    "POST",
                    "/api/providers",
                    {"provider": node_id, **connection_payload},
                ).get("connection", {})
            connection_id = str(connection.get("id", ""))
            if not connection_id:
                raise RuntimeError(f"OmniRoute 未返回连接：{node_name}")

            models = response_list(
                client.request(
                    "GET",
                    f"/api/provider-models?provider={urllib.parse.quote(node_id, safe='')}",
                ),
                "models",
            )
            model_payload = {
                "provider": node_id,
                "modelId": spec["served_model_name"],
                "modelName": spec["served_model_name"],
                "source": MANAGED_TAG,
                "apiFormat": "chat-completions",
                "supportedEndpoints": ["chat"],
                "targetFormat": "openai",
                "max_input_tokens": spec["max_model_len"],
                "max_output_tokens": managed_output_token_limit(spec["max_model_len"]),
                "contextWindowOverride": spec["max_model_len"],
                "supportsVision": spec["supports_vision"],
            }
            existing_model = next(
                (
                    item
                    for item in models
                    if item.get("id") == spec["served_model_name"]
                    or item.get("modelId") == spec["served_model_name"]
                ),
                None,
            )
            client.request("PUT" if existing_model else "POST", "/api/provider-models", model_payload)
            combo_targets.append(
                {
                    "kind": "model",
                    "provider": node_id,
                    "model": spec["served_model_name"],
                    "connectionId": connection_id,
                    "label": node_name,
                }
            )

        for public_model_id in spec["public_model_ids"]:
            desired_combo_names.add(public_model_id)
            existing_combo = next(
                (item for item in combos if item.get("name") == public_model_id), None
            )
            combo_payload = {
                "name": public_model_id,
                "description": OMNIROUTE_DESCRIPTION,
                "models": combo_targets,
                "strategy": "round-robin",
                "config": {
                    "disableSessionStickiness": True,
                    "stickyRoundRobinLimit": 1,
                    "concurrencyPerModel": OMNIROUTE_INFLIGHT_PER_WORKER,
                    "queueTimeoutMs": OMNIROUTE_QUEUE_TIMEOUT_MS,
                    "healthCheckEnabled": True,
                    "maxRetries": 1,
                    "failoverBeforeRetry": True,
                },
                "context_length": spec["max_model_len"],
            }
            if existing_combo:
                if not is_llmctl_managed_combo(existing_combo):
                    raise RuntimeError(
                        f"OmniRoute 模型组合 {public_model_id} 不是由 LLMCtl 管理"
                    )
                combo_id = str(existing_combo.get("id", ""))
                client.request(
                    "PUT",
                    f"/api/combos/{urllib.parse.quote(combo_id, safe='')}",
                    combo_payload,
                )
            else:
                client.request("POST", "/api/combos", combo_payload)

    # 推理兼容规则依赖公开模型已存在；先提交全部新 Combo，再对账规则，最后才
    # 清理退役路由。这样 Qwen 与 Ornith 切换中途失败时仍保留上一条可用链路。
    if reasoning_rules_available:
        reconcile_omniroute_reasoning_rules(client, specs, existing_reasoning_rules)

    # 先创建完整新路由，最后再清理旧资源，任何中途失败都保留上次可用链路。
    # 停用部署的连接不再属于任何期望 Combo；显式删除它们，避免只依赖
    # OmniRoute 删除 Provider Node 时是否级联清理的版本相关行为。
    stale_node_ids = {
        str(node.get("id", ""))
        for node in nodes
        if str(node.get("name", "")).startswith("LLMCtl ")
        and str(node.get("id", ""))
        and str(node.get("id", "")) not in desired_node_ids
    }
    stale_connection_ids.update(
        str(connection.get("id", ""))
        for connection in connections
        if str(connection.get("provider", "")) in stale_node_ids
        and str(connection.get("id", ""))
    )
    for combo in combos:
        combo_id = str(combo.get("id", ""))
        if (
            combo.get("description") == OMNIROUTE_DESCRIPTION
            and str(combo.get("name", "")) not in desired_combo_names
            and combo_id
        ):
            client.request("DELETE", f"/api/combos/{urllib.parse.quote(combo_id, safe='')}")
    for connection_id in sorted(stale_connection_ids):
        client.request("DELETE", f"/api/providers/{urllib.parse.quote(connection_id, safe='')}")
    for node in nodes:
        node_id = str(node.get("id", ""))
        if (
            str(node.get("name", "")).startswith("LLMCtl ")
            and node_id
            and node_id not in desired_node_ids
        ):
            client.request("DELETE", f"/api/provider-nodes/{urllib.parse.quote(node_id, safe='')}")


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
    # New API 的 Token 名称不唯一；选择最新 ID 可让中断的轮换恢复，同时保留
    # 最后一枚可用 Token。
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
    # 所有替代路由创建完成前保留最后一组已知可用路由。
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


def command_reconcile_omniroute_registry(args: argparse.Namespace) -> None:
    """根据多模型部署注册表一次性同步全部 LLMCtl 路由。"""

    reconcile_omniroute_registry(
        OmniRouteClient(required_env("GATEWAY_LOCAL_URL")),
        pathlib.Path(args.registry),
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

    reconcile_omni_registry = subparsers.add_parser("reconcile-omniroute-registry")
    reconcile_omni_registry.add_argument("--registry", required=True)
    reconcile_omni_registry.add_argument("--secrets-file", required=True)
    reconcile_omni_registry.set_defaults(handler=command_reconcile_omniroute_registry)

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
