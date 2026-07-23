"""cmd3453 ManualAuth(Type5 SDK-token) 登录请求构造。"""
from __future__ import annotations

import os
import struct

from . import ilink_packet as P
from . import hybrid_ecdh as H

CMDID_MANUALAUTH = 3453
SCENE = 1901

def _varint(v: int) -> bytes:
    out = bytearray()
    while True:
        b = v & 0x7F; v >>= 7
        if v: out.append(b | 0x80)
        else: out.append(b); return bytes(out)

def _pb_len(field: int, data: bytes) -> bytes:
    return _varint((field << 3) | 2) + _varint(len(data)) + data

def _pb_varint(field: int, val: int) -> bytes:
    return _varint((field << 3) | 0) + _varint(val)

def gen_device_id() -> bytes:
    return os.urandom(32)

def build_manualauth_request(device_id: bytes, auth_payload: bytes,
                             login_kind: int = 4, field7: int = 0, field8: int = 6) -> bytes:
    base = _pb_len(1, device_id)
    verify = _pb_len(1, auth_payload)
    return b"".join([
        _pb_len(1, base),
        _pb_varint(2, SCENE),
        _pb_len(3, verify),
        _pb_varint(4, login_kind),
        _pb_len(6, b""),
        _pb_varint(7, field7),
        _pb_varint(8, field8),
    ])

def build_login_packet(server_static_pub: bytes, device_id: bytes, auth_payload: bytes):
    req = build_manualauth_request(device_id, auth_payload)
    sess = H.encrypt_body_ex(server_static_pub, req)
    pkt = P.build_ilink_packet(CMDID_MANUALAUTH, sess.body,
                               crypto=P.CRYPTO_HYBRID, compress=P.COMPRESS_LZ4)
    return pkt, sess

def login(session, server_static_pub: bytes, device_id: bytes, auth_payload: bytes,
          timeout: float = 8.0, server_static_verifykey: bytes | None = None) -> dict:
    pkt, ecdh_sess = build_login_packet(server_static_pub, device_id, auth_payload)
    session.send_app(pkt)
    resp_inner = session.recv_app()

    resp = P.parse_ilink_response(resp_inner)

    body = resp.get("body") or b""
    if resp.get("crypto") == P.CRYPTO_HYBRID and body:
        try:
            resp["decrypted_body"] = H.decrypt_body(
                ecdh_sess.client_priv, body,
                server_static_verifykey=server_static_verifykey,
                require_signature=False,
            )
        except Exception as exc:
            resp["decrypt_error"] = f"{type(exc).__name__}: {exc}"
    return resp
