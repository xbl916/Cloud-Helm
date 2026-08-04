import hashlib
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import delete, select, update

from cloudhelm.audit import add_audit
from cloudhelm.dependencies import (
    CSRF_COOKIE,
    OAUTH_COOKIE,
    SESSION_COOKIE,
    Config,
    CurrentAuth,
    CurrentUser,
    Db,
    request_ip,
)
from cloudhelm.models import OAuthState, User, WebSession
from cloudhelm.schemas import UserOut
from cloudhelm.security import digest_token, new_opaque_token, safe_relative_path

router = APIRouter(prefix="/auth", tags=["authentication"])
_token_cache: dict[tuple[str, str], tuple[str, float]] = {}
_token_cache_lock = threading.Lock()
_start_attempts: dict[str, deque[float]] = defaultdict(deque)
_start_attempts_lock = threading.Lock()


def _rate_limit_oauth_start(peer: str) -> None:
    now = time.monotonic()
    with _start_attempts_lock:
        attempts = _start_attempts[peer]
        while attempts and now - attempts[0] > 300:
            attempts.popleft()
        if len(attempts) >= 20:
            raise HTTPException(status_code=429, detail="登录请求过多，请稍后再试")
        attempts.append(now)


def _wecom_error(message: str, status_code: int = 403) -> HTMLResponse:
    body = (
        "<!doctype html><meta charset='utf-8'><meta name='viewport' "
        "content='width=device-width,initial-scale=1'><title>云舵登录失败</title>"
        "<style>body{font-family:system-ui;margin:0;background:#f3f7f8;color:#142a35}"
        "main{max-width:420px;margin:16vh auto;padding:28px;background:white;"
        "border-radius:18px;box-shadow:0 12px 40px #16334218}h1{font-size:22px}"
        "p{line-height:1.7;color:#607680}</style><main><h1>无法进入云舵</h1>"
        f"<p>{message}</p><p>请从企业微信工作台重新打开，或联系管理员检查成员绑定。</p></main>"
    )
    return HTMLResponse(body, status_code=status_code)


def _cached_access_token(settings: Config) -> str | None:
    key = (
        settings.wecom_corp_id,
        hashlib.sha256(settings.wecom_secret.encode()).hexdigest(),
    )
    with _token_cache_lock:
        cached = _token_cache.get(key)
        if cached and cached[1] > time.monotonic():
            return cached[0]
    return None


def _store_access_token(settings: Config, token: str, expires_in: int) -> None:
    key = (
        settings.wecom_corp_id,
        hashlib.sha256(settings.wecom_secret.encode()).hexdigest(),
    )
    ttl = max(0, min(expires_in - 60, 7000))
    if ttl == 0:
        return
    with _token_cache_lock:
        _token_cache[key] = (token, time.monotonic() + ttl)


async def _get_access_token(settings: Config) -> str:
    cached = _cached_access_token(settings)
    if cached:
        return cached
    try:
        async with httpx.AsyncClient(timeout=settings.wecom_api_timeout_seconds) as client:
            response = await client.get(
                f"{settings.wecom_api_base}/cgi-bin/gettoken",
                params={"corpid": settings.wecom_corp_id, "corpsecret": settings.wecom_secret},
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="企业微信认证服务暂不可用") from exc
    if payload.get("errcode") != 0 or not payload.get("access_token"):
        raise HTTPException(status_code=502, detail="企业微信应用凭据校验失败")
    token = str(payload["access_token"])
    _store_access_token(settings, token, int(payload.get("expires_in", 7200)))
    return token


async def resolve_wecom_userid(code: str, settings: Config) -> str:
    access_token = await _get_access_token(settings)
    try:
        async with httpx.AsyncClient(timeout=settings.wecom_api_timeout_seconds) as client:
            response = await client.get(
                f"{settings.wecom_api_base}/cgi-bin/auth/getuserinfo",
                params={"access_token": access_token, "code": code},
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="企业微信身份读取失败") from exc
    if payload.get("errcode") != 0:
        raise HTTPException(status_code=401, detail="企业微信授权码无效或已使用")
    user_id = payload.get("userid") or payload.get("UserId")
    if not isinstance(user_id, str) or not user_id.strip():
        raise HTTPException(status_code=403, detail="仅允许企业内部成员访问")
    return user_id.strip()


@router.get("/wecom/start")
def wecom_start(
    request: Request,
    db: Db,
    settings: Config,
    next_path: str | None = Query(default=None, alias="next", max_length=512),
) -> RedirectResponse:
    _rate_limit_oauth_start(request_ip(request, settings) or "unknown")
    raw_state = secrets.token_hex(32)
    now = datetime.now(UTC)
    db.execute(delete(OAuthState).where(OAuthState.expires_at < now))
    db.execute(
        delete(WebSession).where(WebSession.expires_at < now - timedelta(days=7))
    )
    db.add(
        OAuthState(
            state_hash=digest_token(raw_state),
            next_path=safe_relative_path(next_path),
            expires_at=now + timedelta(seconds=settings.oauth_state_seconds),
        )
    )
    db.commit()
    callback = f"{settings.public_base_url}/api/v1/auth/wecom/callback"
    query = urlencode(
        {
            "appid": settings.wecom_corp_id,
            "redirect_uri": callback,
            "response_type": "code",
            "scope": "snsapi_base",
            "state": raw_state,
            "agentid": settings.wecom_agent_id,
        }
    )
    response = RedirectResponse(
        f"https://open.weixin.qq.com/connect/oauth2/authorize?{query}#wechat_redirect",
        status_code=302,
    )
    response.set_cookie(
        OAUTH_COOKIE,
        raw_state,
        max_age=settings.oauth_state_seconds,
        secure=True,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return response


@router.get("/wecom/callback")
async def wecom_callback(
    request: Request,
    db: Db,
    settings: Config,
    code: str = Query(min_length=1, max_length=512),
    state: str = Query(min_length=20, max_length=256),
) -> Response:
    cookie_state = request.cookies.get(OAUTH_COOKIE)
    if not cookie_state or not secrets.compare_digest(cookie_state, state):
        return _wecom_error("登录请求校验失败，请重新发起登录。")
    now = datetime.now(UTC)
    state_hash = digest_token(state)
    oauth_state = db.scalar(select(OAuthState).where(OAuthState.state_hash == state_hash))
    if (
        not oauth_state
        or oauth_state.used_at is not None
        or (oauth_state.expires_at.replace(tzinfo=UTC) if oauth_state.expires_at.tzinfo is None else oauth_state.expires_at) <= now
    ):
        return _wecom_error("登录请求已经过期或已被使用，请重新发起登录。")
    consumed = db.execute(
        update(OAuthState)
        .where(OAuthState.id == oauth_state.id, OAuthState.used_at.is_(None))
        .values(used_at=now)
    )
    db.commit()
    if consumed.rowcount != 1:
        return _wecom_error("登录请求已经被使用，请重新发起登录。")

    try:
        wecom_userid = await resolve_wecom_userid(code, settings)
    except HTTPException as exc:
        return _wecom_error(str(exc.detail), exc.status_code)
    user = db.scalar(select(User).where(User.wecom_userid == wecom_userid))
    if not user or not user.is_active:
        add_audit(
            db,
            action="wecom.login.denied",
            target_type="session",
            target_name=wecom_userid,
            success=False,
            detail="member is not bound or is disabled",
            request=request,
            settings=settings,
        )
        db.commit()
        return _wecom_error("当前企业微信成员尚未获得云舵访问权限。")

    active_sessions = list(
        db.scalars(
            select(WebSession)
            .where(
                WebSession.user_id == user.id,
                WebSession.revoked_at.is_(None),
                WebSession.expires_at > now,
            )
            .order_by(WebSession.created_at.desc())
        ).all()
    )
    for old_session in active_sessions[settings.max_sessions_per_user - 1 :]:
        old_session.revoked_at = now
        old_session.revoke_reason = "session limit"

    raw_session = new_opaque_token()
    raw_csrf = new_opaque_token()
    expires_at = now + timedelta(minutes=settings.session_minutes)
    db.add(
        WebSession(
            token_hash=digest_token(raw_session),
            csrf_token_hash=digest_token(raw_csrf),
            user_id=user.id,
            expires_at=expires_at,
            ip_address=request_ip(request, settings),
            user_agent=request.headers.get("user-agent", "")[:512] or None,
        )
    )
    user.last_login_at = now
    add_audit(
        db,
        action="wecom.login",
        target_type="session",
        user=user,
        target_name=user.username,
        request=request,
        settings=settings,
    )
    db.commit()
    response = RedirectResponse(oauth_state.next_path, status_code=303)
    response.delete_cookie(OAUTH_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    response.set_cookie(
        SESSION_COOKIE,
        raw_session,
        max_age=settings.session_minutes * 60,
        secure=True,
        httponly=True,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE,
        raw_csrf,
        max_age=settings.session_minutes * 60,
        secure=True,
        httponly=False,
        samesite="strict",
        path="/",
    )
    return response


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    request: Request,
    response: Response,
    db: Db,
    settings: Config,
    auth: CurrentAuth,
) -> None:
    auth.session.revoked_at = datetime.now(UTC)
    auth.session.revoke_reason = "logout"
    add_audit(
        db,
        action="logout",
        target_type="session",
        user=auth.user,
        target_name=auth.user.username,
        request=request,
        settings=settings,
    )
    db.commit()
    response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    response.delete_cookie(CSRF_COOKIE, path="/", secure=True, httponly=False, samesite="strict")


@router.get("/me", response_model=UserOut)
def me(user: CurrentUser) -> User:
    return user
