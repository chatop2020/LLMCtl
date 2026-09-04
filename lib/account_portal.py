#!/usr/bin/env python3
"""LLMCtl OmniRoute 账户门户的可执行入口与服务组合根。

各领域实现按数据库、外部控制面、HTTP、监控和业务策略拆分；本文件只负责
组合稳定公开类型、创建服务并处理命令行生命周期。
"""

from __future__ import annotations

import pathlib
import sys

# 安装后入口与测试夹具都可能按文件路径加载本模块。显式加入同级目录，确保
# 拆分出的领域模块始终从当前发布目录解析，不依赖调用者的工作目录。
_PORTAL_LIB_DIRECTORY = str(pathlib.Path(__file__).resolve().parent)
if _PORTAL_LIB_DIRECTORY not in sys.path:
    sys.path.insert(0, _PORTAL_LIB_DIRECTORY)

from account_portal_common import *
from account_portal_database import *
from account_portal_gateway import *
from account_portal_monitor import *
from account_portal_http import *
from account_portal_control_models import PortalModelControlMixin
from account_portal_control_usage import PortalUsageControlMixin
from account_portal_control_stress import PortalStressControlMixin
from account_portal_control_admin import PortalAdminControlMixin


class PortalControlPlane(
    PortalModelControlMixin,
    PortalUsageControlMixin,
    PortalStressControlMixin,
    PortalAdminControlMixin,
):
    """组合门户各业务策略，并持有跨领域共享的运行状态。

    该类型负责共享锁、用量对账时间和路由迁移备份位置，不重复实现各领域
    业务规则。数据库、OmniRoute 和配置对象由服务组合根注入。
    """

    def __init__(
        self,
        config: Config,
        db: Database,
        omni: OmniRouteClient,
        models: ModelDeploymentClient | None = None,
    ):
        """初始化共享状态。

        参数：
            config: 已完成启动校验的门户配置。
            db: 当前活动的门户数据库访问入口。
            omni: 访问 OmniRoute 管理 API 的受限客户端。
            models: 可选的模型部署注册表客户端；用于让本机运行配置覆盖接入层
                中陈旧的模型上下文元数据。
        """
        self.config, self.db, self.omni = config, db, omni
        self.models = models
        self.lock = threading.RLock()
        self.usage_reconciled_at: dict[str, int] = {}
        self.free_visibility_reconciled_at = 0
        self.public_combo_backup_dir: pathlib.Path | None = None


class PortalHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class PortalServer:
    def __init__(self, config: Config):
        self.config = config
        self.db = Database(config)
        self.db.initialize()
        self.database_migration = DatabaseMigrationManager(self.db)
        self.omni = OmniRouteClient(config)
        self.workflow = WorkflowClient()
        self.models = ModelDeploymentClient()
        self.control = PortalControlPlane(config, self.db, self.omni, self.models)
        self.monitor = SystemMonitor()
        # 首次升级进程可能仍是上一版本升级器；修改 OmniRoute 或任一 SQLite
        # 数据库前，先等待其健康验收和文件回滚窗口结束，避免控制面升级失败后
        # 留下只完成一半的 Responses API 路由迁移。
        self.route_migration_due = (
            time.monotonic() + PUBLIC_COMBO_MIGRATION_DELAY_SECONDS
        )
        self.route_migration_finished = False
        self.request_content_logging_due = time.monotonic()
        self.request_content_logging_ready = False
        # 第一次维护 tick 做一次完整权限投影；之后只在六小时兜底窗口执行全量
        # 对账，普通每分钟维护仅重试失败或待迁移用户。
        self.control.permission_full_reconciled_at = 0
        try:
            self.control.prepare_public_combo_migration_backup()
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

    def reconcile_request_content_logging(self) -> None:
        """在 OmniRoute 可用后开启受控请求正文与最终响应保留，并有限重试。"""

        current = time.monotonic()
        if current < self.request_content_logging_due:
            return
        try:
            changed = self.omni.ensure_request_content_logging()
        except Exception as error:
            self.request_content_logging_ready = False
            self.request_content_logging_due = current + 30
            print(
                f"[account-portal] request content logging warning: {error}",
                file=sys.stderr,
                flush=True,
            )
            return
        self.request_content_logging_ready = True
        self.request_content_logging_due = current + 300
        if changed:
            print(
                "[account-portal] OmniRoute request and response content logging enabled",
                flush=True,
            )

    def reconcile_public_routes_after_acceptance(self) -> None:
        current = time.monotonic()
        if current < self.route_migration_due:
            return
        if self.route_migration_finished:
            try:
                status = self.control.public_combo_route_status()
            except Exception as error:
                self.route_migration_due = current + PUBLIC_COMBO_MIGRATION_RETRY_SECONDS
                print(
                    f"[account-portal] public combo audit warning: {error}",
                    file=sys.stderr,
                    flush=True,
                )
                return
            if status["ready"]:
                self.route_migration_due = current + PUBLIC_COMBO_AUDIT_SECONDS
                return
            # 来源 Worker 集合或路由策略在上次成功迁移后变化；无需等待再次升级，
            # 立即修复公开镜像。
            self.route_migration_finished = False
        try:
            route_migration = self.control.reconcile_public_combo_routes()
        except Exception as error:
            # 保留旧路由继续服务并稍后重试；备份过程会在修改任何网关路由前故障关闭。
            self.route_migration_due = (
                time.monotonic() + PUBLIC_COMBO_MIGRATION_RETRY_SECONDS
            )
            print(
                f"[account-portal] delayed public combo migration warning: {error}",
                file=sys.stderr,
                flush=True,
            )
            return
        # 单模型失败不代表迁移完成。若在此标记全部完成，/v1/responses 会永久把
        # 缺失的裸 Combo 重写为 codex/<public-id>。门户在有限延迟后重试期间，
        # Chat 流量继续走旧路由。
        self.route_migration_finished = route_migration["failed"] == 0
        self.route_migration_due = time.monotonic() + (
            PUBLIC_COMBO_AUDIT_SECONDS
            if self.route_migration_finished
            else PUBLIC_COMBO_MIGRATION_RETRY_SECONDS
        )
        if route_migration["migrated"] or route_migration["failed"]:
            print(
                "[account-portal] public combo reconciliation: "
                f"migrated={route_migration['migrated']}, "
                f"unchanged={route_migration['unchanged']}, "
                f"failed={route_migration['failed']}",
                flush=True,
            )

    def maintenance_loop(self) -> None:
        if self.stop_event.wait(2):
            return
        while not self.stop_event.is_set():
            try:
                self.reconcile_request_content_logging()
                self.reconcile_public_routes_after_acceptance()
                self.control.background_tick()
            except Exception as error:
                print(f"[account-portal] maintenance warning: {error}", file=sys.stderr, flush=True)
            self.stop_event.wait(5 if not self.route_migration_finished else 60)

    def billing_loop(self) -> None:
        """在 `/v1` 保持直连网关的同时，及时结算已经完成的调用。"""
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
            "public-route-status",
            "reconcile-public-routes",
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
    if args.command in {"public-route-status", "reconcile-public-routes"}:
        database = Database(config)
        database.initialize()
        control = PortalControlPlane(config, database, OmniRouteClient(config))
        result: dict[str, Any] = {}
        if args.command == "reconcile-public-routes":
            # stdout 保持供 llmctl 读取的机器格式，快照路径和逐模型诊断写入 stderr。
            with contextlib.redirect_stdout(sys.stderr):
                result["reconciliation"] = control.reconcile_public_combo_routes()
                if result["reconciliation"]["failed"] == 0:
                    result["permission_sync"] = control.sync_all_users()
                else:
                    result["permission_sync"] = {"synced": 0, "failed": 0}
        result["status"] = control.public_combo_route_status()
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        failed = int(result.get("reconciliation", {}).get("failed", 0))
        permission_failed = int(result.get("permission_sync", {}).get("failed", 0))
        if failed or permission_failed or not result["status"]["ready"]:
            raise SystemExit(2)
        return
    PortalServer(config).serve()


if __name__ == "__main__":
    main()
