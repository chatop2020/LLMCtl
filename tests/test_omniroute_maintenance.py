import importlib.util
import json
import pathlib
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "llmctl_omniroute_maintenance", ROOT / "lib/omniroute_maintenance.py"
)
OMNI = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(OMNI)


class FakeRunner:
    """模拟 Docker、systemd 与 llmctl，只对隔离 SQLite 施加明确故障。"""

    def __init__(self, cluster_env: pathlib.Path, database: pathlib.Path):
        self.cluster_env = cluster_env
        self.database = database
        self.commands: list[list[str]] = []
        self.running_image = "diegosouzapw/omniroute:3.8.48"
        self.running_image_id = "sha256:old-image"
        self.fail_smoke_count = 0
        self.mutate_on_next_restart = False
        self.forced_image_id = ""
        self.fail_router_stop_count = 0

    def run(
        self,
        command,
        *,
        timeout=None,
        check=True,
        capture_output=True,
        env=None,
    ):
        """返回与 CommandRunner 相同的最小结果，并按配置注入冒烟失败。"""

        rendered = [str(value) for value in command]
        self.commands.append(rendered)
        stdout = ""
        returncode = 0
        if rendered[:3] == ["docker", "inspect", "--format"]:
            stdout = f"{self.running_image}|{self.running_image_id}\n"
        elif rendered[:3] == ["docker", "image", "inspect"]:
            stdout = "sha256:new-image\n"
        elif rendered[:2] == ["docker", "pull"]:
            stdout = "pulled\n"
        elif (
            rendered == ["systemctl", "stop", "llm-router.service"]
            and self.fail_router_stop_count
        ):
            self.fail_router_stop_count -= 1
            returncode = 1
            stdout = "router stop failed\n"
        elif rendered[-2:] == ["router", "restart"]:
            values = OMNI.parse_env_file(self.cluster_env)
            self.running_image = values.get(
                "OMNIROUTE_IMAGE", values.get("GATEWAY_IMAGE", self.running_image)
            )
            self.running_image_id = self.forced_image_id or (
                "sha256:new-image"
                if self.running_image.endswith(":3.8.49")
                else "sha256:old-image"
            )
            if self.mutate_on_next_restart:
                with sqlite3.connect(self.database) as connection:
                    connection.execute("UPDATE sample SET value='migrated'")
                self.mutate_on_next_restart = False
            # 真实 OmniRoute 启动会以读写方式打开 WAL 数据库并重建 SHM；测试
            # 需要模拟该生命周期，避免只读评估因缺少伴随文件产生环境假失败。
            with sqlite3.connect(self.database) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("SELECT 1").fetchone()
        elif rendered[-2:] == ["smoke", "--full"] and self.fail_smoke_count:
            self.fail_smoke_count -= 1
            returncode = 1
            stdout = "smoke failed\n"
        result = subprocess.CompletedProcess(rendered, returncode, stdout, "")
        if check and returncode:
            raise RuntimeError(f"命令执行失败，退出码 {returncode}：{stdout.strip()}")
        return result


class OmniRouteMaintenanceTests(unittest.TestCase):
    """验证 SQLite 一致性备份、维护、升级和双向恢复不变量。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.config = self.root / "etc/llm-cluster"
        self.state = self.root / "var/lib/llm-cluster"
        self.config.mkdir(parents=True)
        self.database = self.state / "omniroute/gateway/storage.sqlite"
        self.database.parent.mkdir(parents=True)
        self.cluster_env = self.config / "cluster.env"
        self.write_cluster("omniroute")
        with sqlite3.connect(self.database) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                "CREATE TABLE sample(id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute("INSERT INTO sample(value) VALUES('original')")
        self.paths = OMNI.OmniRouteMaintenancePaths(
            cluster_env=self.cluster_env,
            database=self.database,
            state_dir=self.state / "omniroute-maintenance",
            jobs_dir=self.state / "omniroute-maintenance/jobs",
            backup_root=self.root / "var/backups/llmctl/omniroute",
            last_assessment=self.state
            / "omniroute-maintenance/last-assessment.json",
            llmctl=self.root / "usr/local/sbin/llmctl",
        )
        self.runner = FakeRunner(self.cluster_env, self.database)
        self.manager = OMNI.OmniRouteMaintenanceManager(self.paths, self.runner)

    def tearDown(self):
        self.temporary.cleanup()

    def write_cluster(self, gateway: str) -> None:
        """写入只包含测试所需字段的受管 cluster.env。"""

        self.cluster_env.write_text(
            "\n".join(
                [
                    f"GATEWAY_KIND={gateway}",
                    "OMNIROUTE_IMAGE=diegosouzapw/omniroute:3.8.48",
                    "GATEWAY_IMAGE=diegosouzapw/omniroute:3.8.48",
                    "ACTIVE_WORKERS=0,1",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

    def wait_job(self, job: dict, timeout: float = 8) -> dict:
        """等待隔离后台任务进入终态，超时立即使测试失败。"""

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current = self.manager.jobs.get(job["id"])
            if current["state"] in OMNI.TERMINAL_STATES:
                return current
            time.sleep(0.02)
        self.fail(f"job did not finish: {job['id']}")

    def submit(self, action: str, **values) -> dict:
        """补齐服务端要求的确认短语并提交测试任务。"""

        confirmations = {
            "online": "MAINTAIN ONLINE",
            "compact": "COMPACT SQLITE",
            "update": "UPDATE OMNIROUTE",
        }
        payload = {"action": action, **values}
        if action in confirmations:
            payload["confirmation"] = confirmations[action]
        if action == "rollback":
            payload["confirmation"] = f"ROLLBACK {values['backup_id']}"
        return self.manager.submit(payload)

    def sample_value(self) -> str:
        """读取示例业务值，用于证明恢复的是目标快照而不是新空库。"""

        with sqlite3.connect(self.database) as connection:
            return str(connection.execute("SELECT value FROM sample").fetchone()[0])

    def test_assessment_reports_integrity_wal_storage_and_backup_advice(self):
        assessment = self.manager.assess(deep=True)

        self.assertTrue(assessment["integrity"]["ok"])
        self.assertTrue(assessment["foreign_keys"]["ok"])
        self.assertEqual(assessment["sqlite"]["journal_mode"], "wal")
        self.assertGreater(assessment["storage"]["database_size"], 0)
        self.assertEqual(assessment["backup"]["count"], 0)
        self.assertIn(
            "backup_stale",
            {item["code"] for item in assessment["recommendations"]},
        )
        self.assertTrue(self.paths.last_assessment.is_file())

    def test_foreign_key_violation_is_a_critical_assessment(self):
        with sqlite3.connect(self.database) as connection:
            connection.executescript(
                "CREATE TABLE parent(id INTEGER PRIMARY KEY);"
                "CREATE TABLE child(parent_id INTEGER REFERENCES parent(id));"
                "INSERT INTO child(parent_id) VALUES(99);"
            )

        assessment = self.manager.assess()

        self.assertEqual(assessment["health"], "critical")
        self.assertFalse(assessment["foreign_keys"]["ok"])

    def test_backup_and_manual_rollback_restore_database_and_image(self):
        backup_job = self.wait_job(self.submit("backup"))
        self.assertEqual(backup_job["state"], "succeeded")
        backup_id = backup_job["backup_id"]
        with sqlite3.connect(self.database) as connection:
            connection.execute("UPDATE sample SET value='changed'")
        OMNI.update_env_values(
            self.cluster_env,
            {
                "OMNIROUTE_IMAGE": "diegosouzapw/omniroute:3.8.49",
                "GATEWAY_IMAGE": "diegosouzapw/omniroute:3.8.49",
            },
        )

        rollback = self.wait_job(self.submit("rollback", backup_id=backup_id))

        self.assertEqual(rollback["state"], "succeeded")
        self.assertEqual(self.sample_value(), "original")
        cluster = OMNI.parse_env_file(self.cluster_env)
        self.assertEqual(
            cluster["OMNIROUTE_IMAGE"], "diegosouzapw/omniroute:3.8.48"
        )
        self.assertTrue(rollback["pre_rollback_backup_id"].startswith("pre-rollback-"))
        self.assertFalse(any("llm-worker" in " ".join(cmd) for cmd in self.runner.commands))

    def test_corrupted_current_database_keeps_incident_copy_then_restores_backup(self):
        """当前库损坏时仍可恢复已验证快照，并保留不可自动恢复的原始副本。"""

        backup_job = self.wait_job(self.submit("backup"))
        backup_id = backup_job["backup_id"]
        self.database.write_bytes(b"not-a-sqlite-database")

        rollback = self.wait_job(self.submit("rollback", backup_id=backup_id))

        self.assertEqual(rollback["state"], "succeeded")
        self.assertEqual(self.sample_value(), "original")
        self.assertTrue(rollback["incident_backup_id"].startswith("incident-"))
        incident_dir = self.paths.backup_root / rollback["incident_backup_id"]
        incident = json.loads(
            (incident_dir / "metadata.json").read_text(encoding="utf-8")
        )
        self.assertEqual(incident["backup_status"], "incident")
        self.assertNotIn(
            rollback["incident_backup_id"],
            {item["id"] for item in self.manager.list_backups()},
        )

    def test_online_maintenance_backs_up_without_restarting_router(self):
        job = self.wait_job(self.submit("online"))

        self.assertEqual(job["state"], "succeeded")
        self.assertTrue(job["backup_id"].startswith("online-"))
        self.assertFalse(any("systemctl" in cmd for cmd in self.runner.commands))
        self.assertFalse(any("router" in cmd for cmd in self.runner.commands))
        self.assertEqual(self.sample_value(), "original")

    def test_compact_uses_maintenance_window_and_preserves_data(self):
        with sqlite3.connect(self.database) as connection:
            connection.executemany(
                "INSERT INTO sample(value) VALUES(?)",
                [("x" * 1000,) for _ in range(300)],
            )
            connection.execute("DELETE FROM sample WHERE id > 1")

        job = self.wait_job(self.submit("compact"))

        self.assertEqual(job["state"], "succeeded")
        self.assertEqual(self.sample_value(), "original")
        commands = [" ".join(command) for command in self.runner.commands]
        self.assertTrue(any("systemctl stop llm-router.service" in item for item in commands))
        self.assertTrue(any("router restart" in item for item in commands))
        self.assertTrue(any("smoke --full" in item for item in commands))

    def test_update_switches_fixed_image_after_backup_and_full_smoke(self):
        job = self.wait_job(
            self.submit("update", image="diegosouzapw/omniroute:3.8.49")
        )

        self.assertEqual(job["state"], "succeeded")
        self.assertTrue(job["backup_id"].startswith("upgrade-"))
        cluster = OMNI.parse_env_file(self.cluster_env)
        self.assertEqual(
            cluster["OMNIROUTE_IMAGE"], "diegosouzapw/omniroute:3.8.49"
        )
        self.assertEqual(self.runner.running_image, "diegosouzapw/omniroute:3.8.49")
        commands = [" ".join(command) for command in self.runner.commands]
        self.assertLess(
            next(index for index, item in enumerate(commands) if "docker pull" in item),
            next(index for index, item in enumerate(commands) if "router restart" in item),
        )

    def test_failed_update_restores_original_database_and_image(self):
        self.runner.mutate_on_next_restart = True
        self.runner.fail_smoke_count = 1

        job = self.wait_job(
            self.submit("update", image="diegosouzapw/omniroute:3.8.49")
        )

        self.assertEqual(job["state"], "rolled_back")
        self.assertEqual(self.sample_value(), "original")
        cluster = OMNI.parse_env_file(self.cluster_env)
        self.assertEqual(
            cluster["OMNIROUTE_IMAGE"], "diegosouzapw/omniroute:3.8.48"
        )
        self.assertEqual(self.runner.running_image, "diegosouzapw/omniroute:3.8.48")

    def test_update_image_id_mismatch_triggers_database_and_image_rollback(self):
        """标签相同但实际镜像 ID 不符时也必须视为供应链验收失败。"""

        self.runner.forced_image_id = "sha256:unexpected-image"

        job = self.wait_job(
            self.submit("update", image="diegosouzapw/omniroute:3.8.49")
        )

        self.assertEqual(job["state"], "rolled_back")
        self.assertIn("镜像 ID", job["message"])
        self.assertEqual(self.sample_value(), "original")
        self.assertEqual(
            OMNI.parse_env_file(self.cluster_env)["OMNIROUTE_IMAGE"],
            "diegosouzapw/omniroute:3.8.48",
        )

    def test_update_rejects_configured_and_running_image_drift(self):
        """升级前的实际版本无法复现时不得生成一个错误的回退点。"""

        self.runner.running_image = "diegosouzapw/omniroute:3.8.47"
        self.runner.running_image_id = "sha256:drifted-image"

        job = self.wait_job(
            self.submit("update", image="diegosouzapw/omniroute:3.8.49")
        )

        self.assertEqual(job["state"], "failed")
        self.assertIn("配置镜像与实际运行镜像不一致", job["message"])
        self.assertFalse(self.manager.list_backups())

    def test_failed_compact_restores_premaintenance_snapshot(self):
        self.runner.fail_smoke_count = 1

        job = self.wait_job(self.submit("compact"))

        self.assertEqual(job["state"], "rolled_back")
        self.assertEqual(self.sample_value(), "original")

    def test_compact_reports_restored_database_when_smoke_remains_unhealthy(self):
        """两次冒烟均失败时也必须区分数据库已恢复与服务尚未通过验收。"""

        self.runner.fail_smoke_count = 2

        job = self.wait_job(self.submit("compact"))

        self.assertEqual(job["state"], "failed")
        self.assertEqual(self.sample_value(), "original")
        self.assertIn("已恢复维护前数据库文件", job["message"])
        self.assertIn("恢复后服务冒烟仍失败", job["message"])

    def test_router_stop_failure_restarts_account_portal_without_touching_database(self):
        """维护窗口无法建立时应恢复账户门户，并保持主库和 Router 配置不变。"""

        self.runner.fail_router_stop_count = 1

        job = self.wait_job(self.submit("compact"))

        self.assertEqual(job["state"], "failed")
        self.assertEqual(self.sample_value(), "original")
        commands = [" ".join(command) for command in self.runner.commands]
        self.assertTrue(any("start llm-account.service" in item for item in commands))
        self.assertFalse(any("router restart" in item for item in commands))

    def test_mutable_image_path_traversal_and_wrong_confirmation_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "可变标签"):
            self.manager.submit(
                {
                    "action": "update",
                    "image": "diegosouzapw/omniroute:latest",
                    "confirmation": "UPDATE OMNIROUTE",
                }
            )
        with self.assertRaisesRegex(ValueError, "确认操作"):
            self.manager.submit(
                {
                    "action": "update",
                    "image": "diegosouzapw/omniroute:3.8.49",
                    "confirmation": "yes",
                }
            )
        with self.assertRaisesRegex(ValueError, "备份 ID"):
            self.manager.submit(
                {
                    "action": "rollback",
                    "backup_id": "../../etc/passwd",
                    "confirmation": "ROLLBACK ../../etc/passwd",
                }
            )

    def test_corrupted_backup_hash_is_rejected_before_a_rollback_job_exists(self):
        """备份内容与元数据不匹配时必须在停止 Router 前失败。"""

        backup_job = self.wait_job(self.submit("backup"))
        backup_id = backup_job["backup_id"]
        backup_file = self.paths.backup_root / backup_id / "database.sqlite"
        with backup_file.open("ab") as handle:
            handle.write(b"tampered")

        with self.assertRaisesRegex(ValueError, "大小或 SHA256"):
            self.submit("rollback", backup_id=backup_id)
        self.assertFalse(
            any("systemctl" in command for command in self.runner.commands)
        )

    def test_non_omniroute_and_active_model_job_fail_closed(self):
        self.write_cluster("litellm")
        with self.assertRaisesRegex(ValueError, "不是 OmniRoute"):
            self.manager.assess()
        self.write_cluster("omniroute")
        blocked = OMNI.OmniRouteMaintenanceManager(
            dataclasses_replace(self.paths, jobs_dir=self.state / "blocked/jobs"),
            self.runner,
            active_model_job=lambda: True,
        )
        with self.assertRaisesRegex(RuntimeError, "模型部署"):
            blocked.submit({"action": "backup"})

    def test_interrupted_job_becomes_explicit_failure_after_restart(self):
        waiting = self.manager.jobs.create("backup", {"action": "backup"})

        recovered = OMNI.OmniRouteMaintenanceManager(self.paths, self.runner)

        current = recovered.jobs.get(waiting["id"])
        self.assertEqual(current["state"], "failed")
        self.assertEqual(current["phase"], "interrupted")

    def test_manager_accepts_the_model_controllers_shared_submission_lock(self):
        """模型任务和 OmniRoute 任务必须由同一把提交锁消除检查竞态。"""

        shared_lock = threading.Lock()
        isolated = OMNI.OmniRouteMaintenanceManager(
            dataclasses_replace(self.paths, jobs_dir=self.state / "shared-lock/jobs"),
            self.runner,
            submission_lock=shared_lock,
        )

        self.assertIs(isolated._submission_lock, shared_lock)


def dataclasses_replace(value, **changes):
    """避免测试模块依赖实现内部导出的 dataclasses 名称。"""

    import dataclasses

    return dataclasses.replace(value, **changes)


if __name__ == "__main__":
    unittest.main()
