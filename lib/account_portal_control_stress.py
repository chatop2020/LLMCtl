#!/usr/bin/env python3
"""门户后台压测任务的启动、同步、取消与路由归因策略。"""

from __future__ import annotations

from account_portal_common import *
from account_portal_database import *
from account_portal_gateway import *


class PortalStressControlMixin:
    """门户后台压测任务的启动、同步、取消与路由归因策略。该类型只提供领域方法，运行状态由组合控制器持有。"""

    @property
    def stress_root(self) -> pathlib.Path:
        return self.config.db_path.parent / "stress"

    @staticmethod
    def process_alive(pid: int | None) -> bool:
        if not pid or pid <= 1:
            return False
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    @staticmethod
    def stress_process_matches(pid: int, run_id: str) -> bool:
        """确认 PID 仍属于当前任务，避免向已经复用的进程发送信号。"""
        try:
            arguments = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\x00")
        except OSError:
            return False
        decoded = [value.decode("utf-8", errors="replace") for value in arguments]
        return run_id in decoded and any(
            pathlib.Path(value).name == "llm_benchmark.py" for value in decoded
        )

    def stress_route_map(self, published: sqlite3.Row) -> dict[str, str]:
        """把 OmniRoute 响应标识解析为便于管理员理解的 Worker 名称。

        路由元数据由 OmniRoute 返回，显示标签来自 LLMCtl 管理的 Combo 目标。
        该查询采用尽力而为语义，构建选项接口不可用时仍允许压测继续，并保留
        原始 Provider 或连接标识用于排障。
        """
        if str(published["source_kind"] or "") != "combo":
            return {}
        source_ref = str(published["source_ref"] or "").strip()
        source_model = str(published["source_model"] or "").strip()
        try:
            combos = self.omni.combos()
        except RuntimeError:
            return {}
        combo = next(
            (
                item
                for item in combos
                if source_ref in {str(item.get("id", "")), str(item.get("name", ""))}
                or source_model == str(item.get("name", ""))
            ),
            None,
        )
        if not combo:
            return {}
        models = combo.get("models", [])
        if not isinstance(models, list):
            return {}

        route_map: dict[str, str] = {}
        target_provider_labels: dict[str, set[str]] = {}
        target_connection_labels: dict[str, str] = {}
        for index, target in enumerate(models):
            if not isinstance(target, dict):
                continue
            label = str(target.get("label") or f"Worker {index}").strip()
            provider = str(target.get("providerId") or target.get("provider") or "").strip()
            connection = str(target.get("connectionId") or "").strip()
            if provider:
                target_provider_labels.setdefault(provider, set()).add(label)
            if connection:
                target_connection_labels[connection] = label
                route_map[connection] = label

        # Provider 标识只在唯一指向一个 Combo 目标时才可安全标注；共享 Provider
        # 必须通过 Connection 区分。
        for provider, labels in target_provider_labels.items():
            if len(labels) == 1:
                route_map[provider] = next(iter(labels))

        try:
            options = self.omni.combo_builder_options()
        except RuntimeError:
            options = {}
        providers = options.get("providers", []) if isinstance(options, dict) else []
        if isinstance(providers, list):
            for provider in providers:
                if not isinstance(provider, dict):
                    continue
                provider_id = str(
                    provider.get("providerId") or provider.get("id") or ""
                ).strip()
                labels = target_provider_labels.get(provider_id, set())
                if len(labels) == 1:
                    label = next(iter(labels))
                    for key in ("providerId", "id", "alias", "prefix"):
                        value = str(provider.get(key) or "").strip()
                        if value:
                            route_map[value] = label
                connections = provider.get("connections", [])
                if not isinstance(connections, list):
                    continue
                for connection in connections:
                    if not isinstance(connection, dict):
                        continue
                    connection_id = str(connection.get("id") or "").strip()
                    if connection_id in target_connection_labels:
                        route_map[connection_id] = target_connection_labels[connection_id]
        return route_map

    def sync_stress_run(self, run_id: str) -> dict[str, Any]:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM stress_runs WHERE id=?", (run_id,)
            ).fetchone()
        if not row:
            raise ValueError("压测任务不存在")
        record = dict(row)
        status_path = pathlib.Path(record["result_dir"]) / "status.json"
        status_document: dict[str, Any] = {}
        if status_path.is_file():
            try:
                value = json.loads(status_path.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    status_document = value
            except (OSError, json.JSONDecodeError):
                pass
        stored_status = str(record["status"])
        file_status = str(status_document.get("status", stored_status))
        terminal_statuses = {"completed", "failed", "canceled"}
        if stored_status in terminal_statuses and file_status not in terminal_statuses:
            status = stored_status
        elif stored_status == "canceling" and file_status not in terminal_statuses:
            status = "canceling"
        else:
            status = file_status
        if status not in {"starting", "running", "canceling", "completed", "failed", "canceled"}:
            status = "failed"
        pid = int(record.get("pid") or 0)
        if status == "canceling" and not self.process_alive(pid):
            status = "canceled"
        elif status in {"starting", "running"} and not self.process_alive(pid):
            grace_expired = now() - int(record["created_at"]) > 5
            if grace_expired:
                status = "failed"
                status_document["error"] = status_document.get("error") or "压测执行器意外退出"
        metrics = status_document.get("metrics", {})
        if not isinstance(metrics, dict):
            metrics = {}
        finished_at = (
            int(status_document.get("updated_at", now()))
            if status in {"completed", "failed", "canceled"}
            else None
        )
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE stress_runs SET status=?,metrics_json=?,error=?,started_at=COALESCE(started_at,?),finished_at=COALESCE(?,finished_at),updated_at=? WHERE id=?",
                (
                    status,
                    json.dumps(metrics, ensure_ascii=False, separators=(",", ":")),
                    str(status_document.get("error", ""))[:500],
                    status_document.get("started_at"),
                    finished_at,
                    now(),
                    run_id,
                ),
            )
            current = connection.execute(
                "SELECT * FROM stress_runs WHERE id=?", (run_id,)
            ).fetchone()
        result = dict(current)
        result["metrics"] = json.loads(result.pop("metrics_json") or "{}")
        result["progress"] = float(status_document.get("progress", 0) or 0)
        result["elapsed_seconds"] = float(status_document.get("elapsed_seconds", 0) or 0)
        gpu = status_document.get("gpu", {})
        result["gpu"] = gpu if isinstance(gpu, dict) else {}
        event_path = pathlib.Path(result["result_dir"]) / "events.jsonl"
        events: list[dict[str, Any]] = []
        if event_path.is_file():
            try:
                lines = event_path.read_text(encoding="utf-8").splitlines()[-20:]
                for line in lines:
                    value = json.loads(line)
                    if isinstance(value, dict):
                        events.append(value)
            except (OSError, json.JSONDecodeError):
                events = []
        result["recent_requests"] = events
        result.pop("result_dir", None)
        result.pop("pid", None)
        return result

    def stress_runs(self, limit: int = 30) -> list[dict[str, Any]]:
        with self.db.connect() as connection:
            identifiers = [
                str(row["id"])
                for row in connection.execute(
                    "SELECT id FROM stress_runs ORDER BY created_at DESC LIMIT ?",
                    (max(1, min(limit, 100)),),
                )
            ]
        return [self.sync_stress_run(identifier) for identifier in identifiers]

    def start_stress_run(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        try:
            concurrency = int(payload.get("concurrency", 1))
            input_tokens = int(payload.get("input_tokens", 50))
            output_tokens = int(payload.get("output_tokens", 128))
            request_multiplier = int(payload.get("request_multiplier", 2))
        except (TypeError, ValueError) as error:
            raise ValueError("压测参数必须为整数") from error
        if concurrency not in STRESS_CONCURRENCY_CHOICES:
            raise ValueError("不支持该并发档位")
        if input_tokens not in STRESS_INPUT_TOKEN_CHOICES:
            raise ValueError("不支持该提示词 Token 档位")
        if output_tokens not in STRESS_OUTPUT_TOKEN_CHOICES:
            raise ValueError("不支持该最大输出 Token 档位")
        if request_multiplier not in STRESS_REQUEST_MULTIPLIER_CHOICES:
            raise ValueError("每个并发槽位的请求数必须为 1-4")
        if (concurrency >= 20 or input_tokens >= 8000) and payload.get("risk_confirmed") is not True:
            raise ValueError("高负载压测必须确认风险")
        model = str(payload.get("model", "")).strip()
        with self.lock:
            with self.db.connect() as connection:
                published = connection.execute(
                    "SELECT * FROM published_models WHERE public_model_id=? AND status='published'",
                    (model,),
                ).fetchone()
                active = connection.execute(
                    "SELECT id FROM stress_runs WHERE status IN ('starting','running','canceling') ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
            if not published:
                raise ValueError("只能压测已发布的公开模型 ID")
            if active:
                current = self.sync_stress_run(str(active["id"]))
                if current["status"] in {"starting", "running", "canceling"}:
                    raise ValueError("已有压测任务正在运行，请等待完成或先停止")
            runner = pathlib.Path(__file__).resolve().with_name("llm_benchmark.py")
            if not runner.is_file():
                raise RuntimeError("缺少后台压测执行器 llm_benchmark.py")
            run_id = str(uuid.uuid4())
            run_dir = self.stress_root / run_id
            run_dir.mkdir(parents=True, exist_ok=False)
            os.chmod(run_dir, 0o700)
            route_map_path = run_dir / "route-map.json"
            route_map_path.write_text(
                json.dumps(
                    self.stress_route_map(published),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            os.chmod(route_map_path, 0o600)
            command = [
                sys.executable,
                str(runner),
                "--run-id", run_id,
                "--base-url", self.config.gateway_url,
                "--model", model,
                "--concurrency", str(concurrency),
                "--input-tokens", str(input_tokens),
                "--output-tokens", str(output_tokens),
                "--request-multiplier", str(request_multiplier),
                "--result-dir", str(run_dir),
                "--route-map", str(route_map_path),
            ]
            environment = os.environ.copy()
            environment["LLMCTL_BENCHMARK_API_KEY"] = self.config.gateway_manage_key
            log_file = (run_dir / "runner.log").open("ab", buffering=0)
            try:
                process = subprocess.Popen(  # noqa: S603 - fixed local executable and validated arguments
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    env=environment,
                    start_new_session=True,
                    close_fds=True,
                )
            finally:
                log_file.close()
            stamp = now()
            try:
                with self.db.connect() as connection:
                    connection.execute(
                        """INSERT INTO stress_runs(id,public_model_id,concurrency,target_input_tokens,max_output_tokens,request_multiplier,request_count,status,pid,result_dir,created_by,created_at,updated_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            run_id, model, concurrency, input_tokens, output_tokens,
                            request_multiplier, concurrency * request_multiplier,
                            "starting", process.pid, str(run_dir), actor, stamp, stamp,
                        ),
                    )
            except Exception:
                # Popen 成功但持久运行记录失败时，不能让无人跟踪的压测继续在后台
                # 消耗网关/GPU。
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.killpg(process.pid, signal.SIGKILL)
                raise
            threading.Thread(
                target=process.wait,
                name=f"stress-reaper-{run_id[:8]}",
                daemon=True,
            ).start()
        return self.sync_stress_run(run_id)

    def cancel_stress_run(self, run_id: str) -> dict[str, Any]:
        current = self.sync_stress_run(run_id)
        if current["status"] not in {"starting", "running", "canceling"}:
            return current
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT pid FROM stress_runs WHERE id=?", (run_id,)
            ).fetchone()
            connection.execute(
                "UPDATE stress_runs SET status='canceling',updated_at=? WHERE id=?",
                (now(), run_id),
            )
        pid = int(row["pid"] or 0)
        if self.process_alive(pid) and self.stress_process_matches(pid, run_id):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pid, signal.SIGTERM)

            def enforce_stop() -> None:
                time.sleep(10)
                with self.db.connect() as connection:
                    latest = connection.execute(
                        "SELECT pid,status FROM stress_runs WHERE id=?", (run_id,)
                    ).fetchone()
                if not latest or latest["status"] != "canceling":
                    return
                latest_pid = int(latest["pid"] or 0)
                if not (
                    self.process_alive(latest_pid)
                    and self.stress_process_matches(latest_pid, run_id)
                ):
                    return
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(latest_pid, signal.SIGKILL)
                with self.db.connect() as connection:
                    connection.execute(
                        "UPDATE stress_runs SET status='canceled',error=?,finished_at=?,updated_at=? WHERE id=? AND status='canceling'",
                        ("停止等待超过 10 秒，已强制结束后台压测进程", now(), now(), run_id),
                    )

            threading.Thread(
                target=enforce_stop,
                name=f"stress-stop-{run_id[:8]}",
                daemon=True,
            ).start()
        return self.sync_stress_run(run_id)
