"""统一日志：控制台 + 轮转文件，格式 `2026-07-02 21:43:01 [INFO] [tag] 消息 k=v k=v`。

用法：
    from .logs import event, actor_name
    event("checkQr", "查询扫码状态", project=actor_name(uid), 来源=ip, 状态="waiting")
    -> 2026-07-02 21:43:01 [INFO] [checkQr] 查询扫码状态 project=sf 来源=1.2.3.4 状态=waiting
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from . import db
from .config import LOG_DIR

LOGGER_NAME = "yyb"
log = logging.getLogger(LOGGER_NAME)


def setup_logging(level: int = logging.INFO) -> None:
    """初始化 yyb 日志器（幂等）。只挂到 yyb 上并关掉向 root 传播，避免掺入三方库噪声。"""
    if getattr(setup_logging, "_done", False):
        return
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)

    try:
        fh = RotatingFileHandler(LOG_DIR / "app.log", maxBytes=5 * 1024 * 1024,
                                 backupCount=5, encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except Exception:
        pass  # 文件不可写时退化为仅控制台

    log.setLevel(level)
    log.propagate = False
    setup_logging._done = True  # type: ignore[attr-defined]


def event(tag: str, msg: str, *, level: int = logging.INFO, **fields) -> None:
    """结构化打点：`[tag] msg k=v k=v`，空值字段自动跳过。"""
    parts = [f"[{tag}] {msg}"]
    for k, v in fields.items():
        if v is None or v == "":
            continue
        parts.append(f"{k}={v}")
    log.log(level, " ".join(parts))


def actor_name(uid: int) -> str:
    """user_id → 用户名（做日志里的 project 标识）；查不到就退回 id。"""
    try:
        u = db.query_one("SELECT username FROM users WHERE id=?", (uid,))
        return u["username"] if u else f"uid{uid}"
    except Exception:
        return f"uid{uid}"


def mask_id(s: str, keep: int = 8) -> str:
    """openid/长 id 脱敏：只留前 keep 位，其余省略。"""
    if not s:
        return ""
    return s if len(s) <= keep else s[:keep] + "…"
