from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from cloudhelm.config import Settings, get_settings
from cloudhelm.db import Base
from cloudhelm.metrics import record_metric_history
from cloudhelm.models import Container, MetricSample, Node
from cloudhelm.schemas import HeartbeatRequest


def test_postgres_sized_history_configuration_is_accepted():
    values = get_settings().model_dump()
    values.update(
        metrics_history_retention_hours=4320,
        metrics_history_max_rows=3_500_000,
    )
    settings = Settings.model_validate(values)
    assert settings.metrics_history_retention_hours == 4320
    assert settings.metrics_history_max_rows == 3_500_000


def test_metric_history_retention_and_hard_row_cap(tmp_path):
    history_engine = create_engine(f"sqlite:///{tmp_path / 'history.db'}")
    Base.metadata.create_all(history_engine)
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
        for offset in (0, 60, 120):
            now = start + timedelta(seconds=offset)
            record_metric_history(db, node, [container], payload, now, settings)
            db.commit()
        assert db.scalar(select(func.count()).select_from(MetricSample)) == 4

        record_metric_history(
            db, node, [container], payload, start + timedelta(hours=2), settings
        )
        db.commit()
        samples = list(db.scalars(select(MetricSample)).all())
        assert len(samples) == 2
        assert all(
            sample.sampled_at.replace(tzinfo=UTC) >= start + timedelta(hours=1)
            for sample in samples
        )
