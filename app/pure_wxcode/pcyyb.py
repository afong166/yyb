"""应用宝一次性 login_buffer 获取(UAL 签名)。"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import random
import sys
import time
from pathlib import Path
from typing import Any, Optional

try:
    import requests
    from requests.packages.urllib3.exceptions import InsecureRequestWarning
    requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
except ImportError:
    print("[fatal] 需要 requests: pip install requests", file=sys.stderr)
    raise

try:
    from .oauth import Credentials, load_credentials, ensure_credentials
except Exception:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from oauth import Credentials, load_credentials, ensure_credentials

HOST = "https://yybadaccess.3g.qq.com"
PC_YYB_AUTH_KEY = "wgrdg373hy26ww2"
PC_YYB_KEY = "df683a7f1c814b4f84e752c6ed95c057"
DEFAULT_MANAGER_APPID = "ilinkapp_060000b7b93f6c"
DEFAULT_WX_AUTH_APPID = "wxd44977328b36e647"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0.0 CefView/1.0 (Windows; en-us) YYBAppClient/5.10.6400.6119 Safari/537.36")

def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))

def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()

def ual_headers(body: Any, business_id: str, access_key: str, guid: Optional[str] = None) -> tuple[dict, str]:
    body_str = _dumps(body)
    timestamp = str(int(time.time() * 1000))
    nonce = str(int(random.random() * 10000))
    signature = _md5(f"{body_str}{timestamp}{access_key}{nonce}")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": UA,
        "Origin": "https://pc.yyb.qq.com",
        "Referer": "https://pc.yyb.qq.com/",
        "Ual-Access-Businessid": business_id,
        "Ual-Access-Timestamp": timestamp,
        "Ual-Access-Nonce": nonce,
        "Ual-Access-Signature": signature,
    }
    if guid:
        headers["Ual-Access-Guid"] = guid
    return headers, body_str

def _post(url: str, body_str: str, headers: dict, timeout: float = 20.0) -> tuple[int, str]:
    r = requests.post(url, data=body_str.encode("utf-8"), headers=headers, timeout=timeout, verify=False)
    return r.status_code, r.content.decode("utf-8", "ignore")

def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    val = shift = 0
    while pos < len(data):
        b = data[pos]; pos += 1
        val |= (b & 0x7F) << shift
        if b < 0x80:
            return val, pos
        shift += 7
    raise ValueError("truncated varint")

def _parse_pb_len_fields(data: bytes) -> dict[int, bytes]:
    fields: dict[int, bytes] = {}
    pos = 0
    while pos < len(data):
        key, pos = _read_varint(data, pos)
        fno, wt = key >> 3, key & 7
        if wt == 2:
            ln, pos = _read_varint(data, pos)
            fields[fno] = data[pos:pos + ln]; pos += ln
        elif wt == 0:
            _v, pos = _read_varint(data, pos)
        elif wt == 1:
            pos += 8
        elif wt == 5:
            pos += 4
        else:
            raise ValueError(f"bad wire_type {wt}")
    return fields

def parse_login_buffer(login_buffer_b64: str) -> dict[str, Any]:
    raw = base64.b64decode(login_buffer_b64, validate=True)
    f = _parse_pb_len_fields(raw)
    auth_payload = f.get(1, b"")
    ilink_appid = f.get(2, b"").decode("utf-8", "replace")
    host_appid = f.get(3, b"").decode("utf-8", "replace")
    return {
        "raw_bytes": raw,
        "raw_len": len(raw),
        "auth_payload": auth_payload,
        "auth_payload_len": len(auth_payload),
        "ilink_appid": ilink_appid,
        "host_appid": host_appid,
    }

def fetch_unionid(openid: str, access_token: str) -> dict[str, Any]:
    body = {"extInfo": {"listS": {"openid": {"value": [openid]}, "access_token": {"value": [access_token]}}}}
    body_str = _dumps(body)
    status, text = _post(
        f"{HOST}/pc_yyb/pcyyb_get_wx_unionid", body_str,
        {"Content-Type": "application/json", "User-Agent": UA, "Referer": "https://pc.yyb.qq.com/"},
    )
    data = json.loads(text) if text.strip().startswith("{") else {}
    unionid = (((data.get("ext_info") or {}).get("list_s") or {}).get("unionid") or {}).get("value", [None])[0]
    return {"http_status": status, "code": data.get("code"), "unionid": unionid, "raw": data}

def fetch_login_buffer(guid: str, access_token: str, unionid: Optional[str] = None,
                       user_type: int = 1) -> dict[str, Any]:
    body = {
        "extInfo": {
            "listS": {
                "unionid": {"value": [unionid]},
                "user_id": {"value": [guid]},
                "access_token": {"value": [access_token]},
            },
            "listI": {"user_type": {"value": [user_type]}},
        }
    }
    headers, body_str = ual_headers(body, "pc_yyb_auth", PC_YYB_AUTH_KEY, guid=guid)
    status, text = _post(f"{HOST}/pc_yyb_auth/pcyyb_get_wx_login_buffer_auth", body_str, headers)
    data = json.loads(text) if text.strip().startswith("{") else {}
    lb = (((data.get("ext_info") or {}).get("list_s") or {}).get("login_buffer") or {}).get("value", [None])[0]
    return {"http_status": status, "code": data.get("code"), "msg": data.get("msg"),
            "login_buffer": lb, "raw": data}

def get_fresh_login_buffer(cred: Credentials, do_unionid: bool = False) -> dict[str, Any]:
    if not cred.access_token:
        raise RuntimeError("credentials 缺 access_token")
    guid = cred.guid or ""
    if not guid:
        raise RuntimeError("credentials 缺 guid（请用新版 oauth.py 重新扫码，会保存 guid）")
    unionid = None
    if do_unionid:
        u = fetch_unionid(cred.openid, cred.access_token)
        unionid = u.get("unionid")
    res = fetch_login_buffer(guid, cred.access_token, unionid=unionid)
    if res.get("code") == 0 and res.get("login_buffer"):
        res["parsed"] = parse_login_buffer(res["login_buffer"])
    return res

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="应用宝 login_buffer 获取（蓝图 #2）")
    ap.add_argument("--credentials", default="")
    ap.add_argument("--unionid", action="store_true", help="先取 unionid 再产 buffer（默认 unionid=null）")
    ap.add_argument("--ensure", action="store_true", help="凭据失效自动 refresh/扫码")
    args = ap.parse_args(argv)

    cred = (ensure_credentials(args.credentials or None) if args.ensure
            else load_credentials(args.credentials or None))
    if not cred or not cred.access_token:
        print(json.dumps({"ok": False, "error": "无有效凭据，先 python oauth.py qr"}, ensure_ascii=False))
        return 1

    try:
        res = get_fresh_login_buffer(cred, do_unionid=args.unionid)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1

    out = {
        "ok": res.get("code") == 0 and bool(res.get("login_buffer")),
        "http_status": res.get("http_status"),
        "code": res.get("code"),
        "msg": res.get("msg"),
    }
    p = res.get("parsed")
    if p:
        out["login_buffer_len_b64"] = len(res["login_buffer"])
        out["loginv4"] = {
            "raw_len": p["raw_len"],
            "auth_payload_len": p["auth_payload_len"],
            "ilink_appid": p["ilink_appid"],
            "host_appid": p["host_appid"],
        }
    else:
        out["raw"] = res.get("raw")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out["ok"] else 2

if __name__ == "__main__":
    raise SystemExit(main())
