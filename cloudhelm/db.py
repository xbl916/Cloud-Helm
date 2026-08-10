import logging
import sqlite3
from collections.abc import Generator
from pathlib import Path

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


def _add_columns(
    target_engine: Engine, additions: dict[str, dict[str, str]]
) -> None:
    """Apply one idempotent additive schema migration."""
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


def _migration_definitions(target_engine: Engine) -> list[tuple[str, dict]]:
    timestamp_type = (
        "TIMESTAMP WITH TIME ZONE"
        if target_engine.dialect.name == "postgresql"
        else "DATETIME"
    )
    return [
        (
            "0001_node_metrics",
            {
                "nodes": {
                    "system_metrics_json": "TEXT NOT NULL DEFAULT '{}'",
                    "system_metrics_status": "VARCHAR(20) NOT NULL DEFAULT 'unavailable'",
                    "system_metrics_error": "VARCHAR(500)",
                    "system_metrics_updated_at": timestamp_type,
                    "metrics_history_at": timestamp_type,
                }
            },
        ),
        (
            "0002_container_metrics",
            {
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
                }
            },
        ),
        (
            "0003_resource_administrators",
            {"access_rules": {"can_manage": "BOOLEAN NOT NULL DEFAULT FALSE"}},
        ),
        (
            "0004_access_optimistic_lock",
            {"users": {"access_version": "INTEGER NOT NULL DEFAULT 1"}},
        ),
        ("0005_alerting", {}),
        (
            "0006_alert_subscriptions",
            {
                "users": {
                    "alert_notifications": "BOOLEAN NOT NULL DEFAULT FALSE"
                },
                "access_rules": {
                    "alert_notify": "BOOLEAN NOT NULL DEFAULT FALSE"
                },
            },
        ),
        (
            "0007_extended_alert_metrics",
            {
                "nodes": {
                    "gpu_expected_count": "INTEGER NOT NULL DEFAULT 0",
                    "network_baseline_bps": "FLOAT NOT NULL DEFAULT 0",
                    "network_baseline_samples": "INTEGER NOT NULL DEFAULT 0",
                    "network_surge_percent": "FLOAT",
                },
                "containers": {
                    "writable_layer_growth_mibps": "FLOAT",
                },
            },
        ),
    ]


def _apply_migrations(target_engine: Engine) -> None:
    """Record and apply additive migrations for SQLite and PostgreSQL."""
    with target_engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version VARCHAR(80) PRIMARY KEY, "
                "applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
        )
        applied = set(
            connection.execute(text("SELECT version FROM schema_migrations")).scalars()
        )
    for version, additions in _migration_definitions(target_engine):
        if version in applied:
            continue
        _add_columns(target_engine, additions)
        with target_engine.begin() as connection:
            connection.execute(
                text("INSERT INTO schema_migrations (version) VALUES (:version)"),
                {"version": version},
            )
        logger.info("Applied database migration %s", version)


def _sqlite_integrity_check(target_engine: Engine) -> None:
    if target_engine.dialect.name != "sqlite":
        return
    with target_engine.connect() as connection:
        result = connection.exec_driver_sql("PRAGMA integrity_check").scalar_one()
    if result != "ok":
        raise RuntimeError(f"SQLite integrity check failed: {result}")


def _has_pending_migrations(target_engine: Engine) -> bool:
    with target_engine.connect() as connection:
        if "schema_migrations" not in inspect(connection).get_table_names():
            return True
        applied = set(
            connection.execute(text("SELECT version FROM schema_migrations")).scalars()
        )
    return any(
        version not in applied for version, _ in _migration_definitions(target_engine)
    )


def _backup_sqlite(
    target_engine: Engine, existed: bool, migration_pending: bool
) -> Path | None:
    """Create one consistent pre-migration backup per application version."""
    database = target_engine.url.database
    if (
        target_engine.dialect.name != "sqlite"
        or not database
        or not existed
        or not migration_pending
    ):
        return None
    source = Path(database)
    if not source.is_file():
        return None
    from cloudhelm import __version__

    backup = source.with_name(f"{source.name}.pre-{__version__}.bak")
    if backup.exists():
        return backup
    with sqlite3.connect(source) as source_db, sqlite3.connect(backup) as backup_db:
        source_db.backup(backup_db)
    logger.info("Created SQLite pre-migration backup %s", backup)
    return backup


def initialize_database(target_engine: Engine = engine) -> None:
    from cloudhelm import models  # noqa: F401

    database = target_engine.url.database
    sqlite_existed = bool(
        target_engine.dialect.name == "sqlite"
        and database
        and Path(database).is_file()
        and Path(database).stat().st_size > 0
    )
    _sqlite_integrity_check(target_engine)
    migration_pending = _has_pending_migrations(target_engine)
    _backup_sqlite(
        target_engine, sqlite_existed, migration_pending
    )
    Base.metadata.create_all(bind=target_engine)
    _apply_migrations(target_engine)
    _sqlite_integrity_check(target_engine)
