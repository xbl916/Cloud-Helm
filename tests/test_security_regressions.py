import base64
import hashlib
import secrets
import time
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from pydantic import ValidationError
from sqlalchemy import select

from cloudhelm.config import Settings, get_settings
from cloudhelm.db import SessionLocal
from cloudhelm.models import (
    AccessRule,
    Container,
    Node,
    ServiceInstance,
    User,
    UserRole,
)
from cloudhelm.security import hash_secret


def test_report_cannot_bind_or_rebind_foreign_container(client, admin_headers):
    service = client.post(
        "/api/v1/scaling/services",
        headers=admin_headers,
        json={
            "name": f"audit-{uuid4()}",
            "workload_name": "allowed",
        },
    ).json()
    with SessionLocal() as db:
        node = Node(
            name="audit", agent_key=str(uuid4()), agent_token_hash=hash_secret("test")
        )
        db.add(node)
        db.flush()
        containers = [
            Container(
                node_id=node.id, docker_id=str(uuid4()), name=f"c{i}", image="test:v1"
            )
            for i in range(2)
        ]
        db.add_all(containers)
        db.flush()
        ids = [c.id for c in containers]
        db.add(
            ServiceInstance(
                service_id=service["id"],
                instance_id="existing",
                pool="local",
                container_id=ids[0],
            )
        )
        db.commit()
    url = f"/api/v1/service-metrics/{service['id']}/report"
    headers = {"X-CloudHelm-Service-Token": service["report_token"]}
    for instance_id in ("new", "existing"):
        assert (
            client.post(
                url,
                headers=headers,
                json={
                    "instance_id": instance_id,
                    "active_businesses": 0,
                    "container_id": ids[1],
                },
            ).status_code
            == 403
        )
    for body in ({"container_id": ids[0]}, {}):
        response = client.post(
            url,
            headers=headers,
            json={
                "instance_id": "existing",
                "active_businesses": 2,
                **body,
            },
        )
        assert response.status_code == 200
        assert response.json()["container_id"] == ids[0]
    with SessionLocal() as db:
        assert (
            db.scalar(
                select(ServiceInstance).where(
                    ServiceInstance.service_id == service["id"],
                    ServiceInstance.instance_id == "new",
                )
            )
            is None
        )


def test_operator_cannot_retarget_kubernetes(client, admin_headers, session_for):
    payload = {"name": f"audit-target-{uuid4()}", "workload_name": "allowed"}
    service = client.post(
        "/api/v1/scaling/services", headers=admin_headers, json=payload
    ).json()
    userid = str(uuid4())
    with SessionLocal() as db:
        user = User(
            username=userid,
            wecom_userid=userid,
            display_name="op",
            role=UserRole.operator,
        )
        db.add(user)
        db.flush()
        db.add(
            AccessRule(
                user_id=user.id,
                scope_type="service",
                service_id=service["id"],
                can_view=True,
                can_operate=True,
                can_manage=True,
            )
        )
        db.commit()
    headers = session_for(userid)
    url = f"/api/v1/scaling/services/{service['id']}"
    for field, value in [
        ("namespace", "other"),
        ("workload_name", "other"),
        ("workload_kind", "StatefulSet"),
        ("acs_container_name", "other"),
    ]:
        assert (
            client.put(url, headers=headers, json={**payload, field: value}).status_code
            == 403
        )
    assert (
        client.put(
            url, headers=headers, json={**payload, "capacity_per_replica": 20}
        ).status_code
        == 200
    )
    headers = session_for("admin-wecom-id")
    assert (
        client.put(
            url, headers=headers, json={**payload, "namespace": "other"}
        ).status_code
        == 200
    )


def test_enrollment_cannot_replace_existing_credentials(client):
    payload = {
        "agent_key": str(uuid4()),
        "name": "original",
        "enrollment_token": "test-agent-enrollment-token",
    }
    first = client.post("/api/v1/agent/enroll", json=payload)
    assert first.status_code == 200
    credentials = first.json()
    assert (
        client.post(
            "/api/v1/agent/enroll", json={**payload, "name": "replacement"}
        ).status_code
        == 409
    )
    assert (
        client.get(
            "/api/v1/agent/tasks/next",
            headers={
                "X-Node-Id": credentials["node_id"],
                "X-Agent-Token": credentials["node_token"],
            },
        ).status_code
        == 204
    )


def test_production_wecom_api_requires_https():
    config = get_settings().model_dump()
    config.update(environment="production")
    with pytest.raises(ValidationError, match="wecom_api_base must use HTTPS"):
        Settings(**{**config, "wecom_api_base": "http://example.invalid"})
    assert Settings(**config).wecom_api_base == "https://qyapi.weixin.qq.com"


@pytest.fixture
def callback_settings(client):
    settings = get_settings().model_copy(
        update={
            "wecom_callback_token": "callback-test-token-123456",
            "wecom_callback_aes_key": base64.b64encode(b"k" * 32).decode().rstrip("="),
        }
    )
    client.app.dependency_overrides[get_settings] = lambda: settings
    yield settings
    client.app.dependency_overrides.pop(get_settings, None)


def _encrypted(message, settings, receiver=None, timestamp=None):
    key = base64.b64decode(settings.wecom_callback_aes_key + "=")
    raw = (
        secrets.token_bytes(16)
        + len(message).to_bytes(4, "big")
        + message
        + (receiver or settings.wecom_corp_id).encode()
    )
    padder = padding.PKCS7(256).padder()
    padded = padder.update(raw) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
    encrypted = base64.b64encode(
        encryptor.update(padded) + encryptor.finalize()
    ).decode()
    params = {
        "timestamp": str(timestamp or int(time.time())),
        "nonce": secrets.token_hex(8),
    }
    params["msg_signature"] = hashlib.sha1(
        "".join(
            sorted(
                [
                    settings.wecom_callback_token,
                    params["timestamp"],
                    params["nonce"],
                    encrypted,
                ]
            )
        ).encode()
    ).hexdigest()
    return encrypted, params


def test_callback_url_verification_and_invalid_messages(client, callback_settings):
    encrypted, params = _encrypted(b"challenge", callback_settings)
    url = "/api/v1/auth/wecom/events"
    assert (
        client.get(url, params={**params, "echostr": encrypted}).content == b"challenge"
    )
    assert (
        client.get(
            url, params={**params, "echostr": encrypted, "msg_signature": "0" * 40}
        ).status_code
        == 403
    )
    for kwargs in ({"receiver": "other-corp"}, {"timestamp": int(time.time()) - 1000}):
        encrypted, params = _encrypted(b"challenge", callback_settings, **kwargs)
        assert (
            client.get(url, params={**params, "echostr": encrypted}).status_code == 403
        )
    assert client.post(url, params=params, content=b"x" * 65537).status_code == 413
    assert (
        client.post(url, params=params, content=b"<!DOCTYPE xml><xml/>").status_code
        == 400
    )


@pytest.mark.parametrize("change", ["delete_user", "create_user", "update_user"])
def test_directory_event_revokes_session_and_requires_manual_reactivation(
    client,
    callback_settings,
    session_for,
    change,
):
    userid = str(uuid4())
    with SessionLocal() as db:
        db.add(User(username=userid, wecom_userid=userid, display_name="member"))
        db.commit()
    session_for(userid)
    assert client.get("/api/v1/auth/me").status_code == 200
    message = (
        f"<xml><ToUserName>{callback_settings.wecom_corp_id}</ToUserName>"
        f"<MsgType>event</MsgType><Event>change_contact</Event><ChangeType>{change}</ChangeType>"
        f"<UserID>{userid}</UserID><Status>2</Status><CreateTime>{int(time.time())}</CreateTime></xml>"
    ).encode()
    encrypted, params = _encrypted(message, callback_settings)
    content = f"<xml><Encrypt><![CDATA[{encrypted}]]></Encrypt></xml>"
    url = "/api/v1/auth/wecom/events"
    assert (
        client.post(
            url, params={**params, "msg_signature": "0" * 40}, content=content
        ).status_code
        == 403
    )
    assert client.get("/api/v1/auth/me").status_code == 200
    assert client.post(url, params=params, content=content).status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 401
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.wecom_userid == userid))
        assert not user.is_active
        user.is_active = (
            True  # Simulate an administrator reviewing and restoring the account.
        )
        db.commit()
    # A duplicate event must not undo an administrator's later recovery.
    encrypted, params = _encrypted(message, callback_settings)
    assert (
        client.post(
            url, params=params, content=f"<xml><Encrypt>{encrypted}</Encrypt></xml>"
        ).status_code
        == 200
    )
    with SessionLocal() as db:
        assert db.scalar(select(User).where(User.wecom_userid == userid)).is_active


def test_only_admin_can_recover_node_token(client, admin_headers, session_for):
    payload = {
        "agent_key": str(uuid4()),
        "name": "recover",
        "enrollment_token": "test-agent-enrollment-token",
    }
    original = client.post("/api/v1/agent/enroll", json=payload).json()
    userid = str(uuid4())
    with SessionLocal() as db:
        db.add(
            User(
                username=userid,
                wecom_userid=userid,
                display_name="op",
                role=UserRole.operator,
                resource_restricted=False,
            )
        )
        db.commit()
    url = f"/api/v1/nodes/{original['node_id']}/agent-token/rotate"
    assert client.post(url, headers=session_for(userid)).status_code == 403
    response = client.post(url, headers=session_for("admin-wecom-id"))
    assert response.status_code == 200
    assert response.json()["agent_key"] == payload["agent_key"]
    for token, expected in (
        (original["node_token"], 401),
        (response.json()["node_token"], 204),
    ):
        assert (
            client.get(
                "/api/v1/agent/tasks/next",
                headers={
                    "X-Node-Id": original["node_id"],
                    "X-Agent-Token": token,
                },
            ).status_code
            == expected
        )


def test_callback_config_requires_complete_valid_key():
    values = get_settings().model_dump()
    with pytest.raises(ValidationError, match="configured together"):
        Settings(**{**values, "wecom_callback_token": "callback-token-test"})
    with pytest.raises(ValidationError, match="AES key"):
        Settings(
            **{
                **values,
                "wecom_callback_token": "callback-token-test",
                "wecom_callback_aes_key": "!" * 43,
            }
        )


def test_callback_disabled_and_unrelated_events_do_not_grant_access(
    client, callback_settings
):
    message = (
        f"<xml><ToUserName>{callback_settings.wecom_corp_id}</ToUserName>"
        "<MsgType>event</MsgType><Event>change_contact</Event>"
        "<ChangeType>create_user</ChangeType><UserID>unprovisioned-event-member</UserID></xml>"
    ).encode()
    encrypted, params = _encrypted(message, callback_settings)
    body = f"<xml><Encrypt>{encrypted}</Encrypt></xml>"
    url = "/api/v1/auth/wecom/events"
    assert client.post(url, params=params, content=body).status_code == 200
    with SessionLocal() as db:
        assert (
            db.scalar(
                select(User).where(User.wecom_userid == "unprovisioned-event-member")
            )
            is None
        )
    client.app.dependency_overrides.pop(get_settings)
    assert client.post(url, params=params, content=body).status_code == 503
