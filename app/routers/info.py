"""GET /api — 信息页。"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("")
async def info():
    return {
        "name": "yyb-code",
        "version": "2.0.0",
        "impl": "python-fastapi",
        "endpoints": [
            "POST /api/auth/register", "POST /api/auth/login", "GET /api/auth/me",
            "POST /api/login/start", "GET /api/login/status",
            "GET /api/accounts", "POST /api/accounts/refresh", "POST /api/accounts/delete",
            "POST /api/yyb/get-code   {openid, appid}   需授权码/登录",
            "POST /api/yyb/get-codes  {accounts:[openid,...], appid}",
            "POST /wx/code            {openid, appid}   wx_server 兼容",
        ],
    }
