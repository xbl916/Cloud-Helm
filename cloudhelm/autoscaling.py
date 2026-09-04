import math
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import httpx
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from cloudhelm.alerts import evaluate_service_business_alerts
from cloudhelm.config import Settings
from cloudhelm.image_reference import parse_tagged_image
from cloudhelm.models import (
    BusinessMetricSample,
    Container,
    ScalingEvent,
    ScalingService,
    ServiceInstance,
)


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


class AcsKubernetesClient:
    """Small Kubernetes scale client for an Alibaba Cloud ACS cluster."""

    def __init__(self, settings: Settings):
        if not settings.acs_kubernetes_api_url:
            raise RuntimeError("未配置 CLOUDHELM_ACS_KUBERNETES_API_URL")
        token = settings.acs_kubernetes_bearer_token
        if settings.acs_kubernetes_bearer_token_file:
            token_file = Path(settings.acs_kubernetes_bearer_token_file)
            if not token_file.is_file():
                raise RuntimeError(f"ACS Kubernetes Token 文件不存在：{token_file}")
            token = token_file.read_text(encoding="utf-8").strip()
        if not token:
            raise RuntimeError("未配置 ACS Kubernetes Bearer Token 或 Token 文件")
        verify: bool | str = True
        if settings.acs_kubernetes_ca_file:
            ca_file = Path(settings.acs_kubernetes_ca_file)
            if not ca_file.is_file():
                raise RuntimeError(f"ACS Kubernetes CA 文件不存在：{ca_file}")
            verify = str(ca_file)
        self.client = httpx.Client(
            base_url=settings.acs_kubernetes_api_url.rstrip("/"),
            timeout=settings.acs_kubernetes_timeout_seconds,
            verify=verify,
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": "cloudhelm-autoscaler/1",
            },
        )

    @staticmethod
    def _resource(service: ScalingService) -> str:
        return "deployments" if service.workload_kind == "Deployment" else "statefulsets"

    def _scale_path(self, service: ScalingService) -> str:
        namespace = quote(service.namespace, safe="")
        name = quote(service.workload_name, safe="")
        return f"/apis/apps/v1/namespaces/{namespace}/{self._resource(service)}/{name}/scale"

    def get_replicas(self, service: ScalingService) -> int:
        response = self.client.get(self._scale_path(service))
        response.raise_for_status()
        payload = response.json()
        return int(payload.get("spec", {}).get("replicas", 0))

    def prefer_pod_for_removal(self, service: ScalingService, pod_name: str) -> None:
        namespace = quote(service.namespace, safe="")
        pod = quote(pod_name, safe="")
        response = self.client.patch(
            f"/api/v1/namespaces/{namespace}/pods/{pod}",
            headers={"Content-Type": "application/merge-patch+json"},
            json={
                "metadata": {
                    "annotations": {"controller.kubernetes.io/pod-deletion-cost": "-1000"}
                }
            },
        )
        response.raise_for_status()

    def set_replicas(
        self, service: ScalingService, expected: int, target: int
    ) -> int:
        response = self.client.patch(
            self._scale_path(service),
            headers={"Content-Type": "application/json-patch+json"},
            json=[
                {"op": "test", "path": "/spec/replicas", "value": expected},
                {"op": "replace", "path": "/spec/replicas", "value": target},
            ],
        )
        response.raise_for_status()
        return int(response.json().get("spec", {}).get("replicas", target))

    def _workload_path(self, service: ScalingService) -> str:
        namespace = quote(service.namespace, safe="")
        name = quote(service.workload_name, safe="")
        return f"/apis/apps/v1/namespaces/{namespace}/{self._resource(service)}/{name}"

    def get_workload(self, service: ScalingService) -> dict:
        response = self.client.get(self._workload_path(service))
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise TypeError("ACS 工作负载响应格式无效")
        return value

    @staticmethod
    def _workload_container(service: ScalingService, workload: dict) -> dict:
        containers = (
            workload.get("spec", {})
            .get("template", {})
            .get("spec", {})
            .get("containers", [])
        )
        if service.acs_container_name:
            match = next(
                (
                    item
                    for item in containers
                    if item.get("name") == service.acs_container_name
                ),
                None,
            )
            if not match:
                raise RuntimeError(
                    f"ACS 工作负载中不存在容器 {service.acs_container_name}"
                )
            return match
        if len(containers) != 1:
            raise RuntimeError("ACS 工作负载包含多个容器，必须配置目标容器名称")
        return containers[0]

    def set_workload_image(
        self, service: ScalingService, target_image: str
    ) -> str:
        workload = self.get_workload(service)
        container = self._workload_container(service, workload)
        current = parse_tagged_image(str(container.get("image") or ""))
        target = parse_tagged_image(target_image)
        if current.canonical_repository != target.canonical_repository:
            raise RuntimeError("ACS 新旧镜像必须属于同一镜像仓库")
        if current.canonical == target.canonical:
            return target.canonical
        response = self.client.patch(
            self._workload_path(service),
            headers={"Content-Type": "application/strategic-merge-patch+json"},
            json={
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {
                                "cloudhelm.io/released-at": datetime.now(UTC).isoformat()
                            }
                        },
                        "spec": {
                            "containers": [
                                {"name": container["name"], "image": target.canonical}
                            ]
                        },
                    }
                }
            },
        )
        response.raise_for_status()
        return target.canonical

    def workload_image_status(
        self, service: ScalingService, target_image: str
    ) -> tuple[bool, str, int]:
        workload = self.get_workload(service)
        container = self._workload_container(service, workload)
        current = parse_tagged_image(str(container.get("image") or ""))
        target = parse_tagged_image(target_image)
        spec = workload.get("spec", {})
        status = workload.get("status", {})
        replicas = int(spec.get("replicas") or 0)
        generation = int(workload.get("metadata", {}).get("generation") or 0)
        observed = int(status.get("observedGeneration") or 0)
        updated = int(status.get("updatedReplicas") or 0)
        ready = int(status.get("readyReplicas") or 0)
        available = int(status.get("availableReplicas") or ready)
        revisions_ready = True
        if service.workload_kind == "StatefulSet":
            revisions_ready = bool(status.get("currentRevision")) and (
                status.get("currentRevision") == status.get("updateRevision")
            )
        rollout_ready = bool(
            current.canonical == target.canonical
            and observed >= generation
            and (
                replicas == 0
                or (
                    updated >= replicas
                    and ready >= replicas
                    and available >= replicas
                    and revisions_ready
                )
            )
        )
        return rollout_ready, current.canonical, replicas

    @staticmethod
    def release_guard_name(service: ScalingService, release_id: str) -> str:
        suffix = f"-cloudhelm-release-{release_id[:8]}"
        prefix = service.workload_name[: 253 - len(suffix)].rstrip("-")
        return f"{prefix}{suffix}"

    def _deployment_path(self, service: ScalingService, name: str = "") -> str:
        namespace = quote(service.namespace, safe="")
        suffix = f"/{quote(name, safe='')}" if name else ""
        return f"/apis/apps/v1/namespaces/{namespace}/deployments{suffix}"

    def ensure_release_guard(
        self, service: ScalingService, release_id: str, image: str
    ) -> str:
        name = self.release_guard_name(service, release_id)
        existing = self.client.get(self._deployment_path(service, name))
        if existing.status_code == 200:
            return name
        if existing.status_code != 404:
            existing.raise_for_status()

        workload = self.get_workload(service)
        template = deepcopy(workload.get("spec", {}).get("template") or {})
        template_metadata = template.setdefault("metadata", {})
        template_labels = dict(template_metadata.get("labels") or {})
        template_annotations = dict(template_metadata.get("annotations") or {})
        guard_labels = {
            "cloudhelm.io/release-id": release_id,
            "cloudhelm.io/instance-pool": "acs_release",
        }
        template_labels.update(guard_labels)
        template_metadata.clear()
        template_metadata["labels"] = template_labels
        if template_annotations:
            template_metadata["annotations"] = template_annotations
        pod_spec = template.get("spec") or {}
        template["spec"] = pod_spec
        target = self._workload_container(service, {"spec": {"template": template}})
        current_image = parse_tagged_image(str(target.get("image") or ""))
        guard_image = parse_tagged_image(image)
        if current_image.canonical_repository != guard_image.canonical_repository:
            raise RuntimeError("升级保护实例与本地实例必须使用同一镜像仓库")
        target["image"] = guard_image.canonical
        env = [
            item
            for item in list(target.get("env") or [])
            if item.get("name")
            not in {"CLOUDHELM_INSTANCE_POOL", "CLOUDHELM_RELEASE_ID"}
        ]
        env.extend(
            [
                {"name": "CLOUDHELM_INSTANCE_POOL", "value": "acs_release"},
                {"name": "CLOUDHELM_RELEASE_ID", "value": release_id},
            ]
        )
        target["env"] = env
        response = self.client.post(
            self._deployment_path(service),
            json={
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": name, "labels": guard_labels},
                "spec": {
                    "replicas": 1,
                    "selector": {"matchLabels": guard_labels},
                    "strategy": {
                        "type": "RollingUpdate",
                        "rollingUpdate": {"maxUnavailable": 0, "maxSurge": 1},
                    },
                    "template": template,
                },
            },
        )
        response.raise_for_status()
        return name

    def release_guard_ready(
        self, service: ScalingService, name: str, image: str
    ) -> bool:
        response = self.client.get(self._deployment_path(service, name))
        response.raise_for_status()
        workload = response.json()
        container = self._workload_container(service, workload)
        current = parse_tagged_image(str(container.get("image") or ""))
        status = workload.get("status", {})
        generation = int(workload.get("metadata", {}).get("generation") or 0)
        return bool(
            current.canonical == parse_tagged_image(image).canonical
            and int(status.get("observedGeneration") or 0) >= generation
            and int(status.get("updatedReplicas") or 0) >= 1
            and int(status.get("readyReplicas") or 0) >= 1
            and int(status.get("availableReplicas") or 0) >= 1
        )

    def delete_release_guard(self, service: ScalingService, name: str) -> None:
        response = self.client.delete(
            self._deployment_path(service, name),
            params={"propagationPolicy": "Foreground"},
        )
        if response.status_code not in {200, 202, 404}:
            response.raise_for_status()

    def close(self) -> None:
        self.client.close()


def _instance_capacity(service: ScalingService, instance: ServiceInstance) -> int:
    return instance.capacity_limit or service.capacity_per_replica


def _container_is_healthy(db: Session, instance: ServiceInstance) -> bool:
    if not instance.container_id:
        return True
    container = db.get(Container, instance.container_id)
    return bool(
        container
        and container.present
        and container.status == "running"
        and container.health != "unhealthy"
    )


def _event(
    db: Session,
    service: ScalingService,
    action: str,
    event_status: str,
    source: int,
    target: int,
    detail: str,
) -> None:
    db.add(
        ScalingEvent(
            service_id=service.id,
            action=action,
            status=event_status,
            from_replicas=source,
            to_replicas=target,
            average_businesses=service.average_businesses,
            utilization_percent=service.utilization_percent,
            detail=detail[:500],
        )
    )


def _cancel_drains(instances: list[ServiceInstance]) -> None:
    for instance in instances:
        if instance.pool == "acs" and instance.desired_state in {
            "drain",
            "draining",
            "maintenance",
        }:
            instance.desired_state = "serve"
            instance.drain_requested_at = None


def evaluate_service(
    db: Session,
    service: ScalingService,
    now: datetime,
    settings: Settings,
    provider: AcsKubernetesClient | None = None,
    alert_notifications: list[str] | None = None,
) -> None:
    previous_status = service.status
    previous_error = service.last_error
    stale_before = now - timedelta(seconds=service.report_stale_seconds)
    instances = list(
        db.scalars(
            select(ServiceInstance).where(
                ServiceInstance.service_id == service.id,
                ServiceInstance.monitored.is_(True),
            )
        ).all()
    )
    fresh = [item for item in instances if (_aware(item.updated_at) or now) >= stale_before]
    available = [
        item
        for item in fresh
        if item.pool != "acs_release"
        and item.healthy
        and item.ready
        and item.accepting
        and _container_is_healthy(db, item)
    ]
    total_businesses = sum(item.active_businesses for item in fresh)
    total_capacity = sum(_instance_capacity(service, item) for item in available)
    service.current_businesses = total_businesses
    service.reporting_instances = len(fresh)
    service.last_evaluated_at = now
    if fresh:
        service.last_report_at = max(_aware(item.updated_at) or now for item in fresh)
    db.add(
        BusinessMetricSample(
            service_id=service.id,
            sampled_at=now,
            active_businesses=total_businesses,
            total_capacity=total_capacity,
            reporting_instances=len(fresh),
        )
    )
    db.flush()

    window_start = now - timedelta(seconds=service.window_seconds)
    samples = list(
        db.scalars(
            select(BusinessMetricSample)
            .where(
                BusinessMetricSample.service_id == service.id,
                BusinessMetricSample.sampled_at >= window_start,
            )
            .order_by(BusinessMetricSample.sampled_at)
        ).all()
    )
    service.average_businesses = (
        sum(item.active_businesses for item in samples) / len(samples) if samples else 0
    )
    average_capacity = (
        sum(item.total_capacity for item in samples) / len(samples) if samples else 0
    )
    service.utilization_percent = (
        service.average_businesses * 100 / average_capacity if average_capacity else 0
    )
    coverage = 0.0
    if len(samples) >= 2:
        first = _aware(samples[0].sampled_at) or now
        last = _aware(samples[-1].sampled_at) or now
        coverage = max(0.0, (last - first).total_seconds())
    window_ready = coverage >= service.window_seconds * 0.8
    service_alerts = evaluate_service_business_alerts(
        db, service, average_capacity, window_ready, now, settings
    )
    if alert_notifications is not None:
        alert_notifications.extend(service_alerts)

    local_instances = [item for item in instances if item.pool == "local"]
    unavailable_local = [
        item
        for item in local_instances
        if item not in available
        and item.desired_state not in {"draining", "maintenance", "restoring"}
    ]
    local_failure = bool(unavailable_local)
    missing_local_capacity = sum(
        _instance_capacity(service, item) for item in unavailable_local
    )

    if not service.autoscaling_enabled:
        _cancel_drains(instances)
        service.status = "monitoring"
        service.last_error = None
        return
    if not settings.autoscaling_enabled:
        service.status = "provider_disabled"
        service.last_error = "全局自动扩缩容开关未启用"
        return
    try:
        if provider is None:
            provider = AcsKubernetesClient(settings)
        current = provider.get_replicas(service)
        service.current_replicas = current
        service.desired_replicas = current
    except (httpx.HTTPError, RuntimeError, TypeError, ValueError) as exc:
        service.status = "provider_error"
        service.last_error = str(exc)[:500]
        if previous_status != service.status or previous_error != service.last_error:
            _event(
                db,
                service,
                "sync",
                "failed",
                service.current_replicas,
                service.current_replicas,
                str(exc),
            )
        return

    last_scaled = _aware(service.last_scaled_at)
    cooling = bool(
        last_scaled and (now - last_scaled).total_seconds() < service.cooldown_seconds
    )
    failure_target = 0
    if local_failure:
        failure_target = max(
            service.failover_replicas,
            math.ceil(missing_local_capacity / service.capacity_per_replica),
        )
    failure_target = min(service.max_replicas, failure_target)
    needs_failure_capacity = local_failure and current < failure_target
    load_scale_up = window_ready and (
        service.utilization_percent >= service.scale_up_threshold_percent
    )
    if (needs_failure_capacity or load_scale_up) and current < service.max_replicas:
        _cancel_drains(instances)
        if cooling and not local_failure:
            service.status = "cooldown"
            return
        healthy_local_capacity = sum(
            _instance_capacity(service, item)
            for item in available
            if item.pool == "local"
        )
        required_capacity = service.average_businesses / (
            service.scale_up_threshold_percent / 100
        )
        load_target = math.ceil(
            max(0, required_capacity - healthy_local_capacity)
            / service.capacity_per_replica
        )
        target = max(current + service.scale_up_step, failure_target, load_target)
        target = min(service.max_replicas, max(service.min_replicas, target))
        try:
            actual = provider.set_replicas(service, current, target)
            service.desired_replicas = actual
            service.status = "scaling_up_failover" if local_failure else "scaling_up"
            service.last_error = None
            service.last_scaled_at = now
            reason = (
                "本地实例故障兜底"
                if needs_failure_capacity
                else "5 分钟平均负载达到扩容阈值"
            )
            event_action = "failover_scale_up" if local_failure else "load_scale_up"
            _event(db, service, event_action, "success", current, actual, reason)
        except (httpx.HTTPError, RuntimeError, TypeError, ValueError) as exc:
            service.status = "scale_failed"
            service.last_error = str(exc)[:500]
            if previous_status != service.status or previous_error != service.last_error:
                event_action = "failover_scale_up" if local_failure else "load_scale_up"
                _event(db, service, event_action, "failed", current, target, str(exc))
        return

    if local_failure:
        _cancel_drains(instances)
        service.status = "failover_active"
        service.last_error = None
        return
    if service.release_status in {
        "preflighting_local",
        "preparing_guard",
        "waiting_guard",
        "draining_local",
        "preparing_acs",
        "waiting_acs",
        "updating_acs",
        "updating_local",
        "verifying_local",
        "cleaning_guard",
    }:
        _cancel_drains(instances)
        service.status = "release_protection"
        service.last_error = None
        return
    if not window_ready:
        _cancel_drains(instances)
        service.status = "collecting"
        service.last_error = None
        return
    if service.utilization_percent >= service.scale_down_threshold_percent:
        _cancel_drains(instances)
        service.status = "stable"
        service.last_error = None
        return
    if current <= service.min_replicas:
        _cancel_drains(instances)
        service.status = "at_minimum"
        service.last_error = None
        return
    if cooling:
        service.status = "cooldown"
        return

    acs_fresh = [item for item in fresh if item.pool == "acs" and item.healthy]
    drained = next(
        (
            item
            for item in acs_fresh
            if item.desired_state in {"drain", "draining", "maintenance"}
            and not item.accepting
            and item.active_businesses == 0
        ),
        None,
    )
    if not drained:
        serving = [item for item in acs_fresh if item.desired_state == "serve"]
        if not serving:
            service.status = "scale_down_waiting_for_reports"
            return
        candidate = min(serving, key=lambda item: (item.active_businesses, item.updated_at))
        candidate.desired_state = "draining"
        candidate.drain_requested_at = now
        service.status = "draining"
        _event(
            db,
            service,
            "load_drain_request",
            "pending",
            current,
            current - 1,
            f"等待 ACS 实例 {candidate.instance_id} 停止接单并清空在途业务",
        )
        return
    target = max(service.min_replicas, current - 1)
    try:
        provider.prefer_pod_for_removal(service, drained.instance_id)
        actual = provider.set_replicas(service, current, target)
        service.desired_replicas = actual
        service.status = "scaling_down"
        service.last_error = None
        service.last_scaled_at = now
        _event(
            db,
            service,
            "load_scale_down",
            "success",
            current,
            actual,
            f"ACS 实例 {drained.instance_id} 已排空，低负载缩容",
        )
    except (httpx.HTTPError, RuntimeError, TypeError, ValueError) as exc:
        service.status = "scale_failed"
        service.last_error = str(exc)[:500]
        drained.desired_state = "serve"
        drained.drain_requested_at = None
        if previous_status != service.status or previous_error != service.last_error:
            _event(db, service, "load_scale_down", "failed", current, target, str(exc))


def evaluate_all_services(
    db: Session, now: datetime, settings: Settings
) -> list[str]:
    provider: AcsKubernetesClient | None = None
    alert_notifications: list[str] = []
    try:
        if settings.autoscaling_enabled:
            try:
                provider = AcsKubernetesClient(settings)
            except RuntimeError:
                provider = None
        for service in db.scalars(select(ScalingService)).all():
            evaluate_service(
                db,
                service,
                now,
                settings,
                provider,
                alert_notifications,
            )
        cutoff = now - timedelta(hours=settings.autoscaling_history_retention_hours)
        db.execute(
            delete(BusinessMetricSample).where(
                BusinessMetricSample.sampled_at < cutoff
            )
        )
    finally:
        if provider:
            provider.close()
    return alert_notifications
