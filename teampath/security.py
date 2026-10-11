"""Password hashing, tokens, MFA, encryption at rest and signed URLs."""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import secrets
import time

import pyotp
import qrcode
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet

from .config import get_settings

_ph = PasswordHasher()  # Argon2id with library defaults (memory-hard)

# A short list of very common passwords; the optional HIBP check covers the rest.
_COMMON = {
    "password", "password1", "password123", "123456789012", "qwertyuiop12", "letmein12345",
    "iloveyou1234", "welcome12345", "changeme1234", "insurance123", "admin1234567",
}


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    if not password_hash:
        # Spend comparable time so missing accounts are not distinguishable by timing.
        _ph.hash(password)
        return False
    try:
        return _ph.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_problems(password: str, email: str = "", name: str = "") -> list[str]:
    s = get_settings()
    problems = []
    if len(password) < s.password_min_length:
        problems.append(f"Use at least {s.password_min_length} characters.")
    if password.lower() in _COMMON:
        problems.append("That password is too common.")
    local = email.split("@")[0].lower() if email else ""
    if local and len(local) >= 4 and local in password.lower():
        problems.append("Don't include your email address in the password.")
    if s.hibp_check and not problems and _pwned(password):
        problems.append("That password has appeared in a data breach. Choose another.")
    return problems


def _pwned(password: str) -> bool:
    """k-anonymity check: only the first 5 chars of the SHA-1 leave the server."""
    import httpx

    digest = hashlib.sha1(password.encode()).hexdigest().upper()
    try:
        r = httpx.get(f"https://api.pwnedpasswords.com/range/{digest[:5]}", timeout=3)
        return any(line.split(":")[0] == digest[5:] for line in r.text.splitlines())
    except Exception:
        return False  # fail open; the length and common-list rules still apply


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ------------------------------------------------------------ encryption at rest

def _fernet() -> Fernet:
    key = get_settings().encryption_key
    if not key:
        # Development fallback derived from the secret key. Production refuses to
        # start without ENCRYPTION_KEY (see config.get_settings).
        key = base64.urlsafe_b64encode(hashlib.sha256(get_settings().secret_key.encode()).digest()).decode()
    return Fernet(key.encode())


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    return _fernet().decrypt(value.encode()).decode()


# ------------------------------------------------------------ MFA (TOTP)

def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_uri(secret: str, email: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=get_settings().app_name)


def verify_totp(secret: str, code: str) -> bool:
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit() or len(code) != 6:
        return False
    return pyotp.TOTP(secret).verify(code, valid_window=1)


def qr_png_base64(data: str) -> str:
    img = qrcode.make(data)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


# ------------------------------------------------------------ signed URLs

def _sign(message: str) -> str:
    key = get_settings().secret_key.encode()
    return base64.urlsafe_b64encode(hmac.new(key, message.encode(), hashlib.sha256).digest()).decode().rstrip("=")


def sign_file_url(file_id: str, user_id: str, variant: str = "orig", ttl: int = 300) -> str:
    """Short-lived URL for one user to fetch one file (or one preview page)."""
    exp = int(time.time()) + ttl
    msg = f"{file_id}|{user_id}|{variant}|{exp}"
    return f"/files/{file_id}?u={user_id}&v={variant}&e={exp}&s={_sign(msg)}"


def verify_file_signature(file_id: str, user_id: str, variant: str, exp: int, sig: str) -> bool:
    if exp < time.time():
        return False
    expected = _sign(f"{file_id}|{user_id}|{variant}|{exp}")
    return hmac.compare_digest(expected, sig or "")
