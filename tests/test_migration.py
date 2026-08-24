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
    }.issubset(inspector.get_table_names())
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
        "gateway_address",
    }.issubset(node_columns)
    container_columns = {item["name"] for item in inspector.get_columns("containers")}
    metric_columns = {item["name"] for item in inspector.get_columns("metric_samples")}
    assert {
        "active_businesses",
        "business_capacity",
        "business_instances",
    }.issubset(metric_columns)
    access_rule_columns = {
        item["name"] for item in inspector.get_columns("access_rules")
    }
    assert "can_manage" in access_rule_columns
    assert "alert_notify" in access_rule_columns
    assert "service_id" in access_rule_columns
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
                connection.execute(
                    text(f'ALTER TABLE "{table}" DROP COLUMN "{column}"')
                )
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
    assert "service_id" in access_rule_columns
    user_columns = {item["name"] for item in inspector.get_columns("users")}
    assert "access_version" in user_columns
    assert "alert_notifications" in user_columns
    assert "schema_migrations" in inspector.get_table_names()
    with old_engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM schema_migrations")
            ).scalar_one()
            == 14
        )
        assert (
            connection.execute(text("SELECT name FROM nodes")).scalar_one() == "原节点"
        )
        assert (
            connection.execute(text("SELECT name FROM containers")).scalar_one()
            == "原容器"
        )
    assert list(tmp_path.glob("cloudhelm-0.5.2.db.pre-*.bak"))


def test_initialize_database_adds_module_release_columns(tmp_path):
    database = tmp_path / "cloudhelm-0.9.0.db"
    legacy_engine = create_engine(f"sqlite:///{database}")
    with legacy_engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE scaling_services (id VARCHAR(36) PRIMARY KEY)")
        )

    initialize_database(legacy_engine)

    columns = {
        item["name"] for item in inspect(legacy_engine).get_columns("scaling_services")
    }
    assert {
        "acs_container_name",
        "desired_image",
        "previous_image",
        "release_status",
        "release_error",
        "release_requested_by",
        "release_task_id",
        "release_acs_original_replicas",
        "release_started_at",
        "release_updated_at",
    }.issubset(columns)


def test_initialize_database_adds_pc_qr_auth_columns(tmp_path):
    database = tmp_path / "cloudhelm-before-pc-qr.db"
    legacy_engine = create_engine(f"sqlite:///{database}")
    with legacy_engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE web_sessions (id VARCHAR(36) PRIMARY KEY)")
        )
        connection.execute(
            text("CREATE TABLE oauth_states (id VARCHAR(36) PRIMARY KEY)")
        )

    initialize_database(legacy_engine)

    inspector = inspect(legacy_engine)
    session_columns = {item["name"] for item in inspector.get_columns("web_sessions")}
    state_columns = {item["name"] for item in inspector.get_columns("oauth_states")}
    assert "auth_method" in session_columns
    assert "provider" in state_columns


def test_initialize_database_adds_gateway_endpoint_columns(tmp_path):
    database = tmp_path / "cloudhelm-before-discovery.db"
    legacy_engine = create_engine(f"sqlite:///{database}")
    with legacy_engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE service_instances (id VARCHAR(36) PRIMARY KEY)")
        )

    initialize_database(legacy_engine)

    columns = {
        item["name"] for item in inspect(legacy_engine).get_columns("service_instances")
    }
    assert {"endpoint_scheme", "endpoint_host", "endpoint_port"}.issubset(columns)


def test_initialize_database_adds_node_gateway_address(tmp_path):
    database = tmp_path / "cloudhelm-before-node-gateway.db"
    legacy_engine = create_engine(f"sqlite:///{database}")
    with legacy_engine.begin() as connection:
        connection.execute(text("CREATE TABLE nodes (id VARCHAR(36) PRIMARY KEY)"))

    initialize_database(legacy_engine)

    columns = {item["name"] for item in inspect(legacy_engine).get_columns("nodes")}
    assert "gateway_address" in columns


def test_initialize_database_adds_node_business_history_columns(tmp_path):
    database = tmp_path / "cloudhelm-before-business-history.db"
    legacy_engine = create_engine(f"sqlite:///{database}")
    with legacy_engine.begin() as connection:
        connection.execute(text("CREATE TABLE metric_samples (id INTEGER PRIMARY KEY)"))

    initialize_database(legacy_engine)

    columns = {
        item["name"] for item in inspect(legacy_engine).get_columns("metric_samples")
    }
    assert {
        "active_businesses",
        "business_capacity",
        "business_instances",
    }.issubset(columns)


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
        assert (
            connection.execute(text("SELECT value FROM legacy_marker")).scalar_one()
            == "preserved"
        )
