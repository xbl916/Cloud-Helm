import logging
from collections.abc import Generator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from cloudhelm.config import get_settings

logger = logging.getLogger("cloudhelm.database")


class Base(DeclarativeBase):
    pass


def _engine_kwargs(url: str) -> dict:
    if url.startswith("sqlite"):
        return {"connect_args": {"check_same_thread": False}}
    return {"pool_pre_ping": True}


settings = get_settings()
engine = create_engine(settings.database_url, **_engine_kwargs(settings.database_url))
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session


def _add_compatible_columns(target_engine: Engine) -> None:
    """Upgrade an existing database without replacing tables or rows."""
    timestamp_type = (
        "TIMESTAMP WITH TIME ZONE"
        if target_engine.dialect.name == "postgresql"
        else "DATETIME"
    )
    additions = {
        "nodes": {
            "system_metrics_json": "TEXT NOT NULL DEFAULT '{}'",
            "system_metrics_status": "VARCHAR(20) NOT NULL DEFAULT 'unavailable'",
            "system_metrics_error": "VARCHAR(500)",
            "system_metrics_updated_at": timestamp_type,
            "metrics_history_at": timestamp_type,
        },
        "containers": {
            "network_rx_bytes": "BIGINT NOT NULL DEFAULT 0",
            "network_tx_bytes": "BIGINT NOT NULL DEFAULT 0",
            "network_rx_bps": "FLOAT NOT NULL DEFAULT 0",
            "network_tx_bps": "FLOAT NOT NULL DEFAULT 0",
            "writable_layer_bytes": "BIGINT NOT NULL DEFAULT 0",
            "rootfs_bytes": "BIGINT NOT NULL DEFAULT 0",
            "block_read_bytes": "BIGINT NOT NULL DEFAULT 0",
            "block_write_bytes": "BIGINT NOT NULL DEFAULT 0",
            "block_read_bps": "FLOAT NOT NULL DEFAULT 0",
            "block_write_bps": "FLOAT NOT NULL DEFAULT 0",
            "pids": "INTEGER NOT NULL DEFAULT 0",
            "restart_count": "INTEGER NOT NULL DEFAULT 0",
            "oom_killed": "BOOLEAN NOT NULL DEFAULT FALSE",
            "exit_code": "INTEGER",
            "finished_at": "VARCHAR(60)",
            "health_failing_streak": "INTEGER NOT NULL DEFAULT 0",
        },
        "access_rules": {
            "can_manage": "BOOLEAN NOT NULL DEFAULT FALSE",
        },
    }
    with target_engine.begin() as connection:
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())
        for table, columns in additions.items():
            if table not in tables:
                continue
            existing = {item["name"] for item in inspector.get_columns(table)}
            for column, definition in columns.items():
                if column in existing:
                    continue
                connection.execute(
                    text(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {definition}')
                )
                logger.info("Added compatible column %s.%s", table, column)


def initialize_database(target_engine: Engine = engine) -> None:
    from cloudhelm import models  # noqa: F401

    Base.metadata.create_all(bind=target_engine)
    _add_compatible_columns(target_engine)
