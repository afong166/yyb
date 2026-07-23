"""shortcloud 短链 mmtls-over-HTTP 0-RTT 传输 + 端到端取码。"""
from __future__ import annotations
import socket
import struct
import hashlib

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import ilink_mmtls_client as M

SHORTCLOUD_HOST = "shortcloud.weixin.com"
SHORTCLOUD_PORT = 80

def build_mmtls_post(body: bytes, host: str = SHORTCLOUD_HOST, ip: str = "",
                     path_hex: str = "00003c88") -> bytes:
    online = ip or host
    hdr = (f"POST /mmtls/{path_hex} HTTP/1.0\r\n"
           "Accept: */*\r\n"
           "Cache-Control: no-cache\r\n"
           "Connection: close\r\n"
           f"Content-Length: {len(body)}\r\n"
           "Content-Type: application/octet-stream\r\n"
           f"Host: {online}\r\n"
           "Upgrade: mmtls\r\n"
           "User-Agent: MicroMessenger Client\r\n"
           f"X-Online-Host: {online}\r\n\r\n").encode("latin1")
    return hdr + body

def resolve_ip(host: str = SHORTCLOUD_HOST) -> str:
    try:
        return socket.gethostbyname(host)
    except Exception:
        return host

def http_post(body: bytes, host: str = SHORTCLOUD_HOST, port: int = SHORTCLOUD_PORT,
              ip: str = "", path_hex: str = "00003c88", timeout: float = 8.0):
    dst = ip or resolve_ip(host)
    req = build_mmtls_post(body, host=host, ip=ip, path_hex=path_hex)
    s = socket.socket(); s.settimeout(timeout); s.connect((dst, port))
    s.sendall(req)
    raw = b""
    try:
        while True:
            d = s.recv(8192)
            if not d:
                break
            raw += d
    except socket.timeout:
        pass
    s.close()
    status = None; rbody = b""
    if raw.startswith(b"HTTP/"):
        line_end = raw.find(b"\r\n")
        try:
            status = int(raw[9:line_end].split()[0])
        except Exception:
            status = None
        he = raw.find(b"\r\n\r\n")
        if he > 0:
            rbody = raw[he + 4:]
    return status, rbody, raw

def derive_early_keys(psk_access: bytes, psk_client_hello_record: bytes,
                      key_len: int = 16, iv_len: int = 12) -> "M.TrafficKeys":
    ch_payload = psk_client_hello_record[5:]
    hh = hashlib.sha256(ch_payload).digest()
    kb = M.hkdf_expand(psk_access, b"early data key expansion" + hh, key_len + iv_len)
    tk = M.TrafficKeys.__new__(M.TrafficKeys)
    tk.client_key = kb[0:key_len]
    tk.client_nonce = kb[key_len:key_len + iv_len]
    tk.server_key = b""; tk.server_nonce = b""
    return tk

def encrypt_early_record(early_keys: "M.TrafficKeys", seq: int, rec_type: int, plaintext: bytes) -> bytes:
    ct = M.encrypt_record(early_keys.client_key, early_keys.client_nonce, seq, plaintext, rec_type)
    return M.build_record(rec_type, ct)

def build_encrypted_extensions(timestamp: int) -> bytes:
    return (struct.pack(">I", 0x10) + b"\x08" + struct.pack(">I", 0x0b)
            + b"\x01" + struct.pack(">I", 6) + struct.pack(">H", 0x12)
            + struct.pack(">I", timestamp & 0xFFFFFFFF))

EARLY_ALERT_END_OF_EARLY_DATA = struct.pack(">I", 3) + b"\x00\x01\x01"

CMD_WXARUNTIME_TRANSFER = 2881

def _lz4_compress(data: bytes) -> bytes:
    import lz4.block
    return lz4.block.compress(data, store_size=False)

def build_cmd2881(uin: int, server_id: bytes, session_send_key: bytes, payload_protobuf: bytes) -> bytes:
    import os as _os
    from . import ilink_packet as P
    comp = _lz4_compress(payload_protobuf)
    iv = _os.urandom(12)
    ct_tag = AESGCM(session_send_key).encrypt(iv, comp, b"")
    body = ct_tag[:-16] + iv + ct_tag[-16:]
    return P.build_ilink_packet(CMD_WXARUNTIME_TRANSFER, body,
                                crypto=P.CRYPTO_AESGCM, compress=4,
                                extra_numeric={2: uin, 22: uin},
                                extra_strings={27: server_id})

def decrypt_cmd2881_body(session_key: bytes, ilink_packet: bytes) -> bytes:
    import lz4.block
    from . import ilink_packet as P
    info = P.parse_ilink_response(ilink_packet)
    body = info["body"]
    ct, iv, tag = body[:-28], body[-28:-16], body[-16:]
    comp = AESGCM(session_key).decrypt(iv, ct + tag, b"")
    return lz4.block.decompress(comp, uncompressed_size=65536)

def wrap_wxaruntime_transfer(ilink_packet: bytes,
                             cgi: bytes = b"/ilink/ilinkapp/mp/wxaruntime_transfer",
                             host: bytes = b"shortcloud.weixin.com") -> bytes:
    inner = (struct.pack(">H", len(cgi)) + cgi
             + struct.pack(">H", len(host)) + host
             + struct.pack(">I", len(ilink_packet)) + ilink_packet)
    return struct.pack(">I", len(inner)) + inner

def build_getcode_body(ch_record: bytes, early_keys: "M.TrafficKeys",
                       cmd2881_packet: bytes, timestamp: int) -> bytes:
    biz = wrap_wxaruntime_transfer(cmd2881_packet)
    r1 = encrypt_early_record(early_keys, 1, M.RECORD_HANDSHAKE_0RTT, build_encrypted_extensions(timestamp))
    r2 = encrypt_early_record(early_keys, 2, M.RECORD_APPDATA, biz)
    r3 = encrypt_early_record(early_keys, 3, M.RECORD_ALERT, EARLY_ALERT_END_OF_EARLY_DATA)
    return ch_record + r1 + r2 + r3

def response_handshake_key(psk_access: bytes, ch_record: bytes, encext_plain: bytes, server_hello: bytes):
    tr = ch_record[5:] + encext_plain + server_hello
    kb = M.hkdf_expand(psk_access, b"handshake key expansion" + hashlib.sha256(tr).digest(), 28)
    return kb[0:16], kb[16:28]

def decrypt_response(resp_body: bytes, psk_access: bytes, ch_record: bytes, encext_plain: bytes,
                     recv_keys=()):
    import lz4.block
    from . import ilink_packet as P
    recs = M.parse_records(resp_body)
    out = {"records": [(hex(t), len(p)) for t, p in recs], "cmd2881": None, "payload": None, "link_err": None}
    if not recs or recs[0][0] != 0x16:
        return out
    sh = recs[0][1]
    key, iv = response_handshake_key(psk_access, ch_record, encext_plain, sh)
    idx17 = next((i for i, (t, _) in enumerate(recs) if t == 0x17), None)
    if idx17 is None:
        return out

    seq = idx17
    try:
        r2 = AESGCM(key).decrypt(M.record_nonce(iv, seq), recs[idx17][1], M.record_aad(seq, len(recs[idx17][1]), 0x17))
    except Exception:
        return out
    out["cmd2881"] = r2
    info = P.parse_ilink_response(r2)
    out["link_err"] = info["numeric"].get(4, 0) - (1 << 64) if info["numeric"].get(4, 0) > (1 << 63) else info["numeric"].get(4, 0)
    cbody = info["body"]
    for rk in recv_keys:
        try:
            ct, civ, tag = cbody[:-28], cbody[-28:-16], cbody[-16:]
            comp = AESGCM(rk).decrypt(civ, ct + tag, b"")
            out["payload"] = lz4.block.decompress(comp, uncompressed_size=65536)
            break
        except Exception:
            continue
    return out

def prepare_session(device_id: bytes, login_data: bytes):
    from . import pure_login as PL
    sess = M.handshake(verify_server=True)
    try:
        ap = M.access_psk(sess.hs_messages)
        psk_secret = M.access_psk_secret(sess)
        cred = PL.login_on_session(sess, device_id, login_data)
    finally:
        sess.sock.close()
    return ap, psk_secret, cred

def getcode_with_session(access_psk, psk_secret, cred, payload_protobuf: bytes, *,
                         path_hex: str = "00003c88", ip: str = ""):
    import secrets, time as _t
    dst = ip or resolve_ip()
    ts = int(_t.time())
    ch = M.build_psk_client_hello(secrets.token_bytes(32), access_psk, timestamp=ts)
    early = derive_early_keys(psk_secret, ch)
    encext_plain = build_encrypted_extensions(ts)
    for sk in cred.keys:
        cmd2881 = build_cmd2881(cred.uin, cred.server_id, sk, payload_protobuf)
        body = build_getcode_body(ch, early, cmd2881, ts)
        status, rbody, raw = http_post(body, ip=dst, path_hex=path_hex)
        recs = M.parse_records(rbody) if rbody else []
        if not any(t == 0x17 and len(p) > 120 for t, p in recs):
            continue
        info = decrypt_response(rbody, psk_secret, ch, encext_plain, recv_keys=cred.keys)
        info["send_key"] = sk
        return info
    return None

def getcode(device_id: bytes, login_data: bytes, payload_protobuf: bytes, *,
            path_hex: str = "00003c88", ip: str = ""):
    ap, psk_secret, cred = prepare_session(device_id, login_data)
    return getcode_with_session(ap, psk_secret, cred, payload_protobuf, path_hex=path_hex, ip=ip)

def _pb_varint(fid, n):
    tag = bytearray()
    t = (fid << 3)
    while True:
        b = t & 0x7F; t >>= 7
        tag.append(b | (0x80 if t else 0))
        if not t: break
    out = bytearray(tag)
    while True:
        b = n & 0x7F; n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n: break
    return bytes(out)

def _v(n):
    out = bytearray()
    while True:
        b = n & 0x7F; n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n: break
    return bytes(out)

def _pb_len(fid, data):
    if isinstance(data, str):
        data = data.encode()
    return bytes([(fid << 3) | 2]) + _v(len(data)) + data

def _session_ctx(sessionkey_val: int, mac: str, id4: int) -> bytes:
    return (_pb_len(1, "sessionkey") + _pb_varint(2, sessionkey_val)
            + _pb_len(3, mac) + _pb_varint(4, id4) + _pb_len(5, "Windows") + _pb_varint(6, 0))

def build_getcode_payload(target_appid: str, *, host_appid: str = "wxd44977328b36e647",
                          sessionkey_val: int = 145715353, mac: str = "34-5A-60-63-65-E6",
                          id4: int = 1661404927, f7: int = 1029, f8: int = 1610627409,
                          f10: int = 573651281) -> bytes:
    sctx = _session_ctx(sessionkey_val, mac, id4)
    nested = (_pb_len(1, sctx) + _pb_len(2, target_appid) + _pb_varint(4, 1)
              + _pb_len(5, b"") + _pb_len(6, b"") + _pb_varint(7, 1))
    return (_pb_len(1, sctx) + _pb_len(2, "/cgi-bin/mmbiz-bin/js-login") + _pb_len(3, host_appid)
            + _pb_varint(4, 5) + _pb_len(5, nested) + _pb_len(6, target_appid)
            + _pb_varint(7, f7) + _pb_varint(8, f8) + _pb_len(9, "WindowsxWebPlugin") + _pb_varint(10, f10))


# ───────────────────────── 云函数 / 取手机号 payload ─────────────────────────
# Ground truth = WMPF frida browserHook.js buildCloudFunctionBody/buildPhoneNumberBody + doInvokeApi
# (doInvokeApi 直接调 native SendWxaTransferRequest，body=内层 f5，外层信封 native 建 = _wxaruntime_envelope)。
CGI_OPERATEWXDATA = "/cgi-bin/mmbiz-bin/js-operatewxdata"
CGI_GETALLPHONE = "/cgi-bin/mmbiz-bin/wxaapp/customphone/getallphone"
CMDID_OPERATEWXDATA = 1133          # 云函数
CMDID_GETALLPHONE = 2536            # 取手机号（getallphone，非 js-getuserwxphone/1141）


def _wxaruntime_envelope(cgi: str, cmdid: int, nested: bytes, target_appid: str,
                         host_appid: str, sessionkey_val: int, mac: str, id4: int,
                         f8: int, f10: int) -> bytes:
    """外层 wxaruntime_transfer 信封（native SendWxaTransferRequest，所有 cgi 通用）:
       f1=sctx · f2=cgi · f3=host · f4=5 · f5=内层req · f6=target · f7=cmdid · f8 · f9 · f10。"""
    sctx = _session_ctx(sessionkey_val, mac, id4)
    return (_pb_len(1, sctx) + _pb_len(2, cgi) + _pb_len(3, host_appid)
            + _pb_varint(4, 5) + _pb_len(5, nested) + _pb_len(6, target_appid)
            + _pb_varint(7, cmdid) + _pb_varint(8, f8)
            + _pb_len(9, "WindowsxWebPlugin") + _pb_varint(10, f10))


def build_operatewxdata_payload(target_appid: str, data_json: str = "{}", *,
                                host_appid: str = "wxd44977328b36e647",
                                sessionkey_val: int = 145715353, mac: str = "34-5A-60-63-65-E6",
                                id4: int = 1661404927, f8: int = 1610627409,
                                f10: int = 573651281) -> bytes:
    """云函数/operateWXData（cmdid=1133，js-operatewxdata）。
      内层 f5 = {f1=sctx, f2=appId, f3=jsonPayload, f4="", f5=0, f6=0}。
    data_json = param2，例 {"api_name":"<云操作>","data":{...}}。"""
    sctx = _session_ctx(sessionkey_val, mac, id4)
    nested = (_pb_len(1, sctx) + _pb_len(2, target_appid) + _pb_len(3, data_json)
              + _pb_len(4, b"") + _pb_varint(5, 0) + _pb_varint(6, 0))
    return _wxaruntime_envelope(CGI_OPERATEWXDATA, CMDID_OPERATEWXDATA, nested, target_appid,
                                host_appid, sessionkey_val, mac, id4, f8, f10)


def build_getphone_payload(target_appid: str, data_json: str = "", *,
                           host_appid: str = "wxd44977328b36e647",
                           sessionkey_val: int = 145715353, mac: str = "34-5A-60-63-65-E6",
                           id4: int = 1661404927, f8: int = 1610627409,
                           f10: int = 573651281) -> bytes:
    """取手机号（cmdid=2536，/cgi-bin/mmbiz-bin/wxaapp/customphone/getallphone）。
      内层 f5 = {f1=sctx, f2=appId, f3=jsonPayload(可空)}。
    响应 {"wx_phone":{mobile,encryptedData,iv,cloud_id,code}, "custom_phone_list":[...]}。"""
    sctx = _session_ctx(sessionkey_val, mac, id4)
    nested = _pb_len(1, sctx) + _pb_len(2, target_appid) + _pb_len(3, data_json)
    return _wxaruntime_envelope(CGI_GETALLPHONE, CMDID_GETALLPHONE, nested, target_appid,
                                host_appid, sessionkey_val, mac, id4, f8, f10)


def build_oauth_authorize_payload(target_appid: str, url: str, *,
                                  biz_username: str = "", scene: int = 0,
                                  referrer_url: str = "", sub_scene: int | None = None,
                                  auto_oauth: bool | None = None,
                                  host_appid: str = "wxd44977328b36e647",
                                  sessionkey_val: int = 145715353, mac: str = "34-5A-60-63-65-E6",
                                  id4: int = 1661404927, f8: int = 1610627409,
                                  f10: int = 573651281) -> bytes:
    """构造 oauth_authorize 请求 payload。

    对应 ilink 路径: /ilink/ilinkapp/mm/bizoauth/oauth_authorize
    OauthAuthorizeReq 字段:
      1: url (OAuth2 授权 URL)
      2: biz_username (业务用户名)
      3: scene (场景值)
      4: referrer_url (来源 URL)
      5: sub_scene (子场景, 可选)
      6: auto_oauth (自动授权, 可选)
    """
    sctx = _session_ctx(sessionkey_val, mac, id4)
    req_body = _pb_len(1, url)
    if biz_username:
        req_body += _pb_len(2, biz_username)
    req_body += _pb_varint(3, scene)
    if referrer_url:
        req_body += _pb_len(4, referrer_url)
    if sub_scene is not None:
        req_body += _pb_varint(5, sub_scene)
    if auto_oauth is not None:
        req_body += _pb_varint(6, 1 if auto_oauth else 0)
    nested = _pb_len(1, sctx) + _pb_len(2, target_appid) + _pb_len(3, req_body)
    return (_pb_len(1, sctx) + _pb_len(2, "/ilink/ilinkapp/mm/bizoauth/oauth_authorize")
            + _pb_len(3, host_appid) + _pb_varint(4, 5) + _pb_len(5, nested)
            + _pb_len(6, target_appid) + _pb_varint(7, 4313) + _pb_varint(8, f8)
            + _pb_len(9, "WindowsxWebPlugin") + _pb_varint(10, f10))


def build_oauth_authorize_confirm_payload(target_appid: str, oauth_url: str, *,
                                         opt: int = 0, avatar_id: str = "",
                                         redirect_uri: str = "",
                                         host_appid: str = "wxd44977328b36e647",
                                         sessionkey_val: int = 145715353,
                                         mac: str = "34-5A-60-63-65-E6",
                                         id4: int = 1661404927, f8: int = 1610627409,
                                         f10: int = 573651281) -> bytes:
    """构造 oauth_authorize_confirm 请求 payload。

    对应 ilink 路径: /ilink/ilinkapp/mm/bizoauth/oauth_authorize_confirm
    OauthAuthorizeConfirmReq 字段:
      1: oauth_url (OAuth2 URL)
      2: opt (操作类型)
      3: avatar_id (头像 ID)
      4: redirect_uri (回调地址, OauthAuthorizeConfirmResp.redirect_url 含 code)
    """
    sctx = _session_ctx(sessionkey_val, mac, id4)
    req_body = _pb_len(1, oauth_url) + _pb_varint(2, opt)
    if avatar_id:
        req_body += _pb_len(3, avatar_id)
    if redirect_uri:
        req_body += _pb_len(4, redirect_uri)
    nested = _pb_len(1, sctx) + _pb_len(2, target_appid) + _pb_len(3, req_body)
    return (_pb_len(1, sctx) + _pb_len(2, "/ilink/ilinkapp/mm/bizoauth/oauth_authorize_confirm")
            + _pb_len(3, host_appid) + _pb_varint(4, 5) + _pb_len(5, nested)
            + _pb_len(6, target_appid) + _pb_varint(7, 4313) + _pb_varint(8, f8)
            + _pb_len(9, "WindowsxWebPlugin") + _pb_varint(10, f10))


if __name__ == "__main__":

    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    import secrets
    sess = M.handshake(verify_server=True)
    ap = M.access_psk(sess.hs_messages)
    psk_secret = M.access_psk_secret(sess)
    sess.sock.close()
    if not ap:
        print("未取到 access_psk"); sys.exit(1)
    cr = secrets.token_bytes(32)
    ch = M.build_psk_client_hello(cr, ap)
    print(f"PSK CH {len(ch)}B type=0x{ch[0]:02x}  access_psk enc_ticket={len(ap.encrypted_ticket)}B")
    ip = resolve_ip()
    print(f"shortcloud.weixin.com → {ip}:80")

    for phex in ("00003c88", "00000001"):
        status, rbody, raw = http_post(ch, ip=ip, path_hex=phex)
        recs = M.parse_records(rbody) if rbody else []
        print(f"[/mmtls/{phex}] HTTP {status} resp_body={len(rbody)}B records={[(hex(t),len(p)) for t,p in recs]} raw_head={raw[:48]!r}")
