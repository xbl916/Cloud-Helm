from sqlalchemy import create_engine, inspect

from cloudhelm.db import Base


def test_fresh_schema_contains_wecom_sessions_without_passwords(tmp_path):
    fresh_engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    Base.metadata.create_all(fresh_engine)
    inspector = inspect(fresh_engine)
    user_columns = {item["name"] for item in inspector.get_columns("users")}
    assert "wecom_userid" in user_columns
    assert "password_hash" not in user_columns
    assert {"web_sessions", "oauth_states"}.issubset(inspector.get_table_names())
