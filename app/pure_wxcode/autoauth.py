"""TDI 设备凭据(tdi_account.txt)加载，提供 device_id。"""
from __future__ import annotations

import base64
import os
from dataclasses import dataclass

from . import ilink_packet as P
from . import hybrid_ecdh as H

CMDID_AUTOAUTH = 3198
SCENE = 1901

SK_G1 = bytes.fromhex(
    "04e6fe85b5ab946f9916db2b64f0a2d830efd08968c34d7ec8476c85574059ea"
    "926dd8f1e802c0083af95ae0cf3944c60dc468895a5738e7e8141b0f411767a783")

DEFAULT_TDI = (r"D:\爱学习的呆子\算法逆向工作间\应用宝分析文件夹"
               r"\AndrowsData\wmpf_data\ilink\default\tdi\ilinkapp_060000b7b93f6c\0\6\tdi_account.txt")

DEVICE_ID_HEX = "08ca94032b01920e4636249859afd6512235e78fd67a367ebc2481438205acf4"


def get_device_id() -> bytes:
    """跨平台 device_id：环境变量 YYB_DEVICE_ID(hex) > TDI 文件 > 内置常量。Linux 部署走内置常量即可。"""
    env = os.environ.get("YYB_DEVICE_ID")
    if env:
        return bytes.fromhex(env.strip())
    try:
        if os.path.exists(DEFAULT_TDI):
            return load_tdi().device_id
    except Exception:
        pass
    return bytes.fromhex(DEVICE_ID_HEX)

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

@dataclass
class TdiCreds:
    device_id: bytes
    autoauth_key: bytes
    autoauth_enc_key: bytes
    server_id: bytes
    uin: int
    username: str
    raw: dict

    def summary(self) -> str:
        return (f"device_id={len(self.device_id)}B autoauth_key={len(self.autoauth_key)}B "
                f"autoauth_enc_key={len(self.autoauth_enc_key)}B server_id={len(self.server_id)}B "
                f"uin={self.uin} user={self.username[:24]}...")

def load_tdi(path: str = DEFAULT_TDI) -> TdiCreds:
    kv: dict[str, str] = {}
    with open(path, encoding="utf-8") as fp:
        for line in fp:
            line = line.rstrip("\r\n")
            if "=" in line:
                k, v = line.split("=", 1)
                kv[k.strip()] = v
    def b64(k: str) -> bytes:
        return base64.b64decode(kv[k]) if kv.get(k) else b""
    return TdiCreds(
        device_id=b64("kTdiKeyDeviceId"),
        autoauth_key=b64("kTdiKeyAutoauthKey"),
        autoauth_enc_key=b64("kTdiKeyAutoauthEncKey"),
        server_id=b64("kTdiKeyServerId"),
        uin=int(kv.get("kTdiKeyUin", "0")),
        username=kv.get("kTdiKeyUsername", ""),
        raw=kv,
    )

def build_base_request(device_id: bytes) -> bytes:
    return _pb_len(1, device_id)

def build_autoauth_request(creds: TdiCreds, *,
                           field3_kind: str = "autoauth_key",
                           field4: bytes | None = None) -> bytes:
    body = _pb_len(1, build_base_request(creds.device_id))
    body += _pb_varint(2, SCENE)
    f3map = {
        "autoauth_key": creds.autoauth_key,
        "server_id": creds.server_id,
        "username": creds.username.encode("utf-8"),
        "empty": None,
    }
    f3 = f3map[field3_kind]
    if f3:
        body += _pb_len(3, f3)
    if field4:
        body += _pb_len(4, field4)
    return body

@dataclass
class AutoAuthPacket:
    pkt: bytes
    plaintext: bytes
    ecdh_sess: H.HybridEcdhSession

def build_autoauth_packet(creds: TdiCreds, *,
                          server_static_pub: bytes = SK_G1,
                          field3_kind: str = "autoauth_key",
                          field4: bytes | None = None,
                          with_enckey: bool = True,
                          hash_narrow: bool = True,
                          wpkg_uin: bool = False,
                          wpkg_server_id: bytes | None = None,
                          shared_key_len: int = H.AUTOAUTH_SHARED_KEY_LEN,
                          body_key_len: int = H.AUTOAUTH_BODY_KEY_LEN) -> AutoAuthPacket:
    plaintext = build_autoauth_request(creds, field3_kind=field3_kind, field4=field4)
    enc_key = creds.autoauth_enc_key if with_enckey else None
    sess = H.encrypt_body_autoauth(
        server_static_pub, plaintext, enc_key,
        shared_key_len=shared_key_len, body_key_len=body_key_len, hash_narrow=hash_narrow)
    extra_numeric = {2: creds.uin, 22: creds.uin} if wpkg_uin else None
    extra_strings = {27: wpkg_server_id} if wpkg_server_id is not None else None
    pkt = P.build_ilink_packet(CMDID_AUTOAUTH, sess.body,
                               crypto=P.CRYPTO_HYBRID, compress=P.COMPRESS_LZ4,
                               extra_numeric=extra_numeric, extra_strings=extra_strings)
    return AutoAuthPacket(pkt=pkt, plaintext=plaintext, ecdh_sess=sess)

def send_autoauth(session, creds: TdiCreds, **kw) -> dict:
    req = build_autoauth_packet(creds, **kw)
    session.send_app(req.pkt)
    resp_inner = session.recv_app()
    resp = P.parse_ilink_response(resp_inner)

    def signed(x: int) -> int:
        return x - (1 << 64) if x > (1 << 63) else x

    resp["link_err_signed"] = signed(resp.get("link_err", 0))
    f23 = resp["numeric"].get(23, 0)
    resp["field23"] = f23
    resp["field23_signed"] = signed(f23)
    resp["request_plaintext_hex"] = req.plaintext[:64].hex()
    resp["request_pkt_len"] = len(req.pkt)

    body = resp.get("body") or b""
    if resp.get("crypto") == P.CRYPTO_HYBRID and body:
        try:
            resp["decrypted_body"] = H.decrypt_body(req.ecdh_sess.client_priv, body)
        except Exception as exc:
            resp["decrypt_error"] = f"{type(exc).__name__}: {exc}"
    return resp

if __name__ == "__main__":

    c = load_tdi()
    print("TDI:", c.summary())
    for kind in ("autoauth_key", "server_id", "empty"):
        p = build_autoauth_packet(c, field3_kind=kind)
        print(f"  field3={kind:12s} plaintext={len(p.plaintext)}B body={len(p.ecdh_sess.body)}B "
              f"pkt={len(p.pkt)}B plaintext_head={p.plaintext[:32].hex()}")

    p0 = build_autoauth_packet(c, with_enckey=False)
    print(f"  无encK2(对照)         body={len(p0.ecdh_sess.body)}B（应比含encK2短~62B）")
