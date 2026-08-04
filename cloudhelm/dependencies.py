from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from cloudhelm.config import Settings, get_settings
from cloudhelm.db import get_db
from cloudhelm.models import Node, User, UserRole, WebSession
from cloudhelm.security import digest_token, verify_secret

SESSION_COOKIE = "__Host-cloudhelm_session"
CSRF_COOKIE = "__Host-cloudhelm_csrf"
OAUTH_COOKIE = "__Host-cloudhelm_oauth"

Db = Annotated[Session, Depends(get_db)]
Config = Annotated[Settings, Depends(get_settings)]


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


@dataclass(frozen=True)
class AuthContext:
    user: User
    session: WebSession


def current_auth(request: Request, db: Db) -> AuthContext:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
    web_session = db.scalar(
        select(WebSession).where(WebSession.token_hash == digest_token(token))
    )
    now = datetime.now(UTC)
    if (
        not web_session
        or web_session.revoked_at is not None
        or _as_utc(web_session.expires_at) <= now
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已过期"
        )
    user = db.get(User, web_session.user_id)
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="账号不可用"
        )
    return AuthContext(user=user, session=web_session)


CurrentAuth = Annotated[AuthContext, Depends(current_auth)]


def current_user(auth: CurrentAuth) -> User:
    return auth.user


CurrentUser = Annotated[User, Depends(current_user)]


def require_operator(user: CurrentUser) -> User:
    if user.role not in (UserRole.admin, UserRole.operator):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="当前账号只有查看权限"
        )
    return user


Operator = Annotated[User, Depends(require_operator)]


def require_admin(user: CurrentUser) -> User:
    if user.role != UserRole.admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限"
        )
    return user


Admin = Annotated[User, Depends(require_admin)]


def agent_node(
    db: Db,
    x_node_id: Annotated[str | None, Header()] = None,
    x_agent_token: Annotated[str | None, Header()] = None,
) -> Node:
    if not x_node_id or not x_agent_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="agent credentials missing"
        )
    node = db.scalar(select(Node).where(Node.id == x_node_id))
    if not node or not verify_secret(x_agent_token, node.agent_token_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid agent credentials"
        )
    return node


AgentNode = Annotated[Node, Depends(agent_node)]


def request_ip(request: Request, settings: Settings) -> str | None:
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",", 1)[0].strip()[:64]
    return request.client.host[:64] if request.client else None
