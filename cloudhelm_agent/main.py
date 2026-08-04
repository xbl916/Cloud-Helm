import json
import logging
import platform
import signal
import socket
import time
import uuid
from threading import Event

import httpx

from cloudhelm_agent import __version__
from cloudhelm_agent.config import AgentSettings
from cloudhelm_agent.docker_runtime import DockerRuntime
from cloudhelm_agent.gpu_monitor import GpuMonitor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("cloudhelm-agent")


class Agent:
    def __init__(self, settings: AgentSettings):
        self.settings = settings
        self.runtime = DockerRuntime(settings.max_containers)
        self.gpu_monitor = GpuMonitor(
            enabled=settings.gpu_monitoring_enabled,
            executable=settings.nvidia_smi_path,
            timeout_seconds=settings.gpu_query_timeout_seconds,
            max_output_bytes=settings.gpu_max_output_bytes,
        )
        self.stop_event = Event()
        self.state = self._load_state()
        self.client = httpx.Client(
            base_url=settings.api_url,
            timeout=settings.request_timeout_seconds,
            verify=settings.verify_tls,
            headers={"User-Agent": f"cloudhelm-agent/{__version__}"},
        )

    def _load_state(self) -> dict[str, str]:
        path = self.settings.state_file
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if (
                    data.get("agent_key")
                    and data.get("node_id")
                    and data.get("node_token")
                ):
                    return data
            except (OSError, json.JSONDecodeError):
                logger.warning("Ignoring invalid agent state file: %s", path)
        return {"agent_key": str(uuid.uuid4())}

    def _save_state(self) -> None:
        path = self.settings.state_file
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)

    def _headers(self) -> dict[str, str]:
        return {
            "X-Node-Id": self.state["node_id"],
            "X-Agent-Token": self.state["node_token"],
        }

    def enroll(self) -> None:
        if self.state.get("node_id") and self.state.get("node_token"):
            return
        if not self.settings.enrollment_token:
            raise RuntimeError(
                "CLOUDHELM_AGENT_ENROLLMENT_TOKEN is required for first enrollment"
            )
        response = self.client.post(
            "/agent/enroll",
            json={
                "enrollment_token": self.settings.enrollment_token,
                "agent_key": self.state["agent_key"],
                "name": self.settings.name,
                "hostname": socket.gethostname(),
                "environment": self.settings.environment,
                "agent_version": __version__,
            },
        )
        response.raise_for_status()
        self.state.update(response.json())
        self._save_state()
        logger.info("Enrolled node %s as %s", self.settings.name, self.state["node_id"])

    def report(self) -> None:
        runtime_info = self.runtime.info()
        gpu = self.gpu_monitor.snapshot()
        payload = {
            "hostname": socket.gethostname(),
            "agent_version": __version__,
            "docker_version": runtime_info["docker_version"],
            "os": runtime_info.get("os") or platform.platform(),
            "gpu_status": gpu.status,
            "gpu_error": gpu.error,
            "gpus": gpu.gpus,
            "containers": self.runtime.inventory(),
        }
        response = self.client.post(
            "/agent/heartbeat", headers=self._headers(), json=payload
        )
        response.raise_for_status()
        logger.info("Reported %d containers", len(payload["containers"]))
        if gpu.status == "ok":
            logger.info("Reported %d NVIDIA GPUs", len(gpu.gpus))
        elif gpu.status == "error":
            logger.warning("GPU monitoring failed: %s", gpu.error)

    def poll_task(self) -> None:
        response = self.client.get("/agent/tasks/next", headers=self._headers())
        if response.status_code == 204:
            return
        response.raise_for_status()
        task = response.json()
        logger.info("Executing task %s: %s", task["id"], task["action"])
        success = True
        result = error = None
        try:
            result = self.runtime.execute(
                task["docker_id"], task["action"], task.get("arguments") or {}
            )
        except Exception as exc:  # Docker SDK errors vary by daemon version
            success = False
            error = f"{type(exc).__name__}: {exc}"
            logger.exception("Task %s failed", task["id"])
        response = self.client.post(
            f"/agent/tasks/{task['id']}/result",
            headers=self._headers(),
            json={"success": success, "result": result, "error": error},
        )
        response.raise_for_status()

    def run(self) -> None:
        self.runtime.ping()
        self.enroll()
        next_report = 0.0
        backoff = 1.0
        while not self.stop_event.is_set():
            try:
                now = time.monotonic()
                if now >= next_report:
                    self.report()
                    next_report = now + self.settings.report_seconds
                self.poll_task()
                backoff = 1.0
                self.stop_event.wait(self.settings.poll_seconds)
            except (httpx.HTTPError, OSError) as exc:
                logger.warning(
                    "Server communication failed: %s; retrying in %.0fs", exc, backoff
                )
                self.stop_event.wait(backoff)
                backoff = min(backoff * 2, 30.0)

    def close(self) -> None:
        self.stop_event.set()
        self.client.close()


def main() -> None:
    settings = AgentSettings()  # type: ignore[call-arg]
    agent = Agent(settings)
    signal.signal(signal.SIGTERM, lambda *_: agent.stop_event.set())
    signal.signal(signal.SIGINT, lambda *_: agent.stop_event.set())
    try:
        agent.run()
    finally:
        agent.close()


if __name__ == "__main__":
    main()
