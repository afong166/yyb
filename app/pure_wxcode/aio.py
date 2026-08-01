"""asyncio 版取码流水线：复用全部纯 CPU 加密逻辑，仅把 socket / HTTP 收发换成异步，单进程高并发。"""
from __future__ import annotations
import asyncio
import json
import secrets
import struct
import time

import httpx

from . import ilink_mmtls_client as M
from . import ilink_packet as P
from . import pure_login as PL
from . import shortcloud as SC
from . import pcyyb, cloud_auth as CA


def _rv(b, p):
    o = s = 0
    while True:
        x = b[p]; p += 1; o |= (x & 0x7F) << s
        if x < 0x80:
            return o, p
        s += 7


def extract_code(payload: bytes):
    def walk(b):
        p, n = 0, len(b)
        while p < n:
            try:
                tag, p = _rv(b, p)
            except Exception:
                return
            wt = tag & 7
            if wt == 2:
                l, p = _rv(b, p); v = b[p:p + l]; p += l
                if 28 <= l <= 40 and v[:1] == b"0" and all(32 <= c < 127 for c in v):
                    yield v.decode()
                elif 2 < l < 400:
                    yield from walk(v)
            elif wt == 0:
                _, p = _rv(b, p)
            elif wt == 5:
                p += 4
            elif wt == 1:
                p += 8
            else:
                return
    for c in walk(payload):
        return c
    return None


class AsyncSession:
    def __init__(self, reader, writer, app):
        self.reader = reader
        self.writer = writer
        self.app = app
        self.client_seq = 0
        self.server_seq = 0

    async def send_app(self, plaintext: bytes):
        ct = M.encrypt_record(self.app.client_key, self.app.client_nonce, self.client_seq,
                              plaintext, M.RECORD_APPDATA)
        self.writer.write(M.build_record(M.RECORD_APPDATA, ct))
        await self.writer.drain()
        self.client_seq += 1

    async def recv_app(self) -> bytes:
        hdr = await self.reader.readexactly(5)
        ln = struct.unpack(">H", hdr[3:5])[0]
        ct = await self.reader.readexactly(ln)
        pt = M.decrypt_record(self.app.server_key, self.app.server_nonce, self.server_seq, ct, hdr[0])
        self.server_seq += 1
        return pt

    def close(self):
        try:
            self.writer.close()
        except Exception:
            pass


async def _read_record(reader, timeout: float):
    hdr = await asyncio.wait_for(reader.readexactly(5), timeout)
    if hdr[1:3] != M.MMTLS_VERSION:
        raise RuntimeError(f"bad record version {hdr[1:3].hex()}")
    ln = struct.unpack(">H", hdr[3:5])[0]
    payload = await asyncio.wait_for(reader.readexactly(ln), timeout)
    return hdr[0], payload


async def _open_conn(host, port, proxy_url: str, timeout: float):
    """建 TCP 连接：有 proxy_url(SOCKS5/4) 则走代理隧道(远端解析域名)，否则直连。返回 (reader, writer)。"""
    if proxy_url:
        try:
            from python_socks.async_.asyncio import Proxy
        except ImportError as e:
            # python-socks 的 asyncio 后端在 Python<3.11 需要 async_timeout 包。
            raise RuntimeError(
                f"缺少代理依赖（{e}）：请在服务器执行  pip install \"python-socks[asyncio]\"  后重启服务"
            ) from e
        proxy = Proxy.from_url(proxy_url)
        sock = await asyncio.wait_for(proxy.connect(dest_host=host, dest_port=port, timeout=timeout), timeout)
        return await asyncio.open_connection(sock=sock)
    return await asyncio.wait_for(asyncio.open_connection(host, port), timeout)


async def async_handshake(host: str = "longcloud.weixin.com", port: int = 443,
                          timeout: float = 8.0, verify_server: bool = True,
                          proxy_url: str = "") -> AsyncSession:
    k1, k2 = M.gen_keypair(), M.gen_keypair()
    ch = M.build_client_hello(secrets.token_bytes(32), M.pub_uncompressed(k1), M.pub_uncompressed(k2))
    reader, writer = await _open_conn(host, port, proxy_url, timeout)
    writer.write(ch)
    await writer.drain()
    typ0, sh = await _read_record(reader, timeout)
    if typ0 != M.RECORD_HANDSHAKE:
        raise RuntimeError("未收到 ServerHello")
    com_key = M.compute_share_key(k1, M.parse_server_keyshare(sh))
    hsk = M.handshake_keys(com_key, ch[5:] + sh)
    plain = []
    seq = 1
    transcript = ch[5:] + sh
    sf_verify = None
    while True:
        _, pl = await _read_record(reader, timeout)
        p = M.decrypt_record(hsk.server_key, hsk.server_nonce, seq, pl)
        seq += 1
        plain.append(p)
        if p[4] == 0x14:
            sf_verify = p[7:7 + M.HASH_LEN]
            break
        transcript += p
    if verify_server:
        exp = M.server_finished_expected(com_key, transcript)
        if exp != sf_verify:
            raise RuntimeError("ServerFinished 校验失败")
    cf = M.client_finished(com_key, transcript)
    cf_ct = M.encrypt_record(hsk.client_key, hsk.client_nonce, 1,
                             M.wrap_handshake_msg(0x14, cf), M.RECORD_HANDSHAKE)
    writer.write(M.build_record(M.RECORD_HANDSHAKE, cf_ct))
    await writer.drain()
    sess = AsyncSession(reader, writer, M.app_keys(com_key, transcript))
    sess.client_seq = 2
    sess.server_seq = len(plain) + 1
    sess.com_key = com_key
    sess.hs_messages = plain
    sess.transcript_before_sf = transcript
    sess.client_hello = ch
    sess.server_hello = sh
    return sess


async def _login_on_session(sess: AsyncSession, device_id: bytes, login_data: bytes,
                            server_pub: bytes = PL.PER_APP_PUBKEY,
                            ilink_appid: str = P.ILINK_APPID):
    reqbody, priv, body_km, plaintext = PL._build_request(device_id, login_data, server_pub)
    pkt = P.build_ilink_packet(3453, reqbody, crypto=P.CRYPTO_HYBRID, compress=P.COMPRESS_LZ4,
                               ilink_appid=ilink_appid)
    await sess.send_app(pkt)
    resp = P.parse_ilink_response(await sess.recv_app())
    f23 = resp["numeric"].get(23, 0)
    sf23 = f23 - (1 << 64) if f23 > (1 << 63) else f23
    body = resp.get("body") or b""
    if sf23 != 0 or not body:
        raise RuntimeError(f"cmd3453 登录失败 field23={sf23}")
    plain = PL._decrypt_response(body, priv, body_km, plaintext)
    return PL.parse_credential(plain)


async def async_prepare_session(device_id: bytes, login_data: bytes, proxy_url: str = "",
                                ilink_appid: str = P.ILINK_APPID):
    sess = await async_handshake(proxy_url=proxy_url)
    try:
        ap = M.access_psk(sess.hs_messages)
        psk_secret = M.access_psk_secret(sess)
        cred = await _login_on_session(sess, device_id, login_data, ilink_appid=ilink_appid)
    finally:
        sess.close()
    return ap, psk_secret, cred


async def async_fresh_psk(proxy_url: str = ""):
    """只做一次 longcloud 握手拿新的 PSK 票据(ap, psk_secret)，不重新登录（复用已缓存的登录凭据用）。"""
    sess = await async_handshake(proxy_url=proxy_url)
    try:
        return M.access_psk(sess.hs_messages), M.access_psk_secret(sess)
    finally:
        sess.close()


async def _mmtls_post(body: bytes, ip: str, port: int = SC.SHORTCLOUD_PORT,
                      path_hex: str = "00003c88", timeout: float = 8.0, proxy_url: str = ""):
    req = SC.build_mmtls_post(body, ip=ip, path_hex=path_hex)
    reader, writer = await _open_conn(ip, port, proxy_url, timeout)
    writer.write(req)
    await writer.drain()
    raw = bytearray()
    header_end = -1
    content_len = None
    try:
        while True:
            d = await asyncio.wait_for(reader.read(8192), timeout)
            if not d:
                break
            raw.extend(d)

            # shortcloud 返回的是 HTTP 包；拿到完整 body 就可以解 MMTLS 记录，
            # 不需要继续等对端关闭连接，否则代理/网络慢关连接时会多等到 read timeout。
            if header_end < 0:
                header_end = raw.find(b"\r\n\r\n")
                if header_end >= 0:
                    head = raw[:header_end].decode("latin1", "ignore")
                    for line in head.split("\r\n"):
                        name, _, value = line.partition(":")
                        if name.strip().lower() != "content-length":
                            continue
                        try:
                            content_len = int(value.strip())
                        except ValueError:
                            content_len = None
                        break

            if header_end >= 0 and content_len is not None:
                body_have = len(raw) - header_end - 4
                if body_have >= content_len:
                    break
    except asyncio.TimeoutError:
        pass
    try:
        writer.close()
    except Exception:
        pass
    raw = bytes(raw)
    status = None
    rbody = b""
    if raw.startswith(b"HTTP/"):
        le = raw.find(b"\r\n")
        if le > 0:
            try:
                status = int(raw[9:le].split()[0])
            except Exception:
                status = None
        he = raw.find(b"\r\n\r\n")
        if he > 0:
            rbody = raw[he + 4:]
            if content_len is not None:
                rbody = rbody[:content_len]
    return status, rbody, raw


async def async_getcode_with_session(access_psk, psk_secret, cred, payload_protobuf: bytes, *,
                                     path_hex: str = "00003c88", ip: str = "", proxy_url: str = "",
                                     ilink_appid: str = P.ILINK_APPID):
    # 走代理时把域名交给代理端解析（账号所在地区 IP），不走代理才本地解析成 IP
    dst = SC.SHORTCLOUD_HOST if proxy_url else (ip or await async_resolve_ip())
    ts = int(time.time())
    ch = M.build_psk_client_hello(secrets.token_bytes(32), access_psk, timestamp=ts)
    early = SC.derive_early_keys(psk_secret, ch)
    encext_plain = SC.build_encrypted_extensions(ts)
    for sk in cred.keys:
        cmd2881 = SC.build_cmd2881(cred.uin, cred.server_id, sk, payload_protobuf, ilink_appid=ilink_appid)
        body = SC.build_getcode_body(ch, early, cmd2881, ts)
        status, rbody, raw = await _mmtls_post(body, dst, path_hex=path_hex, proxy_url=proxy_url)
        recs = M.parse_records(rbody) if rbody else []
        if not any(t == 0x17 and len(p) > 120 for t, p in recs):
            continue
        info = SC.decrypt_response(rbody, psk_secret, ch, encext_plain, recv_keys=cred.keys)
        info["send_key"] = sk
        return info
    return None


_IP_CACHE = {}


async def async_resolve_ip(host: str = SC.SHORTCLOUD_HOST) -> str:
    if host in _IP_CACHE:
        return _IP_CACHE[host]
    try:
        infos = await asyncio.get_event_loop().getaddrinfo(host, SC.SHORTCLOUD_PORT)
        ip = infos[0][4][0]
    except Exception:
        ip = host
    _IP_CACHE[host] = ip
    return ip


async def async_fetch_login_buffer(client: httpx.AsyncClient, guid: str, access_token: str,
                                   unionid=None, user_type: int = 1, proxy_url: str = "",
                                   user_id: str | None = None, refresh_token: str = "") -> dict:
    # 正常运行态按应用宝 Go 版形状传 openid；保留 guid 兜底，兼容旧调试脚本。
    login_user_id = user_id or guid
    body = {"extInfo": {"listS": {"unionid": {"value": [unionid]},
                                  "user_id": {"value": [login_user_id]},
                                  "access_token": {"value": [access_token]}},
                        "listI": {"user_type": {"value": [user_type]}}}}
    headers, body_str = pcyyb.ual_headers(body, "pc_yyb_auth", pcyyb.PC_YYB_AUTH_KEY, guid=guid)
    if login_user_id and refresh_token:
        headers["Cookie"] = f"openid={login_user_id}; accesstoken={access_token}; refreshtoken={refresh_token}"
    url = f"{pcyyb.HOST}/pc_yyb_auth/pcyyb_get_wx_login_buffer_auth"
    content = body_str.encode("utf-8")
    if proxy_url:
        async with httpx.AsyncClient(verify=False, timeout=20.0, proxy=proxy_url) as pc:
            r = await pc.post(url, content=content, headers=headers)
    else:
        r = await client.post(url, content=content, headers=headers)
    text = r.text
    data = json.loads(text) if text.strip().startswith("{") else {}
    lb = (((data.get("ext_info") or {}).get("list_s") or {}).get("login_buffer") or {}).get("value", [None])[0]
    return {"code": data.get("code"), "msg": data.get("msg"), "login_buffer": lb}


async def _async_fetch_once(target_appid: str, cred, device_id: bytes,
                            client: httpx.AsyncClient, shortcloud_ip: str, proxy_url: str) -> str:
    r = await async_fetch_login_buffer(client, cred.guid, cred.access_token, proxy_url=proxy_url)
    if r.get("code") != 0 or not r.get("login_buffer"):
        raise RuntimeError(f"取 login_buffer 失败: {r.get('msg')}（token 过期/被抢新，2h 内重试或重扫）")
    ld = CA.login_data_from_buffer(r["login_buffer"])
    payload = SC.build_getcode_payload(target_appid)
    ap, ps, crd = await async_prepare_session(device_id, ld, proxy_url=proxy_url)
    info = await async_getcode_with_session(ap, ps, crd, payload, ip=shortcloud_ip, proxy_url=proxy_url)
    if not info:
        raise RuntimeError("未收到成功响应")
    if info.get("link_err"):
        raise RuntimeError(f"服务器返回 link_err={info['link_err']}")
    if not info.get("payload"):
        raise RuntimeError("cmd2881 响应 crypto16 未解")
    code = extract_code(info["payload"])
    if not code:
        raise RuntimeError("响应中未解析出 code")
    return code


async def async_fetch(target_appid: str, cred, device_id: bytes, client: httpx.AsyncClient,
                      shortcloud_ip: str = "", retries: int = 1, proxy_url: str = "") -> str:
    if not cred or not cred.access_token or not cred.guid:
        raise RuntimeError("凭据缺 access_token/guid")
    proxy_url = proxy_url or getattr(cred, "proxyUrl", "") or ""
    last = None
    for _ in range(retries + 1):
        try:
            return await _async_fetch_once(target_appid, cred, device_id, client, shortcloud_ip, proxy_url)
        except RuntimeError as e:
            last = e
    raise last


# ───────────────────────── OAuth2 公众号授权 响应解析（合并自纯协议更新） ─────────────────────────

def _pb_get_field(b: bytes, fid: int):
    p, n = 0, len(b)
    while p < n:
        try:
            tag, p = _rv(b, p)
        except Exception:
            return None
        wt = tag & 7; cur = tag >> 3
        if wt == 2:
            l, p = _rv(b, p); v = b[p:p + l]; p += l
            if cur == fid:
                return v
        elif wt == 0:
            _, p = _rv(b, p)
        elif wt == 5:
            p += 4
        elif wt == 1:
            p += 8
        else:
            return None
    return None


def _pb_get_varint(b: bytes, fid: int):
    p, n = 0, len(b)
    while p < n:
        try:
            tag, p = _rv(b, p)
        except Exception:
            return None
        wt = tag & 7; cur = tag >> 3
        if wt == 0:
            val, p = _rv(b, p)
            if cur == fid:
                if val > (1 << 63):
                    val -= (1 << 64)
                return val
        elif wt == 2:
            l, p = _rv(b, p); p += l
        elif wt == 5:
            p += 4
        elif wt == 1:
            p += 8
        else:
            return None
    return None


def _pb_get_repeated_field(payload: bytes, fid: int):
    """枚举 payload 中指定 fid 的所有 length-delimited 字段值。"""
    p, n = 0, len(payload)
    while p < n:
        try:
            tag, p = _rv(payload, p)
        except Exception:
            return
        wt = tag & 7; cur = tag >> 3
        if wt == 2:
            l, p = _rv(payload, p); v = payload[p:p + l]; p += l
            if cur == fid:
                yield v
        elif wt == 0:
            _, p = _rv(payload, p)
        elif wt == 5:
            p += 4
        elif wt == 1:
            p += 8
        else:
            return


def _looks_like_base_response(b: bytes) -> bool:
    """BaseResponse：field1=ret(varint)，不是 length 字符串（排除 sctx 的 sessionkey）。"""
    if not b:
        return False
    if _pb_get_field(b, 1) is not None:
        return False
    return _pb_get_varint(b, 1) is not None


def _looks_like_oauth_body(b: bytes) -> bool:
    """OauthAuthorizeResp/ConfirmResp：field1=BaseResponse。"""
    if not b:
        return False
    base = _pb_get_field(b, 1)
    return bool(base and _looks_like_base_response(base))


def _pb_errmsg(base: bytes):
    """取出 BaseResponse.errmsg：微信常再包一层 SKBuiltinString(f1=string)。"""
    if not base:
        return None
    raw = _pb_get_field(base, 2)
    if raw is None:
        return None
    inner = _pb_get_field(raw, 1)
    if inner:
        try:
            t = inner.decode("utf-8").replace("\x00", "").strip()
            if t:
                return t
        except Exception:
            pass
    try:
        s = raw.decode("utf-8").replace("\x00", "").strip("\n\r\t ")
    except Exception:
        return None
    if not s or s[0] in "\n\r\t" or (len(s) > 1 and s[0] == "#" and "scope" in s):
        p, n = 0, len(raw)
        while p < n:
            try:
                tag, p = _rv(raw, p)
            except Exception:
                break
            wt = tag & 7
            if wt == 2:
                try:
                    l, p = _rv(raw, p)
                except Exception:
                    break
                v = raw[p:p + l]
                p += l
                try:
                    t = v.decode("utf-8").replace("\x00", "").strip()
                except Exception:
                    continue
                if t and any(c.isalpha() or "一" <= c <= "鿿" for c in t):
                    return t
            elif wt == 0:
                try:
                    _, p = _rv(raw, p)
                except Exception:
                    break
            elif wt == 5:
                p += 4
            elif wt == 1:
                p += 8
            else:
                break
        return None
    return s or None


def _unwrap_oauth_body(payload: bytes) -> bytes:
    """取出业务 OauthAuthorize(Confirm)Resp。

    shortcloud/wxaruntime 响应与 getcode/getphone 相同：
      outer{ f1=传输BaseResponse, f2=业务体, f3="" }
    旧逻辑若把 outer 当业务体，会得到 ret=0 且 scope/redirect 全空（业务错误在 f2 里）。
    """
    if not payload:
        return payload
    top_base = _pb_get_field(payload, 1)
    f2 = _pb_get_field(payload, 2)
    if top_base and _looks_like_base_response(top_base) and f2:
        if _looks_like_oauth_body(f2):
            return f2
        if _pb_get_field(f2, 1) and _looks_like_base_response(_pb_get_field(f2, 1)):
            return f2
    if _looks_like_oauth_body(payload):
        return payload
    nested = _pb_get_field(payload, 5)
    if nested:
        body = _pb_get_field(nested, 3)
        if body and _looks_like_oauth_body(body):
            return body
        if _looks_like_oauth_body(nested):
            return nested
    body = _pb_get_field(payload, 3)
    if body and _looks_like_oauth_body(body):
        return body
    return payload


def parse_oauth_response(payload: bytes) -> dict:
    """解析 oauth_authorize / oauth_authorize_confirm 响应。

    传输层: outer{1=BaseResponse, 2=业务体}（与 js-login/getphone 一致）

    OauthAuthorizeResp (mm123.proto):
      1 BaseResponse
      2 scopeList (repeated bytes/ScopeInfo)
      4 appname
      5 appiconUrl
      6 redirectUrl
      9 isRecentHasAuth
      10 isSlienctAuth

    Confirm 响应常见:
      1 BaseResponse
      2 redirectUrl
    """
    def _utf8(v):
        if v is None:
            return None
        try:
            return v.decode("utf-8")
        except Exception:
            return None

    def _parse_scope_item(raw: bytes) -> dict | None:
        if not raw:
            return None
        scope = _utf8(_pb_get_field(raw, 1))
        if scope and (scope.startswith("snsapi_") or scope.startswith("scope")):
            return {
                "scope": scope,
                "desc": _utf8(_pb_get_field(raw, 2)),
                "auth_state": _pb_get_varint(raw, 3),
                "ext_desc": _utf8(_pb_get_field(raw, 4)),
                "auth_sub_desc": _utf8(_pb_get_field(raw, 5)),
            }
        s = _utf8(raw)
        if s and (s.startswith("snsapi_") or s.startswith("scope") or len(s) < 64):
            if all(32 <= ord(c) < 127 for c in s) or s.startswith("snsapi"):
                return {"scope": s, "desc": None, "auth_state": None,
                        "ext_desc": None, "auth_sub_desc": None}
        if scope:
            return {
                "scope": scope,
                "desc": _utf8(_pb_get_field(raw, 2)),
                "auth_state": _pb_get_varint(raw, 3),
                "ext_desc": _utf8(_pb_get_field(raw, 4)),
                "auth_sub_desc": _utf8(_pb_get_field(raw, 5)),
            }
        return None

    body = _unwrap_oauth_body(payload)
    base = _pb_get_field(body, 1)
    ret = None
    errmsg = None
    if base and _looks_like_base_response(base):
        ret = _pb_get_varint(base, 1)
        errmsg = _pb_errmsg(base)

    redirect_url = _utf8(_pb_get_field(body, 6))
    if redirect_url is None:
        f2b = _pb_get_field(body, 2)
        f2s = _utf8(f2b)
        if f2s and (f2s.startswith("http://") or f2s.startswith("https://") or f2s.startswith("weixin://")):
            redirect_url = f2s
    if redirect_url is None:
        cand = _utf8(_pb_get_field(body, 4))
        if cand and (cand.startswith("http://") or cand.startswith("https://")):
            redirect_url = cand
    if redirect_url and (redirect_url.startswith("/ilink/") or redirect_url.startswith("/cgi-bin/")):
        redirect_url = None

    is_recent = _pb_get_varint(body, 9)
    if is_recent is None:
        v = _pb_get_varint(body, 2)
        if v is not None and redirect_url is None:
            is_recent = v
    is_slient = _pb_get_varint(body, 10)
    if is_slient is None:
        is_slient = _pb_get_varint(body, 3)

    scope_list = []
    for fid in (3, 2, 5):
        for item in _pb_get_repeated_field(body, fid):
            parsed = _parse_scope_item(item)
            if not parsed or not parsed.get("scope"):
                continue
            if parsed["scope"].startswith("http"):
                continue
            scope_list.append(parsed)
        if scope_list:
            break

    appname = _utf8(_pb_get_field(body, 4))
    appicon = _utf8(_pb_get_field(body, 5))
    if appname and appname.startswith("http"):
        appname = None
    if appicon and not (appicon.startswith("http://") or appicon.startswith("https://")):
        appicon = None

    avatar_list = []

    ok = (ret == 0)
    out = {
        "ok": ok, "ret": ret, "errmsg": errmsg,
        "redirect_url": redirect_url,
        "is_recent_has_auth": is_recent,
        "is_slient_auth": is_slient,
        "scope_list": scope_list,
        "avatar_list": avatar_list,
        "payload": payload,
    }
    if appname:
        out["appname"] = appname
    if appicon:
        out["appicon_url"] = appicon
    return out

