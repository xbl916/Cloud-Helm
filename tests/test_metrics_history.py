import asyncio
import os
import threading
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session

from cloudhelm.config import Settings, get_settings
from cloudhelm.db import Base
from cloudhelm.metrics import (
    _cleanup_session,
    cleanup_metric_history,
    record_metric_history,
)
from cloudhelm.models import (
    Container,
    MetricSample,
    Node,
    ScalingService,
    ServiceInstance,
)
from cloudhelm.schemas import HeartbeatRequest


@pytest.fixture(
    params=["sqlite"]
    + (["postgresql"] if os.getenv("CLOUDHELM_TEST_POSTGRES_URL") else [])
)
def history_engine(request, tmp_path):
    if request.param == "postgresql":
        schema = f"history_test_{uuid4().hex}"
        target_engine = create_engine(
            os.environ["CLOUDHELM_TEST_POSTGRES_URL"],
            connect_args={"options": f"-csearch_path={schema}"},
        )
        with target_engine.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    else:
        target_engine = create_engine(f"sqlite:///{tmp_path / 'history.db'}")
    try:
        Base.metadata.create_all(target_engine)
        yield target_engine
    finally:
        if request.param == "postgresql":
            with target_engine.begin() as connection:
                connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        target_engine.dispose()


@pytest.mark.parametrize("max_rows", [0, 3_500_000, 50_000_000])
def test_postgres_sized_history_configuration_is_accepted(max_rows):
    values = get_settings().model_dump()
    values.update(
        metrics_history_retention_hours=4320,
        metrics_history_max_rows=max_rows,
    )
    settings = Settings.model_validate(values)
    assert settings.metrics_history_retention_hours == 4320
    assert settings.metrics_history_max_rows == max_rows


def test_negative_history_row_cap_is_rejected():
    values = get_settings().model_dump()
    values["metrics_history_max_rows"] = -1
    with pytest.raises(ValidationError):
        Settings.model_validate(values)


def test_metric_history_recording_and_background_retention(history_engine):
    settings = get_settings().model_copy(
        update={
            "metrics_history_interval_seconds": 60,
            "metrics_history_retention_hours": 1,
            "metrics_history_max_rows": 4,
        }
    )
    payload = HeartbeatRequest(
        system_metrics_status="ok",
        system_metrics={"cpu_percent": 20, "memory_percent": 30},
    )
    start = datetime(2026, 8, 5, tzinfo=UTC)
    with Session(history_engine) as db:
        node = Node(
            agent_key="history-agent",
            name="历史节点",
            agent_token_hash="hash",
        )
        db.add(node)
        db.flush()
        container = Container(
            node_id=node.id,
            docker_id="history-container",
            name="历史容器",
        )
        db.add(container)
        db.flush()
        service = ScalingService(
            name="历史业务服务",
            service_type="vlm",
            report_token_hash="hash",
            capacity_per_replica=10,
            workload_name="history-vlm",
        )
        db.add(service)
        db.flush()
        db.add(
            ServiceInstance(
                service_id=service.id,
                instance_id="history-vlm-1",
                container_id=container.id,
                active_businesses=7,
                capacity_limit=16,
            )
        )
        db.flush()
        for offset in (0, 60, 120):
            now = start + timedelta(seconds=offset)
            record_metric_history(db, node, [container], payload, now, settings)
            db.commit()
        # Recording never cleans up or scans the existing history table.
        assert db.scalar(select(func.count()).select_from(MetricSample)) == 6
        assert not cleanup_metric_history(settings, now, history_engine)
        assert db.scalar(select(func.count()).select_from(MetricSample)) == 4
        node_sample = db.scalar(
            select(MetricSample)
            .where(MetricSample.target_type == "node")
            .order_by(MetricSample.sampled_at.desc())
        )
        assert node_sample is not None
        assert node_sample.active_businesses == 7
        assert node_sample.business_capacity == 16
        assert node_sample.business_instances == 1
        container_sample = db.scalar(
            select(MetricSample)
            .where(MetricSample.target_type == "container")
            .order_by(MetricSample.sampled_at.desc())
        )
        assert container_sample is not None
        assert container_sample.active_businesses == 7
        assert container_sample.business_capacity == 16
        assert container_sample.business_instances == 1

        record_metric_history(
            db, node, [container], payload, start + timedelta(hours=2), settings
        )
        db.commit()
        cleanup_metric_history(settings, start + timedelta(hours=2), history_engine)
        samples = list(db.scalars(select(MetricSample)).all())
        assert len(samples) == 2
        assert all(
            sample.sampled_at.replace(tzinfo=UTC) >= start + timedelta(hours=1)
            for sample in samples
        )


def test_time_only_cleanup_is_bounded_and_works_without_sampling(history_engine):
    now = datetime(2026, 11, 3, tzinfo=UTC)
    settings = get_settings().model_copy(
        update={
            "metrics_history_enabled": False,
            "metrics_history_retention_hours": 4320,
            "metrics_history_max_rows": 0,
        }
    )
    cutoff = now - timedelta(days=180)
    with Session(history_engine) as db:
        for sample_id, sampled_at in enumerate(
            [now] + [cutoff - timedelta(seconds=1)] * 5 + [cutoff], start=1
        ):
            db.add(
                MetricSample(
                    id=sample_id,
                    target_type="node",
                    target_id="node",
                    node_id="node",
                    sampled_at=sampled_at,
                )
            )
        db.commit()
    statements = []
    event.listen(
        history_engine,
        "before_cursor_execute",
        lambda conn, cursor, statement, parameters, context, executemany: (
            statements.append(statement.upper())
        ),
    )
    assert cleanup_metric_history(
        settings, now, history_engine, batch_size=2, max_batches=2
    )
    with Session(history_engine) as db:
        assert db.scalar(select(func.count()).select_from(MetricSample)) == 3
    statements.clear()
    assert not cleanup_metric_history(
        settings, now, history_engine, batch_size=2, max_batches=2
    )
    # No count or large offset is needed when the row cap is disabled.
    assert not any("COUNT(" in statement for statement in statements)
    assert not any("ORDER BY METRIC_SAMPLES.ID DESC" in s for s in statements)
    with Session(history_engine) as db:
        assert list(db.scalars(select(MetricSample.id).order_by(MetricSample.id))) == [
            1,
            7,
        ]


def test_optional_row_cap_preserves_exact_count_with_id_gaps(history_engine):
    now = datetime.now(UTC)
    settings = get_settings().model_copy(update={"metrics_history_max_rows": 3})
    with Session(history_engine) as db:
        for sample_id in (1, 4, 20, 21, 99, 100, 1000):
            db.add(
                MetricSample(
                    id=sample_id,
                    target_type="node",
                    target_id="node",
                    node_id="node",
                    sampled_at=now,
                )
            )
        db.commit()
    assert cleanup_metric_history(
        settings, now, history_engine, batch_size=2, max_batches=1
    )
    assert not cleanup_metric_history(
        settings, now, history_engine, batch_size=2, max_batches=2
    )
    with Session(history_engine) as db:
        assert list(db.scalars(select(MetricSample.id).order_by(MetricSample.id))) == [
            99,
            100,
            1000,
        ]


def test_cleanup_commits_completed_batches_and_can_retry(history_engine):
    now = datetime.now(UTC)
    settings = get_settings().model_copy(update={"metrics_history_max_rows": 0})
    with Session(history_engine) as db:
        for _ in range(5):
            db.add(
                MetricSample(
                    target_type="node",
                    target_id="node",
                    node_id="node",
                    sampled_at=now - timedelta(days=8),
                )
            )
        db.commit()

    deletes = 0

    def fail_second_delete(conn, cursor, statement, parameters, context, executemany):
        nonlocal deletes
        if statement.startswith("DELETE"):
            deletes += 1
            if deletes == 2:
                raise RuntimeError("simulated cleanup failure")

    event.listen(history_engine, "before_cursor_execute", fail_second_delete)
    with pytest.raises(RuntimeError, match="simulated cleanup failure"):
        cleanup_metric_history(settings, now, history_engine, batch_size=2)
    event.remove(history_engine, "before_cursor_execute", fail_second_delete)
    with Session(history_engine) as db:
        assert db.scalar(select(func.count()).select_from(MetricSample)) == 3
    assert not cleanup_metric_history(settings, now, history_engine, batch_size=2)
    with Session(history_engine) as db:
        assert db.scalar(select(func.count()).select_from(MetricSample)) == 0


def test_postgres_cleanup_lock_excludes_other_workers_and_releases_on_error(
    history_engine,
):
    if history_engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL advisory lock integration test")
    now = datetime.now(UTC)
    settings = get_settings().model_copy(update={"metrics_history_max_rows": 0})
    with Session(history_engine) as db:
        db.add(
            MetricSample(
                target_type="node",
                target_id="node",
                node_id="node",
                sampled_at=now - timedelta(days=8),
            )
        )
        db.commit()
    with (
        pytest.raises(RuntimeError, match="simulated worker failure"),
        _cleanup_session(history_engine) as held,
    ):
        assert held is not None
        held.commit()
        # The lock survives batch commits and rejects another connection.
        assert not cleanup_metric_history(settings, now, history_engine)
        with Session(history_engine) as db:
            assert db.scalar(select(func.count()).select_from(MetricSample)) == 1
        raise RuntimeError("simulated worker failure")
    assert not cleanup_metric_history(settings, now, history_engine)
    with Session(history_engine) as db:
        assert db.scalar(select(func.count()).select_from(MetricSample)) == 0


@pytest.mark.parametrize("fail_first", [False, True])
def test_background_monitor_retries_and_runs_off_event_loop(
    monkeypatch, caplog, fail_first
):
    from cloudhelm import main

    async def exercise():
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        loop_thread = threading.get_ident()
        calls = 0
        delays = []
        real_wait_for = asyncio.wait_for

        def cleanup(settings, now):
            nonlocal calls
            assert threading.get_ident() != loop_thread
            calls += 1
            if calls == 1:
                if fail_first:
                    raise RuntimeError("simulated worker failure")
                return True
            loop.call_soon_threadsafe(stop.set)
            return False

        async def fast_wait(awaitable, timeout):
            delays.append(timeout)
            return await real_wait_for(awaitable, timeout=0.01)

        monkeypatch.setattr(main, "cleanup_metric_history", cleanup)
        monkeypatch.setattr(main.asyncio, "wait_for", fast_wait)
        await main.metric_history_monitor(stop)
        assert calls == 2
        assert delays == [300 if fail_first else 1, 300]

    asyncio.run(exercise())
    if fail_first:
        assert "Metric history cleanup failed; will retry" in caplog.text
