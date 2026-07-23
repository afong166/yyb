"""登录防爆破：进程内失败计数 + 临时锁定（按 IP/用户名维度）。

无外部依赖，够用于单进程 uvicorn 自托管部署。若横向扩多进程，应换成 Redis 等共享存储。
"""
from __future__ import annotations

import threading
import time

_lock = threading.Lock()
# key -> (fail_count, window_start_ts, locked_until_ts)
_state: dict[str, tuple[int, float, float]] = {}

MAX_FAILS = 5          # 窗口内允许的最大失败次数
WINDOW = 15 * 60       # 计数窗口（秒）
LOCK = 15 * 60         # 触发锁定时长（秒）


def locked_seconds(key: str) -> int:
    """返回该 key 还需锁定的剩余秒数；0 表示未锁定。"""
    now = time.time()
    with _lock:
        rec = _state.get(key)
        if rec and rec[2] > now:
            return int(rec[2] - now)
    return 0


def record_fail(key: str) -> None:
    now = time.time()
    with _lock:
        count, start, locked_until = _state.get(key, (0, now, 0.0))
        if now - start > WINDOW:  # 窗口过期，重新计数
            count, start = 0, now
        count += 1
        if count >= MAX_FAILS:
            locked_until = now + LOCK
            count, start = 0, now  # 锁定后重置计数
        _state[key] = (count, start, locked_until)


def clear(key: str) -> None:
    with _lock:
        _state.pop(key, None)
