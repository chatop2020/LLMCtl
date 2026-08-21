#!/usr/bin/env python3
"""账户门户的数据库结构、访问适配与可恢复迁移。"""

from __future__ import annotations

from account_portal_common import *

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
        self.runtime = DatabaseRuntime(config.db_path)
        self._connection_condition = threading.Condition()
        self._active_connections = 0
        self._migration_exclusive = False

    @staticmethod
    def _pymysql() -> Any:
        try:
            return importlib.import_module("pymysql")
        except ImportError as error:
            raise DatabaseCapabilityError(
                "MySQL 运行时未激活；请先在服务器执行 llmctl database enable-mysql"
            ) from error

    def _mysql_raw_connection(self, config: dict[str, Any] | None = None) -> Any:
        capability = self.runtime.capability()
        if not capability["enabled"]:
            raise DatabaseCapabilityError(
                "MySQL 能力尚未激活；请先执行 llmctl database enable-mysql"
            )
        mysql = self._pymysql()
        config = config or self.runtime.config(include_password=True)
        if not all(config.get(name) for name in ("host", "database", "username", "password")):
            raise DatabaseCapabilityError("MySQL 连接信息尚未配置完整")
        ssl_config: dict[str, Any] | None = None
        tls_mode = str(config.get("tls_mode") or "preferred")
        if tls_mode in {"preferred", "required"}:
            ssl_config = {"check_hostname": False}
        elif tls_mode == "verify_ca":
            ssl_config = {"ca": str(config.get("ca_file") or ""), "check_hostname": True}
        connect_options = {
            "host": str(config["host"]),
            "port": int(config["port"]),
            "user": str(config["username"]),
            "password": str(config["password"]),
            "database": str(config["database"]),
            "charset": "utf8mb4",
            "autocommit": False,
            "connect_timeout": 5,
            "read_timeout": 30,
            "write_timeout": 30,
            "ssl": ssl_config,
            "cursorclass": mysql.cursors.DictCursor,
        }
        try:
            return mysql.connect(**connect_options)
        except mysql.err.OperationalError as error:
            # preferred 模式只在服务器明确不支持 TLS（2026）时回退明文；
            # 认证、网络和证书错误不能被回退掩盖。
            if tls_mode != "preferred" or not error.args or error.args[0] != 2026:
                raise
            connect_options["ssl"] = None
            return mysql.connect(**connect_options)

    @contextlib.contextmanager
    def _sqlite_connect(self):
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

    @contextlib.contextmanager
    def connect(self):
        with self._connection_condition:
            if self._migration_exclusive:
                raise DatabaseMigrationError("数据库正在迁移，请稍后重试")
            self._active_connections += 1
        try:
            if self.runtime.config(include_password=False)["active_backend"] != "mysql":
                with self._sqlite_connect() as connection:
                    yield connection
                return
            raw_connection = self._mysql_raw_connection()
            connection = MySQLConnection(raw_connection)
            try:
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
        finally:
            with self._connection_condition:
                self._active_connections -= 1
                self._connection_condition.notify_all()

    @contextlib.contextmanager
    def migration_exclusive(self):
        """暂停新数据库请求，并等待已开始的事务结束。"""
        with self._connection_condition:
            if self._migration_exclusive:
                raise DatabaseMigrationError("已有数据库迁移正在执行")
            self._migration_exclusive = True
            while self._active_connections:
                self._connection_condition.wait(timeout=1)
        try:
            yield
        finally:
            with self._connection_condition:
                self._migration_exclusive = False
                self._connection_condition.notify_all()

    @property
    def is_mysql(self) -> bool:
        return self.runtime.config(include_password=False)["active_backend"] == "mysql"

    def is_integrity_error(self, error: BaseException) -> bool:
        if isinstance(error, sqlite3.IntegrityError):
            return True
        with contextlib.suppress(DatabaseCapabilityError):
            mysql = self._pymysql()
            return isinstance(error, mysql.err.IntegrityError)
        return False

    def initialize(self) -> None:
        if self.runtime.config(include_password=False)["active_backend"] == "mysql":
            self._initialize_mysql()
            return
        self._initialize_sqlite()

    def _initialize_mysql(self) -> None:
        with self.connect() as connection:
            marker = connection.execute(
                "SELECT schema_version FROM llmctl_database_meta WHERE id=1"
            ).fetchone()
            if not marker or int(marker["schema_version"] or 0) != MYSQL_SCHEMA_VERSION:
                raise SystemExit(
                    "MySQL schema 未通过 LLMCtl 迁移校验；已停止，避免使用不完整数据"
                )
            for table in ("users", "settings", "usage_ledger", "billing_accounts"):
                connection.execute(f"SELECT 1 FROM `{table}` LIMIT 1").fetchone()

    def _initialize_sqlite(self) -> None:
        self.config.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._sqlite_connect() as connection:
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
            # 旧安装在注册初始关闭时会创建空公开来源；修复这些值，避免 SMTP
            # 和后续注册变更被无关空字段阻断。
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
                # 既有 Key 早于此控制，管理员明确修改前保持不限；新注册使用
                # default_max_sessions（默认 1）。
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
            # 历史记录只保存钱包扣款；根据不可变价格快照重建标价费用，让升级后
            # 立即显示真实账单拆分。
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
        """把全部剩余赠送 Token 一次性折算为现金。

        迁移全程使用事务并采用失败关闭策略：任一有效赠额无法定价时，
        所有赠额均保持不变；唯一来源标识可防止初始化重试时重复入账。
        """
        stamp = now()
        grants = connection.execute(
            "SELECT * FROM token_grants WHERE status='active' AND tokens_remaining>0 "
            "ORDER BY created_at,id"
        ).fetchall()
        price_case = (
            "CASE WHEN input_price_micros>=output_price_micros "
            "AND input_price_micros>=cached_price_micros "
            "AND input_price_micros>=reasoning_price_micros THEN input_price_micros "
            "WHEN output_price_micros>=cached_price_micros "
            "AND output_price_micros>=reasoning_price_micros THEN output_price_micros "
            "WHEN cached_price_micros>=reasoning_price_micros THEN cached_price_micros "
            "ELSE reasoning_price_micros END"
        )
        public_rate_row = connection.execute(
            f"SELECT MAX({price_case}) rate FROM published_models "
            "WHERE status='published'"
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
                    f"SELECT {price_case} rate FROM published_models WHERE id=?",
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
            # initialize() 为新设置写入了临时值；先删除它，使模型价格可用后的
            # 重试仍能识别并转换旧注册策略。
            connection.execute(
                "DELETE FROM settings WHERE key='default_welcome_balance'"
            )
        if not blocked:
            # 所有赠额完成估值后才停用旧来源。
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
        """在受管模型及其价格就绪后重试旧计费数据转换。"""
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
        """返回 ``llmctl info`` 所需的门户持久化状态。"""
        backend = "mysql" if self.is_mysql else "sqlite"
        if backend == "sqlite" and not self.config.db_path.is_file():
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
            integrity = "connected"
            if backend == "sqlite":
                integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        if not show_secrets and settings.get("smtp_password"):
            settings["smtp_password"] = "<redacted>"
        database: dict[str, Any] = {
            "backend": backend,
            "quick_check": integrity,
        }
        if backend == "sqlite":
            stat = self.config.db_path.stat()
            database.update(
                {
                    "path": str(self.config.db_path),
                    "bytes": stat.st_size,
                    "mode": oct(stat.st_mode & 0o777),
                }
            )
        else:
            database.update(self.runtime.config(include_password=show_secrets))
        return {
            "version": APP_VERSION,
            "database": database,
            "settings": settings,
            "counts": counts,
        }

    def audit(
        self, actor: str, action: str, target: str, status: str, remote: str, detail: Any = ""
    ) -> None:
        """保存完整可审查详情，并在极端输入超过安全上限时明确标记截断。"""

        if not isinstance(detail, str):
            detail = json.dumps(detail, ensure_ascii=False, separators=(",", ":"))
        if len(detail) > REQUEST_DETAIL_TEXT_LIMIT:
            detail = (
                detail[:REQUEST_DETAIL_TEXT_LIMIT]
                + "\n…[详情超过 1,000,000 个字符，已按安全上限截断]"
            )
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO audit_events(created_at,actor,action,target,status,remote_addr,detail) VALUES(?,?,?,?,?,?,?)",
                (now(), actor, action, target, status, remote, detail),
            )


class DatabaseMigrationManager:
    """在后台执行 SQLite 到 MySQL 的可验证单向迁移。"""

    def __init__(self, database: Database):
        self.database = database
        self.runtime = database.runtime
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._progress_token = ""

    def snapshot(self) -> dict[str, Any]:
        capability = self.runtime.capability()
        config = self.runtime.config(include_password=False)
        migration = self.runtime.migration()
        thread_running = bool(self._thread and self._thread.is_alive())
        sqlite = {
            "path": str(self.database.config.db_path),
            "exists": self.database.config.db_path.is_file(),
            "bytes": (
                self.database.config.db_path.stat().st_size
                if self.database.config.db_path.is_file()
                else 0
            ),
        }
        return {
            "capability": capability,
            "config": config,
            "migration": migration,
            "sqlite": sqlite,
            "busy": thread_running or migration.get("status") == "running",
            "requirements": {
                "server": "MySQL 8.0+",
                "empty_database": True,
                "scope": "LLMCtl account portal only",
            },
        }

    def save_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.runtime.capability()["enabled"]:
            raise DatabaseCapabilityError(
                "MySQL 能力尚未激活；请先在服务器执行 llmctl database enable-mysql"
            )
        if self.database.is_mysql:
            raise DatabaseMigrationError(
                "当前已使用 MySQL；为避免运行中换库，请先回滚到 SQLite"
            )
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise DatabaseMigrationError("数据库迁移执行期间不能修改连接配置")
            return self.runtime.save_config(payload)

    def test(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if payload:
            self.save_config(payload)
        config = self.runtime.config(include_password=True)
        started = time.monotonic()
        connection = self.database._mysql_raw_connection(config)
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT VERSION() AS version, DATABASE() AS database_name")
                row = cursor.fetchone()
                cursor.execute("SHOW TABLES")
                tables = cursor.fetchall()
            version = str(row.get("version") or "")
            match = re.match(r"^(\d+)\.(\d+)", version)
            if not match or int(match.group(1)) < 8:
                raise DatabaseMigrationError(
                    f"需要 MySQL 8.0 或更高版本，当前为 {version or 'unknown'}"
                )
            return {
                "ok": True,
                "version": version,
                "database": str(row.get("database_name") or ""),
                "tables": len(tables),
                "empty": not tables,
                "latency_ms": round((time.monotonic() - started) * 1000, 1),
            }
        finally:
            connection.close()

    def start(self, actor: str) -> dict[str, Any]:
        if self.database.is_mysql:
            raise DatabaseMigrationError("门户当前已经使用 MySQL")
        if not self.database.config.db_path.is_file():
            raise DatabaseMigrationError("SQLite 源数据库不存在")
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise DatabaseMigrationError("已有数据库迁移正在执行")
            test_result = self.test()
            if not test_result["empty"]:
                raise DatabaseMigrationError(
                    "目标 MySQL database 必须为空；不会覆盖已有表"
                )
            migration_id = str(uuid.uuid4())
            self._progress_token = secrets.token_urlsafe(32)
            self.runtime.save_migration(
                id=migration_id,
                status="running",
                stage="准备冻结 SQLite 写入",
                progress=1,
                actor=actor,
                started_at=now(),
                finished_at=0,
                error="",
                backup_path="",
                source_backend="sqlite",
                target_backend="mysql",
                table_counts={},
            )
            self._thread = threading.Thread(
                target=self._run,
                args=(migration_id,),
                name="llmctl-database-migration",
                daemon=True,
            )
            self._thread.start()
        result = self.snapshot()
        result["progress_token"] = self._progress_token
        return result

    def progress(self, supplied_token: str) -> dict[str, Any]:
        """迁移期间不访问业务数据库，使用一次性随机令牌返回进度。"""
        if not self._progress_token or not hmac.compare_digest(
            self._progress_token, supplied_token
        ):
            raise PermissionError("迁移进度令牌无效")
        return {
            "migration": self.runtime.migration(),
            "busy": bool(self._thread and self._thread.is_alive()),
        }

    def rollback_to_sqlite(self, confirmation: str, actor: str) -> dict[str, Any]:
        if confirmation != "ROLLBACK_TO_SQLITE":
            raise ValueError("请输入 ROLLBACK_TO_SQLITE 确认回滚")
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise DatabaseMigrationError("数据库迁移执行期间不能回滚")
            if not self.database.is_mysql:
                raise DatabaseMigrationError("当前没有使用 MySQL")
            if not self.database.config.db_path.is_file():
                raise DatabaseMigrationError("原 SQLite 数据库不存在，不能回滚")
            with self.database._sqlite_connect() as source:
                quick_check = str(source.execute("PRAGMA quick_check").fetchone()[0])
            if quick_check != "ok":
                raise DatabaseMigrationError(
                    f"SQLite 完整性检查失败：{quick_check}"
                )
            self.runtime.set_active_backend("sqlite")
            self.runtime.save_migration(
                status="rolled_back",
                stage="已回滚到迁移时保留的 SQLite",
                progress=100,
                rolled_back_at=now(),
                rolled_back_by=actor,
                error="",
            )
        return self.snapshot()

    @staticmethod
    def _identifier(value: str) -> str:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            raise DatabaseMigrationError(f"发现不安全的数据库标识符：{value}")
        return f"`{value}`"

    @staticmethod
    def _sqlite_default(value: Any, mysql_type: str) -> str:
        if value is None:
            return ""
        raw = str(value).strip()
        if re.fullmatch(r"-?\d+(?:\.\d+)?", raw):
            return f" DEFAULT {raw}"
        if raw.upper() == "NULL":
            return " DEFAULT NULL"
        if len(raw) >= 2 and raw[0] in {"'", '"'} and raw[-1] == raw[0]:
            literal = raw[1:-1].replace(raw[0] * 2, raw[0])
            escaped = literal.replace("'", "''")
            if mysql_type in {"LONGTEXT", "LONGBLOB"}:
                return f" DEFAULT ('{escaped}')"
            return f" DEFAULT '{escaped}'"
        return ""

    def _mysql_table_definition(
        self, source: sqlite3.Connection, table: str
    ) -> tuple[str, list[dict[str, Any]]]:
        columns = [dict(row) for row in source.execute(f"PRAGMA table_info({self._identifier(table)})")]
        indexes = [dict(row) for row in source.execute(f"PRAGMA index_list({self._identifier(table)})")]
        foreign_keys = [
            dict(row)
            for row in source.execute(f"PRAGMA foreign_key_list({self._identifier(table)})")
        ]
        keyed: set[str] = {str(row["name"]) for row in columns if int(row["pk"] or 0)}
        for index in indexes:
            keyed.update(
                str(row["name"])
                for row in source.execute(
                    f"PRAGMA index_info({self._identifier(str(index['name']))})"
                )
            )
        keyed.update(str(row["from"]) for row in foreign_keys)
        keyed.update(str(row["to"]) for row in foreign_keys)
        primary = sorted(
            (row for row in columns if int(row["pk"] or 0)),
            key=lambda row: int(row["pk"]),
        )
        primary_names = {str(row["name"]) for row in primary}
        auto_primary = (
            len(primary) == 1
            and "INT" in str(primary[0]["type"] or "").upper()
        )
        definitions: list[str] = []
        for column in columns:
            declared = str(column["type"] or "TEXT").upper()
            name = str(column["name"])
            if "INT" in declared:
                mysql_type = "BIGINT"
            elif any(value in declared for value in ("REAL", "FLOA", "DOUB")):
                mysql_type = "DOUBLE"
            elif "BLOB" in declared:
                mysql_type = "LONGBLOB"
            elif name in keyed:
                mysql_type = "VARCHAR(191)"
            else:
                mysql_type = "LONGTEXT"
            item = f"{self._identifier(name)} {mysql_type}"
            if auto_primary and name == str(primary[0]["name"]):
                item += " NOT NULL AUTO_INCREMENT PRIMARY KEY"
            else:
                # SQLite 的复合主键列可能在 PRAGMA table_info 中仍显示
                # notnull=0；MySQL 要求主键的每一列都显式声明 NOT NULL。
                item += (
                    " NOT NULL"
                    if name in primary_names or int(column["notnull"] or 0)
                    else " NULL"
                )
                item += self._sqlite_default(column["dflt_value"], mysql_type)
            definitions.append(item)
        if primary and not auto_primary:
            definitions.append(
                "PRIMARY KEY ("
                + ",".join(self._identifier(str(row["name"])) for row in primary)
                + ")"
            )
        for foreign in foreign_keys:
            definitions.append(
                "FOREIGN KEY ("
                + self._identifier(str(foreign["from"]))
                + ") REFERENCES "
                + self._identifier(str(foreign["table"]))
                + " ("
                + self._identifier(str(foreign["to"]))
                + ") ON UPDATE "
                + str(foreign["on_update"] or "NO ACTION")
                + " ON DELETE "
                + str(foreign["on_delete"] or "NO ACTION")
            )
        statement = (
            f"CREATE TABLE {self._identifier(table)} ("
            + ",".join(definitions)
            + ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
        )
        return statement, indexes

    def _create_indexes(
        self,
        source: sqlite3.Connection,
        cursor: Any,
        table: str,
        indexes: list[dict[str, Any]],
    ) -> None:
        for index in indexes:
            if str(index.get("origin")) == "pk":
                continue
            name = str(index["name"])
            columns = [
                str(row["name"])
                for row in source.execute(
                    f"PRAGMA index_info({self._identifier(name)})"
                )
            ]
            if not columns:
                continue
            prefix = "CREATE UNIQUE INDEX" if int(index.get("unique") or 0) else "CREATE INDEX"
            cursor.execute(
                f"{prefix} {self._identifier(name[:64])} ON {self._identifier(table)} ("
                + ",".join(self._identifier(value) for value in columns)
                + ")"
            )

    @staticmethod
    def _digest_value(value: Any) -> Any:
        if isinstance(value, bytes):
            return {"base64": base64.b64encode(value).decode("ascii")}
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return str(value)

    def _table_digest(
        self, connection: Any, table: str, columns: list[str], primary: list[str]
    ) -> tuple[int, str]:
        order = primary or columns
        statement = (
            "SELECT "
            + ",".join(self._identifier(value) for value in columns)
            + f" FROM {self._identifier(table)} ORDER BY "
            + ",".join(self._identifier(value) for value in order)
        )
        digest = hashlib.sha256()
        count = 0
        cursor = connection.execute(statement) if isinstance(connection, sqlite3.Connection) else connection.cursor()
        if not isinstance(connection, sqlite3.Connection):
            cursor.execute(statement)
        while True:
            rows = cursor.fetchmany(500)
            if not rows:
                break
            for row in rows:
                values = [
                    self._digest_value(row[name] if hasattr(row, "keys") else row[index])
                    for index, name in enumerate(columns)
                ]
                digest.update(
                    json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode(
                        "utf-8"
                    )
                    + b"\n"
                )
                count += 1
        return count, digest.hexdigest()

    def _run(self, migration_id: str) -> None:
        source: sqlite3.Connection | None = None
        target: Any = None
        backup_path = ""
        created_tables: list[str] = []
        target_cleaned = False
        exclusive = self.database.migration_exclusive()
        exclusive_entered = False
        try:
            # 全程暂停门户数据库请求，防止校验完成后仍有请求写入旧 SQLite。
            # 管理页面通过独立的无数据库状态接口轮询进度。
            exclusive.__enter__()
            exclusive_entered = True
            self.runtime.save_migration(
                stage="已暂停门户数据库请求，正在创建 SQLite 备份", progress=5
            )
            source = sqlite3.connect(self.database.config.db_path, timeout=30)
            source.row_factory = sqlite3.Row
            source.execute("PRAGMA foreign_keys=ON")
            backup_directory = self.database.config.db_path.parent / "backups"
            backup_directory.mkdir(parents=True, exist_ok=True)
            backup = backup_directory / (
                "before-mysql-"
                + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                + ".db"
            )
            backup_connection = sqlite3.connect(backup)
            try:
                source.backup(backup_connection)
                check = str(backup_connection.execute("PRAGMA quick_check").fetchone()[0])
                if check != "ok":
                    raise DatabaseMigrationError(f"SQLite 备份完整性检查失败：{check}")
            finally:
                backup_connection.close()
            os.chmod(backup, 0o600)
            backup_path = str(backup)
            source.execute("BEGIN IMMEDIATE")
            self.runtime.save_migration(
                backup_path=backup_path,
                stage="SQLite 备份完成，正在创建 MySQL schema",
                progress=12,
            )

            target = self.database._mysql_raw_connection()
            target.autocommit(False)
            with target.cursor() as cursor:
                cursor.execute("SHOW TABLES")
                if cursor.fetchall():
                    raise DatabaseMigrationError(
                        "目标 MySQL database 在迁移开始后出现了表，已拒绝覆盖"
                    )
                cursor.execute("SET FOREIGN_KEY_CHECKS=0")
                tables = [
                    str(row["name"])
                    for row in source.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name NOT LIKE 'sqlite_%' ORDER BY rootpage"
                    )
                ]
                definitions: dict[str, list[dict[str, Any]]] = {}
                for table in tables:
                    ddl, indexes = self._mysql_table_definition(source, table)
                    cursor.execute(ddl)
                    created_tables.append(table)
                    definitions[table] = indexes
                table_counts: dict[str, int] = {}
                for table_index, table in enumerate(tables):
                    columns = [
                        str(row["name"])
                        for row in source.execute(
                            f"PRAGMA table_info({self._identifier(table)})"
                        )
                    ]
                    select_cursor = source.execute(
                        "SELECT "
                        + ",".join(self._identifier(value) for value in columns)
                        + f" FROM {self._identifier(table)}"
                    )
                    insert = (
                        f"INSERT INTO {self._identifier(table)} ("
                        + ",".join(self._identifier(value) for value in columns)
                        + ") VALUES ("
                        + ",".join("%s" for _ in columns)
                        + ")"
                    )
                    copied = 0
                    while True:
                        batch = select_cursor.fetchmany(500)
                        if not batch:
                            break
                        cursor.executemany(
                            insert,
                            [tuple(row[value] for value in columns) for row in batch],
                        )
                        copied += len(batch)
                    table_counts[table] = copied
                    self.runtime.save_migration(
                        stage=f"正在复制 {table}",
                        progress=15 + int((table_index + 1) * 55 / max(1, len(tables))),
                        table_counts=table_counts,
                    )
                for table in tables:
                    self._create_indexes(source, cursor, table, definitions[table])
                cursor.execute("SET FOREIGN_KEY_CHECKS=1")
            target.commit()
            self.runtime.save_migration(stage="正在逐表校验数量与 SHA-256", progress=75)
            validation: dict[str, Any] = {}
            for table_index, table in enumerate(tables):
                info = [
                    dict(row)
                    for row in source.execute(
                        f"PRAGMA table_info({self._identifier(table)})"
                    )
                ]
                columns = [str(row["name"]) for row in info]
                primary = [
                    str(row["name"])
                    for row in sorted(
                        (row for row in info if int(row["pk"] or 0)),
                        key=lambda value: int(value["pk"]),
                    )
                ]
                source_count, source_digest = self._table_digest(
                    source, table, columns, primary
                )
                target_count, target_digest = self._table_digest(
                    target, table, columns, primary
                )
                if source_count != target_count or source_digest != target_digest:
                    raise DatabaseMigrationError(
                        f"{table} 校验失败：SQLite={source_count}/{source_digest[:12]}，"
                        f"MySQL={target_count}/{target_digest[:12]}"
                    )
                validation[table] = {
                    "rows": source_count,
                    "sha256": source_digest,
                }
                self.runtime.save_migration(
                    stage=f"已校验 {table}",
                    progress=75 + int((table_index + 1) * 18 / max(1, len(tables))),
                )

            with target.cursor() as cursor:
                cursor.execute(
                    "CREATE TABLE llmctl_database_meta ("
                    "id BIGINT NOT NULL PRIMARY KEY,schema_version BIGINT NOT NULL,"
                    "source_backend VARCHAR(32) NOT NULL,migration_id VARCHAR(64) NOT NULL,"
                    "migrated_at BIGINT NOT NULL) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
                )
                created_tables.append("llmctl_database_meta")
                cursor.execute(
                    "INSERT INTO llmctl_database_meta VALUES (1,%s,'sqlite',%s,%s)",
                    (MYSQL_SCHEMA_VERSION, migration_id, now()),
                )
            target.commit()
            self.runtime.set_active_backend("mysql")
            source.commit()
            self.runtime.save_migration(
                status="completed",
                stage="迁移校验通过，门户已切换到 MySQL",
                progress=100,
                finished_at=now(),
                validation=validation,
                error="",
            )
        except Exception as error:
            with contextlib.suppress(Exception):
                if source:
                    source.rollback()
            with contextlib.suppress(Exception):
                if target:
                    target.rollback()
            # MySQL 的 CREATE TABLE 会隐式提交。目标库在开始时已经验证为空，
            # 因此失败时只删除本次迁移明确创建的表，使管理员修正问题后可重试；
            # 绝不扫描或删除迁移期间由其他系统新增的未知表。
            if target and created_tables:
                try:
                    with target.cursor() as cursor:
                        cursor.execute("SET FOREIGN_KEY_CHECKS=0")
                        for table in reversed(created_tables):
                            cursor.execute(
                                f"DROP TABLE IF EXISTS {self._identifier(table)}"
                            )
                        cursor.execute("SET FOREIGN_KEY_CHECKS=1")
                    target.commit()
                    target_cleaned = True
                except Exception as cleanup_error:
                    print(
                        f"[account-portal] MySQL migration cleanup failed: {cleanup_error}",
                        file=sys.stderr,
                        flush=True,
                    )
            self.runtime.set_active_backend("sqlite")
            self.runtime.save_migration(
                status="failed",
                stage=(
                    "迁移失败，已清理本次创建的 MySQL 表，门户继续使用 SQLite"
                    if target_cleaned
                    else "迁移失败，门户继续使用 SQLite"
                ),
                finished_at=now(),
                error=str(error)[:2000],
                backup_path=backup_path,
                target_cleaned=target_cleaned,
            )
            print(
                f"[account-portal] MySQL migration failed: {error}",
                file=sys.stderr,
                flush=True,
            )
        finally:
            if source:
                source.close()
            if target:
                target.close()
            if exclusive_entered:
                exclusive.__exit__(None, None, None)
