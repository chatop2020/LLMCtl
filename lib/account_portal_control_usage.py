#!/usr/bin/env python3
"""门户计费对账、用量分析、报表与请求详情策略。"""

from __future__ import annotations

from account_portal_common import *
from account_portal_database import *
from account_portal_gateway import *


class PortalUsageControlMixin:
    """门户计费对账、用量分析、报表与请求详情策略。该类型只提供领域方法，运行状态由组合控制器持有。"""

    @staticmethod
    def parse_timestamp(value: Any) -> int:
        try:
            return int(datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
        except (ValueError, TypeError):
            return now()

    def reset_due_grants(self) -> int:
        """重置到期赠额，不要求请求先通过已经停用的 API Key。"""
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
        """把网关日志归因到用户当时获准调用的公开模型。"""
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
        """建立本地日历统计桶，不依赖 SQLite 所在主机的时区。

        Epoch 边界使 SQL 保持可索引，并在存在夏令时切换的命名时区中维持正确
        边界。最后一个统计桶允许不完整，并在生成管理面板时结束。
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

    @staticmethod
    def usage_report_window(
        period: str,
        anchor: str = "",
        start_date: str = "",
        end_date: str = "",
        stamp: int | None = None,
    ) -> dict[str, Any]:
        """按门户时区建立完整、左闭右开的报表区间和展示桶。"""
        if period not in {"day", "month", "year", "custom"}:
            raise ValueError("统计周期无效")
        timezone_name = os.environ.get("TZ", "Asia/Shanghai")
        try:
            zone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            zone, timezone_name = ZoneInfo("UTC"), "UTC"
        current = datetime.datetime.fromtimestamp(stamp if stamp is not None else now(), zone)

        def local_midnight(value: datetime.date) -> datetime.datetime:
            return datetime.datetime.combine(value, datetime.time.min, zone)

        if period == "day":
            raw = anchor or current.strftime("%Y-%m-%d")
            try:
                selected = datetime.date.fromisoformat(raw)
            except ValueError as error:
                raise ValueError("日期必须使用 YYYY-MM-DD") from error
            start = local_midnight(selected)
            end = local_midnight(selected + datetime.timedelta(days=1))
            label, grain = selected.strftime("%Y年%m月%d日"), "hour"
        elif period == "month":
            raw = anchor or current.strftime("%Y-%m")
            if not re.fullmatch(r"\d{4}-\d{2}", raw):
                raise ValueError("月份必须使用 YYYY-MM")
            try:
                selected = datetime.date.fromisoformat(raw + "-01")
            except ValueError as error:
                raise ValueError("月份无效") from error
            start = local_midnight(selected)
            next_month = selected.replace(
                year=selected.year + (1 if selected.month == 12 else 0),
                month=1 if selected.month == 12 else selected.month + 1,
            )
            end = local_midnight(next_month)
            label, grain = selected.strftime("%Y年%m月"), "day"
        elif period == "year":
            raw = anchor or str(current.year)
            if not re.fullmatch(r"\d{4}", raw):
                raise ValueError("年份必须使用四位数字")
            year = int(raw)
            if year < 1970 or year > 9998:
                raise ValueError("年份超出允许范围")
            start = local_midnight(datetime.date(year, 1, 1))
            end = local_midnight(datetime.date(year + 1, 1, 1))
            label, grain = f"{year}年", "month"
        else:
            try:
                selected_start = datetime.date.fromisoformat(start_date)
                selected_end = datetime.date.fromisoformat(end_date)
            except ValueError as error:
                raise ValueError("自定义日期必须使用 YYYY-MM-DD") from error
            if selected_end < selected_start:
                raise ValueError("结束日期不能早于开始日期")
            span_days = (selected_end - selected_start).days + 1
            if span_days > 1830:
                raise ValueError("单次自定义统计范围不能超过 5 年")
            start = local_midnight(selected_start)
            end = local_midnight(selected_end + datetime.timedelta(days=1))
            grain = "hour" if span_days <= 2 else ("day" if span_days <= 400 else "month")
            label = f"{selected_start.isoformat()} 至 {selected_end.isoformat()}"

        def advance(value: datetime.datetime) -> datetime.datetime:
            if grain == "hour":
                return value + datetime.timedelta(hours=1)
            if grain == "day":
                return value + datetime.timedelta(days=1)
            month_index = value.year * 12 + value.month
            return value.replace(year=month_index // 12, month=month_index % 12 + 1)

        def bucket_label(value: datetime.datetime) -> str:
            if grain == "hour":
                return value.strftime("%m/%d %H:00") if period == "custom" else value.strftime("%H:00")
            if grain == "day":
                return value.strftime("%m/%d")
            return value.strftime("%Y/%m")

        buckets: list[dict[str, Any]] = []
        cursor = start
        while cursor < end:
            next_cursor = min(advance(cursor), end)
            buckets.append(
                {
                    "index": len(buckets),
                    "label": bucket_label(cursor),
                    "start_at": int(cursor.timestamp()),
                    "end_at": int(next_cursor.timestamp()),
                }
            )
            cursor = next_cursor
        return {
            "key": period,
            "label": label,
            "grain": grain,
            "timezone": timezone_name,
            "start_at": int(start.timestamp()),
            "end_at": int(end.timestamp()),
            "buckets": buckets,
        }

    @staticmethod
    def _report_user_conditions(
        user_query: str = "", status: str = ""
    ) -> tuple[list[str], list[Any]]:
        conditions = ["u.role='user'"]
        parameters: list[Any] = []
        if status:
            if status not in {"pending", "active", "disabled"}:
                raise ValueError("用户状态筛选无效")
            conditions.append("u.status=?")
            parameters.append(status)
        if user_query:
            keyword = f"%{user_query.lower()}%"
            conditions.append(
                "(LOWER(u.email) LIKE ? OR LOWER(COALESCE(u.login_name,'')) LIKE ?)"
            )
            parameters.extend((keyword, keyword))
        return conditions, parameters

    def _usage_report_series(
        self,
        connection: sqlite3.Connection,
        window: dict[str, Any],
        model_id: str,
        user_query: str,
        status: str,
    ) -> list[dict[str, Any]]:
        buckets = window["buckets"]
        parameters: list[Any] = []
        if self.db.is_mysql:
            bucket_source = " UNION ALL ".join(
                "SELECT ? AS bucket_index,? AS start_at,? AS end_at" for _ in buckets
            )
            bucket_definition = f"buckets AS ({bucket_source})"
        else:
            values = ",".join("(?,?,?)" for _ in buckets)
            bucket_definition = f"buckets(bucket_index,start_at,end_at) AS (VALUES {values})"
        for bucket in buckets:
            parameters.extend((bucket["index"], bucket["start_at"], bucket["end_at"]))
        user_conditions, user_parameters = self._report_user_conditions(user_query, status)
        usage_conditions = [
            "l.occurred_at>=?",
            "l.occurred_at<?",
            *user_conditions,
        ]
        usage_parameters: list[Any] = [window["start_at"], window["end_at"], *user_parameters]
        if model_id:
            usage_conditions.append("l.public_model_id=?")
            usage_parameters.append(model_id)
        rows = connection.execute(
            f"""WITH {bucket_definition},
                       filtered_usage AS (
                         SELECT l.* FROM usage_ledger l
                         JOIN users u ON u.id=l.user_id
                         WHERE {' AND '.join(usage_conditions)}
                       )
                SELECT b.bucket_index,COUNT(l.id) requests,
                       COUNT(DISTINCT l.user_id) active_users,
                       COALESCE(SUM(l.input_tokens),0) input_tokens,
                       COALESCE(SUM(l.output_tokens),0) output_tokens,
                       COALESCE(SUM(l.input_tokens+l.output_tokens),0) total_tokens,
                       COALESCE(SUM(l.cached_tokens),0) cached_tokens,
                       COALESCE(SUM(l.reasoning_tokens),0) reasoning_tokens,
                       COALESCE(SUM(l.amount_micros),0) amount_micros
                FROM buckets b LEFT JOIN filtered_usage l
                  ON l.occurred_at>=b.start_at AND l.occurred_at<b.end_at
                GROUP BY b.bucket_index ORDER BY b.bucket_index""",
            [*parameters, *usage_parameters],
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

    def admin_usage_report(
        self,
        period: str = "day",
        anchor: str = "",
        start_date: str = "",
        end_date: str = "",
        model_id: str = "",
        user_query: str = "",
        status: str = "",
        page: int = 1,
        page_size: int = 20,
        export_all: bool = False,
    ) -> dict[str, Any]:
        """返回包含零用量用户的全员统计；筛选在数据库侧完成。"""
        if any(len(value) > 200 for value in (anchor, start_date, end_date, model_id, user_query, status)):
            raise ValueError("筛选条件过长")
        if page < 1 or page_size < 1 or page_size > 100:
            raise ValueError("分页范围无效")
        window = self.usage_report_window(period, anchor, start_date, end_date)
        user_conditions, user_parameters = self._report_user_conditions(user_query, status)
        ledger_conditions = [
            "l.occurred_at>=?",
            "l.occurred_at<?",
            *user_conditions,
        ]
        ledger_parameters: list[Any] = [window["start_at"], window["end_at"], *user_parameters]
        if model_id:
            ledger_conditions.append("l.public_model_id=?")
            ledger_parameters.append(model_id)
        join_conditions = ["l.user_id=u.id", "l.occurred_at>=?", "l.occurred_at<?"]
        join_parameters: list[Any] = [window["start_at"], window["end_at"]]
        if model_id:
            join_conditions.append("l.public_model_id=?")
            join_parameters.append(model_id)

        with self.db.connect() as connection:
            summary_row = connection.execute(
                f"""SELECT COUNT(l.id) requests,COUNT(DISTINCT l.user_id) active_users,
                           COALESCE(SUM(l.input_tokens),0) input_tokens,
                           COALESCE(SUM(l.output_tokens),0) output_tokens,
                           COALESCE(SUM(l.input_tokens+l.output_tokens),0) total_tokens,
                           COALESCE(SUM(l.cached_tokens),0) cached_tokens,
                           COALESCE(SUM(l.reasoning_tokens),0) reasoning_tokens,
                           COALESCE(SUM(l.amount_micros),0) amount_micros,
                           MAX(l.occurred_at) last_activity_at
                    FROM usage_ledger l JOIN users u ON u.id=l.user_id
                    WHERE {' AND '.join(ledger_conditions)}""",
                ledger_parameters,
            ).fetchone()
            summary = dict(summary_row)
            total_users = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM users u WHERE {' AND '.join(user_conditions)}",
                    user_parameters,
                ).fetchone()[0]
            )
            summary["total_users"] = total_users
            summary["inactive_users"] = max(0, total_users - int(summary["active_users"] or 0))
            summary["average_tokens_per_request"] = (
                round(int(summary["total_tokens"] or 0) / int(summary["requests"]), 2)
                if int(summary["requests"] or 0)
                else 0
            )
            pages = max(1, (total_users + page_size - 1) // page_size)
            effective_page = min(page, pages)
            limit_clause = "" if export_all else " LIMIT ? OFFSET ?"
            staff_parameters = [*join_parameters, *user_parameters]
            if not export_all:
                staff_parameters.extend((page_size, (effective_page - 1) * page_size))
            users = self.rows(
                connection.execute(
                    f"""SELECT u.id user_id,u.email,u.login_name,u.status,u.created_at,
                               COALESCE(b.balance_micros,0) balance_micros,
                               COUNT(l.id) requests,
                               COALESCE(SUM(l.input_tokens),0) input_tokens,
                               COALESCE(SUM(l.output_tokens),0) output_tokens,
                               COALESCE(SUM(l.input_tokens+l.output_tokens),0) total_tokens,
                               COALESCE(SUM(l.cached_tokens),0) cached_tokens,
                               COALESCE(SUM(l.reasoning_tokens),0) reasoning_tokens,
                               COALESCE(SUM(l.amount_micros),0) amount_micros,
                               MAX(l.occurred_at) last_activity_at
                        FROM users u
                        LEFT JOIN billing_accounts b ON b.user_id=u.id
                        LEFT JOIN usage_ledger l ON {' AND '.join(join_conditions)}
                        WHERE {' AND '.join(user_conditions)}
                        GROUP BY u.id,u.email,u.login_name,u.status,u.created_at,b.balance_micros
                        ORDER BY total_tokens DESC,requests DESC,u.email{limit_clause}""",
                    staff_parameters,
                ).fetchall()
            )
            timeseries = self._usage_report_series(
                connection, window, model_id, user_query, status
            )
            models = self.rows(
                connection.execute(
                    f"""SELECT l.public_model_id,COUNT(l.id) requests,
                               COUNT(DISTINCT l.user_id) active_users,
                               COALESCE(SUM(l.input_tokens),0) input_tokens,
                               COALESCE(SUM(l.output_tokens),0) output_tokens,
                               COALESCE(SUM(l.input_tokens+l.output_tokens),0) total_tokens,
                               COALESCE(SUM(l.amount_micros),0) amount_micros
                        FROM usage_ledger l JOIN users u ON u.id=l.user_id
                        WHERE {' AND '.join(ledger_conditions)}
                        GROUP BY l.public_model_id
                        ORDER BY total_tokens DESC,l.public_model_id""",
                    ledger_parameters,
                ).fetchall()
            )
        return {
            "generated_at": now(),
            "timezone": window["timezone"],
            "source": "usage_ledger",
            "token_definition": "input_tokens + output_tokens",
            "range": {key: value for key, value in window.items() if key != "buckets"},
            "filters": {"model": model_id, "user_query": user_query, "status": status},
            "summary": summary,
            "timeseries": timeseries,
            "models": models,
            "users": users,
            "pagination": {
                "page": effective_page,
                "page_size": page_size,
                "pages": pages,
                "total": total_users,
            },
        }

    @staticmethod
    def _xlsx_column_name(index: int) -> str:
        result = ""
        while index:
            index, remainder = divmod(index - 1, 26)
            result = chr(65 + remainder) + result
        return result

    @staticmethod
    def _xlsx_text(value: Any) -> str:
        """过滤 OOXML 非法控制字符，并阻止表格公式注入。"""
        text = "" if value is None else str(value)
        text = "".join(
            character
            for character in text
            if character in "\t\n\r" or ord(character) >= 32
        )
        if text.startswith(("=", "+", "-", "@")):
            text = "'" + text
        return text

    @classmethod
    def _xlsx_sheet(
        cls,
        rows: list[list[Any]],
        widths: list[float],
        header_rows: int = 1,
        money_columns: set[int] | None = None,
    ) -> str:
        money_columns = money_columns or set()
        row_xml: list[str] = []
        for row_index, row in enumerate(rows, 1):
            cells: list[str] = []
            for column_index, value in enumerate(row, 1):
                reference = f"{cls._xlsx_column_name(column_index)}{row_index}"
                style = 1 if row_index <= header_rows else (2 if column_index in money_columns else 0)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    cells.append(f'<c r="{reference}" s="{style}"><v>{value}</v></c>')
                else:
                    safe = xml_escape(cls._xlsx_text(value))
                    cells.append(
                        f'<c r="{reference}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{safe}</t></is></c>'
                    )
            row_xml.append(f'<row r="{row_index}">{"".join(cells)}</row>')
        column_xml = "".join(
            f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>'
            for index, width in enumerate(widths, 1)
        )
        last_column = cls._xlsx_column_name(max((len(row) for row in rows), default=1))
        last_row = max(1, len(rows))
        filter_xml = (
            f'<autoFilter ref="A{header_rows}:{last_column}{last_row}"/>'
            if header_rows and last_row > header_rows
            else ""
        )
        pane_xml = (
            f'<pane ySplit="{header_rows}" topLeftCell="A{header_rows + 1}" activePane="bottomLeft" state="frozen"/>'
            if header_rows
            else ""
        )
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<sheetViews><sheetView workbookViewId="0">{pane_xml}</sheetView></sheetViews>'
            '<sheetFormatPr defaultRowHeight="18"/>'
            f'<cols>{column_xml}</cols><sheetData>{"".join(row_xml)}</sheetData>{filter_xml}'
            '</worksheet>'
        )

    @classmethod
    def usage_report_workbook(cls, report: dict[str, Any]) -> bytes:
        """仅使用标准库生成真实 XLSX，避免生产环境引入大型表格依赖。"""
        summary = report["summary"]
        range_info = report["range"]
        generated = datetime.datetime.fromtimestamp(
            report["generated_at"], datetime.timezone.utc
        ).isoformat()
        summary_rows = [
            ["指标", "值"],
            ["统计范围", range_info["label"]],
            ["时区", report["timezone"]],
            ["导出时间（UTC）", generated],
            ["全员人数", int(summary["total_users"] or 0)],
            ["活跃人数", int(summary["active_users"] or 0)],
            ["零用量人数", int(summary["inactive_users"] or 0)],
            ["请求数", int(summary["requests"] or 0)],
            ["输入 Token", int(summary["input_tokens"] or 0)],
            ["输出 Token", int(summary["output_tokens"] or 0)],
            ["Token 总量", int(summary["total_tokens"] or 0)],
            ["缓存命中 Token", int(summary["cached_tokens"] or 0)],
            ["思考 Token", int(summary["reasoning_tokens"] or 0)],
            ["余额扣款（USD）", int(summary["amount_micros"] or 0) / 1_000_000],
        ]
        user_rows: list[list[Any]] = [[
            "用户", "登录名", "状态", "请求数", "输入 Token", "输出 Token",
            "Token 总量", "缓存命中 Token", "思考 Token", "余额扣款（USD）",
            "当前余额（USD）", "最后活跃时间", "注册时间",
        ]]
        for row in report["users"]:
            user_rows.append([
                row["email"], row.get("login_name") or "", row["status"],
                int(row["requests"] or 0), int(row["input_tokens"] or 0),
                int(row["output_tokens"] or 0), int(row["total_tokens"] or 0),
                int(row["cached_tokens"] or 0), int(row["reasoning_tokens"] or 0),
                int(row["amount_micros"] or 0) / 1_000_000,
                int(row["balance_micros"] or 0) / 1_000_000,
                (
                    datetime.datetime.fromtimestamp(row["last_activity_at"], datetime.timezone.utc).isoformat()
                    if row.get("last_activity_at") else ""
                ),
                datetime.datetime.fromtimestamp(row["created_at"], datetime.timezone.utc).isoformat(),
            ])
        trend_rows: list[list[Any]] = [[
            "时间桶", "请求数", "活跃用户", "输入 Token", "输出 Token", "Token 总量",
            "缓存命中 Token", "思考 Token", "余额扣款（USD）",
        ]]
        for row in report["timeseries"]:
            trend_rows.append([
                row["label"], int(row.get("requests") or 0), int(row.get("active_users") or 0),
                int(row.get("input_tokens") or 0), int(row.get("output_tokens") or 0),
                int(row.get("total_tokens") or 0), int(row.get("cached_tokens") or 0),
                int(row.get("reasoning_tokens") or 0), int(row.get("amount_micros") or 0) / 1_000_000,
            ])
        model_rows: list[list[Any]] = [[
            "模型 ID", "请求数", "活跃用户", "输入 Token", "输出 Token", "Token 总量", "余额扣款（USD）",
        ]]
        for row in report["models"]:
            model_rows.append([
                row["public_model_id"], int(row["requests"] or 0), int(row["active_users"] or 0),
                int(row["input_tokens"] or 0), int(row["output_tokens"] or 0),
                int(row["total_tokens"] or 0), int(row["amount_micros"] or 0) / 1_000_000,
            ])
        sheets = [
            ("汇总", cls._xlsx_sheet(summary_rows, [24, 34])),
            ("用户用量", cls._xlsx_sheet(user_rows, [34, 20, 12, 12, 16, 16, 18, 18, 16, 18, 18, 25, 25], money_columns={10, 11})),
            ("时间趋势", cls._xlsx_sheet(trend_rows, [20, 12, 14, 16, 16, 18, 18, 16, 18], money_columns={9})),
            ("模型用量", cls._xlsx_sheet(model_rows, [30, 12, 14, 16, 16, 18, 18], money_columns={7})),
        ]
        content_types = "".join(
            f'<Override PartName="/xl/worksheets/sheet{index}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            for index in range(1, len(sheets) + 1)
        )
        workbook_sheets = "".join(
            f'<sheet name="{xml_escape(name)}" sheetId="{index}" r:id="rId{index}"/>'
            for index, (name, _content) in enumerate(sheets, 1)
        )
        workbook_relationships = "".join(
            f'<Relationship Id="rId{index}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{index}.xml"/>'
            for index in range(1, len(sheets) + 1)
        )
        workbook_relationships += (
            f'<Relationship Id="rId{len(sheets) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        )
        files = {
            "[Content_Types].xml": (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="xml" ContentType="application/xml"/>'
                '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
                '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
                f'{content_types}</Types>'
            ),
            "_rels/.rels": (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
                '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
                '</Relationships>'
            ),
            "docProps/app.xml": (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
                'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"><Application>LLMCtl</Application></Properties>'
            ),
            "docProps/core.xml": (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
                'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
                'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><dc:creator>LLMCtl</dc:creator>'
                '<dc:title>全员用量报表</dc:title></cp:coreProperties>'
            ),
            "xl/workbook.xml": (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                f'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>{workbook_sheets}</sheets></workbook>'
            ),
            "xl/_rels/workbook.xml.rels": (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                f'{workbook_relationships}</Relationships>'
            ),
            "xl/styles.xml": (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                '<fonts count="2"><font><sz val="11"/><name val="Aptos"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Aptos"/></font></fonts>'
                '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF167EAF"/><bgColor indexed="64"/></patternFill></fill></fills>'
                '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
                '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
                '<cellXfs count="3"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
                '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'
                '<xf numFmtId="4" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs>'
                '</styleSheet>'
            ),
        }
        for index, (_name, content) in enumerate(sheets, 1):
            files[f"xl/worksheets/sheet{index}.xml"] = content
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for path, content in files.items():
                archive.writestr(path, content.encode("utf-8"))
        return output.getvalue()

    def admin_usage_report_export(self, **filters: Any) -> tuple[bytes, str, dict[str, Any]]:
        export_filters = dict(filters)
        export_filters.update(page=1, page_size=100, export_all=True)
        report = self.admin_usage_report(**export_filters)
        content = self.usage_report_workbook(report)
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return content, f"LLMCtl-全员用量-{stamp}.xlsx", report

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
