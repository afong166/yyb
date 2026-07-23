"""项目运行会话注册表：后台跑项目，前端按 runId 轮询增量日志与最终结果。

内存态、进程内即可（单实例部署）；带 TTL 惰性回收，避免泄漏。
"""
from __future__ import annotations

import time
import uuid

_RUNS: dict[str, dict] = {}
_TTL = 900  # 会话保留 15 分钟


def _gc() -> None:
    now = time.time()
    for k in [k for k, v in _RUNS.items() if now - v["ts"] > _TTL]:
        _RUNS.pop(k, None)


def new_run(uid: int) -> str:
    _gc()
    rid = uuid.uuid4().hex
    _RUNS[rid] = {"uid": uid, "lines": [], "done": False, "result": None, "ts": time.time()}
    return rid


def push(rid: str, line: str) -> None:
    st = _RUNS.get(rid)
    if st is not None:
        st["lines"].append(line)
        st["ts"] = time.time()


def finish(rid: str, result: dict) -> None:
    st = _RUNS.get(rid)
    if st is not None:
        st["result"] = result
        st["done"] = True
        st["ts"] = time.time()


def poll(rid: str, uid: int, cursor: int) -> dict | None:
    """返回 cursor 之后的新日志行 + 是否完成 + 完成时的最终结果。会话不存在/越权返回 None。"""
    st = _RUNS.get(rid)
    if st is None or st["uid"] != uid:
        return None
    lines = st["lines"]
    cursor = max(0, min(int(cursor or 0), len(lines)))
    return {
        "lines": lines[cursor:],
        "cursor": len(lines),
        "done": st["done"],
        "result": st["result"] if st["done"] else None,
    }
