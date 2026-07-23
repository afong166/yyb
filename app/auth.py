"""鉴权：bcrypt 密码、HMAC 签名会话 cookie（用户/管理员）、授权码校验、secretBox（面板密钥加密）。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import bcrypt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import db
from .config import SECRET_KEY, ADMIN_TOKEN

USER_COOKIE = "yyb_sess"
ADMIN_COOKIE = "yyb_admin"
SESSION_TTL = 30 * 24 * 3600

_KEY = hashlib.sha256(SECRET_KEY.encode()).digest()


# ---------------- 密码 ----------------
def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode("utf-8"), hashed.encode("ascii"))
    except Exception:
        return False


# ---------------- 签名令牌（会话 cookie） ----------------
def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign_token(data: dict, ttl: int = SESSION_TTL) -> str:
    payload = {**data, "exp": int(time.time()) + ttl}
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = _b64e(hmac.new(_KEY, body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify_token(token: str) -> dict | None:
    if not token or "." not in token:
        return None
    body, sig = token.rsplit(".", 1)
    expect = _b64e(hmac.new(_KEY, body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expect):
        return None
    try:
        data = json.loads(_b64d(body))
    except Exception:
        return None
    if int(data.get("exp", 0)) < int(time.time()):
        return None
    return data


# ---------------- 授权码 ----------------
def check_admin_token(token: str) -> bool:
    return bool(token) and hmac.compare_digest(token, ADMIN_TOKEN)


def get_admin_password_hash() -> str:
    row = db.query_one("SELECT value FROM admin_config WHERE key='password_hash'")
    return row["value"] if row else ""


def set_admin_password(pw: str) -> None:
    h = hash_password(pw)
    db.execute(
        "INSERT INTO admin_config(key,value) VALUES('password_hash',?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (h,),
    )


# ---------------- secretBox（面板密钥等敏感字段加密存库） ----------------
def secretbox_encrypt(plaintext: str) -> str:
    if not plaintext:
        return ""
    import os
    iv = os.urandom(12)
    ct = AESGCM(_KEY).encrypt(iv, plaintext.encode("utf-8"), b"")
    return _b64e(iv + ct)


def secretbox_decrypt(blob: str) -> str:
    if not blob:
        return ""
    try:
        raw = _b64d(blob)
        return AESGCM(_KEY).decrypt(raw[:12], raw[12:], b"").decode("utf-8")
    except Exception:
        return ""
