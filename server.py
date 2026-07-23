#!/usr/bin/env python3
"""呆呆应用宝获取 Code —— Python/FastAPI 启动器。

后端已整体用 Python(FastAPI) 重写；前端(Vue3)预构建在 dist/ 里，由 Python 直接托管。
核心服务需要 Python 3.9+；饿了么参数与益禾堂签名功能还需要 Node.js：

    python3 server.py

首次会自动 pip 安装依赖。端口 YYB_API_PORT（默认 18273）、YYB_HOST（默认 0.0.0.0）。
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)


def _ensure_deps():
    try:
        import fastapi, uvicorn, httpx, cryptography, lz4, bcrypt, segno, requests  # noqa: F401
        # 代理取码依赖：python-socks 的 asyncio 后端在 Python<3.11 需要 async_timeout；
        # 缺失时会在「配置 SOCKS5 代理取码」时报 No module named 'async_timeout'。
        # 把这条真实的导入路径纳入探针，缺依赖时下次启动能自动重装、自愈。
        from python_socks.async_.asyncio import Proxy  # noqa: F401
        return
    except ImportError:
        print("[server] 检测到依赖缺失：pip 安装依赖（稍等）…", flush=True)
        req = os.path.join(ROOT, "requirements.txt")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "-r", req])


def main():
    if sys.version_info < (3, 9):
        sys.exit("需要 Python 3.9+")
    _ensure_deps()
    os.chdir(ROOT)
    import uvicorn
    from app import config

    port = int(os.environ.get("YYB_API_PORT", "18273"))
    host = os.environ.get("YYB_HOST", "0.0.0.0")
    sep = "=" * 70
    print(f"\n{sep}")
    print("  应用宝取码后端已就绪（Python API / Node 辅助功能）")
    print("  " + "-" * 66)
    print(f"  网站 / 控制台 :  http://127.0.0.1:{port}/")
    print(f"  管理后台      :  http://127.0.0.1:{port}/admin  （用户名 admin）")
    print(f"  管理员令牌    :  {config.ADMIN_TOKEN}   （首次即 admin 登录密码）")
    print("  退出          :  Ctrl+C")
    print(f"{sep}\n", flush=True)

    uvicorn.run("app.main:app", host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
