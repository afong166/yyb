"""纯协议 cmd3453 登录 → 会话凭据(3密钥/uin/serverid)。"""
from __future__ import annotations
import hashlib
from dataclasses import dataclass, field as dc_field

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import ilink_mmtls_client as M
from . import ilink_packet as P
from . import hybrid_ecdh as H
from . import type5_auth as T5

try:
    import lz4.block as _lz4
except Exception:
    _lz4 = None

PER_APP_PUBKEY = bytes.fromhex(
    "04ef87876d6478b15f1796eab12068610541173b7176b67f1dcc86683e901acd"
    "44d18b4ac36938251d0812dd0cf842aa2d6cbb8115712d1c0087dcefc14a44cd58")

def _rv(b, p):
    o = s = 0
    while True:
        x = b[p]; p += 1; o |= (x & 0x7F) << s
        if x < 0x80:
            return o, p
        s += 7

def _parse(b):
    f = {}; p, n = 0, len(b)
    while p < n:
        t, p = _rv(b, p); fn, wt = t >> 3, t & 7
        if wt == 0:
            v, p = _rv(b, p)
        elif wt == 2:
            l, p = _rv(b, p); v = b[p:p + l]; p += l
        elif wt == 5:
            v = b[p:p + 4]; p += 4
        elif wt == 1:
            v = b[p:p + 8]; p += 8
        else:
            break
        f.setdefault(fn, []).append(v)
    return f

@dataclass
class SessionCredential:
    keys: list
    server_id: bytes
    uin: int
    session_token: bytes
    username: str
    raw_plain: bytes = dc_field(repr=False, default=b"")

def _build_request(device_id: bytes, login_data: bytes, server_pub: bytes):
    plaintext = T5.build_manualauth_request(device_id, login_data)
    priv = ec.generate_private_key(ec.SECP256R1())
    client_pub = priv.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    shared_req = H.ecdh_shared(priv, server_pub)
    import os as _os
    K = _os.urandom(32)
    one, nid_str = b"1", b"415"
    h1 = H._sha256(one, nid_str, client_pub)
    encK1 = H._aesgcm(shared_req[:24], h1, K)
    body_km = H._hkdf(b"security hdkf expand", K, h1, 56)
    h2 = H._sha256(one, nid_str, client_pub, encK1)
    field5 = H._aesgcm(body_km[:24], h2, H.lz4_literal_block(plaintext))
    reqbody = (H._pb_varint(1, 1)
               + H._pb_len(2, H._pb_varint(1, 415) + H._pb_len(2, client_pub))
               + H._pb_len(3, encK1) + H._pb_len(5, field5))
    return reqbody, priv, body_km, plaintext

def _decrypt_response(resp_body: bytes, client_priv, body_km: bytes, plaintext: bytes) -> bytes:
    top = _parse(resp_body); sec = _parse(top[1][-1])
    server_eph = sec[2][-1]; cred_type = top[2][-1]; ctblob = top[3][-1]
    key = H.ecdh_shared(client_priv, server_eph)[:24]
    aad = hashlib.sha256(body_km[24:56] + H.lz4_literal_block(plaintext)
                         + b"415" + server_eph + str(cred_type).encode()).digest()
    pt = AESGCM(key).decrypt(ctblob[-28:-16], ctblob[:-28] + ctblob[-16:], aad)
    if _lz4:
        for u in (8192, 16384, 32768, 65536, 131072):
            try:
                return _lz4.decompress(pt, uncompressed_size=u)
            except Exception:
                continue
    return pt

def parse_credential(plain: bytes) -> SessionCredential:
    top = _parse(plain)
    detail = _parse(top[3][-1]); sc = _parse(detail[2][-1]); acct = _parse(detail[3][-1])
    return SessionCredential(
        keys=[sc[1][-1], sc[2][-1], sc[4][-1]],
        server_id=sc[9][-1],
        uin=acct[1][-1],
        session_token=sc[12][-1],
        username=acct[2][-1].decode("utf-8", "replace"),
        raw_plain=plain,
    )

def login_on_session(sess, device_id: bytes, login_data: bytes, *,
                     server_pub: bytes = PER_APP_PUBKEY) -> SessionCredential:
    reqbody, priv, body_km, plaintext = _build_request(device_id, login_data, server_pub)
    pkt = P.build_ilink_packet(3453, reqbody, crypto=P.CRYPTO_HYBRID, compress=P.COMPRESS_LZ4)
    sess.send_app(pkt)
    resp = P.parse_ilink_response(sess.recv_app())
    f23 = resp["numeric"].get(23, 0)
    sf23 = f23 - (1 << 64) if f23 > (1 << 63) else f23
    body = resp.get("body") or b""
    if sf23 != 0 or not body:
        raise RuntimeError(f"cmd3453 登录失败 field23={sf23} body={len(body)}B")
    plain = _decrypt_response(body, priv, body_km, plaintext)
    return parse_credential(plain)

def login(device_id: bytes, login_data: bytes, *,
          server_pub: bytes = PER_APP_PUBKEY, host: str = "longcloud.weixin.com") -> SessionCredential:
    sess = M.handshake(host=host, verify_server=True)
    try:
        return login_on_session(sess, device_id, login_data, server_pub=server_pub)
    finally:
        sess.sock.close()

if __name__ == "__main__":
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from pure_wxcode import oauth as OA, pcyyb, autoauth
    cred = OA.load_credentials(None)
    tdi = autoauth.load_tdi()
    r = pcyyb.get_fresh_login_buffer(cred, do_unionid=False)
    if r.get("code") != 0:
        print(f"取 login_buffer 失败: code={r.get('code')} msg={r.get('msg')}"); sys.exit(1)
    from pure_wxcode import cloud_auth as CA
    ld = CA.login_data_from_buffer(r["login_buffer"])
    sc = login(tdi.device_id, ld)
    print(f"✅ 纯协议登录成功！")
    print(f"  uin={sc.uin}")
    print(f"  username={sc.username}")
    print(f"  ServerId={sc.server_id.hex()}")
    print(f"  session_token={sc.session_token[:48]}")
    print(f"  keys(3×24B AES-192): {[k.hex() for k in sc.keys]}")
