from pathlib import Path
from unittest.mock import Mock, patch

from cloudhelm_agent.config import AgentSettings
from cloudhelm_agent.docker_runtime import DockerRuntime
from cloudhelm_agent.system_monitor import SystemMonitor


def _network_payload(received: int, transmitted: int) -> str:
    return f"""
Inter-|   Receive                                                |  Transmit
 face |bytes packets errs drop fifo frame compressed multicast|bytes packets errs drop fifo colls carrier compressed
    lo: 999 0 0 0 0 0 0 0 999 0 0 0 0 0 0 0
ens65f0np0: {received} 1 0 0 0 0 0 0 {transmitted} 1 0 0 0 0 0 0
ens65f1np1: 3000 1 0 0 0 0 0 0 4000 1 0 0 0 0 0 0
  eth0: 9000 1 0 0 0 0 0 0 8000 1 0 0 0 0 0 0
docker0: 500 1 0 0 0 0 0 0 700 1 0 0 0 0 0 0
"""


def _address_payload() -> list[dict]:
    return [
        {
            "ifname": "ens65f0np0",
            "flags": ["BROADCAST", "UP", "LOWER_UP"],
            "addr_info": [
                {
                    "family": "inet",
                    "local": "192.0.2.10",
                    "prefixlen": 24,
                    "scope": "global",
                },
                {
                    "family": "inet6",
                    "local": "fe80::1",
                    "prefixlen": 64,
                    "scope": "link",
                },
            ],
        },
        {
            "ifname": "ens65f1np1",
            "flags": ["BROADCAST", "UP", "LOWER_UP"],
            "addr_info": [
                {
                    "family": "inet",
                    "local": "198.51.100.10",
                    "prefixlen": 24,
                    "scope": "global",
                }
            ],
        },
        {
            "ifname": "ens66f0",
            "flags": ["BROADCAST", "UP", "LOWER_UP"],
            "addr_info": [],
        },
        {
            "ifname": "eth0",
            "flags": ["BROADCAST", "UP", "LOWER_UP"],
            "linkinfo": {"info_kind": "veth"},
            "addr_info": [
                {
                    "family": "inet",
                    "local": "172.18.0.2",
                    "prefixlen": 16,
                    "scope": "global",
                }
            ],
        },
    ]


def _host_files(root: Path) -> tuple[Path, Path, Path, Path, Path]:
    network = root / "network-dev"
    cpu = root / "proc-stat"
    memory = root / "meminfo"
    load = root / "loadavg"
    uptime = root / "uptime"
    network.write_text(_network_payload(1000, 2000))
    cpu.write_text("cpu  100 0 100 800 0 0 0 0 0 0\n")
    memory.write_text(
        "MemTotal: 1000000 kB\nMemAvailable: 600000 kB\n"
        "SwapTotal: 200000 kB\nSwapFree: 150000 kB\n"
    )
    load.write_text("0.10 0.20 0.30 1/100 1\n")
    uptime.write_text("3600.0 1000.0\n")
    return network, cpu, memory, load, uptime


def test_system_monitor_reports_host_disk_and_network_rates(tmp_path: Path):
    network_path, cpu_path, memory_path, load_path, uptime_path = _host_files(
        tmp_path
    )
    ticks = iter([10.0, 20.0])
    monitor = SystemMonitor(
        tmp_path,
        network_path,
        cpu_path,
        memory_path,
        load_path,
        uptime_path,
        address_query=_address_payload,
        clock=lambda: next(ticks),
    )

    first = monitor.snapshot()
    assert first.status == "ok"
    assert first.metrics["disk_total_bytes"] > 0
    assert first.metrics["cpu_count"] == 1
    assert first.metrics["network_interfaces"] == ["ens65f0np0", "ens65f1np1"]
    assert first.metrics["network_rx_bytes"] == 4000
    assert first.metrics["network_tx_bytes"] == 6000
    assert first.metrics["network_interface_metrics"] == [
        {
            "name": "ens65f0np0",
            "addresses": ["192.0.2.10/24"],
            "rx_bytes": 1000,
            "tx_bytes": 2000,
            "rx_bps": 0.0,
            "tx_bps": 0.0,
        },
        {
            "name": "ens65f1np1",
            "addresses": ["198.51.100.10/24"],
            "rx_bytes": 3000,
            "tx_bytes": 4000,
            "rx_bps": 0.0,
            "tx_bps": 0.0,
        },
    ]
    assert first.metrics["network_rx_bps"] == 0
    assert first.metrics["memory_percent"] == 40
    assert first.metrics["swap_used_bytes"] == 50000 * 1024
    assert first.metrics["load_15"] == 0.3
    assert first.metrics["uptime_seconds"] == 3600

    network_path.write_text(_network_payload(6000, 5000))
    cpu_path.write_text("cpu  150 0 150 900 0 0 0 0 0 0\n")
    second = monitor.snapshot()
    assert second.metrics["network_rx_bps"] == 500
    assert second.metrics["network_tx_bps"] == 300
    assert second.metrics["network_interface_metrics"][0]["rx_bps"] == 500
    assert second.metrics["cpu_percent"] == 50


def test_system_monitor_prefers_ip_bearing_l3_interface_and_allowlist():
    payload = _address_payload() + [
        {
            "ifname": "bond0",
            "flags": ["UP", "LOWER_UP"],
            "linkinfo": {"info_kind": "bond"},
            "addr_info": [
                {
                    "family": "inet",
                    "local": "10.0.0.10",
                    "prefixlen": 24,
                    "scope": "global",
                }
            ],
        }
    ]
    automatic = SystemMonitor._select_interfaces(payload)
    assert automatic == {
        "bond0": ["10.0.0.10/24"],
        "ens65f0np0": ["192.0.2.10/24"],
        "ens65f1np1": ["198.51.100.10/24"],
    }
    explicit = SystemMonitor._select_interfaces(payload, ("eth0",))
    assert explicit == {"eth0": ["172.18.0.2/16"]}


def test_agent_network_interface_allowlist_is_normalized():
    settings = AgentSettings(
        server_url="https://ops.example.com",
        network_interfaces=" ens65f0np0,ens65f1np1,ens65f0np0 ",
    )
    assert settings.network_interface_allowlist == (
        "ens65f0np0",
        "ens65f1np1",
    )


@patch("cloudhelm_agent.system_monitor.subprocess.run")
def test_system_monitor_uses_fixed_ip_address_query(run):
    run.return_value = Mock(
        returncode=0,
        stdout='[{"ifname":"ens65f0np0","addr_info":[]}]',
        stderr="",
    )
    assert SystemMonitor._query_interface_addresses()[0]["ifname"] == "ens65f0np0"
    run.assert_called_once_with(
        ["ip", "-details", "-json", "address", "show", "up"],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )


def test_system_monitor_requires_host_mount(tmp_path: Path):
    result = SystemMonitor(tmp_path / "missing", tmp_path / "missing-net").snapshot()
    assert result.status == "unavailable"
    assert "agent.compose.yml" in (result.error or "")


@patch("cloudhelm_agent.docker_runtime.docker.from_env")
def test_docker_inventory_reports_network_rates_and_disk_sizes(from_env, monkeypatch):
    client = from_env.return_value
    container = Mock()
    container.id = "1234567890abcdef"
    container.name = "api"
    container.status = "running"
    container.image.tags = ["example/api:1"]
    container.attrs = {
        "Config": {"Image": "example/api:1", "Labels": {}},
        "State": {"Running": True, "Status": "running"},
        "RestartCount": 3,
        "HostConfig": {},
        "NetworkSettings": {"Ports": {}},
    }
    container.stats.side_effect = [
        {
            "networks": {"eth0": {"rx_bytes": 1000, "tx_bytes": 2000}},
            "cpu_stats": {},
            "precpu_stats": {},
            "memory_stats": {},
            "blkio_stats": {"io_service_bytes_recursive": [{"op": "Read", "value": 1000}, {"op": "Write", "value": 2000}]},
            "pids_stats": {"current": 4},
        },
        {
            "networks": {"eth0": {"rx_bytes": 6000, "tx_bytes": 5000}},
            "cpu_stats": {},
            "precpu_stats": {},
            "memory_stats": {},
            "blkio_stats": {"io_service_bytes_recursive": [{"op": "Read", "value": 6000}, {"op": "Write", "value": 5000}]},
            "pids_stats": {"current": 5},
        },
    ]
    client.containers.list.return_value = [container]
    client.df.return_value = {
        "Containers": [
            {"Id": container.id, "SizeRw": 1234, "SizeRootFs": 5678}
        ]
    }
    ticks = iter([0.0, 0.0, 10.0, 10.0])
    monkeypatch.setattr(
        "cloudhelm_agent.docker_runtime.time.monotonic", lambda: next(ticks)
    )
    runtime = DockerRuntime(disk_query_seconds=60)

    first = runtime.inventory()[0]
    second = runtime.inventory()[0]

    assert first["writable_layer_bytes"] == 1234
    assert first["rootfs_bytes"] == 5678
    assert first["network_rx_bps"] == 0
    assert second["network_rx_bps"] == 500
    assert second["network_tx_bps"] == 300
    assert second["block_read_bps"] == 500
    assert second["block_write_bps"] == 300
    assert second["pids"] == 5
    assert second["restart_count"] == 3
    assert client.df.call_count == 1


@patch("cloudhelm_agent.docker_runtime.docker.from_env")
def test_container_disk_growth_uses_real_disk_query_interval(from_env, monkeypatch):
    client = from_env.return_value
    container = Mock()
    container.id = "diskgrowth123456"
    container.name = "writer"
    container.status = "exited"
    container.image.tags = ["example/writer:1"]
    container.attrs = {
        "Config": {"Image": "example/writer:1", "Labels": {}},
        "State": {"Running": False, "Status": "exited", "ExitCode": 0},
        "HostConfig": {},
        "NetworkSettings": {"Ports": {}},
    }
    client.containers.list.return_value = [container]
    client.df.side_effect = [
        {"Containers": [{"Id": container.id, "SizeRw": 1024, "SizeRootFs": 2048}]},
        {
            "Containers": [
                {
                    "Id": container.id,
                    "SizeRw": 300 * 1024 * 1024 + 1024,
                    "SizeRootFs": 301 * 1024 * 1024,
                }
            ]
        },
    ]
    ticks = iter([0.0, 300.0])
    monkeypatch.setattr(
        "cloudhelm_agent.docker_runtime.time.monotonic", lambda: next(ticks)
    )
    runtime = DockerRuntime(disk_query_seconds=300)

    first = runtime.inventory()[0]
    second = runtime.inventory()[0]

    assert first["writable_layer_growth_mibps"] is None
    assert second["writable_layer_growth_mibps"] == 1.0
