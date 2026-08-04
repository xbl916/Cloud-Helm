from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
import secrets

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from sqlalchemy import select

from cloudhelm.config import get_settings
from cloudhelm.db import SessionLocal, initialize_database
from cloudhelm.dependencies import CSRF_COOKIE, SESSION_COOKIE
from cloudhelm.models import User, UserRole, WebSession
from cloudhelm.routers import agent, api, auth
from cloudhelm.security import digest_token

settings = get_settings()


def bootstrap_admin() -> None:
    with SessionLocal() as db:
        user = db.scalar(
            select(User).where(
                User.wecom_userid == settings.bootstrap_admin_wecom_userid
            )
        )
        if user:
            return
        db.add(
            User(
                username=settings.bootstrap_admin_wecom_userid,
                wecom_userid=settings.bootstrap_admin_wecom_userid,
                display_name=settings.bootstrap_admin_display_name,
                role=UserRole.admin,
                resource_restricted=False,
            )
        )
        db.commit()


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    bootstrap_admin()
    yield


app = FastAPI(
    title=settings.app_name,
    version="0.3.0",
    docs_url="/api/docs" if settings.environment != "production" else None,
    redoc_url=None,
    lifespan=lifespan,
)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=[settings.public_host, "127.0.0.1", "localhost"],
)
app.include_router(auth.router, prefix="/api/v1")
app.include_router(agent.router, prefix="/api/v1")
app.include_router(api.router, prefix="/api/v1")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    if (
        request.method in {"POST", "PUT", "PATCH", "DELETE"}
        and request.url.path.startswith("/api/v1/")
        and not request.url.path.startswith("/api/v1/agent/")
    ):
        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        valid_source = origin == settings.public_base_url or (
            not origin
            and referer is not None
            and (
                referer == settings.public_base_url
                or referer.startswith(f"{settings.public_base_url}/")
            )
        )
        session_token = request.cookies.get(SESSION_COOKIE, "")
        csrf_token = request.headers.get("x-csrf-token", "")
        cookie_csrf = request.cookies.get(CSRF_COOKIE, "")
        valid_csrf = bool(csrf_token) and bool(cookie_csrf) and secrets.compare_digest(
            csrf_token, cookie_csrf
        )
        if valid_source and valid_csrf and session_token:
            with SessionLocal() as db:
                web_session = db.scalar(
                    select(WebSession).where(
                        WebSession.token_hash == digest_token(session_token)
                    )
                )
                expires_at = web_session.expires_at if web_session else None
                if expires_at is not None and expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=UTC)
                valid_csrf = bool(
                    web_session
                    and web_session.revoked_at is None
                    and expires_at
                    and expires_at > datetime.now(UTC)
                    and secrets.compare_digest(
                        web_session.csrf_token_hash, digest_token(csrf_token)
                    )
                )
        else:
            valid_csrf = False
        if not valid_source or not valid_csrf:
            return JSONResponse(
                status_code=403, content={"detail": "请求来源或安全校验无效"}
            )
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    if settings.environment == "production":
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict[str, str]:
    return {"status": "ok"}


static_dir = Path(settings.static_dir)
app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")


@app.get("/{path:path}", include_in_schema=False)
def frontend(path: str) -> FileResponse:
    if path.startswith("api/"):
        raise HTTPException(status_code=404, detail="not found")
    candidate = (static_dir / path).resolve()
    if path and candidate.is_relative_to(static_dir.resolve()) and candidate.is_file():
        return FileResponse(candidate)
    return FileResponse(static_dir / "index.html")
