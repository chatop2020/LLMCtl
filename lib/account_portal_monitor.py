#!/usr/bin/env python3
"""账户门户向管理员提供的只读主机监控采样器。"""

from __future__ import annotations

from account_portal_common import *

class SystemMonitor:
    """为已认证管理员提供低开销、只读的主机运行指标。

    采样由请求驱动，并在不同浏览器会话间共享。短期缓存避免多个标签页重复
    扫描 `/proc` 或启动额外 `nvidia-smi` 进程；调用方输入不会进入命令参数。
    """

    def __init__(
        self,
        proc_root: pathlib.Path | str = "/proc",
        *,
        cache_seconds: float = SYSTEM_MONITOR_CACHE_SECONDS,
        monotonic=time.monotonic,
        command_runner=None,
    ) -> None:
        self.proc_root = pathlib.Path(proc_root)
        self.cache_seconds = max(0.0, float(cache_seconds))
        self.monotonic = monotonic
        self.command_runner = command_runner
        self.lock = threading.Lock()
        self.cached: dict[str, Any] | None = None
        self.cached_at = 0.0
        self.previous_cpu: tuple[int, int] | None = None
        self.previous_network: tuple[float, dict[str, tuple[int, int]]] | None = None
        self.previous_processes: dict[tuple[int, int], int] = {}
        self.user_names: dict[int, str] = {}
        try:
            self.clock_ticks = int(os.sysconf("SC_CLK_TCK"))
        except (AttributeError, OSError, ValueError):
            self.clock_ticks = 100
        try:
            self.page_size = int(os.sysconf("SC_PAGE_SIZE"))
        except (AttributeError, OSError, ValueError):
            self.page_size = 4096

    @staticmethod
    def _number(value: str, *, integer: bool = False) -> int | float | None:
        value = value.strip()
        if not value or value.lower() in {"n/a", "[n/a]", "not supported"}:
            return None
        try:
            return int(float(value)) if integer else float(value)
        except ValueError:
            return None

    @staticmethod
    def _percent(used: int | float, total: int | float) -> float | None:
        if total <= 0:
            return None
        return round(max(0.0, min(100.0, float(used) * 100.0 / float(total))), 1)

    @staticmethod
    def _decode_mount(value: str) -> str:
        return re.sub(
            r"\\([0-7]{3})",
            lambda match: chr(int(match.group(1), 8)),
            value,
        )

    @staticmethod
    def redact_command_line(arguments: list[str]) -> str:
        """返回可排障的命令摘要，同时隐藏常见密钥和凭据。"""
        redacted: list[str] = []
        redact_next = False
        for argument in arguments:
            argument = SYSTEM_MONITOR_CREDENTIAL_URL.sub(r"\1<redacted>\2", argument)
            if redact_next:
                redacted.append("<redacted>")
                redact_next = False
                continue
            lowered = argument.lower()
            if lowered == "bearer":
                redacted.append(argument)
                redact_next = True
                continue
            if re.match(r"(?i)^sk-[a-z0-9_-]{12,}$", argument):
                redacted.append("<redacted>")
                continue
            if SYSTEM_MONITOR_SECRET_ARGUMENT.search(argument):
                if "=" in argument:
                    name, _value = argument.split("=", 1)
                    redacted.append(f"{name}=<redacted>")
                elif ":" in argument and not argument.startswith(("http://", "https://")):
                    name, _value = argument.split(":", 1)
                    redacted.append(f"{name}:<redacted>")
                else:
                    redacted.append(argument)
                    redact_next = True
                continue
            redacted.append(argument)
        result = shlex.join(redacted)
        return result[:SYSTEM_MONITOR_COMMAND_LIMIT]

    def _read_text(self, relative: str) -> str:
        return (self.proc_root / relative).read_text(errors="replace")

    def _cpu(self) -> tuple[dict[str, Any], int]:
        cpu_line = next(
            line for line in self._read_text("stat").splitlines() if line.startswith("cpu ")
        )
        counters = [int(value) for value in cpu_line.split()[1:]]
        total = sum(counters)
        idle = counters[3] + (counters[4] if len(counters) > 4 else 0)
        percent = None
        total_delta = 0
        if self.previous_cpu:
            previous_total, previous_idle = self.previous_cpu
            total_delta = max(0, total - previous_total)
            idle_delta = max(0, idle - previous_idle)
            if total_delta:
                percent = round(
                    max(0.0, min(100.0, (total_delta - idle_delta) * 100.0 / total_delta)),
                    1,
                )
        self.previous_cpu = (total, idle)
        return {
            "usage_percent": percent,
            "logical_cpus": os.cpu_count() or 1,
        }, total_delta

    def _memory(self) -> dict[str, Any]:
        values: dict[str, int] = {}
        for line in self._read_text("meminfo").splitlines():
            if ":" not in line:
                continue
            name, raw = line.split(":", 1)
            parts = raw.split()
            if parts:
                values[name] = int(parts[0]) * 1024
        total = values.get("MemTotal", 0)
        available = values.get("MemAvailable", values.get("MemFree", 0))
        used = max(0, total - available)
        swap_total = values.get("SwapTotal", 0)
        swap_free = values.get("SwapFree", 0)
        return {
            "total_bytes": total,
            "available_bytes": available,
            "used_bytes": used,
            "used_percent": self._percent(used, total),
            "cached_bytes": values.get("Cached", 0) + values.get("SReclaimable", 0),
            "buffers_bytes": values.get("Buffers", 0),
            "swap_total_bytes": swap_total,
            "swap_used_bytes": max(0, swap_total - swap_free),
            "swap_used_percent": self._percent(max(0, swap_total - swap_free), swap_total),
        }

    def _network(self, sampled_at: float) -> dict[str, Any]:
        counters: dict[str, tuple[int, int]] = {}
        for line in self._read_text("net/dev").splitlines()[2:]:
            if ":" not in line:
                continue
            interface, raw = line.split(":", 1)
            fields = raw.split()
            if len(fields) >= 9:
                counters[interface.strip()] = (int(fields[0]), int(fields[8]))
        previous_at = sampled_at
        previous: dict[str, tuple[int, int]] = {}
        if self.previous_network:
            previous_at, previous = self.previous_network
        elapsed = sampled_at - previous_at
        interfaces: list[dict[str, Any]] = []
        for name, (received, transmitted) in sorted(counters.items()):
            before = previous.get(name)
            rx_rate = tx_rate = None
            if before and elapsed > 0:
                rx_rate = max(0.0, (received - before[0]) / elapsed)
                tx_rate = max(0.0, (transmitted - before[1]) / elapsed)
            interfaces.append(
                {
                    "name": name,
                    "rx_bytes": received,
                    "tx_bytes": transmitted,
                    "rx_bytes_per_second": round(rx_rate, 1) if rx_rate is not None else None,
                    "tx_bytes_per_second": round(tx_rate, 1) if tx_rate is not None else None,
                    "loopback": name == "lo",
                }
            )
        self.previous_network = (sampled_at, counters)
        external = [item for item in interfaces if not item["loopback"]]
        return {
            "interfaces": interfaces,
            "rx_bytes_per_second": (
                round(sum(item["rx_bytes_per_second"] or 0 for item in external), 1)
                if elapsed > 0
                else None
            ),
            "tx_bytes_per_second": (
                round(sum(item["tx_bytes_per_second"] or 0 for item in external), 1)
                if elapsed > 0
                else None
            ),
        }

    def _user_name(self, uid: int) -> str:
        if uid not in self.user_names:
            try:
                self.user_names[uid] = pwd.getpwuid(uid).pw_name
            except KeyError:
                self.user_names[uid] = str(uid)
        return self.user_names[uid]

    def _processes(
        self, total_cpu_delta: int, memory_total: int
    ) -> tuple[dict[str, int], list[dict[str, Any]]]:
        records: list[dict[str, Any]] = []
        current_ticks: dict[tuple[int, int], int] = {}
        states: dict[str, int] = {}
        logical_cpus = os.cpu_count() or 1
        for entry in self.proc_root.iterdir():
            if not entry.name.isdecimal():
                continue
            try:
                raw = (entry / "stat").read_text(errors="replace")
                right = raw.rfind(")")
                left = raw.find("(")
                if left < 0 or right <= left:
                    continue
                fields = raw[right + 2 :].split()
                if len(fields) < 22:
                    continue
                pid = int(entry.name)
                command = raw[left + 1 : right]
                state = fields[0]
                ticks = int(fields[11]) + int(fields[12])
                start_time = int(fields[19])
                rss_bytes = max(0, int(fields[21])) * self.page_size
            except (FileNotFoundError, PermissionError, OSError, ValueError):
                continue
            key = (pid, start_time)
            current_ticks[key] = ticks
            previous_ticks = self.previous_processes.get(key, ticks)
            cpu_percent = 0.0
            if total_cpu_delta > 0:
                cpu_percent = max(
                    0.0,
                    (ticks - previous_ticks) * logical_cpus * 100.0 / total_cpu_delta,
                )
            states[state] = states.get(state, 0) + 1
            records.append(
                {
                    "pid": pid,
                    "command": command,
                    "state": state,
                    "cpu_percent": round(cpu_percent, 1),
                    "memory_percent": round(rss_bytes * 100.0 / memory_total, 2)
                    if memory_total
                    else 0.0,
                    "rss_bytes": rss_bytes,
                    "proc_path": entry,
                }
            )
        self.previous_processes = current_ticks
        records.sort(
            key=lambda item: (item["cpu_percent"], item["rss_bytes"]), reverse=True
        )
        result: list[dict[str, Any]] = []
        for record in records[:SYSTEM_MONITOR_PROCESS_LIMIT]:
            entry = record.pop("proc_path")
            try:
                record["user"] = self._user_name(entry.stat().st_uid)
            except (FileNotFoundError, PermissionError, OSError):
                record["user"] = "?"
            try:
                arguments = [
                    value.decode(errors="replace")
                    for value in (entry / "cmdline").read_bytes().split(b"\0")
                    if value
                ]
            except (FileNotFoundError, PermissionError, OSError):
                arguments = []
            record["command_line"] = (
                self.redact_command_line(arguments) if arguments else record["command"]
            )
            result.append(record)
        summary = {
            "total": len(records),
            "running": states.get("R", 0),
            "sleeping": states.get("S", 0),
            "uninterruptible": states.get("D", 0),
            "zombie": states.get("Z", 0),
            "returned": len(result),
        }
        return summary, result

    def _disks(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[tuple[str, int]] = set()
        for line in self._read_text("self/mountinfo").splitlines():
            before, separator, after = line.partition(" - ")
            if not separator:
                continue
            fields, trailing = before.split(), after.split()
            if len(fields) < 6 or len(trailing) < 2:
                continue
            mount_point = self._decode_mount(fields[4])
            filesystem, source = trailing[0], self._decode_mount(trailing[1])
            if filesystem not in SYSTEM_MONITOR_LOCAL_FILESYSTEMS:
                continue
            try:
                stats = os.statvfs(mount_point)
            except (FileNotFoundError, PermissionError, OSError):
                continue
            total = stats.f_blocks * stats.f_frsize
            available = stats.f_bavail * stats.f_frsize
            used = max(0, (stats.f_blocks - stats.f_bfree) * stats.f_frsize)
            identity = (source, total)
            if identity in seen or total <= 0:
                continue
            seen.add(identity)
            rows.append(
                {
                    "source": source,
                    "mount_point": mount_point,
                    "filesystem": filesystem,
                    "total_bytes": total,
                    "used_bytes": used,
                    "available_bytes": available,
                    "used_percent": self._percent(used, used + available),
                }
            )
        rows.sort(key=lambda item: (item["mount_point"] != "/", item["mount_point"]))
        return rows

    def _run_command(self, arguments: list[str]) -> str:
        if self.command_runner is not None:
            result = self.command_runner(list(arguments))
            return result.stdout if hasattr(result, "stdout") else str(result)
        completed = subprocess.run(
            arguments,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=2,
            env={**os.environ, "LC_ALL": "C"},
        )
        return completed.stdout

    def _gpus(self) -> dict[str, Any]:
        executable = shutil.which("nvidia-smi")
        if self.command_runner is not None and not executable:
            executable = "nvidia-smi"
        if not executable:
            return {"available": False, "error": "nvidia-smi 未安装或不在 PATH 中", "gpus": []}
        query = (
            "index,name,uuid,pci.bus_id,driver_version,temperature.gpu,"
            "utilization.gpu,utilization.memory,memory.used,memory.total,"
            "power.draw,power.limit,pstate"
        )
        try:
            raw = self._run_command(
                [executable, f"--query-gpu={query}", "--format=csv,noheader,nounits"]
            )
        except (OSError, subprocess.SubprocessError) as error:
            return {"available": False, "error": f"nvidia-smi 读取失败：{error}", "gpus": []}
        gpu_rows: list[dict[str, Any]] = []
        by_uuid: dict[str, dict[str, Any]] = {}
        for fields in csv.reader(raw.splitlines()):
            if len(fields) < 13:
                continue
            index = self._number(fields[0], integer=True)
            memory_used = self._number(fields[8])
            memory_total = self._number(fields[9])
            item = {
                "index": index,
                "name": fields[1].strip(),
                "pci_bus_id": fields[3].strip(),
                "driver_version": fields[4].strip(),
                "temperature_c": self._number(fields[5]),
                "utilization_percent": self._number(fields[6]),
                "memory_utilization_percent": self._number(fields[7]),
                "memory_used_bytes": int(memory_used * 1024 * 1024) if memory_used is not None else None,
                "memory_total_bytes": int(memory_total * 1024 * 1024) if memory_total is not None else None,
                "power_watts": self._number(fields[10]),
                "power_limit_watts": self._number(fields[11]),
                "pstate": fields[12].strip(),
                "process_count": 0,
                "process_memory_bytes": 0,
            }
            gpu_rows.append(item)
            by_uuid[fields[2].strip()] = item
        process_error = ""
        try:
            processes = self._run_command(
                [
                    executable,
                    "--query-compute-apps=gpu_uuid,pid,process_name,used_gpu_memory",
                    "--format=csv,noheader,nounits",
                ]
            )
            for fields in csv.reader(processes.splitlines()):
                if len(fields) < 4 or fields[0].strip() not in by_uuid:
                    continue
                item = by_uuid[fields[0].strip()]
                used = self._number(fields[3])
                item["process_count"] += 1
                if used is not None:
                    item["process_memory_bytes"] += int(used * 1024 * 1024)
        except (OSError, subprocess.SubprocessError) as error:
            process_error = str(error)
        return {
            "available": bool(gpu_rows),
            "error": "" if gpu_rows else "nvidia-smi 未返回 GPU",
            "process_error": process_error,
            "gpus": sorted(gpu_rows, key=lambda item: item["index"] if item["index"] is not None else 9999),
        }

    def _cpu_model(self) -> str:
        try:
            for line in self._read_text("cpuinfo").splitlines():
                if line.lower().startswith(("model name", "hardware")) and ":" in line:
                    return line.split(":", 1)[1].strip()
        except (FileNotFoundError, PermissionError, OSError):
            pass
        return platform.processor() or "unknown"

    def _collect(self, sampled_at: float) -> dict[str, Any]:
        errors: dict[str, str] = {}
        uptime_seconds = 0.0
        load = [0.0, 0.0, 0.0]
        try:
            uptime_seconds = float(self._read_text("uptime").split()[0])
            load = [float(value) for value in self._read_text("loadavg").split()[:3]]
        except (FileNotFoundError, PermissionError, OSError, ValueError) as error:
            errors["system"] = str(error)
        try:
            cpu, total_cpu_delta = self._cpu()
        except (FileNotFoundError, PermissionError, OSError, StopIteration, ValueError) as error:
            cpu, total_cpu_delta = {"usage_percent": None, "logical_cpus": os.cpu_count() or 1}, 0
            errors["cpu"] = str(error)
        try:
            memory = self._memory()
        except (FileNotFoundError, PermissionError, OSError, ValueError) as error:
            memory = {"total_bytes": 0, "used_bytes": 0, "used_percent": None}
            errors["memory"] = str(error)
        try:
            network = self._network(sampled_at)
        except (FileNotFoundError, PermissionError, OSError, ValueError) as error:
            network = {"interfaces": [], "rx_bytes_per_second": None, "tx_bytes_per_second": None}
            errors["network"] = str(error)
        try:
            process_summary, processes = self._processes(
                total_cpu_delta, int(memory.get("total_bytes") or 0)
            )
        except (FileNotFoundError, PermissionError, OSError, ValueError) as error:
            process_summary, processes = {"total": 0, "returned": 0}, []
            errors["processes"] = str(error)
        try:
            disks = self._disks()
        except (FileNotFoundError, PermissionError, OSError, ValueError) as error:
            disks = []
            errors["disks"] = str(error)
        gpus = self._gpus()
        if gpus.get("error"):
            errors["gpu"] = str(gpus["error"])
        return {
            "sampled_at": int(time.time()),
            "sample_interval_seconds": 2,
            "host": {
                "hostname": socket.gethostname(),
                "kernel": platform.release(),
                "cpu_model": self._cpu_model(),
                "logical_cpus": os.cpu_count() or 1,
                "uptime_seconds": round(uptime_seconds, 1),
                "booted_at": int(time.time() - uptime_seconds) if uptime_seconds else None,
                "load_average": load,
            },
            "cpu": cpu,
            "memory": memory,
            "network": network,
            "disks": disks,
            "gpu": gpus,
            "process_summary": process_summary,
            "processes": processes,
            "errors": errors,
        }

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            sampled_at = self.monotonic()
            if self.cached is not None and sampled_at - self.cached_at < self.cache_seconds:
                return self.cached
            self.cached = self._collect(sampled_at)
            self.cached_at = sampled_at
            return self.cached
