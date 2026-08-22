#!/usr/bin/env python3
"""账户门户的 HTTP 协议适配、鉴权入口与路由分发。"""

from __future__ import annotations

from account_portal_common import *

from account_portal_database import *
from account_portal_gateway import *

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
        """只应用安装时已经确定的 Cookie 安全策略。

        `published_origin` 只是邮件、curl 示例和界面使用的链接元数据，不能改变
        现有局域网或反向代理会话的工作方式。需要 Secure Cookie 的部署应在
        安装或配置阶段显式启用 `ACCOUNT_COOKIE_SECURE`。
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

    def binary_response(
        self,
        status: int,
        content: bytes,
        content_type: str,
        filename: str,
    ) -> None:
        """返回受鉴权保护的二进制下载，并同时兼容中英文文件名。"""
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", filename).strip("-")
        safe_name = safe_name or "LLMCtl-export.xlsx"
        encoded_name = urllib.parse.quote(filename, safe="")
        self.send_headers(status, content_type)
        self.send_header(
            "Content-Disposition",
            f"attachment; filename=\"{safe_name}\"; filename*=UTF-8''{encoded_name}",
        )
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

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
        if path == "/portal-api/admin/usage-report/export":
            user, _ = self.api_require(admin=True)
            if user:
                try:
                    filters = self.usage_report_parameters(query)
                    content, filename, report = self.app.control.admin_usage_report_export(
                        **filters
                    )
                    self.app.db.audit(
                        user_identity(user),
                        "admin.usage-report.export",
                        report["range"]["label"],
                        "success",
                        self.remote_addr(),
                        detail=json.dumps(report["filters"], ensure_ascii=False),
                    )
                    self.binary_response(
                        200,
                        content,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        filename,
                    )
                except (TypeError, ValueError) as error:
                    self.json_response(400, {"error": str(error)})
            return
        if path == "/portal-api/admin/usage-report":
            user, _ = self.api_require(admin=True)
            if user:
                try:
                    filters = self.usage_report_parameters(query)
                    self.json_response(
                        200,
                        self.app.control.admin_usage_report(**filters),
                    )
                except (TypeError, ValueError) as error:
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
        if path == "/portal-api/admin/omniroute":
            user, _ = self.api_require(admin=True)
            if user:
                try:
                    self.json_response(
                        200, self.app.models.request("omniroute-status", {})
                    )
                except Exception as error:
                    self.json_response(503, {"error": str(error), "available": False})
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

    @classmethod
    def usage_report_parameters(cls, query: str) -> dict[str, Any]:
        values = urllib.parse.parse_qs(query, keep_blank_values=True)
        page, page_size = cls.page_parameters(query)
        result: dict[str, Any] = {
            "period": values.get("period", ["day"])[-1].strip(),
            "anchor": values.get("anchor", [""])[-1].strip(),
            "start_date": values.get("start_date", [""])[-1].strip(),
            "end_date": values.get("end_date", [""])[-1].strip(),
            "model_id": values.get("model", [""])[-1].strip(),
            "user_query": values.get("user_query", [""])[-1].strip(),
            "status": values.get("status", [""])[-1].strip(),
            "page": page,
            "page_size": page_size,
        }
        if any(
            len(str(result[key])) > 200
            for key in (
                "period",
                "anchor",
                "start_date",
                "end_date",
                "model_id",
                "user_query",
                "status",
            )
        ):
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
            elif path == "/portal-api/admin/users/key/reveal":
                result = self.app.control.reveal_user_api_key(
                    str(payload.get("user_id", ""))
                )
            elif path == "/portal-api/admin/users/verification/resend":
                result = self.api_resend_user_verification(payload)
            elif path == "/portal-api/admin/users/verification/approve":
                result = self.app.control.manually_verify_pending_user(
                    str(payload.get("user_id", "")),
                    str(payload.get("confirmation_email", "")),
                )
            elif path == "/portal-api/admin/users/pending/delete":
                result = self.app.control.delete_pending_user(
                    str(payload.get("user_id", ""))
                )
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
            elif path == "/portal-api/admin/model-upgrades/plan":
                result = self.app.models.request("upgrade-plan", payload)
            elif path == "/portal-api/admin/model-upgrades/submit":
                result = self.app.models.request("upgrade-submit", payload)
            elif path == "/portal-api/admin/model-deployments/publish":
                result = self.app.models.request("publish", payload)
            elif path == "/portal-api/admin/model-download/proxy/test":
                result = self.app.models.request("download-proxy-test", payload)
            elif path == "/portal-api/admin/model-download/proxy/save":
                result = self.app.models.request("download-proxy-save", payload)
            elif path == "/portal-api/admin/model-download/proxy/clear":
                result = self.app.models.request("download-proxy-clear", payload)
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
            elif path == "/portal-api/admin/omniroute/assess":
                result = self.app.models.request(
                    "omniroute-assess", {"deep": bool(payload.get("deep", False))}
                )
            elif path == "/portal-api/admin/omniroute/submit":
                result = self.app.models.request("omniroute-submit", payload)
            elif path == "/portal-api/admin/omniroute/job":
                result = self.app.models.request(
                    "omniroute-job", {"id": str(payload.get("id", ""))}
                )
            elif path == "/portal-api/admin/omniroute/cancel":
                result = self.app.models.request(
                    "omniroute-cancel", {"id": str(payload.get("id", ""))}
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
                self.app.db.audit(
                    user_identity(user),
                    path.removeprefix("/portal-api/"),
                    str(payload.get("id") or payload.get("user_id") or ""),
                    "failed",
                    self.remote_addr(),
                    str(error),
                )
            status = 409 if isinstance(error, (DatabaseMigrationError, DatabaseCapabilityError)) else 400 if isinstance(error, ValueError) else 502
            self.json_response(status, {"error": str(error)})
            return
        # 审计账本不持久化凭据明文；事件只证明发生展示/轮换，不复制秘密。
        secret_response_paths = {
            "/portal-api/key/reveal",
            "/portal-api/key/rotate",
            "/portal-api/admin/users/key/reveal",
        }
        if path == "/portal-api/admin/users/key/reveal" and isinstance(result, dict):
            audit_result = {
                "ok": True,
                "user_id": str(result.get("user_id", "")),
            }
        elif path in secret_response_paths and isinstance(result, dict):
            audit_result = {"ok": bool(result.get("ok"))}
        else:
            audit_result = result
        audit_status = (
            "partial"
            if path == "/portal-api/admin/users/bulk-policy"
            and isinstance(result, dict)
            and result.get("failed")
            else "success"
        )
        # 高频任务轮询只读取阶段，不代表新的管理员决定；不把每两秒一次的
        # 状态刷新写成审计事件。提交、取消、升级、维护和回滚仍完整审计。
        non_audited_paths = {
            "/portal-api/admin/database/migrate",
            "/portal-api/admin/model-deployments/job",
            "/portal-api/admin/omniroute/job",
        }
        if path not in non_audited_paths:
            self.app.db.audit(
                user_identity(user),
                path.removeprefix("/portal-api/"),
                str(payload.get("id") or payload.get("user_id") or ""),
                audit_status,
                self.remote_addr(),
                audit_result,
            )
        self.json_response(200, result)

    def api_resend_user_verification(self, payload: dict[str, Any]) -> dict[str, Any]:
        """为一个未验证用户发送新链接；发送成功后才使旧链接失效。

        参数：
            payload: 必须包含管理员当前选择的普通用户 ID。

        返回：
            只包含结果和用户 ID，不返回验证令牌或邮箱正文。

        异常：
            ValueError: 用户不存在、已经验证或一分钟内刚发送过验证邮件。
            RuntimeError: SMTP 发送失败；此时删除新令牌并保留此前有效链接。
        """

        user_id = str(payload.get("user_id", "")).strip()
        if not user_id or len(user_id) > 200:
            raise ValueError("用户 ID 无效")
        stamp = now()
        raw_token = secrets.token_urlsafe(40)
        raw_token_hash = token_hash(raw_token)
        with self.app.db.connect() as connection:
            user = connection.execute(
                "SELECT id,email,status,verified_at,api_key_id FROM users "
                "WHERE id=? AND role='user'",
                (user_id,),
            ).fetchone()
            if not user:
                raise ValueError("用户不存在")
            if (
                user["status"] != "pending"
                or user["verified_at"] is not None
                or user["api_key_id"]
            ):
                raise ValueError("该用户已经验证或已创建 API Key")
            latest = connection.execute(
                "SELECT created_at FROM verification_tokens WHERE user_id=? "
                "ORDER BY created_at DESC LIMIT 1",
                (user_id,),
            ).fetchone()
            if latest and stamp - int(latest["created_at"]) < 60:
                raise ValueError("验证邮件刚刚发送，请一分钟后再试")
            connection.execute(
                "INSERT INTO verification_tokens(token_hash,user_id,expires_at,created_at) "
                "VALUES(?,?,?,?)",
                (
                    raw_token_hash,
                    user_id,
                    stamp + self.app.config.verification_ttl,
                    stamp,
                ),
            )
        try:
            send_verification_email(
                effective_mail_config(self.app.config, self.app.db.settings()),
                str(user["email"]),
                raw_token,
            )
        except Exception as error:
            with self.app.db.connect() as connection:
                connection.execute(
                    "DELETE FROM verification_tokens WHERE token_hash=?",
                    (raw_token_hash,),
                )
            raise RuntimeError(f"验证邮件发送失败：{error}") from error
        with self.app.db.connect() as connection:
            connection.execute(
                "DELETE FROM verification_tokens WHERE user_id=? AND token_hash<>?",
                (user_id, raw_token_hash),
            )
        return {"ok": True, "user_id": user_id}

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
        try:
            provisioned = self.app.control.activate_pending_user(
                str(record["user_id"]),
                verification_token_hash=token_hash(raw_token),
            )
            raw_key = provisioned["api_key"]
        except Exception as error:
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
        try:
            provisioned = self.app.control.activate_pending_user(
                str(record["user_id"]),
                verification_token_hash=token_hash(raw_token),
            )
            raw_key = provisioned["api_key"]
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
