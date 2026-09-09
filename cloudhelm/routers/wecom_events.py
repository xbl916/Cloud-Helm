"""Authenticated enterprise directory events; never grant access from an event."""

import base64
import binascii
import hashlib
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Annotated
from xml.etree import ElementTree

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from cloudhelm.audit import add_audit
from cloudhelm.dependencies import Config, Db
from cloudhelm.models import User, WebSession, WecomEventReceipt

router = APIRouter(prefix="/auth/wecom", tags=["authentication"])
MAX_BODY = 65536
Signature = Annotated[
    str, Query(min_length=40, max_length=40, pattern=r"^[0-9a-f]{40}$")
]
Timestamp = Annotated[str, Query(min_length=1, max_length=12, pattern=r"^[0-9]+$")]
Nonce = Annotated[str, Query(min_length=1, max_length=256)]


def _xml(data: bytes) -> ElementTree.Element:
    # Reject DTDs/entities and alternate encodings before handing data to XML parsing.
    if (
        len(data) > MAX_BODY
        or b"\x00" in data
        or b"<!" in data.upper().replace(b"<![CDATA[", b"")
    ):
        raise HTTPException(status_code=400, detail="无效的企微事件 XML")
    try:
        return ElementTree.fromstring(data.decode("utf-8"))
    except (ElementTree.ParseError, UnicodeError) as exc:
        raise HTTPException(status_code=400, detail="无效的企微事件 XML") from exc


def _decrypt(
    encrypted: str, signature: str, timestamp: str, nonce: str, settings: Config
) -> bytes:
    if not settings.wecom_callback_token or not settings.wecom_callback_aes_key:
        raise HTTPException(status_code=503, detail="尚未配置企微通讯录回调")
    if abs(time.time() - int(timestamp)) > 600:
        raise HTTPException(status_code=403, detail="企微回调已过期")
    expected = hashlib.sha1(
        "".join(
            sorted([settings.wecom_callback_token, timestamp, nonce, encrypted])
        ).encode()
    ).hexdigest()
    if not secrets.compare_digest(signature, expected):
        raise HTTPException(status_code=403, detail="企微回调签名无效")
    try:
        key = base64.b64decode(settings.wecom_callback_aes_key + "=", validate=True)
        ciphertext = base64.b64decode(encrypted, validate=True)
        decryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        unpadder = padding.PKCS7(256).unpadder()
        plaintext = unpadder.update(padded) + unpadder.finalize()
        if len(plaintext) < 20:
            raise ValueError("short message")
        size = int.from_bytes(plaintext[16:20], "big")
        if size > len(plaintext) - 20:
            raise ValueError("invalid message size")
        message, receiver = plaintext[20 : 20 + size], plaintext[20 + size :]
        if not secrets.compare_digest(receiver, settings.wecom_corp_id.encode()):
            raise ValueError("wrong receiver")
        return message
    except (ValueError, binascii.Error) as exc:
        raise HTTPException(status_code=403, detail="企微回调密文无效") from exc


@router.get("/events", response_class=PlainTextResponse)
def verify_url(
    settings: Config,
    msg_signature: Signature,
    timestamp: Timestamp,
    nonce: Nonce,
    echostr: Annotated[str, Query(min_length=1, max_length=8192)],
) -> PlainTextResponse:
    return PlainTextResponse(
        _decrypt(echostr, msg_signature, timestamp, nonce, settings)
    )


@router.post("/events", response_class=PlainTextResponse)
async def directory_event(
    request: Request,
    db: Db,
    settings: Config,
    msg_signature: Signature,
    timestamp: Timestamp,
    nonce: Nonce,
) -> PlainTextResponse:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY:
            raise HTTPException(status_code=413, detail="企微事件过大")
    envelope = _xml(bytes(body))
    encrypted = envelope.findtext("Encrypt") or ""
    message = _decrypt(encrypted, msg_signature, timestamp, nonce, settings)
    event = _xml(message)
    if event.findtext("ToUserName") != settings.wecom_corp_id:
        raise HTTPException(status_code=403, detail="企微事件企业不匹配")
    if (
        event.findtext("MsgType") != "event"
        or event.findtext("Event") != "change_contact"
    ):
        return PlainTextResponse("success")
    change = event.findtext("ChangeType")
    # Creation with an existing UserId means its previous local grants must not revive.
    disables = change in {"delete_user", "create_user"} or (
        change == "update_user" and event.findtext("Status") in {"2", "4"}
    )
    if not disables:
        return PlainTextResponse("success")
    userid = (event.findtext("UserID") or "").strip()
    if not userid or len(userid) > 128:
        raise HTTPException(status_code=400, detail="企微事件成员无效")
    receipt_id = hashlib.sha256(message).hexdigest()
    if db.get(WecomEventReceipt, receipt_id):
        return PlainTextResponse("success")
    db.add(WecomEventReceipt(id=receipt_id))
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        if db.get(WecomEventReceipt, receipt_id):
            return PlainTextResponse("success")
        raise
    now = datetime.now(UTC)
    user = db.scalar(select(User).where(User.wecom_userid == userid))
    if user:
        user.is_active = False
        db.execute(
            update(WebSession)
            .where(WebSession.user_id == user.id, WebSession.revoked_at.is_(None))
            .values(revoked_at=now, revoke_reason="wecom directory changed")
        )
        add_audit(
            db,
            action="wecom.directory.disable",
            target_type="user",
            target_id=user.id,
            target_name=userid,
            detail=f"change_type={change}",
        )
    # Receipts outlive the signature window. Same event retried with a fresh signature
    # is deduplicated during the normal delivery period as well.
    db.execute(
        delete(WecomEventReceipt).where(
            WecomEventReceipt.created_at < now - timedelta(days=30)
        )
    )
    db.commit()
    return PlainTextResponse("success")
