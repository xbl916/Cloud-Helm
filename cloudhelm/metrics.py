from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from cloudhelm.config import Settings
from cloudhelm.db import engine
from cloudhelm.models import (
    Container,
    MetricSample,
    Node,
    ScalingService,
    ServiceInstance,
)
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
    if (
        node.metrics_history_at
        and (now - _aware(node.metrics_history_at)).total_seconds()
        < settings.metrics_history_interval_seconds
    ):
        return

    node.metrics_history_at = now
    system = payload.system_metrics
    container_ids = [item.id for item in containers]
    business_rows = (
        list(
            db.execute(
                select(ServiceInstance, ScalingService)
                .join(
                    ScalingService,
                    ScalingService.id == ServiceInstance.service_id,
                )
                .where(ServiceInstance.container_id.in_(container_ids))
            ).all()
        )
        if container_ids
        else []
    )
    business_rows_by_container: dict[
        str, list[tuple[ServiceInstance, ScalingService]]
    ] = {}
    for instance, service in business_rows:
        if instance.container_id:
            business_rows_by_container.setdefault(instance.container_id, []).append(
                (instance, service)
            )
    active_businesses = sum(instance.active_businesses for instance, _ in business_rows)
    business_capacity = sum(
        instance.capacity_limit or service.capacity_per_replica
        for instance, service in business_rows
        if instance.healthy and instance.ready and instance.accepting
    )
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
            active_businesses=active_businesses,
            business_capacity=business_capacity,
            business_instances=len(business_rows),
        )
    )
    for item in containers:
        container_business_rows = business_rows_by_container.get(item.id, [])
        container_active_businesses = sum(
            instance.active_businesses for instance, _ in container_business_rows
        )
        container_business_capacity = sum(
            instance.capacity_limit or service.capacity_per_replica
            for instance, service in container_business_rows
            if instance.healthy and instance.ready and instance.accepting
        )
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
                active_businesses=container_active_businesses,
                business_capacity=container_business_capacity,
                business_instances=len(container_business_rows),
            )
        )
    db.flush()


@contextmanager
def _cleanup_session(target_engine: Engine) -> Generator[Session | None, None, None]:
    """Pin the connection so a PostgreSQL lock spans the batch commits."""
    with target_engine.connect() as connection:
        postgres = connection.dialect.name == "postgresql"
        lock_key = 0x43484D48495354  # CHMHIST; shared by all Server workers.
        acquired = False
        try:
            if postgres:
                acquired = connection.scalar(select(func.pg_try_advisory_lock(lock_key)))
                connection.commit()
                if not acquired:
                    yield None
                    return
            with Session(bind=connection) as db:
                yield db
        finally:
            if acquired:
                try:
                    connection.rollback()
                    connection.execute(select(func.pg_advisory_unlock(lock_key)))
                    connection.commit()
                except Exception:
                    # Never return a connection holding our session lock to the pool.
                    connection.invalidate()
                    raise


def cleanup_metric_history(
    settings: Settings,
    now: datetime,
    target_engine: Engine = engine,
    *,
    batch_size: int = 1000,
    max_batches: int = 20,
) -> bool:
    """Commit bounded deletes; return True when another pass may be needed.

    Time-only retention uses the sampled_at index and never counts the table.
    A configured row cap counts once per pass, then deletes the excess by PK.
    Cleanup also runs when sampling is disabled or every node is offline.
    """
    if batch_size < 1 or max_batches < 1:
        raise ValueError("Cleanup batch limits must be positive")
    cutoff = now - timedelta(hours=settings.metrics_history_retention_hours)
    with _cleanup_session(target_engine) as db:
        if db is None:
            return False
        batches = 0
        while batches < max_batches:
            expired = list(
                db.scalars(
                    select(MetricSample.id)
                    .where(MetricSample.sampled_at < cutoff)
                    .order_by(MetricSample.sampled_at)
                    .limit(batch_size)
                )
            )
            if not expired:
                break
            db.execute(delete(MetricSample).where(MetricSample.id.in_(expired)))
            db.commit()
            batches += 1
        if batches == max_batches:
            return True
        if not settings.metrics_history_max_rows:
            return False

        total = db.scalar(select(func.count()).select_from(MetricSample)) or 0
        excess = total - settings.metrics_history_max_rows
        while excess > 0 and batches < max_batches:
            oldest = list(
                db.scalars(
                    select(MetricSample.id)
                    .order_by(MetricSample.id)
                    .limit(min(batch_size, excess))
                )
            )
            if not oldest:
                break
            db.execute(delete(MetricSample).where(MetricSample.id.in_(oldest)))
            db.commit()
            excess -= len(oldest)
            batches += 1
        return excess > 0
