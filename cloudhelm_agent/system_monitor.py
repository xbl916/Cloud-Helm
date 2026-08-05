import ipaddress
import json
import os
import shutil
import subprocess
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
    _ignored_interface_kinds = frozenset(
        {
            "dummy",
            "geneve",
            "ipvlan",
            "macvlan",
            "tun",
            "veth",
            "vxlan",
            "wireguard",
        }
    )

    def __init__(
        self,
        host_root: Path = Path("/host/rootfs-marker"),
        network_stats_path: Path = Path("/proc/net/dev"),
        cpu_stats_path: Path = Path("/host/proc-stat"),
        memory_stats_path: Path = Path("/host/meminfo"),
        load_stats_path: Path = Path("/host/loadavg"),
        uptime_stats_path: Path = Path("/host/uptime"),
        network_interfaces: tuple[str, ...] = (),
        address_query: Callable[[], list[dict[str, Any]]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.host_root = host_root
        self.network_stats_path = network_stats_path
        self.cpu_stats_path = cpu_stats_path
        self.memory_stats_path = memory_stats_path
        self.load_stats_path = load_stats_path
        self.uptime_stats_path = uptime_stats_path
        self.network_interfaces = network_interfaces
        self.address_query = address_query or self._query_interface_addresses
        self.clock = clock
        self._previous_network: tuple[
            float, dict[str, tuple[int, int]]
        ] | None = None
        self._previous_cpu: tuple[int, int] | None = None

    @staticmethod
    def _query_interface_addresses() -> list[dict[str, Any]]:
        try:
            completed = subprocess.run(
                ["ip", "-details", "-json", "address", "show", "up"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise OSError(f"cannot query host interface addresses: {exc}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or "ip address failed").strip()[:300]
            raise OSError(f"cannot query host interface addresses: {detail}")
        if len(completed.stdout.encode("utf-8")) > 1048576:
            raise OSError("host interface address output exceeds 1 MiB")
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise OSError("invalid host interface address output") from exc
        if not isinstance(result, list):
            raise OSError("invalid host interface address payload")
        return [item for item in result if isinstance(item, dict)]

    @classmethod
    def _select_interfaces(
        cls,
        payload: list[dict[str, Any]],
        allowlist: tuple[str, ...] = (),
    ) -> dict[str, list[str]]:
        allowed = set(allowlist)
        selected: dict[str, list[str]] = {}
        for item in payload:
            name = str(item.get("ifname") or "").strip()
            if not name or (allowed and name not in allowed):
                continue
            flags = {str(value).upper() for value in item.get("flags") or []}
            if flags and "UP" not in flags:
                continue
            if not allowed and (
                name == "lo" or name.startswith(cls._ignored_interfaces[1:])
            ):
                continue
            link_info = item.get("linkinfo") or {}
            kind = str(link_info.get("info_kind") or "").lower()
            if not allowed and kind in cls._ignored_interface_kinds:
                continue
            addresses: list[str] = []
            for address in item.get("addr_info") or []:
                if not isinstance(address, dict):
                    continue
                family = str(address.get("family") or "")
                value = str(address.get("local") or "").strip()
                if family not in {"inet", "inet6"} or not value:
                    continue
                try:
                    parsed = ipaddress.ip_address(value)
                except ValueError:
                    continue
                if (
                    parsed.is_loopback
                    or parsed.is_link_local
                    or parsed.is_multicast
                    or parsed.is_unspecified
                ):
                    continue
                prefix = address.get("prefixlen")
                addresses.append(f"{value}/{prefix}" if prefix is not None else value)
            if addresses:
                selected[name] = addresses[:16]
        if allowlist:
            order = {name: index for index, name in enumerate(allowlist)}
            return dict(sorted(selected.items(), key=lambda item: order[item[0]]))
        return dict(sorted(selected.items()))

    @staticmethod
    def _network_counters(
        payload: str, selected: set[str]
    ) -> dict[str, tuple[int, int]]:
        counters: dict[str, tuple[int, int]] = {}
        for line in payload.splitlines():
            if ":" not in line:
                continue
            name, raw_values = line.split(":", 1)
            name = name.strip()
            if name not in selected:
                continue
            values = raw_values.split()
            if len(values) < 9:
                continue
            try:
                counters[name] = (
                    max(0, int(values[0])),
                    max(0, int(values[8])),
                )
            except ValueError:
                continue
        return counters

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

    def _network_metrics(
        self,
        now: float,
        selected: dict[str, list[str]],
        counters: dict[str, tuple[int, int]],
    ) -> tuple[int, int, float, float, list[dict[str, Any]]]:
        previous_time = None
        previous_counters: dict[str, tuple[int, int]] = {}
        if self._previous_network:
            previous_time, previous_counters = self._previous_network
        elapsed = now - previous_time if previous_time is not None else 0
        interfaces: list[dict[str, Any]] = []
        for name, addresses in selected.items():
            received, transmitted = counters.get(name, (0, 0))
            receive_rate = transmit_rate = 0.0
            if elapsed > 0 and name in previous_counters:
                previous_received, previous_transmitted = previous_counters[name]
                receive_rate = max(0, received - previous_received) / elapsed
                transmit_rate = max(0, transmitted - previous_transmitted) / elapsed
            interfaces.append(
                {
                    "name": name,
                    "addresses": addresses,
                    "rx_bytes": received,
                    "tx_bytes": transmitted,
                    "rx_bps": round(receive_rate, 2),
                    "tx_bps": round(transmit_rate, 2),
                }
            )
        self._previous_network = (now, counters)
        return (
            sum(item["rx_bytes"] for item in interfaces),
            sum(item["tx_bytes"] for item in interfaces),
            round(sum(item["rx_bps"] for item in interfaces), 2),
            round(sum(item["tx_bps"] for item in interfaces), 2),
            interfaces,
        )

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
            selected = self._select_interfaces(
                self.address_query(), self.network_interfaces
            )
            counters = self._network_counters(
                self.network_stats_path.read_text(encoding="utf-8"), set(selected)
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
            (
                received,
                transmitted,
                receive_rate,
                transmit_rate,
                interface_metrics,
            ) = self._network_metrics(now, selected, counters)
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
                    "network_rx_bps": receive_rate,
                    "network_tx_bps": transmit_rate,
                    "network_interfaces": list(selected),
                    "network_interface_metrics": interface_metrics,
                },
            )
        except (OSError, ValueError, IndexError) as exc:
            return SystemProbeResult(
                status="error",
                metrics={},
                error=f"host metrics query failed: {exc}"[:500],
            )
