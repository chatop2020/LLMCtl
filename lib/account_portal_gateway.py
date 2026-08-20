#!/usr/bin/env python3
"""账户门户访问 OmniRoute、工作流和模型部署控制面的受限客户端。"""

from __future__ import annotations

from account_portal_common import *

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
        """返回现有 Key；读取操作绝不能轮换或替换凭据。"""
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
        """创建或更新 OmniRoute 原生 Combo，并返回 ID 和是否新建。"""
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
        """把已启用的 Go 工作流路由发布为显式 OmniRoute Provider。

        该操作必须由管理员主动触发。保存工作流配置不会修改生产网关，升级流程
        也不会调用本方法。`gdn-inside` 等公开别名继续由常规模型发布页面管理；
        本方法只创建无冲突的 `llmctl-workflow-*` Combo 目标供管理员选择。
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
        """执行与 OmniRoute 原生界面一致的 Provider 感知探测。"""
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


class WorkflowClient:
    """访问可选 Go 数据面的本机鉴权控制客户端。"""

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
            "upgrade-plan",
            "upgrade-submit",
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
                # 升级计划需要访问模型目录并检查真实硬件，允许比普通本机状态
                # 请求更长的有界等待；执行任务仍然只提交后立即返回。
                client.settimeout(120 if operation == "upgrade-plan" else 30)
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
