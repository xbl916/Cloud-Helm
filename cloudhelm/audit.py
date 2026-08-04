from fastapi import Request
from sqlalchemy.orm import Session

from cloudhelm.config import Settings
from cloudhelm.dependencies import request_ip
from cloudhelm.models import AuditLog, User


def add_audit(
    db: Session,
    *,
    action: str,
    target_type: str,
    user: User | None = None,
    target_id: str | None = None,
    target_name: str | None = None,
    success: bool = True,
    detail: str = "",
    request: Request | None = None,
    settings: Settings | None = None,
) -> AuditLog:
    entry = AuditLog(
        user_id=user.id if user else None,
        username=user.username if user else "system",
        action=action,
        target_type=target_type,
        target_id=target_id,
        target_name=target_name,
        success=success,
        detail=detail[:4000],
        ip_address=request_ip(request, settings) if request and settings else None,
    )
    db.add(entry)
    return entry
