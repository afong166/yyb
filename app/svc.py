"""共享服务：审计、授权码生成/发放/更新。"""
from __future__ import annotations

import secrets

from . import db, models

_LIC_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def audit(action: str, target_type: str = "", target_id: str = "", detail: str = "", ip: str = "") -> None:
    db.execute(
        "INSERT INTO audit(admin_id,action,target_type,target_id,detail,ip,created_at) VALUES(0,?,?,?,?,?,?)",
        (action, target_type, str(target_id), (detail or "")[:500], ip, db.now()),
    )


def gen_license_key() -> str:
    seg = lambda: "".join(secrets.choice(_LIC_ALPHABET) for _ in range(4))
    return "-".join(seg() for _ in range(4))


def issue_license(user_id: int, max_users: int, note: str = "", expires_at: int = 0, key: str | None = None):
    if not db.query_one("SELECT id FROM users WHERE id=?", (user_id,)):
        raise ValueError("user not found")
    if db.query_one("SELECT id FROM licenses WHERE user_id=?", (user_id,)):
        raise ValueError("user already has a license")
    if max_users < 1:
        raise ValueError("maxUsers must be a positive integer")
    for _ in range(6):
        k = (key or gen_license_key()).upper()
        if not db.query_one("SELECT id FROM licenses WHERE license_key=? COLLATE NOCASE", (k,)):
            db.execute(
                "INSERT INTO licenses(user_id,license_key,max_users,status,note,expires_at,created_at)"
                " VALUES(?,?,?,'active',?,?,?)",
                (user_id, k, max_users, (note or "")[:200], expires_at or 0, db.now()),
            )
            return db.query_one("SELECT * FROM licenses WHERE user_id=?", (user_id,))
        if key:
            raise ValueError("license key already exists")
    raise ValueError("license key already exists")


def update_license(lic, *, max_users=None, note=None, status=None, expires_at=None):
    sets, params = [], []
    if max_users is not None:
        if max_users < models.used_count(lic["user_id"]):
            raise ValueError(f"maxUsers ({max_users}) less than current bound count ({models.used_count(lic['user_id'])})")
        sets.append("max_users=?"); params.append(int(max_users))
    if note is not None:
        sets.append("note=?"); params.append((note or "")[:200])
    if status is not None:
        st = "active" if status == "active" else "disabled"
        sets.append("status=?"); params.append(st)
    if expires_at is not None:
        sets.append("expires_at=?"); params.append(int(expires_at) or 0)
    if sets:
        db.execute(f"UPDATE licenses SET {','.join(sets)} WHERE id=?", tuple(params) + (lic["id"],))
    return db.query_one("SELECT * FROM licenses WHERE id=?", (lic["id"],))
