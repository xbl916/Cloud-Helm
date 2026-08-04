import base64
import hashlib
import hmac
import os
import secrets
from urllib.parse import unquote, urlsplit


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_secret(secret: str) -> str:
    salt = os.urandom(16)
    result = hashlib.scrypt(secret.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${_b64(salt)}${_b64(result)}"


def verify_secret(secret: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$", 5)
        if algorithm != "scrypt":
            return False
        actual = hashlib.scrypt(
            secret.encode(), salt=_unb64(salt), n=int(n), r=int(r), p=int(p), dklen=32
        )
        return hmac.compare_digest(actual, _unb64(expected))
    except (ValueError, TypeError):
        return False


def new_opaque_token() -> str:
    return secrets.token_urlsafe(32)


def digest_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def safe_relative_path(value: str | None) -> str:
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    decoded = unquote(value)
    parsed = urlsplit(decoded)
    if (
        decoded.startswith("//")
        or "\\" in decoded
        or parsed.scheme
        or parsed.netloc
        or any(ord(character) < 32 for character in decoded)
    ):
        return "/"
    return value[:512]
