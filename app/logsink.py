"""运行日志实时管道。

用 contextvar 把 runner 的每行日志推给「当前运行会话」，实现项目运行时的实时流式输出。
contextvar 会随 `asyncio.to_thread` 复制到工作线程，因此在线程里跑的执行类 runner 调用
`log()` 时也能命中当前会话的 sink（不同并发运行各自独立，互不串扰）。
"""
from __future__ import annotations

import contextvars

log_sink: "contextvars.ContextVar" = contextvars.ContextVar("log_sink", default=None)


def emit(line: str) -> None:
    fn = log_sink.get()
    if fn is not None:
        try:
            fn(line)
        except Exception:
            pass
