import re
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class GpuProbeResult:
    status: str
    gpus: list[dict[str, Any]]
    error: str | None = None


def _text(element: ET.Element, path: str) -> str | None:
    value = element.findtext(path)
    if value is None:
        return None
    value = value.strip()
    if not value or value.upper() in {"N/A", "[NOT SUPPORTED]", "NOT SUPPORTED"}:
        return None
    return value


def _number(value: str | None) -> float | None:
    if value is None:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", value.replace(",", ""))
    return float(match.group(0)) if match else None


def _integer(value: str | None) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def parse_nvidia_smi_xml(payload: str) -> list[dict[str, Any]]:
    root = ET.fromstring(payload)
    driver_version = _text(root, "driver_version") or "unknown"
    cuda_version = _text(root, "cuda_version")
    result: list[dict[str, Any]] = []
    for fallback_index, gpu in enumerate(root.findall("gpu")):
        index = _integer(_text(gpu, "minor_number"))
        result.append(
            {
                "index": fallback_index if index is None else index,
                "uuid": _text(gpu, "uuid") or f"gpu-{fallback_index}",
                "name": _text(gpu, "product_name") or "NVIDIA GPU",
                "driver_version": driver_version,
                "cuda_version": cuda_version,
                "utilization_gpu": _number(_text(gpu, "utilization/gpu_util")),
                "utilization_memory": _number(_text(gpu, "utilization/memory_util")),
                "memory_used_mib": _integer(_text(gpu, "fb_memory_usage/used")),
                "memory_total_mib": _integer(_text(gpu, "fb_memory_usage/total")),
                "temperature_c": _number(_text(gpu, "temperature/gpu_temp")),
                "power_draw_w": _number(
                    _text(gpu, "gpu_power_readings/instant_power_draw")
                    or _text(gpu, "gpu_power_readings/average_power_draw")
                    or _text(gpu, "gpu_power_readings/power_draw")
                    or _text(gpu, "power_readings/instant_power_draw")
                    or _text(gpu, "power_readings/average_power_draw")
                    or _text(gpu, "power_readings/power_draw")
                ),
                "power_limit_w": _number(
                    _text(gpu, "gpu_power_readings/current_power_limit")
                    or _text(gpu, "gpu_power_readings/power_limit")
                    or _text(gpu, "power_readings/current_power_limit")
                    or _text(gpu, "power_readings/power_limit")
                ),
                "fan_speed_percent": _number(_text(gpu, "fan_speed")),
                "mig_mode": _text(gpu, "mig_mode/current_mig"),
            }
        )
    return result


class GpuMonitor:
    def __init__(
        self,
        enabled: bool = True,
        executable: Path = Path("/usr/bin/nvidia-smi"),
        timeout_seconds: float = 5.0,
        max_output_bytes: int = 4 * 1024 * 1024,
    ):
        self.enabled = enabled
        self.executable = executable
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes

    def snapshot(self) -> GpuProbeResult:
        if not self.enabled:
            return GpuProbeResult(status="disabled", gpus=[])
        if not self.executable.is_file():
            return GpuProbeResult(
                status="unavailable",
                gpus=[],
                error=f"{self.executable} is not available in the Agent container",
            )
        try:
            completed = subprocess.run(
                [str(self.executable), "-q", "-x"],
                check=False,
                capture_output=True,
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return GpuProbeResult(
                status="error", gpus=[], error=f"nvidia-smi failed: {exc}"
            )
        if completed.returncode != 0:
            error = completed.stderr.decode("utf-8", errors="replace").strip()
            return GpuProbeResult(
                status="unavailable",
                gpus=[],
                error=(error or "nvidia-smi returned a non-zero status")[:500],
            )
        if len(completed.stdout) > self.max_output_bytes:
            return GpuProbeResult(
                status="error", gpus=[], error="nvidia-smi output exceeded limit"
            )
        try:
            gpus = parse_nvidia_smi_xml(
                completed.stdout.decode("utf-8", errors="strict")
            )
        except (UnicodeDecodeError, ET.ParseError, ValueError) as exc:
            return GpuProbeResult(
                status="error", gpus=[], error=f"invalid nvidia-smi XML: {exc}"
            )
        return GpuProbeResult(status="ok", gpus=gpus)
