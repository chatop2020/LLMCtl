#!/usr/bin/env python3
"""门户管理员快照、用户策略、用户组与后台周期任务。"""

from __future__ import annotations

from account_portal_common import *
from account_portal_database import *
from account_portal_gateway import *


class PortalAdminControlMixin:
    """门户管理员快照、用户策略、用户组与后台周期任务。该类型只提供领域方法，运行状态由组合控制器持有。"""

    def reveal_user_api_key(self, user_id: str) -> dict[str, str]:
        """按管理员指定的用户 ID 读取当前 API Key，不创建或轮换凭据。

        参数：
            user_id: 门户普通用户的稳定 ID；管理员账号不属于可读取目标。

        返回：
            目标用户 ID 与 OmniRoute 返回的当前 API Key 明文。调用方只可把
            明文返回给已认证管理员，不能写入门户数据库、日志或审计详情。

        异常：
            ValueError: 用户不存在、不是普通用户，或尚未配置 API Key。
            RuntimeError: OmniRoute 无法安全返回现有 Key。
        """

        normalized_user_id = str(user_id).strip()
        if not normalized_user_id or len(normalized_user_id) > 200:
            raise ValueError("用户 ID 无效")
        with self.db.connect() as connection:
            user = connection.execute(
                "SELECT id,api_key_id FROM users WHERE id=? AND role='user'",
                (normalized_user_id,),
            ).fetchone()
        if not user:
            raise ValueError("用户不存在")
        key_id = str(user["api_key_id"] or "")
        if not key_id:
            raise ValueError("该用户尚未配置 API Key")
        return {
            "user_id": str(user["id"]),
            "api_key": self.omni.reveal_user_key(key_id),
        }

    def activate_pending_user(
        self, user_id: str, *, verification_token_hash: str = ""
    ) -> dict[str, str]:
        """把待验证用户完整开通为可登录、可同步权限的正式账户。

        参数：
            user_id: 待开通普通用户的稳定 ID。
            verification_token_hash: 用户通过邮件确认时提供的令牌哈希。管理员
                手动通过时留空；成功后仍会使该用户的全部旧验证链接失效。

        返回：
            用户 ID、邮箱和 OmniRoute 仅在创建时返回的 API Key 明文。调用方
            只能在用户本人完成邮件验证时展示明文，管理员入口不得返回它。

        异常：
            ValueError: 用户不存在、已开通、邮箱域名不再允许，或验证令牌失效。
            RuntimeError: API Key 创建或权限同步失败。失败时会删除半成品 Key，
                并只回滚本次状态变更，不覆盖并发完成的另一笔开通。

        本方法是邮件验证和管理员手动通过共用的唯一开户状态机。它负责 API
        Key、欢迎余额、默认用户组、权限同步、令牌失效和失败补偿。
        """

        normalized_user_id = str(user_id).strip()
        normalized_token_hash = str(verification_token_hash).strip()
        if not normalized_user_id or len(normalized_user_id) > 200:
            raise ValueError("用户 ID 无效")
        if normalized_token_hash and len(normalized_token_hash) != 64:
            raise ValueError("验证令牌无效")

        with self.db.connect() as connection:
            user = connection.execute(
                "SELECT * FROM users WHERE id=? AND role='user'",
                (normalized_user_id,),
            ).fetchone()
            if not user:
                raise ValueError("用户不存在")
            if (
                user["status"] != "pending"
                or user["verified_at"] is not None
                or user["api_key_id"]
            ):
                raise ValueError("该用户已经验证或已创建 API Key")
            billing_preexisting = bool(
                connection.execute(
                    "SELECT 1 FROM billing_accounts WHERE user_id=?",
                    (normalized_user_id,),
                ).fetchone()
            )
            membership_preexisting = bool(
                connection.execute(
                    "SELECT 1 FROM user_group_members WHERE user_id=? AND group_id='default'",
                    (normalized_user_id,),
                ).fetchone()
            )
            permission_row = connection.execute(
                "SELECT status,error,updated_at FROM permission_sync WHERE user_id=?",
                (normalized_user_id,),
            ).fetchone()
            permission_preexisting = dict(permission_row) if permission_row else None

        settings = self.db.settings()
        _, domain = normalize_email(str(user["email"]))
        if domain not in normalize_domains(settings.get("allowed_domains", "")):
            raise ValueError("该用户邮箱域名已不在允许注册范围内")

        key_id = ""
        welcome_credit_micros = 0
        welcome_source_ref = ""
        claimed = False
        token_consumed = False
        stamp = now()
        try:
            key_id, raw_key = self.omni.create_user_key(
                normalized_user_id,
                str(user["email"]),
                int(user["max_sessions"]),
            )
            with self.db.connect() as connection:
                if normalized_token_hash:
                    token_consumed = connection.execute(
                        "UPDATE verification_tokens SET used_at=? "
                        "WHERE token_hash=? AND user_id=? AND used_at IS NULL AND expires_at>?",
                        (
                            stamp,
                            normalized_token_hash,
                            normalized_user_id,
                            stamp,
                        ),
                    ).rowcount == 1
                    if not token_consumed:
                        raise ValueError("验证链接已失效")
                claimed = connection.execute(
                    "UPDATE users SET status='active',verified_at=?,api_key_id=?,"
                    "token_limit_id=NULL WHERE id=? AND status='pending' "
                    "AND verified_at IS NULL AND api_key_id IS NULL",
                    (stamp, key_id, normalized_user_id),
                ).rowcount == 1
                if not claimed:
                    raise ValueError("用户状态已经变化，请刷新页面后重试")
                connection.execute(
                    "INSERT OR IGNORE INTO billing_accounts"
                    "(user_id,balance_micros,suspended,updated_at) VALUES(?,0,0,?)",
                    (normalized_user_id, stamp),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO user_group_members"
                    "(user_id,group_id,created_at) VALUES(?,'default',?)",
                    (normalized_user_id, stamp),
                )
                welcome_credit_micros, welcome_source_ref = apply_welcome_credit(
                    connection, normalized_user_id, settings, stamp
                )
            self.sync_user(normalized_user_id)
            with self.db.connect() as connection:
                connection.execute(
                    "UPDATE verification_tokens SET used_at=? "
                    "WHERE user_id=? AND used_at IS NULL",
                    (stamp, normalized_user_id),
                )
            return {
                "user_id": normalized_user_id,
                "email": str(user["email"]),
                "api_key": raw_key,
            }
        except Exception:
            if key_id:
                with contextlib.suppress(Exception):
                    self.omni.delete_key(key_id)
            # 只有本次成功认领了待验证状态时才回滚账户数据；并发完成的另一笔
            # 开通拥有不同 api_key_id，条件更新不会把它重新降级为 pending。
            with self.db.connect() as connection:
                if claimed:
                    if welcome_credit_micros > 0 and welcome_source_ref:
                        rollback_source_credit(
                            connection,
                            normalized_user_id,
                            welcome_source_ref,
                            now(),
                        )
                    if permission_preexisting:
                        connection.execute(
                            "INSERT INTO permission_sync(user_id,status,error,updated_at) "
                            "VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
                            "status=excluded.status,error=excluded.error,"
                            "updated_at=excluded.updated_at",
                            (
                                normalized_user_id,
                                permission_preexisting["status"],
                                permission_preexisting["error"],
                                permission_preexisting["updated_at"],
                            ),
                        )
                    else:
                        connection.execute(
                            "DELETE FROM permission_sync WHERE user_id=?",
                            (normalized_user_id,),
                        )
                    if not membership_preexisting:
                        connection.execute(
                            "DELETE FROM user_group_members "
                            "WHERE user_id=? AND group_id='default'",
                            (normalized_user_id,),
                        )
                    if not billing_preexisting:
                        connection.execute(
                            "DELETE FROM billing_accounts WHERE user_id=? AND NOT EXISTS "
                            "(SELECT 1 FROM balance_transactions WHERE user_id=?) AND NOT EXISTS "
                            "(SELECT 1 FROM usage_ledger WHERE user_id=?)",
                            (
                                normalized_user_id,
                                normalized_user_id,
                                normalized_user_id,
                            ),
                        )
                    connection.execute(
                        "UPDATE users SET status='pending',verified_at=NULL,api_key_id=NULL,"
                        "token_limit_id=NULL WHERE id=? AND status='active' AND api_key_id=?",
                        (normalized_user_id, key_id),
                    )
                if normalized_token_hash and token_consumed:
                    connection.execute(
                        "UPDATE verification_tokens SET used_at=NULL "
                        "WHERE token_hash=? AND user_id=? AND used_at=?",
                        (normalized_token_hash, normalized_user_id, stamp),
                    )
            raise

    def manually_verify_pending_user(
        self, user_id: str, confirmation_email: str
    ) -> dict[str, Any]:
        """由管理员在准确确认邮箱后手动开通一个待验证用户。

        参数：
            user_id: 管理列表中明确选择的待验证普通用户 ID。
            confirmation_email: 管理员在确认框中重新输入的完整目标邮箱。

        返回：
            只包含成功状态和用户 ID；API Key 明文不会返回管理员入口。

        异常：
            ValueError: 用户不存在、确认邮箱不匹配，或用户不再处于待验证状态。
            RuntimeError: 开户或权限同步失败；账户保持待验证且半成品 Key 被删除。
        """

        normalized_user_id = str(user_id).strip()
        normalized_confirmation = str(confirmation_email).strip()
        with self.db.connect() as connection:
            user = connection.execute(
                "SELECT id,email FROM users WHERE id=? AND role='user'",
                (normalized_user_id,),
            ).fetchone()
        if not user:
            raise ValueError("用户不存在")
        if not normalized_confirmation or normalized_confirmation != str(user["email"]):
            raise ValueError("确认邮箱与目标用户不一致")
        result = self.activate_pending_user(normalized_user_id)
        return {"ok": True, "user_id": str(result["user_id"])}

    def delete_pending_user(self, user_id: str) -> dict[str, Any]:
        """删除尚未验证且没有凭据、用量或资金记录的注册占位。

        参数：
            user_id: 管理员明确选择的普通用户 ID。

        返回：
            删除结果与目标用户 ID，不返回邮箱、密码哈希或验证令牌。

        异常：
            ValueError: 用户不存在、已经验证、已经创建 Key，或存在不可删除的
                账单、赠额和调用记录。

        正式用户必须通过停用保留账单和审计链，不能使用本方法删除。
        """

        normalized_user_id = str(user_id).strip()
        if not normalized_user_id or len(normalized_user_id) > 200:
            raise ValueError("用户 ID 无效")
        with self.db.connect() as connection:
            user = connection.execute(
                "SELECT id,status,verified_at,api_key_id FROM users "
                "WHERE id=? AND role='user'",
                (normalized_user_id,),
            ).fetchone()
            if not user:
                raise ValueError("用户不存在")
            if (
                user["status"] != "pending"
                or user["verified_at"] is not None
                or user["api_key_id"]
            ):
                raise ValueError("只能删除尚未验证且未创建 API Key 的用户")
            related = sum(
                int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE user_id=?",
                        (normalized_user_id,),
                    ).fetchone()[0]
                )
                for table in ("usage_ledger", "balance_transactions", "token_grants")
            )
            account = connection.execute(
                "SELECT balance_micros FROM billing_accounts WHERE user_id=?",
                (normalized_user_id,),
            ).fetchone()
            if related or (account and int(account["balance_micros"] or 0) != 0):
                raise ValueError("该用户已有账单、赠额或调用记录，只能停用，不能删除")
            deleted = connection.execute(
                "DELETE FROM users WHERE id=? AND status='pending' AND api_key_id IS NULL",
                (normalized_user_id,),
            ).rowcount
        if deleted != 1:
            raise ValueError("用户状态已经变化，请刷新页面后重试")
        return {"ok": True, "user_id": normalized_user_id}

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
