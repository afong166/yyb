"""login_buffer → login_data(auth_payload) 提取等云端认证辅助。"""
from __future__ import annotations

import base64
import os
from dataclasses import dataclass, field as dc_field
from typing import Optional

from . import ilink_packet as P
from . import hybrid_ecdh as H

try:
    import lz4.block as _lz4
except Exception:
    _lz4 = None

CMDID_CLOUD_PROXY = 1901
SCENE = 1901
CLOUD_CGI_NAME = "enccloudlogin"
ILINK_APPID = P.ILINK_APPID
WX_AUTH_APPID = "wxd44977328b36e647"

SK_G1 = bytes.fromhex(
    "04e6fe85b5ab946f9916db2b64f0a2d830efd08968c34d7ec8476c85574059ea92"
    "6dd8f1e802c0083af95ae0cf3944c60dc468895a5738e7e8141b0f411767a783"
)
SK_G2 = bytes.fromhex(
    "045b2c744d0375a80b37fb5e57c2f06b40d9a50075035ada0ae4f5caf5afb94d47"
    "e0683a1a2156af4e39a9d80ee312da5726f5cb6aae24aecbd64b36b2af2b7e95"
)

def _varint(v: int) -> bytes:
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)

def _pb_len(field_no: int, data: bytes) -> bytes:
    return _varint((field_no << 3) | 2) + _varint(len(data)) + data

def _pb_varint(field_no: int, val: int) -> bytes:
    return _varint((field_no << 3) | 0) + _varint(val)

def _read_varint(buf: bytes, pos: int):
    out = shift = 0
    while True:
        b = buf[pos]
        pos += 1
        out |= (b & 0x7F) << shift
        if b < 0x80:
            return out, pos
        shift += 7

def parse_pb(buf: bytes) -> dict[int, list]:
    fields: dict[int, list] = {}
    pos, n = 0, len(buf)
    while pos < n:
        tag, pos = _read_varint(buf, pos)
        fno, wt = tag >> 3, tag & 7
        if wt == 0:
            val, pos = _read_varint(buf, pos)
        elif wt == 2:
            ln, pos = _read_varint(buf, pos)
            val = buf[pos:pos + ln]
            pos += ln
        elif wt == 5:
            val = buf[pos:pos + 4]
            pos += 4
        elif wt == 1:
            val = buf[pos:pos + 8]
            pos += 8
        else:
            break
        fields.setdefault(fno, []).append(val)
    return fields

def login_data_from_buffer(login_buffer_b64: str) -> bytes:
    raw = base64.b64decode(login_buffer_b64, validate=True)
    f = parse_pb(raw)
    if 1 not in f:
        raise ValueError("login_buffer 缺 field1(auth_payload)")
    return f[1][-1]

def build_inner_iuserinfo(login_data: bytes) -> bytes:
    return _pb_len(3, login_data)

def build_inner_cloud_delegate(login_data: bytes, task_id: int = 0,
                               cgi_name: str = CLOUD_CGI_NAME, cmd: int = CMDID_CLOUD_PROXY,
                               extra: bytes = b"") -> bytes:
    body = build_inner_iuserinfo(login_data)
    out = b"".join([
        _pb_varint(1, cmd),
        _pb_varint(2, task_id),
        _pb_len(3, cgi_name.encode()),
        _pb_len(4, body),
    ])
    if extra:
        out += _pb_len(5, extra)
    return out

def build_inner_client_req_cloud_app_auth(login_data: bytes, nonce: Optional[bytes] = None) -> bytes:
    if nonce is None:
        nonce = os.urandom(32)
    base = _pb_len(1, nonce)
    return _pb_len(1, base) + _pb_len(3, login_data)

def build_client_req_cloud_app_auth_iida(seed32: Optional[bytes] = None) -> bytes:
    if seed32 is None:
        seed32 = os.urandom(32)
    if len(seed32) != 32:
        raise ValueError("seed32 必须是 32 字节，对应 RunLoginLogic_ 里的 0x20 字节 seed")

    base_request = _pb_len(1, seed32)
    return _pb_len(1, base_request)

def build_cloud_cgi_request_iida(kind: int, task_id: int, payload: bytes,
                                 timeout_ms: int, flag: bool = False) -> bytes:
    out = bytearray()
    out += _pb_varint(1, kind)
    out += _pb_varint(2, task_id)
    out += _pb_len(3, payload)
    out += _pb_varint(4, timeout_ms)
    if flag:

        out += _pb_varint(5, 1)
    else:

        out += _pb_varint(5, 0)
    return bytes(out)

def build_cloud_cgi_auth_request_iida(task_id: int, seed32: Optional[bytes] = None) -> bytes:
    inner = build_client_req_cloud_app_auth_iida(seed32)
    return build_cloud_cgi_request_iida(1, task_id, inner, 10000, False)

def build_cloud_delegate_request_iida(body: bytes, *, task_cmd: int = CMDID_CLOUD_PROXY,
                                      task_extra: bytes = b"",
                                      cgi_name: str = CLOUD_CGI_NAME) -> bytes:
    out = bytearray()
    out += _pb_varint(1, CMDID_CLOUD_PROXY)
    out += _pb_varint(2, task_cmd)
    out += _pb_len(3, task_extra)
    out += _pb_len(4, cgi_name.encode())
    out += _pb_len(5, body)
    return bytes(out)

def build_cloud_netcore_outer_iida(task_id: int, body: bytes, *, task_cmd: int = CMDID_CLOUD_PROXY,
                                   task_extra: bytes = b"", cgi_name: str = CLOUD_CGI_NAME,
                                   timeout_ms: int = 30000, flag: bool = False) -> bytes:
    delegate = build_cloud_delegate_request_iida(
        body,
        task_cmd=task_cmd,
        task_extra=task_extra,
        cgi_name=cgi_name,
    )
    return build_cloud_cgi_request_iida(2, task_id, delegate, timeout_ms, flag)

def build_ilink_app_comm_session_info_iida(auth_field1: bytes = b"", auth_field2: bytes = b"",
                                           auth_field4: bytes = b"", acct_field1: int = 0,
                                           auth_field9: bytes = b"", *,
                                           special_field11_bit0: bool = False) -> bytes:
    out = bytearray()
    if special_field11_bit0:

        out += _pb_varint(4, acct_field1)
        out += _pb_len(5, auth_field9)
        out += _pb_varint(6, 1)
        return bytes(out)

    out += _pb_len(1, auth_field1)
    out += _pb_len(2, auth_field2)
    out += _pb_len(3, auth_field4)
    out += _pb_varint(4, acct_field1)
    out += _pb_len(5, auth_field9)
    return bytes(out)

def _hyp_h1(login_data: bytes, **kw) -> bytes:
    return build_inner_iuserinfo(login_data)

def _hyp_h2(login_data: bytes, **kw) -> bytes:
    return build_inner_cloud_delegate(login_data, task_id=kw.get("task_id", 0))

def _hyp_h3(login_data: bytes, **kw) -> bytes:
    return build_inner_client_req_cloud_app_auth(login_data, nonce=kw.get("nonce"))

def _hyp_h4(_login_data: bytes, **kw) -> bytes:
    return build_cloud_cgi_auth_request_iida(
        task_id=kw.get("task_id", 1),
        seed32=kw.get("seed32") or kw.get("nonce"),
    )

def _hyp_h5(login_data: bytes, **kw) -> bytes:
    body = kw.get("body")
    if body is None:
        body = build_inner_iuserinfo(login_data)
    return build_cloud_netcore_outer_iida(
        task_id=kw.get("task_id", 1),
        body=body,
        task_cmd=kw.get("task_cmd", CMDID_CLOUD_PROXY),
        task_extra=kw.get("task_extra", b""),
        cgi_name=kw.get("cgi_name", CLOUD_CGI_NAME),
        timeout_ms=kw.get("timeout_ms", 30000),
        flag=kw.get("flag", False),
    )

HYPOTHESES = {
    "H1": ("IlinkUserInfo{f3=login_data} 直发 (最直接)", _hyp_h1),
    "H2": ("IlinkCloudDelegateRequest{f1=1901,f2=taskid,f3='enccloudlogin',f4=IlinkUserInfo}", _hyp_h2),
    "H3": ("ClientReqForCloudAppAuth{BaseRequest{f1=32B nonce}} + login_data", _hyp_h3),
    "H4": ("iida: CloudCgiRequest{f1=1,f3=ClientReqForCloudAppAuth(seed32),f4=10000}", _hyp_h4),
    "H5": ("iida: CloudCgiRequest{f1=2,f3=IlinkCloudDelegateRequest exact}", _hyp_h5),
}

@dataclass
class CloudAuthRequest:
    pkt: bytes
    ecdh_sess: object
    plaintext: bytes
    cmdid: int
    crypto: int
    compress: int
    hypothesis: str

def build_cloud_auth_packet(login_data: bytes, *, hypothesis: str = "H1",
                            cmdid: int = CMDID_CLOUD_PROXY,
                            server_static_pub: bytes = SK_G1,
                            crypto: int = P.CRYPTO_HYBRID,
                            compress: int = P.COMPRESS_LZ4,
                            **hyp_kwargs) -> CloudAuthRequest:
    if hypothesis not in HYPOTHESES:
        raise ValueError(f"未知假设 {hypothesis}，可选 {list(HYPOTHESES)}")
    _desc, fn = HYPOTHESES[hypothesis]
    plaintext = fn(login_data, **hyp_kwargs)

    if crypto == P.CRYPTO_HYBRID:
        sess = H.encrypt_body_ex(server_static_pub, plaintext)
        body = sess.body
    elif crypto == P.CRYPTO_RAW:
        sess = None
        body = H.lz4_literal_block(plaintext) if compress == P.COMPRESS_LZ4 else plaintext
    else:
        raise ValueError(f"不支持 crypto={crypto}")

    pkt = P.build_ilink_packet(cmdid, body, crypto=crypto, compress=compress)
    return CloudAuthRequest(pkt=pkt, ecdh_sess=sess, plaintext=plaintext,
                            cmdid=cmdid, crypto=crypto, compress=compress, hypothesis=hypothesis)

@dataclass
class CloudAuthResult:
    link_err: int
    cmdid: int
    resp_crypto: int
    resp_compress: int
    resp_body_len: int
    decrypted: Optional[bytes] = None
    decompressed: Optional[bytes] = None
    base_ret: Optional[int] = None
    session_token: Optional[bytes] = None
    success: bool = False
    notes: list = dc_field(default_factory=list)
    raw_resp_hex: str = ""

def _lz4_try_decompress(data: bytes) -> Optional[bytes]:
    if _lz4 is None:
        return None
    for usize in (4096, 8192, 16384, 32768, 65536):
        try:
            return _lz4.decompress(data, uncompressed_size=usize)
        except Exception:
            continue
    return None

def parse_cloud_auth_response(resp_inner: bytes, ecdh_sess=None,
                              *, server_static_verifykey: Optional[bytes] = None) -> CloudAuthResult:
    resp = P.parse_ilink_response(resp_inner)
    body = resp.get("body") or b""
    res = CloudAuthResult(
        link_err=_signed(resp.get("link_err", 0)),
        cmdid=resp.get("cmdid", 0),
        resp_crypto=resp.get("crypto", 0),
        resp_compress=resp.get("compress", 0),
        resp_body_len=len(body),
        raw_resp_hex=resp_inner[:64].hex(),
    )
    if not body:
        res.notes.append("响应无 body（多为前置 gate 拦截，看 link_err）")
        return res

    plain = None
    if resp.get("crypto") == P.CRYPTO_HYBRID and ecdh_sess is not None:
        try:
            plain = H.decrypt_body(ecdh_sess.client_priv, body,
                                   server_static_verifykey=server_static_verifykey,
                                   require_signature=False)
            res.decrypted = plain
        except Exception as exc:
            res.notes.append(f"HybridEcdh 解密失败: {type(exc).__name__}: {exc}")
    elif resp.get("crypto") in (0, None):
        plain = body
        res.decrypted = plain
    else:
        res.notes.append(f"未解密 (crypto={resp.get('crypto')}, ecdh_sess={'有' if ecdh_sess else '无'})")

    if plain is None:
        return res

    decompressed = plain
    if resp.get("compress") == P.COMPRESS_LZ4 or (plain[:1] and plain[0] & 0xF0 in (0xF0, 0x00, 0xF3)):
        d = _lz4_try_decompress(plain)
        if d is not None:
            decompressed = d
        else:

            decompressed = _decode_lz4_literal_loose(plain) or plain
    res.decompressed = decompressed

    try:
        _eval_success(decompressed, res)
    except Exception as exc:
        res.notes.append(f"结构判定异常: {type(exc).__name__}: {exc}")
    return res

def _eval_success(plain: bytes, res: CloudAuthResult) -> None:
    top = parse_pb(plain)

    if 1 in top:
        f1 = top[1][-1]
        if isinstance(f1, bytes):
            base = parse_pb(f1)
            if 1 in base and isinstance(base[1][-1], int):
                res.base_ret = base[1][-1]

    if 3 in top and isinstance(top[3][-1], bytes):
        detail = parse_pb(top[3][-1])
        if 2 in detail and isinstance(detail[2][-1], bytes):
            cred = parse_pb(detail[2][-1])
            if 12 in cred and isinstance(cred[12][-1], bytes):
                res.session_token = cred[12][-1]
    tok_ok = bool(res.session_token and res.session_token.startswith(b"ilinkst_"))
    res.success = (res.base_ret == 0) and tok_ok
    if res.base_ret is not None:
        res.notes.append(f"BaseResponse.ret={res.base_ret}")
    if res.session_token:
        res.notes.append(f"session_token={res.session_token[:40]!r}")

def _decode_lz4_literal_loose(data: bytes) -> Optional[bytes]:
    try:
        n = len(data)
        if n == 0:
            return None
        token = data[0]
        lit_len = token >> 4
        pos = 1
        if lit_len == 0xF:
            while pos < n and data[pos] == 0xFF:
                lit_len += 255
                pos += 1
            if pos < n:
                lit_len += data[pos]
                pos += 1
        return data[pos:pos + lit_len] if lit_len else None
    except Exception:
        return None

def _signed(v: int) -> int:
    if v > (1 << 63):
        return v - (1 << 64)
    return v

def send_cloud_auth(session, login_data: bytes, *, hypothesis: str = "H1",
                    cmdid: int = CMDID_CLOUD_PROXY, server_static_pub: bytes = SK_G1,
                    crypto: int = P.CRYPTO_HYBRID, compress: int = P.COMPRESS_LZ4,
                    server_static_verifykey: Optional[bytes] = None,
                    **hyp_kwargs) -> CloudAuthResult:
    req = build_cloud_auth_packet(login_data, hypothesis=hypothesis, cmdid=cmdid,
                                  server_static_pub=server_static_pub,
                                  crypto=crypto, compress=compress, **hyp_kwargs)
    session.send_app(req.pkt)
    resp_inner = session.recv_app()
    return parse_cloud_auth_response(resp_inner, req.ecdh_sess,
                                     server_static_verifykey=server_static_verifykey)

def selftest_against_sample(sample_path: str) -> None:
    plain = open(sample_path, "rb").read()
    res = CloudAuthResult(link_err=0, cmdid=CMDID_CLOUD_PROXY, resp_crypto=0,
                          resp_compress=0, resp_body_len=len(plain), decompressed=plain)
    _eval_success(plain, res)
    print("=== selftest against dec_plain.bin ===")
    print(f"  base_ret      = {res.base_ret}")
    print(f"  session_token = {res.session_token[:48] if res.session_token else None!r}")
    print(f"  success       = {res.success}")
    assert res.base_ret == 0, "样本 BaseResponse.ret 应为 0"
    assert res.session_token and res.session_token.startswith(b"ilinkst_"), "样本应含 ilinkst_ 会话令牌"
    assert res.success, "样本应判定为成功"
    print("  [OK] 成功判定逻辑对样本 PASS")

if __name__ == "__main__":
    import sys
    sp = sys.argv[1] if len(sys.argv) > 1 else \
        r"D:\爱学习的呆子\算法逆向工作间\应用宝分析文件夹\pure_wxcode\capture\dec_plain.bin"
    selftest_against_sample(sp)
