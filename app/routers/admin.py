"""/api/admin/* — 管理后台。除 login/logout 外均需管理员会话。"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request, Response

from .. import auth, db, deps, models, ratelimit, svc
from ..util import get_body, set_admin_cookie, clear_admin_cookie, client_ip

router = APIRouter()


def _guard(request: Request):
    deps.require_admin(request)


# ---------------- 登录 ----------------
@router.post("/login")
async def login(request: Request, response: Response):
    ip = client_ip(request)
    rl_key = f"admin-login:{ip}"
    wait = ratelimit.locked_seconds(rl_key)
    if wait:
        raise HTTPException(429, f"登录尝试过于频繁，请 {wait} 秒后再试")
    b = await get_body(request)
    if (b.get("username") or "").strip().lower() != "admin" or \
            not auth.verify_password(b.get("password") or "", auth.get_admin_password_hash()):
        ratelimit.record_fail(rl_key)
        raise HTTPException(401, "用户名或密码错误")
    ratelimit.clear(rl_key)
    db.execute("INSERT INTO admin_config(key,value) VALUES('admin_last_login',?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(db.now()),))
    set_admin_cookie(response)
    svc.audit("admin-login", ip=client_ip(request))
    return {"success": True, "admin": models.admin_view()}


@router.post("/logout")
async def logout(response: Response):
    clear_admin_cookie(response)
    return {"success": True}


@router.get("/me")
async def me(request: Request):
    _guard(request)
    return {"success": True, "admin": models.admin_view()}


@router.post("/change-password")
async def change_password(request: Request):
    _guard(request)
    b = await get_body(request)
    pw = b.get("password") or ""
    if len(pw) < 8:
        raise HTTPException(400, "管理员密码至少 8 位")
    auth.set_admin_password(pw)
    svc.audit("admin-change-password", ip=client_ip(request))
    return {"success": True}


# ---------------- 临时诊断：确认 CDN 传来的真实 IP 头（用完请删） ----------------
_IP_HEADER_CANDIDATES = (
    "eo-client-ip", "eo-connecting-ip", "x-forwarded-for", "x-real-ip",
    "cf-connecting-ip", "x-client-ip", "true-client-ip", "remote-host",
)
_REDACT = ("cookie", "authorization", "x-admin-token", "x-license-key")


@router.get("/debug/headers")
async def debug_headers(request: Request):
    """仅管理员：回显本次请求的头部与解析出的客户端 IP，用于确认 EdgeOne 注入的真实 IP 头名。"""
    _guard(request)
    headers = {}
    for k, v in request.headers.items():
        headers[k] = "<redacted>" if k.lower() in _REDACT else v
    candidates = {k: request.headers.get(k) for k in _IP_HEADER_CANDIDATES if request.headers.get(k)}
    return {
        "success": True,
        "peerIp": request.client.host if request.client else "",   # 直连对端（走 CDN 时应是 EdgeOne 节点）
        "resolvedClientIp": client_ip(request),                    # 当前配置下程序实际记录的 IP
        "realIpCandidates": candidates,                            # 命中的真实 IP 候选头（挑单值可信的那个）
        "allHeaders": headers,
        "note": "确认真实用户 IP 落在哪个头后，设 YYB_CLIENT_IP_HEADER=该头名；确认完请删除本端点。",
    }


# ---------------- 统计 ----------------
@router.get("/stats")
async def stats(request: Request):
    _guard(request)
    from .. import codebridge
    tot = db.query_one("SELECT COUNT(*) c FROM users")["c"]
    pend = db.query_one("SELECT COUNT(*) c FROM users WHERE status='pending'")["c"]
    act = db.query_one("SELECT COUNT(*) c FROM users WHERE status='active'")["c"]
    lic = db.query_one("SELECT COUNT(*) c FROM licenses")["c"]
    recent = db.query_one("SELECT COUNT(*) c FROM (SELECT id FROM call_records ORDER BY id DESC LIMIT 500)")["c"]
    return {"success": True, "users": {"total": tot, "pending": pend, "active": act},
            "licenses": lic, "recentCalls": recent,
            "sessionPool": codebridge.session_pool_stats()}


# ---------------- 用户 ----------------
@router.get("/users")
async def users(request: Request):
    _guard(request)
    status = request.query_params.get("status")
    if status:
        rows = db.query("SELECT * FROM users WHERE status=? ORDER BY id DESC", (status,))
    else:
        rows = db.query("SELECT * FROM users ORDER BY id DESC")
    # 一次性取全部授权码 + 每用户微信账号计数，避免逐用户 N+1（原为 1+2×用户数 条查询）
    lic_map = {l["user_id"]: l for l in db.query("SELECT * FROM licenses")}
    cnt_map = {r["user_id"]: r["c"] for r in db.query("SELECT user_id, COUNT(*) c FROM accounts GROUP BY user_id")}
    out = []
    for u in rows:
        lic = lic_map.get(u["id"])
        uc = cnt_map.get(u["id"], 0)
        v = models.public_user(u)
        v["license"] = ({"key": lic["license_key"], "maxUsers": lic["max_users"],
                         "status": lic["status"], "usedCount": uc} if lic else None)
        v["wechatCount"] = uc
        out.append(v)
    return {"success": True, "users": out}


@router.get("/users/{uid}")
async def user_detail(request: Request, uid: int):
    _guard(request)
    u = db.query_one("SELECT * FROM users WHERE id=?", (uid,))
    if not u:
        raise HTTPException(404, "user not found")
    lic = db.query_one("SELECT * FROM licenses WHERE user_id=?", (uid,))
    accs = db.query("SELECT * FROM accounts WHERE user_id=? ORDER BY logged_at DESC", (uid,))
    calls = db.query("SELECT * FROM call_records WHERE user_id=? ORDER BY id DESC LIMIT 100", (uid,))
    cc = db.query_one("SELECT COUNT(*) c FROM call_records WHERE user_id=?", (uid,))["c"]
    pnames = {p["id"]: p["name"] for p in db.query("SELECT id, name FROM projects")}
    call_list = []
    for c in calls:
        d = dict(c)
        d["projectName"] = pnames.get(c["project_id"], "") if c["project_id"] else ""
        call_list.append(d)
    return {"success": True, "user": models.public_user(u), "license": models.license_view(lic),
            "wechatAccounts": [dict(a) for a in accs], "callRecords": call_list, "callCount": cc}


def _set_status(uid: int, status: str, action: str, request: Request):
    _guard(request)
    u = db.query_one("SELECT * FROM users WHERE id=?", (uid,))
    if not u:
        raise HTTPException(404, "user not found")
    if status == "active" and not u["approved_at"]:
        db.execute("UPDATE users SET status='active', approved_at=? WHERE id=?", (db.now(), uid))
    else:
        db.execute("UPDATE users SET status=? WHERE id=?", (status, uid))
    svc.audit(action, "user", uid, ip=client_ip(request))
    u = db.query_one("SELECT * FROM users WHERE id=?", (uid,))
    return {"success": True, "user": models.public_user(u)}


@router.post("/users/{uid}/approve")
async def approve(request: Request, uid: int):
    return _set_status(uid, "active", "user-approve", request)


@router.post("/users/{uid}/enable")
async def enable(request: Request, uid: int):
    return _set_status(uid, "active", "user-enable", request)


@router.post("/users/{uid}/disable")
async def disable(request: Request, uid: int):
    return _set_status(uid, "disabled", "user-disable", request)


@router.delete("/users/{uid}")
async def delete_user(request: Request, uid: int):
    _guard(request)
    u = db.query_one("SELECT * FROM users WHERE id=?", (uid,))
    if not u:
        raise HTTPException(404, "user not found")
    for t in ("call_records", "accounts", "panels", "licenses"):
        db.execute(f"DELETE FROM {t} WHERE user_id=?", (uid,))
    db.execute("DELETE FROM users WHERE id=?", (uid,))
    svc.audit("user-delete", "user", uid, detail=u["username"], ip=client_ip(request))
    return {"success": True}


@router.post("/users/{uid}/reset-password")
async def reset_password(request: Request, uid: int):
    _guard(request)
    b = await get_body(request)
    pw = b.get("password") or ""
    if len(pw) < 6:
        raise HTTPException(400, "密码至少 6 位")
    if not db.query_one("SELECT id FROM users WHERE id=?", (uid,)):
        raise HTTPException(404, "user not found")
    db.execute("UPDATE users SET password_hash=? WHERE id=?", (auth.hash_password(pw), uid))
    svc.audit("user-reset-password", "user", uid, ip=client_ip(request))
    return {"success": True}


# ---------------- 授权码（挂在用户下） ----------------
# 注意：URL 段用 authcode 而非 license，规避 nginx/WAF 对含 "license" 路径的拦截规则。
@router.post("/users/{uid}/authcode")
async def issue(request: Request, uid: int):
    _guard(request)
    b = await get_body(request)
    lic = db.query_one("SELECT * FROM licenses WHERE user_id=?", (uid,))
    try:
        if lic:
            lic = svc.update_license(lic, max_users=b.get("maxUsers"), note=b.get("note"),
                                     expires_at=b.get("expiresAt"))
            svc.audit("license-update", "license", lic["license_key"], ip=client_ip(request))
        else:
            lic = svc.issue_license(uid, int(b.get("maxUsers") or 1), b.get("note") or "",
                                    int(b.get("expiresAt") or 0))
            svc.audit("license-issue", "license", lic["license_key"], ip=client_ip(request))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"success": True, "license": models.license_view(lic)}


@router.post("/users/{uid}/authcode/status")
async def license_status(request: Request, uid: int):
    _guard(request)
    b = await get_body(request)
    lic = db.query_one("SELECT * FROM licenses WHERE user_id=?", (uid,))
    if not lic:
        raise HTTPException(404, "该用户无授权码")
    lic = svc.update_license(lic, status=b.get("status"))
    svc.audit("license-status", "license", lic["license_key"], ip=client_ip(request))
    return {"success": True, "license": models.license_view(lic)}


@router.delete("/users/{uid}/authcode")
async def delete_license(request: Request, uid: int):
    _guard(request)
    lic = db.query_one("SELECT * FROM licenses WHERE user_id=?", (uid,))
    if not lic:
        raise HTTPException(404, "该用户无授权码")
    db.execute("DELETE FROM licenses WHERE id=?", (lic["id"],))
    svc.audit("license-delete", "license", lic["license_key"], ip=client_ip(request))
    return {"success": True}


@router.get("/authcodes")
async def licenses(request: Request):
    _guard(request)
    rows = db.query("SELECT * FROM licenses ORDER BY id DESC")
    return {"success": True, "licenses": [models.license_view(r) for r in rows]}


@router.get("/call-records")
async def call_records(request: Request):
    _guard(request)
    limit = min(int(request.query_params.get("limit") or 200), 1000)
    offset = int(request.query_params.get("offset") or 0)
    uid = int(request.query_params.get("userId") or 0)
    if uid > 0:
        rows = db.query("SELECT * FROM call_records WHERE user_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
                        (uid, limit, offset))
    else:
        rows = db.query("SELECT * FROM call_records ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset))
    # 用 id→名称 映射一次性补全用户名 / 项目名，避免逐行查询
    unames = {r["id"]: r["username"] for r in db.query("SELECT id, username FROM users")}
    pnames = {r["id"]: r["name"] for r in db.query("SELECT id, name FROM projects")}
    out = []
    for r in rows:
        d = dict(r)
        d["username"] = unames.get(r["user_id"], "")
        d["projectName"] = pnames.get(r["project_id"], "") if r["project_id"] else ""
        out.append(d)
    total = db.query_one("SELECT COUNT(*) c FROM call_records" + (" WHERE user_id=?" if uid > 0 else ""),
                         (uid,) if uid > 0 else ())["c"]
    return {"success": True, "records": out, "total": total}


@router.get("/audit")
async def audit_log(request: Request):
    _guard(request)
    limit = min(int(request.query_params.get("limit") or 200), 1000)
    rows = db.query("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))
    return {"success": True, "audit": [dict(r) for r in rows]}


# ---------------- 项目 ----------------
@router.get("/projects")
async def projects(request: Request):
    _guard(request)
    rows = db.query("SELECT * FROM projects ORDER BY sort_order DESC, id DESC")
    return {"success": True, "projects": [models.project_view(p) for p in rows]}


@router.get("/projects/{pid}")
async def project(request: Request, pid: int):
    _guard(request)
    p = db.query_one("SELECT * FROM projects WHERE id=?", (pid,))
    if not p:
        raise HTTPException(404, "project not found")
    return {"success": True, "project": models.project_view(p, full=True)}


def _project_fields(b: dict) -> dict:
    rc = b.get("runConfig")
    return {
        "name": b.get("name") or "", "summary": b.get("summary") or "", "intro": b.get("intro") or "",
        "tutorial": b.get("tutorial") or "", "icon": b.get("icon") or "",
        "status": "on" if b.get("status") == "on" else "off",
        "panel_type": b.get("panelType") or "",
        "run_config": json.dumps(rc, ensure_ascii=False) if isinstance(rc, (dict, list)) else (rc or ""),
        "sort_order": int(b.get("sortOrder") or 0),
    }


@router.post("/projects")
async def create_project(request: Request):
    _guard(request)
    b = await get_body(request)
    f = _project_fields(b)
    pid = db.execute(
        "INSERT INTO projects(name,summary,intro,tutorial,icon,status,panel_type,run_config,sort_order,created_at,updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (f["name"], f["summary"], f["intro"], f["tutorial"], f["icon"], f["status"], f["panel_type"],
         f["run_config"], f["sort_order"], db.now(), db.now()),
    )
    svc.audit("project-create", "project", pid, detail=f["name"], ip=client_ip(request))
    p = db.query_one("SELECT * FROM projects WHERE id=?", (pid,))
    return {"success": True, "project": models.project_view(p, full=True)}


@router.put("/projects/{pid}")
async def update_project(request: Request, pid: int):
    _guard(request)
    p = db.query_one("SELECT * FROM projects WHERE id=?", (pid,))
    if not p:
        raise HTTPException(404, "project not found")
    b = await get_body(request)
    f = _project_fields({**{k: p[k] for k in p.keys()}, **b,
                         "panelType": b.get("panelType", p["panel_type"]),
                         "runConfig": b.get("runConfig", p["run_config"]),
                         "sortOrder": b.get("sortOrder", p["sort_order"])})
    db.execute(
        "UPDATE projects SET name=?,summary=?,intro=?,tutorial=?,icon=?,status=?,panel_type=?,run_config=?,sort_order=?,updated_at=? WHERE id=?",
        (f["name"], f["summary"], f["intro"], f["tutorial"], f["icon"], f["status"], f["panel_type"],
         f["run_config"], f["sort_order"], db.now(), pid),
    )
    svc.audit("project-update", "project", pid, ip=client_ip(request))
    p = db.query_one("SELECT * FROM projects WHERE id=?", (pid,))
    return {"success": True, "project": models.project_view(p, full=True)}


@router.post("/projects/{pid}/shelf")
async def shelf(request: Request, pid: int):
    _guard(request)
    b = await get_body(request)
    on = bool(b.get("on"))
    db.execute("UPDATE projects SET status=?, updated_at=? WHERE id=?", ("on" if on else "off", db.now(), pid))
    svc.audit("project-on" if on else "project-off", "project", pid, ip=client_ip(request))
    return {"success": True}


@router.delete("/projects/{pid}")
async def delete_project(request: Request, pid: int):
    _guard(request)
    db.execute("DELETE FROM projects WHERE id=?", (pid,))
    svc.audit("project-delete", "project", pid, ip=client_ip(request))
    return {"success": True}
