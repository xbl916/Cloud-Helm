import os
import time
from copy import deepcopy
from typing import Any

import docker
from docker.errors import DockerException, NotFound

from cloudhelm.image_reference import validate_tag_change


class DockerRuntime:
    def __init__(self, max_containers: int = 500):
        self.client = docker.from_env()
        self.max_containers = max_containers

    def ping(self) -> None:
        self.client.ping()

    def info(self) -> dict[str, str]:
        version = self.client.version()
        info = self.client.info()
        return {
            "docker_version": str(version.get("Version", "unknown")),
            "os": f"{info.get('OperatingSystem', 'unknown')} / {info.get('Architecture', 'unknown')}",
        }

    def inventory(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        containers = self.client.containers.list(all=True)[: self.max_containers]
        for container in containers:
            attrs = container.attrs
            labels = attrs.get("Config", {}).get("Labels") or {}
            state = attrs.get("State") or {}
            cpu_percent = memory_usage = memory_limit = memory_percent = 0
            if state.get("Running"):
                try:
                    stats = container.stats(stream=False)
                    cpu_percent = self._cpu_percent(stats)
                    memory_usage, memory_limit, memory_percent = self._memory(stats)
                except DockerException:
                    pass
            image = str((attrs.get("Config") or {}).get("Image") or "")
            if not image and container.image.tags:
                image = container.image.tags[0]
            elif not image and container.image.id:
                image = container.image.id.split(":")[-1][:12]
            health = (state.get("Health") or {}).get("Status")
            gpu_devices, gpu_all = self._gpu_allocation(attrs)
            result.append(
                {
                    "docker_id": container.id,
                    "name": container.name,
                    "image": image,
                    "status": state.get("Status", container.status or "unknown"),
                    "health": health,
                    "compose_project": labels.get("com.docker.compose.project"),
                    "compose_service": labels.get("com.docker.compose.service"),
                    "cpu_percent": round(cpu_percent, 2),
                    "memory_usage": memory_usage,
                    "memory_limit": memory_limit,
                    "memory_percent": round(memory_percent, 2),
                    "started_at": state.get("StartedAt"),
                    "ports": attrs.get("NetworkSettings", {}).get("Ports") or {},
                    "gpu_devices": gpu_devices,
                    "gpu_all": gpu_all,
                    "labels": {
                        key: str(value)
                        for key, value in labels.items()
                        if key.startswith(("com.docker.compose.", "cloudhelm."))
                    },
                }
            )
        return result

    @staticmethod
    def _gpu_allocation(attrs: dict[str, Any]) -> tuple[list[str], bool]:
        host_config = attrs.get("HostConfig") or {}
        requests = host_config.get("DeviceRequests") or []
        devices: set[str] = set()
        all_gpus = False
        matched_request = False
        for request in requests:
            driver = str(request.get("Driver") or "").lower()
            capabilities = {
                str(capability).lower()
                for group in (request.get("Capabilities") or [])
                for capability in (group or [])
            }
            if driver not in {"", "nvidia"} or "gpu" not in capabilities:
                continue
            matched_request = True
            device_ids = request.get("DeviceIDs") or []
            if device_ids:
                devices.update(str(device) for device in device_ids)
            else:
                try:
                    count = int(request.get("Count") or 0)
                except (TypeError, ValueError):
                    count = 0
                if count == -1:
                    all_gpus = True
                elif count > 0:
                    devices.add(f"count:{count}")

        uses_nvidia_runtime = str(host_config.get("Runtime") or "").lower() == "nvidia"
        if not matched_request and uses_nvidia_runtime:
            environment = attrs.get("Config", {}).get("Env") or []
            visible = next(
                (
                    item.split("=", 1)[1]
                    for item in environment
                    if item.startswith("NVIDIA_VISIBLE_DEVICES=")
                ),
                "",
            ).strip()
            if visible.lower() == "all":
                all_gpus = True
            elif visible.lower() not in {"", "none", "void"}:
                devices.update(
                    item.strip() for item in visible.split(",") if item.strip()
                )
        return sorted(devices), all_gpus

    @staticmethod
    def _cpu_percent(stats: dict[str, Any]) -> float:
        cpu = stats.get("cpu_stats") or {}
        previous = stats.get("precpu_stats") or {}
        cpu_delta = (cpu.get("cpu_usage") or {}).get("total_usage", 0) - (
            previous.get("cpu_usage") or {}
        ).get("total_usage", 0)
        system_delta = cpu.get("system_cpu_usage", 0) - previous.get(
            "system_cpu_usage", 0
        )
        online = (
            cpu.get("online_cpus")
            or len((cpu.get("cpu_usage") or {}).get("percpu_usage") or [])
            or 1
        )
        if cpu_delta > 0 and system_delta > 0:
            return cpu_delta / system_delta * online * 100
        return 0.0

    @staticmethod
    def _memory(stats: dict[str, Any]) -> tuple[int, int, float]:
        memory = stats.get("memory_stats") or {}
        usage = int(memory.get("usage") or 0)
        cache = int((memory.get("stats") or {}).get("inactive_file") or 0)
        usage = max(0, usage - cache)
        limit = int(memory.get("limit") or 0)
        percent = usage / limit * 100 if limit else 0.0
        return usage, limit, percent

    @staticmethod
    def _replacement_config(
        attrs: dict[str, Any], target_image: str, image_id: str
    ) -> dict[str, Any]:
        config = deepcopy(attrs.get("Config") or {})
        host_config = deepcopy(attrs.get("HostConfig") or {})
        if host_config.get("AutoRemove"):
            raise RuntimeError("auto-remove containers cannot be safely replaced")

        config["Image"] = target_image
        labels = dict(config.get("Labels") or {})
        if "com.docker.compose.image" in labels:
            labels["com.docker.compose.image"] = image_id
        config["Labels"] = labels
        config["HostConfig"] = host_config

        # HostConfig.Binds normally contains named volumes. Explicitly reuse any
        # anonymous volume that Docker only exposes through the Mounts section.
        binds = list(host_config.get("Binds") or [])
        for mount in attrs.get("Mounts") or []:
            if mount.get("Type") != "volume" or not mount.get("Source"):
                continue
            destination = str(mount.get("Destination") or "")
            if not destination or any(
                f":{destination}:" in bind or bind.endswith(f":{destination}")
                for bind in binds
            ):
                continue
            mode = str(mount.get("Mode") or ("rw" if mount.get("RW") else "ro"))
            binds.append(f"{mount['Source']}:{destination}:{mode}")
        if binds:
            host_config["Binds"] = binds

        endpoints: dict[str, dict[str, Any]] = {}
        for network_name, settings in (
            (attrs.get("NetworkSettings") or {}).get("Networks") or {}
        ).items():
            ipam = settings.get("IPAMConfig") or {}
            if ipam.get("IPv4Address") or ipam.get("IPv6Address"):
                raise RuntimeError(
                    "containers with a static IP cannot be safely replaced"
                )
            endpoint = {
                key: deepcopy(settings[key])
                for key in (
                    "IPAMConfig",
                    "Links",
                    "Aliases",
                    "DriverOpts",
                    "GwPriority",
                )
                if settings.get(key) is not None
            }
            endpoints[str(network_name)] = endpoint
        if endpoints:
            config["NetworkingConfig"] = {"EndpointsConfig": endpoints}
        return config

    def _update_image(
        self, container: Any, expected_image: str, target_image: str
    ) -> dict[str, str]:
        container.reload()
        attrs = container.attrs
        actual_image = str((attrs.get("Config") or {}).get("Image") or "")
        try:
            expected_ref, target_ref = validate_tag_change(
                expected_image, target_image
            )
            actual_ref, _ = validate_tag_change(actual_image, target_image)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        if actual_ref.canonical != expected_ref.canonical:
            raise RuntimeError(
                "container image changed after the task was queued; refresh and retry"
            )

        own_hostname = os.environ.get("HOSTNAME", "")
        if len(own_hostname) >= 12 and container.id.startswith(own_hostname):
            raise RuntimeError("the Agent cannot replace its own container")

        pulled_image = self.client.images.pull(target_ref.canonical)
        create_config = self._replacement_config(
            attrs, target_ref.canonical, pulled_image.id
        )
        original_name = str(attrs.get("Name") or container.name).lstrip("/")
        backup_name = (
            f"{original_name[:180]}.cloudhelm-backup-"
            f"{container.id[:12]}-{time.monotonic_ns()}"
        )
        was_running = bool((attrs.get("State") or {}).get("Running"))
        replacement = None

        try:
            if was_running:
                container.stop(timeout=30)
            container.rename(backup_name)
            created = self.client.api.create_container_from_config(
                create_config, name=original_name
            )
            replacement = self.client.containers.get(created["Id"])
            if was_running:
                replacement.start()
                time.sleep(0.5)
                replacement.reload()
                state = replacement.attrs.get("State") or {}
                if not state.get("Running") and not state.get("Restarting"):
                    raise RuntimeError("replacement container exited during startup")
        except Exception:
            if replacement is not None:
                try:
                    replacement.remove(force=True, v=False)
                except DockerException:
                    pass
            try:
                container.rename(original_name)
                if was_running:
                    container.start()
            except DockerException:
                pass
            raise

        cleanup_warning = ""
        try:
            container.remove(force=True, v=False)
        except DockerException as exc:
            cleanup_warning = f"; old stopped container retained as {backup_name}: {exc}"
        return {
            "message": f"container image updated to {target_ref.canonical}{cleanup_warning}",
            "docker_id": replacement.id,
        }

    def execute(
        self, docker_id: str, action: str, arguments: dict[str, Any]
    ) -> str | dict[str, str]:
        try:
            container = self.client.containers.get(docker_id)
        except NotFound as exc:
            raise RuntimeError("container no longer exists") from exc

        if action == "logs":
            tail = min(max(int(arguments.get("tail", 200)), 10), 2000)
            output = container.logs(tail=tail, timestamps=True)
            return output.decode("utf-8", errors="replace")
        if action == "start":
            container.start()
            return "container started"
        if action == "stop":
            container.stop(timeout=15)
            return "container stopped"
        if action == "restart":
            container.restart(timeout=15)
            return "container restarted"
        if action == "update_image":
            return self._update_image(
                container,
                str(arguments.get("expected_image") or ""),
                str(arguments.get("target_image") or ""),
            )
        raise RuntimeError(f"unsupported action: {action}")
