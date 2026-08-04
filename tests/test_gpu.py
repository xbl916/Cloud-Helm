import subprocess
from pathlib import Path

from cloudhelm_agent.docker_runtime import DockerRuntime
from cloudhelm_agent.gpu_monitor import GpuMonitor, parse_nvidia_smi_xml

NVIDIA_SMI_XML = """<?xml version="1.0" ?>
<nvidia_smi_log>
  <driver_version>570.124.06</driver_version>
  <cuda_version>12.8</cuda_version>
  <gpu>
    <product_name>NVIDIA RTX 6000 Ada Generation</product_name>
    <uuid>GPU-12345678</uuid>
    <minor_number>0</minor_number>
    <fb_memory_usage><total>49140 MiB</total><used>2048 MiB</used></fb_memory_usage>
    <utilization><gpu_util>37 %</gpu_util><memory_util>12 %</memory_util></utilization>
    <temperature><gpu_temp>54 C</gpu_temp></temperature>
    <fan_speed>30 %</fan_speed>
    <gpu_power_readings>
      <instant_power_draw>112.45 W</instant_power_draw>
      <current_power_limit>300.00 W</current_power_limit>
    </gpu_power_readings>
    <mig_mode><current_mig>Disabled</current_mig></mig_mode>
  </gpu>
</nvidia_smi_log>
"""


def test_parse_nvidia_smi_xml_collects_whitelisted_metrics():
    assert parse_nvidia_smi_xml(NVIDIA_SMI_XML) == [
        {
            "index": 0,
            "uuid": "GPU-12345678",
            "name": "NVIDIA RTX 6000 Ada Generation",
            "driver_version": "570.124.06",
            "cuda_version": "12.8",
            "utilization_gpu": 37.0,
            "utilization_memory": 12.0,
            "memory_used_mib": 2048,
            "memory_total_mib": 49140,
            "temperature_c": 54.0,
            "power_draw_w": 112.45,
            "power_limit_w": 300.0,
            "fan_speed_percent": 30.0,
            "mig_mode": "Disabled",
        }
    ]


def test_gpu_monitor_uses_fixed_command(monkeypatch, tmp_path):
    executable = tmp_path / "nvidia-smi"
    executable.touch()
    observed = []

    def fake_run(command, **kwargs):
        observed.append((command, kwargs))
        return subprocess.CompletedProcess(
            command, 0, stdout=NVIDIA_SMI_XML.encode(), stderr=b""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = GpuMonitor(executable=executable, timeout_seconds=3).snapshot()

    assert result.status == "ok"
    assert result.gpus[0]["uuid"] == "GPU-12345678"
    assert observed == [
        (
            [str(executable), "-q", "-x"],
            {"check": False, "capture_output": True, "timeout": 3},
        )
    ]


def test_gpu_monitor_reports_missing_binary():
    result = GpuMonitor(executable=Path("/definitely/not/nvidia-smi")).snapshot()
    assert result.status == "unavailable"
    assert result.gpus == []


def test_docker_gpu_allocation_from_device_requests():
    devices, all_gpus = DockerRuntime._gpu_allocation(
        {
            "HostConfig": {
                "DeviceRequests": [
                    {
                        "Driver": "nvidia",
                        "Count": 0,
                        "DeviceIDs": ["GPU-b", "0"],
                        "Capabilities": [["gpu"]],
                    }
                ]
            }
        }
    )
    assert devices == ["0", "GPU-b"]
    assert all_gpus is False

    devices, all_gpus = DockerRuntime._gpu_allocation(
        {
            "HostConfig": {
                "DeviceRequests": [
                    {
                        "Driver": "nvidia",
                        "Count": -1,
                        "Capabilities": [["gpu"]],
                    }
                ]
            }
        }
    )
    assert devices == []
    assert all_gpus is True


def test_docker_gpu_allocation_from_legacy_nvidia_runtime():
    devices, all_gpus = DockerRuntime._gpu_allocation(
        {
            "HostConfig": {"Runtime": "nvidia"},
            "Config": {"Env": ["NVIDIA_VISIBLE_DEVICES=1,GPU-c"]},
        }
    )
    assert devices == ["1", "GPU-c"]
    assert all_gpus is False
