from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from cloudhelm.config import Settings
from cloudhelm.models import Container, MetricSample, Node
from cloudhelm.schemas import HeartbeatRequest


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def record_metric_history(
    db: Session,
    node: Node,
    containers: list[Container],
    payload: HeartbeatRequest,
    now: datetime,
    settings: Settings,
) -> None:
    if not settings.metrics_history_enabled:
        return
    if node.metrics_history_at and (
        now - _aware(node.metrics_history_at)
    ).total_seconds() < settings.metrics_history_interval_seconds:
        return

    node.metrics_history_at = now
    system = payload.system_metrics
    db.add(
        MetricSample(
            target_type="node",
            target_id=node.id,
            node_id=node.id,
            sampled_at=now,
            cpu_percent=system.cpu_percent,
            memory_usage=system.memory_used_bytes,
            memory_percent=system.memory_percent,
            network_rx_bps=system.network_rx_bps,
            network_tx_bps=system.network_tx_bps,
            disk_used_bytes=system.disk_used_bytes,
            disk_total_bytes=system.disk_total_bytes,
        )
    )
    for item in containers:
        db.add(
            MetricSample(
                target_type="container",
                target_id=item.id,
                node_id=node.id,
                sampled_at=now,
                cpu_percent=item.cpu_percent,
                memory_usage=item.memory_usage,
                memory_percent=item.memory_percent,
                network_rx_bps=item.network_rx_bps,
                network_tx_bps=item.network_tx_bps,
                disk_used_bytes=item.writable_layer_bytes,
                disk_total_bytes=item.rootfs_bytes,
                block_read_bps=item.block_read_bps,
                block_write_bps=item.block_write_bps,
                pids=item.pids,
                restart_count=item.restart_count,
            )
        )
    db.flush()

    retention_cutoff = now - timedelta(hours=settings.metrics_history_retention_hours)
    db.execute(
        delete(MetricSample).where(MetricSample.sampled_at < retention_cutoff)
    )
    boundary = db.scalar(
        select(MetricSample.id)
        .order_by(MetricSample.id.desc())
        .offset(settings.metrics_history_max_rows - 1)
        .limit(1)
    )
    if boundary is not None:
        db.execute(delete(MetricSample).where(MetricSample.id < boundary))
