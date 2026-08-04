import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

test_dir = Path(tempfile.mkdtemp(prefix="cloudhelm-tests-"))
os.environ["CLOUDHELM_AGENT_ENROLLMENT_TOKEN"] = "test-agent-enrollment-token"
os.environ["CLOUDHELM_PUBLIC_BASE_URL"] = "https://testserver"
os.environ["CLOUDHELM_WECOM_CORP_ID"] = "ww-test-corp"
os.environ["CLOUDHELM_WECOM_AGENT_ID"] = "1000002"
os.environ["CLOUDHELM_WECOM_SECRET"] = "test-wecom-secret"
os.environ["CLOUDHELM_BOOTSTRAP_ADMIN_WECOM_USERID"] = "admin-wecom-id"
os.environ["CLOUDHELM_BOOTSTRAP_ADMIN_DISPLAY_NAME"] = "测试管理员"
os.environ["CLOUDHELM_DATABASE_URL"] = f"sqlite:///{test_dir / 'test.db'}"
os.environ["CLOUDHELM_ENVIRONMENT"] = "test"

from cloudhelm.db import SessionLocal  # noqa: E402
from cloudhelm.dependencies import CSRF_COOKIE, SESSION_COOKIE  # noqa: E402
from cloudhelm.main import app  # noqa: E402
from cloudhelm.models import User, WebSession  # noqa: E402
from cloudhelm.security import digest_token, new_opaque_token  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app, base_url="https://testserver") as value:
        yield value


@pytest.fixture(autouse=True)
def clear_browser_cookies(client: TestClient):
    client.cookies.clear()
    yield
    client.cookies.clear()


@pytest.fixture
def session_for(client: TestClient):
    def activate(wecom_userid: str) -> dict[str, str]:
        raw_session = new_opaque_token()
        raw_csrf = new_opaque_token()
        with SessionLocal() as db:
            user = db.scalar(select(User).where(User.wecom_userid == wecom_userid))
            assert user is not None
            db.add(
                WebSession(
                    token_hash=digest_token(raw_session),
                    csrf_token_hash=digest_token(raw_csrf),
                    user_id=user.id,
                    expires_at=datetime.now(UTC) + timedelta(minutes=30),
                )
            )
            db.commit()
        client.cookies.set(SESSION_COOKIE, raw_session, path="/")
        client.cookies.set(CSRF_COOKIE, raw_csrf, path="/")
        return {"Origin": "https://testserver", "X-CSRF-Token": raw_csrf}

    return activate


@pytest.fixture
def admin_headers(session_for) -> dict[str, str]:
    return session_for("admin-wecom-id")
