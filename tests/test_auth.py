from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from cloudhelm.dependencies import CSRF_COOKIE, OAUTH_COOKIE, SESSION_COOKIE
from cloudhelm.routers import auth


def test_wecom_oauth_issues_server_session_and_blocks_replay(
    client: TestClient, monkeypatch
):
    started = client.get("/api/v1/auth/wecom/start", follow_redirects=False)
    assert started.status_code == 302
    location = started.headers["location"]
    query = parse_qs(urlsplit(location).query)
    state = query["state"][0]
    assert query["appid"] == ["ww-test-corp"]
    assert query["agentid"] == ["1000002"]
    assert query["scope"] == ["snsapi_base"]
    assert client.cookies.get(OAUTH_COOKIE) == state

    async def fake_identity(code, settings):
        assert code == "single-use-code"
        return "admin-wecom-id"

    monkeypatch.setattr(auth, "resolve_wecom_userid", fake_identity)
    callback = client.get(
        "/api/v1/auth/wecom/callback",
        params={"code": "single-use-code", "state": state},
        follow_redirects=False,
    )
    assert callback.status_code == 303
    assert callback.headers["location"] == "/"
    assert client.cookies.get(SESSION_COOKIE)
    assert client.cookies.get(CSRF_COOKIE)
    assert "HttpOnly" in callback.headers.get_list("set-cookie")[1]
    assert client.get("/api/v1/auth/me").status_code == 200

    client.cookies.set(OAUTH_COOKIE, state, path="/")
    replay = client.get(
        "/api/v1/auth/wecom/callback",
        params={"code": "single-use-code", "state": state},
    )
    assert replay.status_code == 403


def test_csrf_and_origin_are_required_for_browser_writes(
    client: TestClient, admin_headers: dict[str, str]
):
    assert client.post("/api/v1/auth/logout").status_code == 403
    assert (
        client.post(
            "/api/v1/auth/logout",
            headers={**admin_headers, "Origin": "https://evil.example"},
        ).status_code
        == 403
    )
    assert client.post("/api/v1/auth/logout", headers=admin_headers).status_code == 204
    assert client.get("/api/v1/auth/me").status_code == 401


def test_password_login_endpoint_does_not_exist(client: TestClient):
    assert client.get("/api/v1/auth/login").status_code == 404


def test_wecom_miniprogram_issues_bearer_session(
    client: TestClient, monkeypatch
):
    async def fake_identity(code, settings):
        assert code == "mini-program-code"
        assert settings.wecom_corp_id == "ww-test-corp"
        return "admin-wecom-id"

    monkeypatch.setattr(auth, "resolve_wecom_miniprogram_userid", fake_identity)
    logged_in = client.post(
        "/api/v1/auth/wecom-mini/login",
        json={"code": "mini-program-code"},
    )
    assert logged_in.status_code == 200
    payload = logged_in.json()
    assert payload["token_type"] == "Bearer"
    assert payload["expires_in"] > 0
    assert payload["user"]["wecom_userid"] == "admin-wecom-id"
    assert client.cookies.get(SESSION_COOKIE) is None

    headers = {"Authorization": f"Bearer {payload['access_token']}"}
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 200
    assert client.get("/api/v1/dashboard", headers=headers).status_code == 200
    assert client.post("/api/v1/auth/logout", headers=headers).status_code == 204
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 401


def test_wecom_miniprogram_rejects_unbound_member(client: TestClient, monkeypatch):
    async def fake_identity(code, settings):
        return "not-bound"

    monkeypatch.setattr(auth, "resolve_wecom_miniprogram_userid", fake_identity)
    denied = client.post(
        "/api/v1/auth/wecom-mini/login",
        json={"code": "mini-program-code"},
    )
    assert denied.status_code == 403
    assert "尚未获得" in denied.json()["detail"]


def test_malformed_bearer_does_not_fall_back_to_browser_cookie(
    client: TestClient, admin_headers: dict[str, str]
):
    response = client.get(
        "/api/v1/auth/me",
        headers={**admin_headers, "Authorization": "not-bearer"},
    )
    assert response.status_code == 401
