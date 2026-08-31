#!/usr/bin/env python3
"""OmniRoute 镜像生命周期与 SQLite 可靠性维护领域实现。

该模块只由 root 权限的本机控制服务调用。账户门户和浏览器仅能通过 Unix
Socket 白名单提交任务，不能直接访问数据库文件、Docker 或 systemd。
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import shlex
import shutil
import sqlite3
import tempfile
import threading
import uuid
from typing import Any, Callable


RECOMMENDED_OMNIROUTE_IMAGE = "diegosouzapw/omniroute:3.8.49"
TERMINAL_STATES = {"succeeded", "failed", "cancelled", "rolled_back"}
IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,299}$")
BACKUP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,119}$")
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
MUTABLE_IMAGE_TAGS = {"latest", "latest-web", "main", "main-web", "next", "next-web"}


def utc_now() -> str:
    """返回无微秒的 UTC ISO 时间，供任务、备份和审计稳定排序。"""

    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def atomic_write(path: pathlib.Path, content: str, mode: int = 0o600) -> None:
    """原子写入 UTF-8 文本并同步文件与目录。

    参数：
        path: 必须由调用方限定在受管配置、状态或备份目录中的目标文件。
        content: 完整文本内容。
        mode: 最终权限；任务和备份元数据默认仅 root 可读。
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def parse_env_file(path: pathlib.Path) -> dict[str, str]:
    """安全读取 LLMCtl 的 NAME=VALUE 环境文件，不执行 Shell 代码。"""

    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, raw_value = line.split("=", 1)
        name = name.strip()
        if not ENV_NAME_RE.fullmatch(name):
            continue
        try:
            parsed = shlex.split(raw_value, comments=False, posix=True)
        except ValueError:
            continue
        values[name] = parsed[0] if len(parsed) == 1 else raw_value.strip()
    return values


def update_env_values(path: pathlib.Path, values: dict[str, str | None]) -> None:
    """只更新指定环境变量，保留同文件中的无关运行配置。

    参数：
        path: LLMCtl 受管的 cluster.env。
        values: 名称到新值的映射；值为 ``None`` 时删除该键，用于精确回滚
            升级前本来不存在的覆盖项。

    异常：
        ValueError: 名称或值包含不可安全写入 Shell 环境文件的内容。
    """

    for name, value in values.items():
        if not ENV_NAME_RE.fullmatch(name):
            raise ValueError(f"环境变量名无效：{name}")
        if value is not None and any(character in value for character in "\r\n\x00"):
            raise ValueError(f"环境变量值包含非法字符：{name}")
    lines = path.read_text(encoding="utf-8").splitlines()
    remaining = dict(values)
    rendered: list[str] = []
    for line in lines:
        match = re.match(r"^([A-Z][A-Z0-9_]*)=", line)
        if not match or match.group(1) not in remaining:
            rendered.append(line)
            continue
        name = match.group(1)
        value = remaining.pop(name)
        if value is not None:
            rendered.append(f"{name}={shlex.quote(value)}")
    for name, value in remaining.items():
        if value is not None:
            rendered.append(f"{name}={shlex.quote(value)}")
    atomic_write(path, "\n".join(rendered) + "\n", 0o640)


def sha256_file(path: pathlib.Path) -> str:
    """流式计算文件 SHA256，避免大型 SQLite 快照进入内存。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_fixed_image(image: str) -> str:
    """校验可复现的 OmniRoute 镜像版本或 digest。

    参数：
        image: 完整镜像引用。

    返回：
        去除首尾空白后的镜像引用。

    异常：
        ValueError: 格式非法或使用 latest/main/next 等可变标签。
    """

    value = str(image or "").strip()
    if not IMAGE_RE.fullmatch(value):
        raise ValueError("OmniRoute 镜像格式无效")
    tail = value.rsplit(":", 1)[-1].lower() if "@sha256:" not in value.lower() else ""
    if tail in MUTABLE_IMAGE_TAGS:
        raise ValueError("升级必须使用固定版本或 sha256 digest，不能使用可变标签")
    if "@" not in value and ":" not in value.rsplit("/", 1)[-1]:
        raise ValueError("升级镜像必须显式包含固定版本标签或 digest")
    return value


@dataclasses.dataclass(frozen=True)
class OmniRouteMaintenancePaths:
    """集中描述 OmniRoute 运维允许访问的固定路径。"""

    cluster_env: pathlib.Path
    database: pathlib.Path
    state_dir: pathlib.Path
    jobs_dir: pathlib.Path
    backup_root: pathlib.Path
    last_assessment: pathlib.Path
    llmctl: pathlib.Path

    @classmethod
    def from_control_paths(cls, paths: Any) -> "OmniRouteMaintenancePaths":
        """从 model-control 的受管根目录派生 OmniRoute 专属路径。

        参数：
            paths: 具有 ``state_dir`` 与 ``cluster_env`` 的控制服务路径对象。
        """

        state = pathlib.Path(paths.state_dir)
        maintenance = state / "omniroute-maintenance"
        return cls(
            cluster_env=pathlib.Path(paths.cluster_env),
            database=state / "omniroute/gateway/storage.sqlite",
            state_dir=maintenance,
            jobs_dir=maintenance / "jobs",
            backup_root=pathlib.Path(
                os.environ.get(
                    "LLM_OMNIROUTE_BACKUPS_DIR",
                    str(pathlib.Path(paths.backups_dir).parent / "omniroute"),
                )
            ),
            last_assessment=maintenance / "last-assessment.json",
            llmctl=pathlib.Path(
                os.environ.get("LLMCTL_BIN", "/usr/local/sbin/llmctl")
            ),
        )


class MaintenanceJobStore:
    """原子持久化 OmniRoute 运维任务与有限日志。"""

    def __init__(self, directory: pathlib.Path):
        """创建 root-only 任务目录。"""

        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        self._lock = threading.RLock()

    def create(self, kind: str, request: dict[str, Any]) -> dict[str, Any]:
        """创建等待执行的任务；确认短语不会写入任务文件。"""

        job = {
            "id": str(uuid.uuid4()),
            "kind": kind,
            "state": "waiting",
            "phase": "waiting",
            "progress": 0,
            "message": "等待执行",
            "request": {
                key: value for key, value in request.items() if key != "confirmation"
            },
            "cancel_requested": False,
            "logs": [],
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }
        self.save(job)
        return job

    def save(self, job: dict[str, Any]) -> None:
        """保存任务并限制日志为最近 500 条。"""

        with self._lock:
            job["updated_at"] = utc_now()
            job["logs"] = list(job.get("logs", []))[-500:]
            atomic_write(
                self.directory / f"{job['id']}.json",
                json.dumps(job, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            )

    def get(self, job_id: str) -> dict[str, Any]:
        """按 UUID 读取任务；不存在或格式非法时明确失败。"""

        try:
            path = self.directory / f"{uuid.UUID(str(job_id))}.json"
            return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, FileNotFoundError, json.JSONDecodeError) as error:
            raise ValueError("OmniRoute 运维任务不存在") from error

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        """按更新时间倒序返回最近任务。"""

        jobs: list[dict[str, Any]] = []
        for path in self.directory.glob("*.json"):
            with contextlib.suppress(OSError, ValueError, json.JSONDecodeError):
                jobs.append(json.loads(path.read_text(encoding="utf-8")))
        return sorted(
            jobs, key=lambda item: str(item.get("updated_at", "")), reverse=True
        )[:limit]


class OmniRouteMaintenanceManager:
    """执行 OmniRoute 评估、备份、维护、升级和可恢复回滚。"""

    def __init__(
        self,
        paths: OmniRouteMaintenancePaths,
        runner: Any,
        active_model_job: Callable[[], bool] | None = None,
        submission_lock: threading.Lock | None = None,
    ):
        """初始化受管路径、命令执行器和跨任务互斥状态。

        参数：
            paths: 所有数据库、状态、备份和配置固定路径。
            runner: 只执行白名单 Docker、systemd 与 llmctl 命令的执行器。
            active_model_job: 判断模型部署/发布是否正在修改 Router 的回调。
            submission_lock: 与模型任务共用的提交锁，防止两类任务同时通过
                “当前无任务”检查后并发修改 Router。
        """

        self.paths = paths
        self.runner = runner
        self.jobs = MaintenanceJobStore(paths.jobs_dir)
        self.active_model_job = active_model_job or (lambda: False)
        self._submission_lock = submission_lock or threading.Lock()
        self._mutation_lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}
        self.paths.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.paths.backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.paths.state_dir, 0o700)
        os.chmod(self.paths.backup_root, 0o700)
        self.recover_interrupted_jobs()

    def recover_interrupted_jobs(self) -> None:
        """把守护进程重启遗留任务标为失败，避免页面永久显示执行中。"""

        for job in self.jobs.list(limit=200):
            if job.get("state") in TERMINAL_STATES:
                continue
            job.update(
                {
                    "state": "failed",
                    "phase": "interrupted",
                    "message": "控制服务在任务期间重启；请先评估数据库和 Router 状态",
                    "error": "controller_restarted",
                }
            )
            self.jobs.save(job)

    def has_active_job(self) -> bool:
        """返回是否存在尚未结束的 OmniRoute 运维任务。"""

        return any(
            job.get("state") not in TERMINAL_STATES
            for job in self.jobs.list(limit=200)
        )

    def _cluster(self) -> dict[str, str]:
        """读取并验证当前必须处于 OmniRoute 接入层。"""

        values = parse_env_file(self.paths.cluster_env)
        if str(values.get("GATEWAY_KIND", "")).lower() != "omniroute":
            raise ValueError("当前 AI 接入层不是 OmniRoute")
        return values

    @staticmethod
    def _image_entries(cluster: dict[str, str]) -> dict[str, dict[str, Any]]:
        """保存升级前两个镜像键是否存在及其精确值。"""

        return {
            name: {"present": name in cluster, "value": cluster.get(name, "")}
            for name in ("OMNIROUTE_IMAGE", "GATEWAY_IMAGE")
        }

    def _running_image(self) -> dict[str, str]:
        """读取当前 Router 容器实际使用的镜像引用和不可变镜像 ID。"""

        try:
            result = self.runner.run(
                [
                    "docker",
                    "inspect",
                    "--format",
                    "{{.Config.Image}}|{{.Image}}",
                    "llm-router",
                ],
                timeout=8,
                check=False,
                capture_output=True,
            )
        except Exception:
            return {"image": "", "image_id": ""}
        if getattr(result, "returncode", 1) != 0:
            return {"image": "", "image_id": ""}
        image, _, image_id = str(getattr(result, "stdout", "")).strip().partition("|")
        return {"image": image, "image_id": image_id}

    def _latest_assessment(self) -> dict[str, Any] | None:
        """读取最近一次评估；损坏文件不阻塞新的评估或维护。"""

        with contextlib.suppress(OSError, ValueError, json.JSONDecodeError):
            value = json.loads(self.paths.last_assessment.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        return None

    def list_backups(self, limit: int = 30) -> list[dict[str, Any]]:
        """列出校验元数据可读的受管备份，不扫描其他目录。"""

        backups: list[dict[str, Any]] = []
        for directory in self.paths.backup_root.iterdir():
            if not directory.is_dir() or not BACKUP_ID_RE.fullmatch(directory.name):
                continue
            metadata_path = directory / "metadata.json"
            with contextlib.suppress(OSError, ValueError, json.JSONDecodeError):
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                if metadata.get("backup_status") != "ready":
                    continue
                backups.append(
                    {
                        "id": directory.name,
                        "created_at": metadata.get("created_at"),
                        "purpose": metadata.get("purpose"),
                        "database_size": metadata.get("database", {}).get("size", 0),
                        "database_sha256": metadata.get("database", {}).get("sha256", ""),
                        "source_image": metadata.get("source_image", ""),
                        "target_image": metadata.get("target_image", ""),
                    }
                )
        return sorted(
            backups, key=lambda item: str(item.get("created_at", "")), reverse=True
        )[:limit]

    def status(self) -> dict[str, Any]:
        """返回适合 CLI 与 WebUI 展示的非敏感运行、任务和备份摘要。"""

        cluster = self._cluster()
        configured = str(
            cluster.get("OMNIROUTE_IMAGE")
            or cluster.get("GATEWAY_IMAGE")
            or RECOMMENDED_OMNIROUTE_IMAGE
        )
        database = self.paths.database
        running = self._running_image()
        jobs = self.jobs.list(limit=30)
        return {
            "available": database.is_file(),
            "configured_image": configured,
            "running_image": running["image"],
            "running_image_id": running["image_id"],
            "image_drift": bool(running["image"] and running["image"] != configured),
            "recommended_image": RECOMMENDED_OMNIROUTE_IMAGE,
            "database": {
                "exists": database.is_file(),
                "size": database.stat().st_size if database.is_file() else 0,
                "wal_size": database.with_name(database.name + "-wal").stat().st_size
                if database.with_name(database.name + "-wal").is_file()
                else 0,
                "shm_size": database.with_name(database.name + "-shm").stat().st_size
                if database.with_name(database.name + "-shm").is_file()
                else 0,
                "modified_at": dt.datetime.fromtimestamp(
                    database.stat().st_mtime, dt.timezone.utc
                ).isoformat()
                if database.is_file()
                else "",
            },
            "latest_assessment": self._latest_assessment(),
            "backups": self.list_backups(),
            "jobs": jobs,
            "active_job": next(
                (job for job in jobs if job.get("state") not in TERMINAL_STATES),
                None,
            ),
        }

    def assess(self, deep: bool = False) -> dict[str, Any]:
        """只读评估 OmniRoute SQLite 完整性、空间、WAL 与恢复准备度。

        参数：
            deep: 为真时执行完整 ``PRAGMA integrity_check``；默认使用更快的
                ``quick_check``，适合页面日常刷新。

        返回：
            健康等级、数据库指标、检查结果、备份摘要和可操作建议。

        异常：
            ValueError: 当前不是 OmniRoute 或数据库不存在。
            sqlite3.Error: 数据库无法只读打开或检查。
        """

        self._cluster()
        database = self.paths.database
        if not database.is_file():
            raise ValueError(f"OmniRoute SQLite 不存在：{database}")
        file_size = database.stat().st_size
        wal_path = database.with_name(database.name + "-wal")
        shm_path = database.with_name(database.name + "-shm")
        disk = shutil.disk_usage(database.parent)
        uri = f"file:{database.as_posix()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=10) as connection:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA busy_timeout=10000")
            check_name = "integrity_check" if deep else "quick_check"
            check_rows = [
                str(row[0]) for row in connection.execute(f"PRAGMA {check_name}").fetchall()
            ]
            foreign_rows = connection.execute("PRAGMA foreign_key_check").fetchmany(50)
            page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
            page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
            freelist_count = int(
                connection.execute("PRAGMA freelist_count").fetchone()[0]
            )
            journal_mode = str(
                connection.execute("PRAGMA journal_mode").fetchone()[0]
            ).lower()
            synchronous = int(connection.execute("PRAGMA synchronous").fetchone()[0])
            wal_autocheckpoint = int(
                connection.execute("PRAGMA wal_autocheckpoint").fetchone()[0]
            )
            user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            table_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'"
                ).fetchone()[0]
            )
        integrity_ok = check_rows == ["ok"]
        foreign_key_ok = not foreign_rows
        free_ratio = freelist_count / max(1, page_count)
        wal_size = wal_path.stat().st_size if wal_path.is_file() else 0
        backups = self.list_backups()
        latest_backup = backups[0] if backups else None
        backup_age_hours: float | None = None
        if latest_backup and latest_backup.get("created_at"):
            with contextlib.suppress(ValueError):
                created = dt.datetime.fromisoformat(str(latest_backup["created_at"]))
                backup_age_hours = (
                    dt.datetime.now(dt.timezone.utc) - created
                ).total_seconds() / 3600
        recommendations: list[dict[str, str]] = []
        if not integrity_ok:
            recommendations.append(
                {
                    "severity": "critical",
                    "code": "integrity_failed",
                    "message": "SQLite 完整性检查失败；停止升级和维护，优先从已验证备份恢复。",
                }
            )
        if not foreign_key_ok:
            recommendations.append(
                {
                    "severity": "critical",
                    "code": "foreign_key_failed",
                    "message": "检测到外键孤儿记录；不要执行 VACUUM 或升级，应先定位损坏来源。",
                }
            )
        if disk.free < max(file_size * 2 + wal_size, 512 * 1024 * 1024):
            recommendations.append(
                {
                    "severity": "critical",
                    "code": "disk_low",
                    "message": "磁盘余量不足以安全创建备份和执行可能的数据库迁移。",
                }
            )
        if journal_mode != "wal":
            recommendations.append(
                {
                    "severity": "warning",
                    "code": "journal_not_wal",
                    "message": "当前不是 WAL 模式；并发读写可用性可能低于 OmniRoute 默认配置。",
                }
            )
        if wal_size > max(file_size // 2, 256 * 1024 * 1024):
            recommendations.append(
                {
                    "severity": "warning",
                    "code": "wal_large",
                    "message": "WAL 文件偏大，建议执行在线维护的 PASSIVE checkpoint。",
                }
            )
        if free_ratio >= 0.25 and file_size >= 64 * 1024 * 1024:
            recommendations.append(
                {
                    "severity": "warning",
                    "code": "fragmented",
                    "message": "空闲页比例较高，可在维护窗口备份后执行压缩。",
                }
            )
        if backup_age_hours is None or backup_age_hours > 24 * 7:
            recommendations.append(
                {
                    "severity": "warning",
                    "code": "backup_stale",
                    "message": "没有 7 天内的受管可校验备份，建议立即创建备份。",
                }
            )
        health = (
            "critical"
            if any(item["severity"] == "critical" for item in recommendations)
            else "warning"
            if recommendations
            else "healthy"
        )
        result = {
            "assessed_at": utc_now(),
            "deep": bool(deep),
            "health": health,
            "integrity": {"ok": integrity_ok, "result": check_rows[:50]},
            "foreign_keys": {
                "ok": foreign_key_ok,
                "violations": [list(row) for row in foreign_rows],
            },
            "sqlite": {
                "version": sqlite3.sqlite_version,
                "journal_mode": journal_mode,
                "synchronous": synchronous,
                "wal_autocheckpoint": wal_autocheckpoint,
                "user_version": user_version,
                "table_count": table_count,
                "page_size": page_size,
                "page_count": page_count,
                "freelist_count": freelist_count,
                "free_ratio": round(free_ratio, 6),
            },
            "storage": {
                "database_size": file_size,
                "wal_size": wal_size,
                "shm_size": shm_path.stat().st_size if shm_path.is_file() else 0,
                "disk_free": disk.free,
                "disk_total": disk.total,
            },
            "backup": {
                "count": len(backups),
                "latest": latest_backup,
                "latest_age_hours": round(backup_age_hours, 2)
                if backup_age_hours is not None
                else None,
            },
            "recommendations": recommendations,
        }
        atomic_write(
            self.paths.last_assessment,
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        return result

    @staticmethod
    def _quick_check(path: pathlib.Path) -> list[str]:
        """对指定 SQLite 文件执行只读 quick_check 并返回全部结果行。"""

        # immutable 只读模式不会为静态备份创建 WAL/SHM；备份由 sqlite3 backup
        # API 完整落盘后才进入这里，不依赖任何未 checkpoint 的伴随文件。
        uri = f"{path.resolve().as_uri()}?mode=ro&immutable=1"
        with sqlite3.connect(uri, uri=True, timeout=10) as connection:
            connection.execute("PRAGMA query_only=ON")
            return [str(row[0]) for row in connection.execute("PRAGMA quick_check")]

    def _create_backup(
        self, purpose: str, *, target_image: str = "", job_id: str = ""
    ) -> dict[str, Any]:
        """使用 SQLite 在线备份 API 创建并验证一致性快照。

        参数：
            purpose: 备份用途，例如 upgrade、online、compact 或 pre-rollback。
            target_image: 升级任务准备切换到的固定镜像；其他维护留空。
            job_id: 关联后台任务 UUID，用于生成不冲突的备份 ID。

        返回：
            已落盘且 quick_check、大小与 SHA256 已记录的元数据。
        """

        cluster = self._cluster()
        database = self.paths.database
        if not database.is_file():
            raise ValueError("OmniRoute SQLite 不存在，无法创建备份")
        safe_purpose = re.sub(r"[^a-z0-9-]+", "-", purpose.lower()).strip("-")
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dt%H%M%Sz")
        suffix = str(job_id or uuid.uuid4()).replace("-", "")[:8]
        backup_id = f"{safe_purpose}-{stamp}-{suffix}"
        if not BACKUP_ID_RE.fullmatch(backup_id):
            raise RuntimeError("内部备份 ID 生成失败")
        directory = self.paths.backup_root / backup_id
        directory.mkdir(mode=0o700)
        partial = directory / "database.sqlite.partial"
        final = directory / "database.sqlite"
        source_stat = database.stat()
        try:
            source_uri = f"file:{database.as_posix()}?mode=ro"
            with sqlite3.connect(source_uri, uri=True, timeout=30) as source:
                with sqlite3.connect(partial) as destination:
                    source.backup(destination, pages=1024, sleep=0.05)
                    destination.commit()
            checks = self._quick_check(partial)
            if checks != ["ok"]:
                raise RuntimeError(f"备份 quick_check 失败：{checks[:3]}")
            os.chmod(partial, 0o600)
            os.replace(partial, final)
            with final.open("rb") as handle:
                os.fsync(handle.fileno())
            current_image = str(
                cluster.get("OMNIROUTE_IMAGE")
                or cluster.get("GATEWAY_IMAGE")
                or RECOMMENDED_OMNIROUTE_IMAGE
            )
            running_image = self._running_image()
            metadata = {
                "schema_version": 1,
                "backup_status": "ready",
                "id": backup_id,
                "created_at": utc_now(),
                "purpose": purpose,
                "source_image": current_image,
                "running_image": running_image["image"],
                "running_image_id": running_image["image_id"],
                "restore_image": running_image["image"] or current_image,
                "target_image": target_image,
                "image_entries": self._image_entries(cluster),
                "database": {
                    "file": "database.sqlite",
                    "size": final.stat().st_size,
                    "sha256": sha256_file(final),
                    "quick_check": checks,
                    "source_uid": source_stat.st_uid,
                    "source_gid": source_stat.st_gid,
                    "source_mode": source_stat.st_mode & 0o777,
                },
            }
            atomic_write(
                directory / "metadata.json",
                json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
            )
            return metadata
        except Exception:
            with contextlib.suppress(OSError):
                shutil.rmtree(directory)
            raise

    def _validated_backup(
        self, backup_id: str
    ) -> tuple[pathlib.Path, dict[str, Any]]:
        """限定在备份根目录内解析 ID，并复核文件大小、哈希与完整性。"""

        identifier = str(backup_id or "").strip()
        if not BACKUP_ID_RE.fullmatch(identifier):
            raise ValueError("OmniRoute 备份 ID 格式无效")
        root = self.paths.backup_root.resolve()
        directory = (root / identifier).resolve()
        if directory.parent != root or not directory.is_dir():
            raise ValueError("OmniRoute 备份不存在")
        try:
            metadata = json.loads(
                (directory / "metadata.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("OmniRoute 备份元数据无效") from error
        if metadata.get("backup_status") != "ready" or metadata.get("id") != identifier:
            raise ValueError("OmniRoute 备份尚未完成或身份不匹配")
        database = directory / "database.sqlite"
        expected = metadata.get("database", {})
        if (
            not database.is_file()
            or database.stat().st_size != int(expected.get("size", -1))
            or sha256_file(database) != str(expected.get("sha256", ""))
        ):
            raise ValueError("OmniRoute 备份大小或 SHA256 校验失败")
        checks = self._quick_check(database)
        if checks != ["ok"]:
            raise ValueError(f"OmniRoute 备份完整性检查失败：{checks[:3]}")
        return directory, metadata

    def _create_incident_copy(self, purpose: str, job_id: str) -> dict[str, Any]:
        """在 Router 停止后保存无法通过 SQLite 校验的原始 DB/WAL/SHM。

        参数：
            purpose: 事故副本原因，用于元数据和目录名。
            job_id: 关联任务 UUID。

        返回：
            标记为 ``incident`` 的元数据。该副本只用于取证，不会出现在可
            自动回滚列表，也不会绕过 quick_check 成为恢复源。
        """

        safe_purpose = re.sub(r"[^a-z0-9-]+", "-", purpose.lower()).strip("-")
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dt%H%M%Sz")
        backup_id = f"incident-{safe_purpose}-{stamp}-{job_id.replace('-', '')[:8]}"
        if not BACKUP_ID_RE.fullmatch(backup_id):
            raise RuntimeError("内部事故副本 ID 生成失败")
        directory = self.paths.backup_root / backup_id
        directory.mkdir(mode=0o700)
        files: list[dict[str, Any]] = []
        database = self.paths.database
        sources = [
            database,
            database.with_name(database.name + "-wal"),
            database.with_name(database.name + "-shm"),
        ]
        try:
            for source in sources:
                if not source.is_file():
                    continue
                target = directory / source.name
                shutil.copyfile(source, target)
                os.chmod(target, 0o600)
                with target.open("rb") as handle:
                    os.fsync(handle.fileno())
                files.append(
                    {
                        "file": source.name,
                        "size": target.stat().st_size,
                        "sha256": sha256_file(target),
                    }
                )
            cluster = self._cluster()
            running = self._running_image()
            metadata = {
                "schema_version": 1,
                "backup_status": "incident",
                "id": backup_id,
                "created_at": utc_now(),
                "purpose": purpose,
                "source_image": str(
                    cluster.get("OMNIROUTE_IMAGE")
                    or cluster.get("GATEWAY_IMAGE")
                    or ""
                ),
                "running_image": running["image"],
                "running_image_id": running["image_id"],
                "files": files,
                "warning": "原始事故副本未通过 SQLite 完整性校验，不能自动恢复",
            }
            atomic_write(
                directory / "metadata.json",
                json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
            )
            return metadata
        except Exception:
            with contextlib.suppress(OSError):
                shutil.rmtree(directory)
            raise

    def _stop_router_stack(self) -> None:
        """先停账户门户再停 Router；GPU Worker 与模型服务保持运行。"""

        self.runner.run(
            ["systemctl", "stop", "llm-account.service"],
            timeout=30,
            check=False,
            capture_output=True,
        )
        try:
            self.runner.run(
                ["systemctl", "stop", "llm-router.service"],
                timeout=60,
                check=True,
                capture_output=True,
            )
        except Exception:
            # Router 未能停止时数据库尚未修改；恢复账户门户，避免停止阶段本身
            # 扩大故障范围。账户门户会按自身重试语义处理暂时不可用的 Router。
            self.runner.run(
                ["systemctl", "start", "llm-account.service"],
                timeout=30,
                check=False,
                capture_output=True,
            )
            raise

    def _restart_and_smoke(self) -> None:
        """启动 Router/账户门户并执行 LLMCtl 完整真实模型冒烟。"""

        self.runner.run(
            [str(self.paths.llmctl), "router", "restart"],
            timeout=240,
            check=True,
            capture_output=True,
        )
        self.runner.run(
            [str(self.paths.llmctl), "smoke", "--full"],
            timeout=300,
            check=True,
            capture_output=True,
        )

    def _restore_database(self, directory: pathlib.Path, metadata: dict[str, Any]) -> None:
        """在 Router 已停止时原子恢复数据库并移除旧 WAL/SHM 伴随文件。"""

        database = self.paths.database
        source = directory / "database.sqlite"
        expected = metadata["database"]
        database.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{database.name}.restore.", dir=database.parent
        )
        os.close(descriptor)
        temporary_path = pathlib.Path(temporary)
        try:
            shutil.copyfile(source, temporary_path)
            with temporary_path.open("rb") as handle:
                os.fsync(handle.fileno())
            os.chmod(temporary_path, int(expected.get("source_mode", 0o660)))
            with contextlib.suppress(PermissionError):
                os.chown(
                    temporary_path,
                    int(expected.get("source_uid", 1000)),
                    int(expected.get("source_gid", 1000)),
                )
            os.replace(temporary_path, database)
            for companion in (
                database.with_name(database.name + "-wal"),
                database.with_name(database.name + "-shm"),
            ):
                companion.unlink(missing_ok=True)
            directory_fd = os.open(database.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary_path.unlink(missing_ok=True)

    def _restore_backup_image(self, metadata: dict[str, Any]) -> None:
        """把备份时实际运行的固定镜像写回两个兼容配置键。"""

        image = validate_fixed_image(
            str(metadata.get("restore_image") or metadata.get("source_image") or "")
        )
        update_env_values(
            self.paths.cluster_env,
            {"OMNIROUTE_IMAGE": image, "GATEWAY_IMAGE": image},
        )

    def _set_job(
        self,
        job: dict[str, Any],
        phase: str,
        progress: int,
        message: str,
        *,
        log: str = "",
        cancellable: bool = True,
    ) -> None:
        """更新任务阶段，并在可安全停止的边界响应取消。"""

        latest = self.jobs.get(str(job["id"]))
        job["cancel_requested"] = bool(latest.get("cancel_requested"))
        if cancellable and job["cancel_requested"]:
            raise InterruptedError("任务已由管理员取消")
        job.update(
            {
                "state": "running",
                "phase": phase,
                "progress": int(progress),
                "message": message,
            }
        )
        if log:
            job.setdefault("logs", []).append(
                {"time": utc_now(), "message": " ".join(log.split())[:2000]}
            )
        self.jobs.save(job)

    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        """校验高风险确认短语并提交一个后台 OmniRoute 运维任务。"""

        action = str(payload.get("action", "")).strip().lower()
        confirmations = {
            "online": "MAINTAIN ONLINE",
            "compact": "COMPACT SQLITE",
            "update": "UPDATE OMNIROUTE",
        }
        if action not in {"backup", "online", "compact", "update", "rollback"}:
            raise ValueError("OmniRoute 操作必须是 backup|online|compact|update|rollback")
        self._cluster()
        request: dict[str, Any] = {"action": action}
        if action == "update":
            request["image"] = validate_fixed_image(
                str(payload.get("image") or RECOMMENDED_OMNIROUTE_IMAGE)
            )
            local_image = payload.get("local_image", False)
            if not isinstance(local_image, bool):
                raise ValueError("local_image 必须是布尔值")
            request["local_image"] = local_image
        if action == "rollback":
            backup_id = str(payload.get("backup_id", "")).strip()
            self._validated_backup(backup_id)
            request["backup_id"] = backup_id
            expected = f"ROLLBACK {backup_id}"
        else:
            expected = confirmations.get(action, "")
        if expected and str(payload.get("confirmation", "")).strip() != expected:
            raise ValueError(f"请输入 {expected} 确认操作")
        with self._submission_lock:
            if self.active_model_job():
                raise RuntimeError("模型部署、升级或路由发布任务正在运行")
            if self.has_active_job():
                raise RuntimeError("已有 OmniRoute 运维任务正在运行")
            job = self.jobs.create(action, request)
            thread = threading.Thread(
                target=self._run_job, args=(str(job["id"]),), daemon=True
            )
            self._threads[str(job["id"])] = thread
            thread.start()
        return job

    def cancel(self, job_id: str) -> dict[str, Any]:
        """请求在下一安全检查点取消任务；已经终结的任务原样返回。"""

        job = self.jobs.get(job_id)
        if job.get("state") in TERMINAL_STATES:
            return job
        job["cancel_requested"] = True
        job["message"] = "已请求取消；将在下一安全检查点停止"
        self.jobs.save(job)
        return job

    def _run_job(self, job_id: str) -> None:
        """在独占变更锁内执行任务，并保存明确终态。"""

        job = self.jobs.get(job_id)
        acquired = False
        try:
            acquired = self._mutation_lock.acquire(blocking=False)
            if not acquired:
                raise RuntimeError("另一个 OmniRoute 操作持有变更锁")
            action = str(job["kind"])
            if action == "backup":
                self._run_backup(job)
            elif action == "online":
                self._run_online_maintenance(job)
            elif action == "compact":
                self._run_compact(job)
            elif action == "update":
                self._run_update(job)
            elif action == "rollback":
                self._run_manual_rollback(job)
            else:
                raise ValueError("未知 OmniRoute 运维任务")
        except InterruptedError as error:
            job.update(
                {
                    "state": "cancelled",
                    "phase": "cancelled",
                    "message": str(error),
                    "error": "cancelled_at_safe_checkpoint",
                }
            )
            self.jobs.save(job)
        except Exception as error:
            if job.get("state") != "rolled_back":
                job.update(
                    {
                        "state": "failed",
                        "phase": "failed",
                        "message": f"操作失败：{error}",
                        "error": str(error)[:2000],
                    }
                )
                self.jobs.save(job)
        finally:
            if acquired:
                self._mutation_lock.release()
            self._threads.pop(job_id, None)

    def _complete(self, job: dict[str, Any], message: str) -> None:
        """把任务标为 100% 成功。"""

        job.update(
            {
                "state": "succeeded",
                "phase": "succeeded",
                "progress": 100,
                "message": message,
                "error": "",
            }
        )
        self.jobs.save(job)

    def _run_backup(self, job: dict[str, Any]) -> None:
        """执行独立在线备份任务。"""

        self._set_job(job, "assessing", 10, "检查 SQLite 完整性和磁盘余量")
        assessment = self.assess(deep=False)
        if assessment["health"] == "critical":
            raise RuntimeError("SQLite 评估为严重异常，拒绝生成可恢复备份")
        self._set_job(job, "backing_up", 40, "创建 SQLite 一致性在线备份")
        backup = self._create_backup("manual", job_id=str(job["id"]))
        job["backup_id"] = backup["id"]
        self._complete(job, f"备份已完成并通过校验：{backup['id']}")

    def _run_online_maintenance(self, job: dict[str, Any]) -> None:
        """不停止 Router，执行 optimize 与 PASSIVE WAL checkpoint。"""

        self._set_job(job, "assessing", 5, "评估 SQLite；在线维护不会停止 Router")
        assessment = self.assess(deep=False)
        if assessment["health"] == "critical":
            raise RuntimeError("SQLite 存在严重异常，拒绝在线写入维护")
        self._set_job(job, "backing_up", 25, "维护前创建一致性备份")
        backup = self._create_backup("online", job_id=str(job["id"]))
        job["backup_id"] = backup["id"]
        self._set_job(job, "optimizing", 55, "执行 PRAGMA optimize")
        with sqlite3.connect(self.paths.database, timeout=30) as connection:
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("PRAGMA optimize")
            checkpoint = list(connection.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone())
        job["checkpoint"] = checkpoint
        self._set_job(job, "verifying", 85, "重新执行完整性与可用性评估")
        result = self.assess(deep=False)
        if not result["integrity"]["ok"] or not result["foreign_keys"]["ok"]:
            raise RuntimeError("在线维护后完整性检查失败；请使用维护前备份恢复")
        self._complete(job, "在线维护完成；Router 与 GPU Worker 未重启")

    def _compact_database(self) -> None:
        """在 Router 停止后 checkpoint、VACUUM、optimize 并做完整检查。"""

        with sqlite3.connect(self.paths.database, timeout=30) as connection:
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.execute("VACUUM")
            connection.execute("PRAGMA optimize")
            checks = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
            if checks != ["ok"]:
                raise RuntimeError(f"VACUUM 后完整性检查失败：{checks[:3]}")

    def _run_compact(self, job: dict[str, Any]) -> None:
        """在维护窗口压缩数据库，失败时自动恢复维护前快照。"""

        self._set_job(job, "assessing", 5, "评估压缩空间与数据库完整性")
        assessment = self.assess(deep=True)
        if assessment["health"] == "critical":
            raise RuntimeError("SQLite 存在严重异常，拒绝执行 VACUUM")
        database_size = int(assessment["storage"]["database_size"])
        if int(assessment["storage"]["disk_free"]) < max(
            database_size * 2, 512 * 1024 * 1024
        ):
            raise RuntimeError("磁盘余量不足以安全执行 VACUUM")
        self._set_job(job, "backing_up", 20, "压缩前创建一致性备份")
        backup = self._create_backup("compact", job_id=str(job["id"]))
        job["backup_id"] = backup["id"]
        directory, metadata = self._validated_backup(str(backup["id"]))
        self._set_job(job, "stopping", 35, "停止 Router 与账户门户；GPU Worker 保持运行")
        self._stop_router_stack()
        try:
            self._set_job(
                job,
                "compacting",
                55,
                "执行 checkpoint、VACUUM 和完整性检查",
                cancellable=False,
            )
            self._compact_database()
            self._set_job(
                job,
                "starting",
                80,
                "恢复 Router 并执行完整模型冒烟",
                cancellable=False,
            )
            self._restart_and_smoke()
            self.assess(deep=False)
            self._complete(job, "SQLite 压缩完成，Router 已恢复并通过完整冒烟")
        except Exception as error:
            try:
                self._stop_router_stack()
                self._restore_database(directory, metadata)
            except Exception as restore_error:
                raise RuntimeError(
                    f"压缩失败：{error}；自动恢复数据库失败：{restore_error}"
                ) from restore_error
            try:
                self._restart_and_smoke()
            except Exception as verification_error:
                raise RuntimeError(
                    f"压缩失败：{error}；已恢复维护前数据库文件，"
                    f"但恢复后服务冒烟仍失败：{verification_error}"
                ) from verification_error
            job.update(
                {
                    "state": "rolled_back",
                    "phase": "rolled_back",
                    "progress": 100,
                    "message": f"压缩失败，已恢复维护前数据库：{error}",
                    "error": str(error)[:2000],
                }
            )
            self.jobs.save(job)

    @staticmethod
    def _docker_architecture(value: str) -> str:
        """把 Docker daemon 和镜像可能使用的架构别名归一化。"""

        normalized = value.strip().lower()
        return {
            "x86_64": "amd64",
            "aarch64": "arm64",
        }.get(normalized, normalized)

    def _inspect_local_image(self, image: str) -> str:
        """校验本地镜像存在、平台匹配，并返回不可变镜像 ID。

        参数：
            image: 已通过固定标签或 digest 校验的 OmniRoute 镜像引用。

        返回：
            Docker daemon 中该引用当前解析到的 `sha256:` 镜像 ID。

        异常：
            RuntimeError: 镜像不存在、元数据无效，或 OS/架构与 daemon 不同。
        """

        daemon = self.runner.run(
            ["docker", "version", "--format", "{{.Server.Os}}|{{.Server.Arch}}"],
            timeout=30,
            check=True,
            capture_output=True,
        )
        daemon_parts = str(getattr(daemon, "stdout", "")).strip().split("|")
        if len(daemon_parts) != 2 or not all(daemon_parts):
            raise RuntimeError("Docker 未返回 daemon 的 OS/架构")
        result = self.runner.run(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{.Id}}|{{.Os}}|{{.Architecture}}",
                image,
            ],
            timeout=30,
            check=True,
            capture_output=True,
        )
        image_parts = str(getattr(result, "stdout", "")).strip().split("|")
        if len(image_parts) != 3 or not image_parts[0].startswith("sha256:"):
            raise RuntimeError("Docker 未返回本地目标镜像的完整元数据")
        daemon_os, daemon_arch = daemon_parts
        image_id, image_os, image_arch = image_parts
        if image_os.strip().lower() != daemon_os.strip().lower() or self._docker_architecture(
            image_arch
        ) != self._docker_architecture(daemon_arch):
            raise RuntimeError(
                "本地 OmniRoute 镜像平台不匹配："
                f"镜像={image_os}/{image_arch}，Docker={daemon_os}/{daemon_arch}"
            )
        return image_id

    def _pull_image(self, image: str) -> str:
        """拉取固定镜像，校验 daemon 平台并返回不可变镜像 ID。"""

        self.runner.run(
            ["docker", "pull", image],
            timeout=900,
            check=True,
            capture_output=True,
        )
        return self._inspect_local_image(image)

    def _run_update(self, job: dict[str, Any]) -> None:
        """升级 OmniRoute；失败时同时恢复原镜像和升级前数据库。"""

        target = validate_fixed_image(str(job["request"].get("image", "")))
        cluster = self._cluster()
        source = str(
            cluster.get("OMNIROUTE_IMAGE")
            or cluster.get("GATEWAY_IMAGE")
            or RECOMMENDED_OMNIROUTE_IMAGE
        )
        if target == source:
            raise ValueError("目标镜像与当前配置相同，无需升级")
        running_before = self._running_image()
        if not running_before["image"] or not running_before["image_id"]:
            raise RuntimeError("当前 Router 未运行或无法读取实际 OmniRoute 镜像")
        if running_before["image"] != source:
            raise RuntimeError(
                "配置镜像与实际运行镜像不一致；请先执行 llmctl router restart 后重新评估"
            )
        job["source_image_id"] = running_before["image_id"]
        self._set_job(job, "assessing", 5, "升级前执行完整 SQLite 评估")
        assessment = self.assess(deep=True)
        if assessment["health"] == "critical":
            raise RuntimeError("SQLite 评估为严重异常，拒绝升级")
        local_image = bool(job["request"].get("local_image", False))
        if local_image:
            self._set_job(
                job,
                "validating_local_image",
                15,
                f"校验已离线导入的 OmniRoute 镜像 {target}",
            )
            target_image_id = self._inspect_local_image(target)
        else:
            self._set_job(job, "pulling", 15, f"拉取固定 OmniRoute 镜像 {target}")
            target_image_id = self._pull_image(target)
        job["target_image_id"] = target_image_id
        job["image_source"] = "local" if local_image else "registry"
        self._set_job(job, "backing_up", 35, "切换前创建 SQLite 一致性备份")
        backup = self._create_backup(
            "upgrade", target_image=target, job_id=str(job["id"])
        )
        job["backup_id"] = backup["id"]
        directory, metadata = self._validated_backup(str(backup["id"]))
        self._set_job(job, "switching", 50, "写入固定镜像并重启 Router")
        update_env_values(
            self.paths.cluster_env,
            {"OMNIROUTE_IMAGE": target, "GATEWAY_IMAGE": target},
        )
        try:
            self._set_job(
                job,
                "starting",
                65,
                "启动新 OmniRoute；GPU Worker 保持运行",
                cancellable=False,
            )
            self._restart_and_smoke()
            running = self._running_image()
            if running["image"] != target:
                raise RuntimeError(
                    f"Router 实际镜像不是目标版本：{running['image'] or 'unknown'}"
                )
            if running["image_id"] != target_image_id:
                raise RuntimeError(
                    "Router 实际镜像 ID 与升级前拉取并校验的镜像不一致"
                )
            self._set_job(
                job,
                "verifying",
                90,
                "验证新版本数据库、Router 与恢复点",
                cancellable=False,
            )
            after = self.assess(deep=False)
            if not after["integrity"]["ok"] or not after["foreign_keys"]["ok"]:
                raise RuntimeError("新版本启动后 SQLite 完整性检查失败")
            self._complete(
                job,
                f"OmniRoute 已升级到 {target}；回退点 {backup['id']} 已保留",
            )
        except Exception as error:
            try:
                self._stop_router_stack()
                self._restore_database(directory, metadata)
                self._restore_backup_image(metadata)
                self._restart_and_smoke()
            except Exception as rollback_error:
                raise RuntimeError(
                    f"升级失败：{error}；自动回滚失败：{rollback_error}"
                ) from rollback_error
            job.update(
                {
                    "state": "rolled_back",
                    "phase": "rolled_back",
                    "progress": 100,
                    "message": f"升级失败，已恢复原镜像和数据库：{error}",
                    "error": str(error)[:2000],
                }
            )
            self.jobs.save(job)

    def _run_manual_rollback(self, job: dict[str, Any]) -> None:
        """回滚到受管快照；失败时恢复本次回滚开始前状态。"""

        backup_id = str(job["request"].get("backup_id", ""))
        self._set_job(job, "verifying_backup", 5, "复核目标备份哈希和完整性")
        target_directory, target_metadata = self._validated_backup(backup_id)
        self._set_job(job, "backing_up", 20, "回滚前备份当前数据库和镜像配置")
        current_backup: dict[str, Any] | None = None
        current_directory: pathlib.Path | None = None
        current_metadata: dict[str, Any] | None = None
        try:
            current_backup = self._create_backup(
                "pre-rollback",
                target_image=str(target_metadata.get("source_image", "")),
                job_id=str(job["id"]),
            )
            job["pre_rollback_backup_id"] = current_backup["id"]
            current_directory, current_metadata = self._validated_backup(
                str(current_backup["id"])
            )
        except Exception as backup_error:
            job["pre_rollback_backup_error"] = str(backup_error)[:1000]
            self._set_job(
                job,
                "backing_up",
                28,
                "当前数据库无法生成一致性备份；停止 Router 后保留事故副本",
                log=str(backup_error),
            )
        self._set_job(job, "stopping", 40, "停止 Router 与账户门户；GPU Worker 保持运行")
        self._stop_router_stack()
        if current_backup is None:
            try:
                incident = self._create_incident_copy(
                    "pre-rollback", str(job["id"])
                )
                job["incident_backup_id"] = incident["id"]
                self.jobs.save(job)
            except Exception as incident_error:
                try:
                    self._restart_and_smoke()
                except Exception as restart_error:
                    raise RuntimeError(
                        f"无法保存损坏数据库的事故副本：{incident_error}；"
                        f"恢复现状服务失败：{restart_error}"
                    ) from restart_error
                raise RuntimeError(
                    f"无法保存损坏数据库的事故副本，未执行回滚：{incident_error}"
                ) from incident_error
        try:
            self._set_job(
                job,
                "restoring",
                60,
                f"恢复数据库与镜像配置：{backup_id}",
                cancellable=False,
            )
            self._restore_database(target_directory, target_metadata)
            self._restore_backup_image(target_metadata)
            self._set_job(
                job,
                "starting",
                80,
                "启动回滚版本并执行完整冒烟",
                cancellable=False,
            )
            self._restart_and_smoke()
            self.assess(deep=False)
            self._complete(
                job,
                f"已恢复备份 {backup_id}；回滚前状态保留为 "
                f"{current_backup['id'] if current_backup else job['incident_backup_id']}",
            )
        except Exception as error:
            if current_directory is None or current_metadata is None:
                raise RuntimeError(
                    f"人工回滚失败：{error}；当前数据库此前已损坏，"
                    f"仅保留事故副本 {job.get('incident_backup_id', 'unknown')}"
                ) from error
            try:
                self._stop_router_stack()
                self._restore_database(current_directory, current_metadata)
                self._restore_backup_image(current_metadata)
                self._restart_and_smoke()
            except Exception as compensation_error:
                raise RuntimeError(
                    f"人工回滚失败：{error}；恢复回滚前状态失败：{compensation_error}"
                ) from compensation_error
            job.update(
                {
                    "state": "rolled_back",
                    "phase": "rolled_back",
                    "progress": 100,
                    "message": f"人工回滚失败，已恢复操作前状态：{error}",
                    "error": str(error)[:2000],
                }
            )
            self.jobs.save(job)
