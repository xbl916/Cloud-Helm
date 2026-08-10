from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from cloudhelm.db import Base, initialize_database
from cloudhelm.models import Container, Node


def test_fresh_schema_contains_wecom_sessions_without_passwords(tmp_path):
    fresh_engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    Base.metadata.create_all(fresh_engine)
    inspector = inspect(fresh_engine)
    user_columns = {item["name"] for item in inspector.get_columns("users")}
    assert "wecom_userid" in user_columns
    assert "password_hash" not in user_columns
    assert {
        "web_sessions",
        "oauth_states",
        "metric_samples",
        "alert_rule_seeds",
    }.issubset(
        inspector.get_table_names()
    )
    node_columns = {item["name"] for item in inspector.get_columns("nodes")}
    assert {
        "gpus_json",
        "gpu_status",
        "gpu_error",
        "gpu_updated_at",
        "gpu_expected_count",
        "network_baseline_bps",
        "network_baseline_samples",
        "network_surge_percent",
        "system_metrics_json",
        "system_metrics_status",
        "system_metrics_error",
        "system_metrics_updated_at",
        "metrics_history_at",
    }.issubset(node_columns)
    container_columns = {item["name"] for item in inspector.get_columns("containers")}
    access_rule_columns = {
        item["name"] for item in inspector.get_columns("access_rules")
    }
    assert "can_manage" in access_rule_columns
    assert "alert_notify" in access_rule_columns
    assert "access_version" in user_columns
    assert "alert_notifications" in user_columns
    assert {
        "gpu_devices_json",
        "gpu_all",
        "network_rx_bytes",
        "network_tx_bytes",
        "network_rx_bps",
        "network_tx_bps",
        "writable_layer_bytes",
        "rootfs_bytes",
        "writable_layer_growth_mibps",
        "block_read_bytes",
        "block_write_bytes",
        "block_read_bps",
        "block_write_bps",
        "pids",
        "restart_count",
        "oom_killed",
        "exit_code",
        "finished_at",
        "health_failing_streak",
    }.issubset(container_columns)


def test_initialize_database_upgrades_052_sqlite_in_place(tmp_path):
    database = tmp_path / "cloudhelm-0.5.2.db"
    old_engine = create_engine(f"sqlite:///{database}")
    Base.metadata.create_all(old_engine)
    with Session(old_engine) as session:
        node = Node(
            agent_key="legacy-agent",
            name="原节点",
            agent_token_hash="legacy-token-hash",
        )
        session.add(node)
        session.flush()
        session.add(
            Container(
                node_id=node.id,
                docker_id="legacy-container-id",
                name="原容器",
            )
        )
        session.commit()

    monitoring_columns = {
        "nodes": [
            "system_metrics_json",
            "system_metrics_status",
            "system_metrics_error",
            "system_metrics_updated_at",
            "metrics_history_at",
            "gpu_expected_count",
            "network_baseline_bps",
            "network_baseline_samples",
            "network_surge_percent",
        ],
        "containers": [
            "network_rx_bytes",
            "network_tx_bytes",
            "network_rx_bps",
            "network_tx_bps",
            "writable_layer_bytes",
            "rootfs_bytes",
            "writable_layer_growth_mibps",
            "block_read_bytes",
            "block_write_bytes",
            "block_read_bps",
            "block_write_bps",
            "pids",
            "restart_count",
            "oom_killed",
            "exit_code",
            "finished_at",
            "health_failing_streak",
        ],
    }
    with old_engine.begin() as connection:
        for table, columns in monitoring_columns.items():
            for column in columns:
                connection.execute(text(f'ALTER TABLE "{table}" DROP COLUMN "{column}"'))
        connection.execute(text('ALTER TABLE "access_rules" DROP COLUMN "can_manage"'))

    initialize_database(old_engine)
    initialize_database(old_engine)

    inspector = inspect(old_engine)
    node_columns = {item["name"] for item in inspector.get_columns("nodes")}
    container_columns = {item["name"] for item in inspector.get_columns("containers")}
    access_rule_columns = {
        item["name"] for item in inspector.get_columns("access_rules")
    }
    assert set(monitoring_columns["nodes"]).issubset(node_columns)
    assert set(monitoring_columns["containers"]).issubset(container_columns)
    assert "can_manage" in access_rule_columns
    assert "alert_notify" in access_rule_columns
    user_columns = {item["name"] for item in inspector.get_columns("users")}
    assert "access_version" in user_columns
    assert "alert_notifications" in user_columns
    assert "schema_migrations" in inspector.get_table_names()
    with old_engine.connect() as connection:
        assert connection.execute(
            text("SELECT COUNT(*) FROM schema_migrations")
        ).scalar_one() == 7
        assert connection.execute(text("SELECT name FROM nodes")).scalar_one() == "原节点"
        assert (
            connection.execute(text("SELECT name FROM containers")).scalar_one()
            == "原容器"
        )
    assert list(tmp_path.glob("cloudhelm-0.5.2.db.pre-*.bak"))


def test_sqlite_backup_is_created_before_schema_changes(tmp_path):
    database = tmp_path / "legacy.db"
    legacy_engine = create_engine(f"sqlite:///{database}")
    with legacy_engine.begin() as connection:
        connection.execute(text("CREATE TABLE legacy_marker (value TEXT NOT NULL)"))
        connection.execute(
            text("INSERT INTO legacy_marker (value) VALUES ('preserved')")
        )

    initialize_database(legacy_engine)

    [backup] = list(tmp_path.glob("legacy.db.pre-*.bak"))
    backup_engine = create_engine(f"sqlite:///{backup}")
    backup_tables = set(inspect(backup_engine).get_table_names())
    assert backup_tables == {"legacy_marker"}
    with backup_engine.connect() as connection:
        assert connection.execute(
            text("SELECT value FROM legacy_marker")
        ).scalar_one() == "preserved"
