from typing import Any

import docker
from docker.errors import DockerException, NotFound


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
            image = ""
            if container.image.tags:
                image = container.image.tags[0]
            elif container.image.id:
                image = container.image.id.split(":")[-1][:12]
            health = (state.get("Health") or {}).get("Status")
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
                    "labels": {
                        key: str(value)
                        for key, value in labels.items()
                        if key.startswith(("com.docker.compose.", "cloudhelm."))
                    },
                }
            )
        return result

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

    def execute(self, docker_id: str, action: str, arguments: dict[str, Any]) -> str:
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
        raise RuntimeError(f"unsupported action: {action}")
