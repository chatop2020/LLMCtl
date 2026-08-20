#!/usr/bin/env python3
"""门户管理员快照、用户策略、用户组与后台周期任务。"""

from __future__ import annotations

from account_portal_common import *
from account_portal_database import *
from account_portal_gateway import *


class PortalAdminControlMixin:
    """门户管理员快照、用户策略、用户组与后台周期任务。该类型只提供领域方法，运行状态由组合控制器持有。"""

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
        """原子更新选中的本地策略，再逐个发布对应 API Key。

        调用方必须提交明确的用户 ID，避免浏览器中的陈旧筛选条件把定向修改
        误变成全局操作。SQLite 提交前先停用 Key，只有新策略同步成功后才会
        重新启用。
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
