import os
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SystemProbeResult:
    status: str
    metrics: dict[str, Any]
    error: str | None = None


class SystemMonitor:
    _ignored_interfaces = (
        "lo",
        "docker",
        "br-",
        "veth",
        "cni",
        "flannel",
        "cali",
        "virbr",
        "podman",
    )

    def __init__(
        self,
        host_root: Path = Path("/host/rootfs-marker"),
        network_stats_path: Path = Path("/host/network-dev"),
        cpu_stats_path: Path = Path("/host/proc-stat"),
        memory_stats_path: Path = Path("/host/meminfo"),
        load_stats_path: Path = Path("/host/loadavg"),
        uptime_stats_path: Path = Path("/host/uptime"),
        clock: Callable[[], float] = time.monotonic,
    ):
        self.host_root = host_root
        self.network_stats_path = network_stats_path
        self.cpu_stats_path = cpu_stats_path
        self.memory_stats_path = memory_stats_path
        self.load_stats_path = load_stats_path
        self.uptime_stats_path = uptime_stats_path
        self.clock = clock
        self._previous_network: tuple[float, int, int] | None = None
        self._previous_cpu: tuple[int, int] | None = None

    @classmethod
    def _network_counters(cls, payload: str) -> tuple[int, int, list[str]]:
        received = transmitted = 0
        interfaces: list[str] = []
        for line in payload.splitlines():
            if ":" not in line:
                continue
            name, raw_values = line.split(":", 1)
            name = name.strip()
            if not name or name == "lo" or name.startswith(cls._ignored_interfaces[1:]):
                continue
            values = raw_values.split()
            if len(values) < 9:
                continue
            try:
                received += max(0, int(values[0]))
                transmitted += max(0, int(values[8]))
            except ValueError:
                continue
            interfaces.append(name)
        return received, transmitted, sorted(interfaces)

    @staticmethod
    def _cpu_counters(payload: str) -> tuple[int, int]:
        first = payload.splitlines()[0].split()
        if not first or first[0] != "cpu" or len(first) < 5:
            raise ValueError("invalid /proc/stat CPU counters")
        values = [max(0, int(value)) for value in first[1:]]
        return sum(values), values[3] + (values[4] if len(values) > 4 else 0)

    @staticmethod
    def _memory(payload: str) -> dict[str, int | float]:
        values: dict[str, int] = {}
        for line in payload.splitlines():
            if ":" not in line:
                continue
            key, raw = line.split(":", 1)
            number = raw.strip().split()[0]
            values[key] = max(0, int(number)) * 1024
        total = values.get("MemTotal", 0)
        available = values.get("MemAvailable", values.get("MemFree", 0))
        used = max(0, total - available)
        swap_total = values.get("SwapTotal", 0)
        swap_used = max(0, swap_total - values.get("SwapFree", 0))
        return {
            "memory_total_bytes": total,
            "memory_used_bytes": used,
            "memory_available_bytes": available,
            "memory_percent": round(used / total * 100, 2) if total else 0.0,
            "swap_total_bytes": swap_total,
            "swap_used_bytes": swap_used,
        }

    def snapshot(self) -> SystemProbeResult:
        required = (
            self.host_root,
            self.network_stats_path,
            self.cpu_stats_path,
            self.memory_stats_path,
            self.load_stats_path,
            self.uptime_stats_path,
        )
        if not all(path.exists() for path in required):
            return SystemProbeResult(
                status="unavailable",
                metrics={},
                error="host metrics mount is unavailable; update agent.compose.yml and recreate the Agent",
            )
        try:
            disk = shutil.disk_usage(self.host_root)
            filesystem = os.statvfs(self.host_root)
            received, transmitted, interfaces = self._network_counters(
                self.network_stats_path.read_text(encoding="utf-8")
            )
            cpu_total, cpu_idle = self._cpu_counters(
                self.cpu_stats_path.read_text(encoding="utf-8")
            )
            memory = self._memory(
                self.memory_stats_path.read_text(encoding="utf-8")
            )
            load_values = self.load_stats_path.read_text(encoding="utf-8").split()
            load_1, load_5, load_15 = map(float, load_values[:3])
            uptime = float(
                self.uptime_stats_path.read_text(encoding="utf-8").split()[0]
            )
            now = self.clock()
            receive_rate = transmit_rate = 0.0
            if self._previous_network:
                previous_time, previous_received, previous_transmitted = (
                    self._previous_network
                )
                elapsed = now - previous_time
                if elapsed > 0:
                    receive_rate = max(0, received - previous_received) / elapsed
                    transmit_rate = max(0, transmitted - previous_transmitted) / elapsed
            self._previous_network = (now, received, transmitted)
            cpu_percent = 0.0
            if self._previous_cpu:
                previous_total, previous_idle = self._previous_cpu
                total_delta = cpu_total - previous_total
                idle_delta = cpu_idle - previous_idle
                if total_delta > 0:
                    cpu_percent = max(
                        0.0, min(100.0, (1 - idle_delta / total_delta) * 100)
                    )
            self._previous_cpu = (cpu_total, cpu_idle)
            inode_total = max(0, filesystem.f_files)
            inode_free = max(0, filesystem.f_ffree)
            return SystemProbeResult(
                status="ok",
                metrics={
                    "cpu_percent": round(cpu_percent, 2),
                    **memory,
                    "load_1": load_1,
                    "load_5": load_5,
                    "load_15": load_15,
                    "uptime_seconds": uptime,
                    "disk_total_bytes": disk.total,
                    "disk_used_bytes": disk.used,
                    "disk_free_bytes": disk.free,
                    "disk_inodes_total": inode_total,
                    "disk_inodes_used": max(0, inode_total - inode_free),
                    "disk_inodes_free": inode_free,
                    "network_rx_bytes": received,
                    "network_tx_bytes": transmitted,
                    "network_rx_bps": round(receive_rate, 2),
                    "network_tx_bps": round(transmit_rate, 2),
                    "network_interfaces": interfaces,
                },
            )
        except (OSError, ValueError, IndexError) as exc:
            return SystemProbeResult(
                status="error",
                metrics={},
                error=f"host metrics query failed: {exc}"[:500],
            )
