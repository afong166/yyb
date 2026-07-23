"""/api/tasks/* — 用户定时任务：运行内置项目 / 获取 Code，Cron 调度，可多账号批量。"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request

from .. import cron, db, deps, scheduler, runstream
from ..logs import actor_name, event
from ..util import get_body, client_ip
from .projects import CODE_LOGIN_RUNNERS, ACTION_RUNNERS

router = APIRouter()

_RUNNABLE = set(CODE_LOGIN_RUNNERS) | set(ACTION_RUNNERS)
MAX_TASKS_PER_USER = 50


def _task_view(t) -> dict:
    openids = json.loads(t["openids"] or "[]")
    names = []
    for o in openids:
        a = db.query_one("SELECT nickname FROM accounts WHERE openid=?", (o,))
        names.append(a["nickname"] if a and a["nickname"] else o)
    proj = None
    if t["task_type"] == "project" and t["project_id"]:
        p = db.query_one("SELECT name, icon FROM projects WHERE id=?", (t["project_id"],))
        if p:
            proj = {"name": p["name"], "icon": p["icon"] or "🧩"}
    return {
        "id": t["id"], "name": t["name"] or "", "taskType": t["task_type"],
        "projectId": t["project_id"] or 0, "project": proj,
        "appid": t["appid"] or "", "openids": openids, "accountNames": names,
        "params": json.loads(t["params"] or "{}"),
        "cron": t["cron"] or "", "cronText": cron.describe(t["cron"] or ""),
        "enabled": bool(t["enabled"]), "nextRunAt": t["next_run_at"] or 0,
        "lastRunAt": t["last_run_at"] or 0, "lastStatus": t["last_status"] or "",
        "lastResult": t["last_result"] or "",
        "createdAt": t["created_at"], "updatedAt": t["updated_at"],
    }


def _parse_input(uid: int, b: dict) -> dict:
    name = (b.get("name") or "").strip()[:60]
    task_type = b.get("taskType") or "project"
    if task_type not in ("project", "code"):
        raise HTTPException(400, "任务类型非法")

    openids = b.get("openids") or []
    if not isinstance(openids, list) or not openids:
        raise HTTPException(400, "请至少选择一个微信账号")
    bound = deps.account_openids(uid)
    bad = [o for o in openids if o not in bound]
    if bad:
        raise HTTPException(403, "含不属于你的微信账号")

    cron_expr = (b.get("cron") or "").strip()
    try:
        cron.validate(cron_expr)
    except ValueError as e:
        raise HTTPException(400, f"Cron 表达式无效：{e}")

    params = b.get("params") if isinstance(b.get("params"), dict) else {}
    project_id, appid = 0, (b.get("appid") or "").strip()
    if task_type == "project":
        project_id = int(b.get("projectId") or 0)
        p = db.query_one("SELECT * FROM projects WHERE id=? AND status='on'", (project_id,))
        if not p:
            raise HTTPException(400, "项目不存在或未上架")
        rc = json.loads(p["run_config"] or "{}")
        if rc.get("builtin") not in _RUNNABLE:
            raise HTTPException(400, "该项目暂不支持定时运行")
        appid = rc.get("appid") or ""
    elif not appid:
        raise HTTPException(400, "获取 Code 任务需填写小程序 appid")

    enabled = 1 if b.get("enabled", True) else 0
    next_ms = scheduler.compute_next_ms(cron_expr) if enabled else 0
    return {
        "name": name, "task_type": task_type, "project_id": project_id, "appid": appid,
        "openids": json.dumps(openids), "params": json.dumps(params, ensure_ascii=False),
        "cron": cron_expr, "enabled": enabled, "next_run_at": next_ms,
    }


@router.get("")
async def list_tasks(request: Request):
    uid = deps.require_user(request)
    rows = db.query("SELECT * FROM scheduled_tasks WHERE user_id=? ORDER BY id DESC", (uid,))
    return {"success": True, "tasks": [_task_view(t) for t in rows]}


@router.post("")
async def create_task(request: Request):
    uid = deps.require_user(request)
    cnt = db.query_one("SELECT COUNT(*) c FROM scheduled_tasks WHERE user_id=?", (uid,))["c"]
    if cnt >= MAX_TASKS_PER_USER:
        raise HTTPException(400, f"定时任务数量已达上限（{MAX_TASKS_PER_USER}）")
    b = await get_body(request)
    d = _parse_input(uid, b)
    tid = db.execute(
        "INSERT INTO scheduled_tasks(user_id,name,task_type,project_id,appid,openids,params,cron,enabled,next_run_at,created_at,updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (uid, d["name"], d["task_type"], d["project_id"], d["appid"], d["openids"], d["params"],
         d["cron"], d["enabled"], d["next_run_at"], db.now(), db.now()),
    )
    event("taskCreate", "新建定时任务", project=actor_name(uid), 来源=client_ip(request),
          任务=d["name"] or f"#{tid}", 类型=d["task_type"], 调度=d["cron"])
    t = db.query_one("SELECT * FROM scheduled_tasks WHERE id=?", (tid,))
    return {"success": True, "task": _task_view(t)}


@router.put("/{tid}")
async def update_task(request: Request, tid: int):
    uid = deps.require_user(request)
    old = db.query_one("SELECT * FROM scheduled_tasks WHERE id=? AND user_id=?", (tid, uid))
    if not old:
        raise HTTPException(404, "任务不存在")
    b = await get_body(request)
    d = _parse_input(uid, b)
    db.execute(
        "UPDATE scheduled_tasks SET name=?,task_type=?,project_id=?,appid=?,openids=?,params=?,cron=?,enabled=?,next_run_at=?,updated_at=?"
        " WHERE id=? AND user_id=?",
        (d["name"], d["task_type"], d["project_id"], d["appid"], d["openids"], d["params"],
         d["cron"], d["enabled"], d["next_run_at"], db.now(), tid, uid),
    )
    t = db.query_one("SELECT * FROM scheduled_tasks WHERE id=?", (tid,))
    return {"success": True, "task": _task_view(t)}


@router.post("/{tid}/toggle")
async def toggle_task(request: Request, tid: int):
    uid = deps.require_user(request)
    t = db.query_one("SELECT * FROM scheduled_tasks WHERE id=? AND user_id=?", (tid, uid))
    if not t:
        raise HTTPException(404, "任务不存在")
    enabled = 0 if t["enabled"] else 1
    next_ms = scheduler.compute_next_ms(t["cron"]) if enabled else 0
    db.execute("UPDATE scheduled_tasks SET enabled=?, next_run_at=?, updated_at=? WHERE id=?",
               (enabled, next_ms, db.now(), tid))
    t = db.query_one("SELECT * FROM scheduled_tasks WHERE id=?", (tid,))
    return {"success": True, "task": _task_view(t)}


@router.delete("/{tid}")
async def delete_task(request: Request, tid: int):
    uid = deps.require_user(request)
    db.execute("DELETE FROM scheduled_tasks WHERE id=? AND user_id=?", (tid, uid))
    return {"success": True}


@router.post("/{tid}/run")
async def run_task_now(request: Request, tid: int):
    uid = deps.require_user(request)
    t = db.query_one("SELECT * FROM scheduled_tasks WHERE id=? AND user_id=?", (tid, uid))
    if not t:
        raise HTTPException(404, "任务不存在")
    res = await scheduler.execute_and_store(t)
    event("taskRunNow", "手动运行定时任务", project=actor_name(uid), 来源=client_ip(request),
          任务=t["name"] or f"#{tid}", 结果=res.get("status"))
    t2 = db.query_one("SELECT * FROM scheduled_tasks WHERE id=?", (tid,))
    return {"success": True, "result": res, "task": _task_view(t2)}


@router.post("/{tid}/run-start")
async def run_task_start(request: Request, tid: int):
    """后台立即运行：多账号任务可能超过网关超时，先秒回 runId，再由前端轮询日志。"""
    uid = deps.require_user(request)
    t = db.query_one("SELECT * FROM scheduled_tasks WHERE id=? AND user_id=?", (tid, uid))
    if not t:
        raise HTTPException(404, "任务不存在")
    run_id = runstream.new_run(uid)
    ip = client_ip(request)

    async def _job():
        try:
            res = await scheduler.execute_and_store(t, lambda line: runstream.push(run_id, line))
            event("taskRunNow", "手动后台运行定时任务", project=actor_name(uid), 来源=ip,
                  任务=t["name"] or f"#{tid}", 结果=res.get("status"))
            runstream.finish(run_id, res)
        except Exception as exc:
            runstream.push(run_id, f"[ERROR] 任务执行异常：{exc}")
            runstream.finish(run_id, {"status": "fail", "log": f"[ERROR] 任务执行异常：{exc}", "lastRunAt": db.now()})

    asyncio.create_task(_job())
    return {"success": True, "runId": run_id}


@router.get("/{tid}/run-poll")
async def run_task_poll(request: Request, tid: int, runId: str = "", cursor: int = 0):
    uid = deps.require_user(request)
    # 先校验任务归属，防止拿别人的 runId 猜测任务状态。
    if not db.query_one("SELECT id FROM scheduled_tasks WHERE id=? AND user_id=?", (tid, uid)):
        raise HTTPException(404, "任务不存在")
    st = runstream.poll(runId, uid, cursor)
    if st is None:
        raise HTTPException(404, "运行会话不存在或已过期")
    return {"success": True, **st}
