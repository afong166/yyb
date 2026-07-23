"""定时任务调度器 + 运行器。

- run 一条任务：按类型分派（运行内置项目 / 获取 Code），支持多账号批量，日志按级别汇总。
- 运行项目：复用 routers.projects 的运行器映射；登录换 Cookie/Token 类项目跑完后按任务配置
  自动提交到用户已配置的青龙/呆呆面板。
- 调度循环：每 ~30s 扫到期任务，先把 next_run_at 前推到下一次（避免重复触发），再执行并写回结果。
"""
from __future__ import annotations

import asyncio
import contextlib
import json

from . import cron, db, deps
from .logs import log


def _acc_name(openid: str) -> str:
    a = db.query_one("SELECT nickname FROM accounts WHERE openid=?", (openid,))
    return (a["nickname"] if a and a["nickname"] else openid) or "账号"


def _panel_label(pt: str) -> str:
    return {"qinglong": "青龙面板", "daidai": "呆呆面板"}.get(pt, pt)


def _panel_remark(openid: str, result: dict | None = None) -> str:
    """定时任务自动提交面板时使用账号备注。

    京东会在 panels_ext 里自动改成 pt_pin；饿了么/蜜雪/美团没有 pt_pin，
    必须用账号标识做备注，否则同一个环境变量名会覆盖上一个账号。
    """
    result = result or {}
    for key in ("ptPin", "account", "username", "userId"):
        value = str(result.get(key) or "").strip()
        if value:
            return value.replace("\r", " ").replace("\n", " ")[:80]
    name = _acc_name(openid)
    if openid and name and name != openid:
        return f"{name}-{openid[-6:]}"[:80]
    return f"微信账号-{openid[-6:]}" if openid else "自动提交"


def compute_next_ms(cron_expr: str) -> int:
    """按当前时间算下一次触发的毫秒时间戳；非法/无匹配返回 0（等于停用调度）。"""
    try:
        dt = cron.next_run(cron_expr)
        return int(dt.timestamp() * 1000) if dt else 0
    except Exception:
        return 0


async def _run_code_task(task, log_fn) -> tuple[int, int]:
    from .codebridge import get_code_for_openid

    uid = task["user_id"]
    appid = task["appid"] or ""
    openids = json.loads(task["openids"] or "[]")
    bound = deps.account_openids(uid)
    ok = 0
    for openid in openids:
        name = _acc_name(openid)
        log_fn(f"[INFO] ▶ 账号：{name}")
        if openid not in bound:
            log_fn(f"[ERROR] {name}：账号不属于你或已删除")
            continue
        r = await get_code_for_openid(openid, appid)
        if r.get("success") and r.get("code"):
            ok += 1
            log_fn(f"[SUCCESS] {name}：{r['code']}")
        else:
            log_fn(f"[ERROR] {name}：{r.get('error') or '取码失败'}")
    return ok, len(openids)


async def _run_project_task(task, log_fn) -> tuple[int, int]:
    from .routers.projects import CODE_LOGIN_RUNNERS, ACTION_RUNNERS
    from .routers.panels import get_panel_config
    from . import panels_ext

    uid = task["user_id"]
    p = db.query_one("SELECT * FROM projects WHERE id=? AND status='on'", (task["project_id"],))
    if not p:
        log_fn("[ERROR] 项目不存在或已下架")
        return 0, 1
    rc = json.loads(p["run_config"] or "{}")
    builtin = rc.get("builtin")
    appid = rc.get("appid") or ""
    params = json.loads(task["params"] or "{}")
    openids = json.loads(task["openids"] or "[]")
    bound = deps.account_openids(uid)

    env_name = (params.get("envName") or rc.get("envName") or "").strip()
    allowed = rc.get("submitPanels") or []
    panels = [pt for pt in (params.get("panels") or []) if pt in allowed]

    is_action = builtin in ACTION_RUNNERS
    is_code = builtin in CODE_LOGIN_RUNNERS
    if not (is_action or is_code):
        log_fn("[ERROR] 该项目不支持定时运行")
        return 0, 1

    ok = 0
    for openid in openids:
        name = _acc_name(openid)
        log_fn(f"[INFO] ▶ 账号：{name}  项目：{p['name']}")
        if openid not in bound:
            log_fn(f"[ERROR] {name}：账号不属于你或已删除")
            continue
        try:
            if is_action:
                result = await ACTION_RUNNERS[builtin](uid, openid, appid, {**rc, **params})
            else:
                result = await CODE_LOGIN_RUNNERS[builtin](uid, openid, appid, params.get("proxyUrl") or "")
        except Exception as exc:
            log_fn(f"[ERROR] {name}：{exc}")
            continue
        if not result.get("ok"):
            log_fn(f"[ERROR] {name}：{result.get('error') or '运行失败'}")
            continue

        if is_action:
            # 执行类：cookie 字段是带级别的运行日志文本，逐行并入
            for ln in str(result.get("cookie") or "").split("\n"):
                if ln.strip():
                    log_fn(ln)
            ok += 1
        else:
            cookie = result.get("cookie") or result.get("jdCookie") or result.get("elemeCookie") or ""
            kind = env_name or "Cookie/Token"
            log_fn(f"[SUCCESS] {name}：已取到 {kind}")
            ok += 1
            if env_name and panels and cookie:
                for pt in panels:
                    cfg = get_panel_config(uid, pt)
                    if not cfg:
                        log_fn(f"[WARNING] {_panel_label(pt)} 未配置，跳过提交")
                        continue
                    r = await panels_ext.submit_env(pt, cfg, {"name": env_name, "value": cookie,
                                                              "remarks": _panel_remark(openid, result)})
                    if r.get("ok"):
                        log_fn(f"[SUCCESS] 已提交 {env_name} → {_panel_label(pt)}")
                    else:
                        log_fn(f"[WARNING] 提交 {_panel_label(pt)} 失败：{r.get('error') or '未知'}")
            else:
                log_fn("[INFO] 未配置自动提交面板，仅取值保存")
    return ok, len(openids)


async def _collect_run(task, live_log_fn=None) -> tuple[str, str]:
    lines: list[str] = []

    def add_log(line: str) -> None:
        lines.append(line)
        if live_log_fn:
            live_log_fn(line)

    add_log(f"[INFO] 任务开始：{task['name'] or ('#' + str(task['id']))}")
    try:
        if task["task_type"] == "code":
            ok, total = await _run_code_task(task, add_log)
        else:
            ok, total = await _run_project_task(task, add_log)
    except Exception as exc:
        log.exception("scheduled task %s failed", task["id"])
        add_log(f"[ERROR] 任务执行异常：{exc}")
        ok, total = 0, 1
    if total and ok == total:
        status = "ok"
    elif ok == 0:
        status = "fail"
    else:
        status = "partial"
    add_log(f"[INFO] 任务结束：成功 {ok}/{total}")
    return status, "\n".join(lines)


async def execute_and_store(task, live_log_fn=None) -> dict:
    """运行一条任务并写回 last_*（不改 next_run_at；调度/手动运行都用它）。"""
    db.execute("UPDATE scheduled_tasks SET last_status='running' WHERE id=?", (task["id"],))
    status, text = await _collect_run(task, live_log_fn)
    now = db.now()
    db.execute("UPDATE scheduled_tasks SET last_run_at=?, last_status=?, last_result=? WHERE id=?",
               (now, status, text[:20000], task["id"]))
    return {"status": status, "log": text, "lastRunAt": now}


async def scheduler_loop() -> None:
    """常驻循环：每 30s 扫到期任务，先前推 next_run_at 再执行。"""
    while True:
        await asyncio.sleep(30)
        try:
            now = db.now()
            due = db.query(
                "SELECT * FROM scheduled_tasks WHERE enabled=1 AND next_run_at>0 AND next_run_at<=?"
                " ORDER BY next_run_at LIMIT 20",
                (now,),
            )
            for task in due:
                # 先把下一次算好写回，避免本轮执行耗时导致下一轮重复触发同一条
                db.execute("UPDATE scheduled_tasks SET next_run_at=? WHERE id=?",
                           (compute_next_ms(task["cron"]), task["id"]))
                with contextlib.suppress(Exception):
                    await execute_and_store(task)
        except Exception:
            log.exception("scheduler loop tick failed")
