"""MMTLS longcloud/shortcloud 握手、记录加解密、PSK 票据与密钥派生。"""
from __future__ import annotations

import hashlib
import hmac
import struct
from dataclasses import dataclass, field as dc_field
from typing import Optional

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MMTLS_VERSION = b"\xf1\x03"
RECORD_HANDSHAKE = 0x16
RECORD_HANDSHAKE_0RTT = 0x19
RECORD_APPDATA = 0x17
RECORD_ALERT = 0x15
CIPHER_ECDHE_AES128_GCM = 0xC02B
HASH_LEN = 32
KEY_LEN = 16
IV_LEN = 12

def gen_keypair() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())

def pub_uncompressed(priv: ec.EllipticCurvePrivateKey) -> bytes:
    return priv.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)

def ecdh_shared(client_priv: ec.EllipticCurvePrivateKey, server_pub_bytes: bytes) -> bytes:
    pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), server_pub_bytes)
    raw_x = client_priv.exchange(ec.ECDH(), pub)
    return hashlib.sha256(raw_x).digest()

def hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    out, t, i = b"", b"", 0
    while len(out) < length:
        i += 1
        t = hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        out += t
    return out[:length]

def mmtls_hkdf(share_key: bytes, label: str, handshake_hash: Optional[bytes], length: int) -> bytes:
    info = label.encode() + (handshake_hash or b"")
    return hkdf_expand(share_key, info, length)

def compute_share_key(client_priv: ec.EllipticCurvePrivateKey, server_pub_bytes: bytes) -> bytes:
    return ecdh_shared(client_priv, server_pub_bytes)

class TrafficKeys:

    def __init__(self, share_key: bytes, label: str, handshake_hash: bytes):
        kb = mmtls_hkdf(share_key, label, handshake_hash, 2 * (KEY_LEN + IV_LEN))
        self.client_key = kb[0:16]
        self.server_key = kb[16:32]
        self.client_nonce = kb[32:44]
        self.server_nonce = kb[44:56]

def handshake_keys(share_key: bytes, transcript: bytes) -> TrafficKeys:
    return TrafficKeys(share_key, "handshake key expansion", hashlib.sha256(transcript).digest())

def server_finished_expected(share_key: bytes, transcript_before_sf: bytes) -> bytes:
    sf_key = mmtls_hkdf(share_key, "server finished", None, HASH_LEN)
    return hmac.new(sf_key, hashlib.sha256(transcript_before_sf).digest(), hashlib.sha256).digest()

def client_finished(share_key: bytes, transcript_incl_sf: bytes) -> bytes:
    cf_key = mmtls_hkdf(share_key, "client finished", None, HASH_LEN)
    return hmac.new(cf_key, hashlib.sha256(transcript_incl_sf).digest(), hashlib.sha256).digest()

def expanded_secret(share_key: bytes, transcript: bytes) -> bytes:
    return mmtls_hkdf(share_key, "expanded secret", hashlib.sha256(transcript).digest(), HASH_LEN)

def app_keys(share_key: bytes, transcript: bytes) -> TrafficKeys:
    exp = expanded_secret(share_key, transcript)
    return TrafficKeys(exp, "application data key expansion", hashlib.sha256(transcript).digest())

def wrap_handshake_msg(msg_type: int, body: bytes) -> bytes:
    inner = bytes([msg_type]) + struct.pack(">H", len(body)) + body
    return struct.pack(">I", len(inner)) + inner

def record_nonce(static_nonce: bytes, seq: int) -> bytes:
    n = bytearray(static_nonce)
    n[11] ^= (seq & 0xFF)
    return bytes(n)

def record_aad(seq: int, ciphertext_len: int, rec_type: int = RECORD_HANDSHAKE) -> bytes:
    return seq.to_bytes(8, "big") + bytes([rec_type]) + MMTLS_VERSION + struct.pack(">H", ciphertext_len)

def decrypt_record(key: bytes, static_nonce: bytes, seq: int, ciphertext: bytes,
                   rec_type: int = RECORD_HANDSHAKE) -> bytes:
    return AESGCM(key).decrypt(record_nonce(static_nonce, seq), ciphertext, record_aad(seq, len(ciphertext), rec_type))

def encrypt_record(key: bytes, static_nonce: bytes, seq: int, plaintext: bytes,
                   rec_type: int = RECORD_HANDSHAKE) -> bytes:
    ct_len = len(plaintext) + 16
    return AESGCM(key).encrypt(record_nonce(static_nonce, seq), plaintext, record_aad(seq, ct_len, rec_type))

def parse_records(data: bytes) -> list[tuple[int, bytes]]:
    out, pos = [], 0
    while pos + 5 <= len(data):
        typ = data[pos]
        ver = data[pos + 1:pos + 3]
        ln = struct.unpack(">H", data[pos + 3:pos + 5])[0]
        if ver != MMTLS_VERSION:
            break
        out.append((typ, data[pos + 5:pos + 5 + ln]))
        pos += 5 + ln
    return out

def build_record(typ: int, payload: bytes) -> bytes:
    return bytes([typ]) + MMTLS_VERSION + struct.pack(">H", len(payload)) + payload

import os as _os
_TEMPLATE_CH = None
def _load_template() -> bytes:
    global _TEMPLATE_CH
    if _TEMPLATE_CH is None:
        p = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), "evidence", "mmtls_handshake", "client_hello_payload.bin")
        _TEMPLATE_CH = open(p, "rb").read()
    return _TEMPLATE_CH

def build_client_hello(client_random: bytes, keyshare1_pub: bytes, keyshare2_pub: bytes,
                       timestamp: Optional[int] = None) -> bytes:
    import time as _t
    magic = b"\x01\x03\xf1"
    ciphers = bytes([2]) + struct.pack(">H", 0xC02B) + struct.pack(">H", 0x00A8)
    ts = struct.pack(">I", timestamp if timestamp is not None else int(_t.time()))

    def key_entry(idx: int, pub: bytes) -> bytes:
        return struct.pack(">I", 65 + 6) + struct.pack(">I", idx) + struct.pack(">H", 65) + pub

    ecdsa_payload = b"\x00\x10\x02" + key_entry(1, keyshare1_pub) + key_entry(2, keyshare2_pub) + struct.pack(">I", 1)
    ecdsa_ext = struct.pack(">I", len(ecdsa_payload)) + ecdsa_payload
    ext_block = bytes([1]) + ecdsa_ext
    body = magic + ciphers + client_random + ts + struct.pack(">I", len(ext_block)) + ext_block
    payload = struct.pack(">I", len(body)) + body
    return build_record(RECORD_HANDSHAKE, payload)

def parse_server_keyshare(serverhello_payload: bytes) -> bytes:
    import re
    m = re.search(rb"\x00\x41\x04", serverhello_payload)
    if not m:
        raise ValueError("ServerHello 无 keyshare")
    return b"\x04" + serverhello_payload[m.start() + 3:m.start() + 3 + 64]

class MMTLSSession:
    def __init__(self, sock, app: "TrafficKeys"):
        self.sock = sock
        self.app = app
        self.client_seq = 0
        self.server_seq = 0

    def send_app(self, plaintext: bytes):
        ct = encrypt_record(self.app.client_key, self.app.client_nonce, self.client_seq, plaintext, RECORD_APPDATA)
        self.sock.sendall(build_record(RECORD_APPDATA, ct))
        self.client_seq += 1

    def recv_app(self) -> bytes:
        hdr = _recv_exact(self.sock, 5)
        ln = struct.unpack(">H", hdr[3:5])[0]
        ct = _recv_exact(self.sock, ln)
        pt = decrypt_record(self.app.server_key, self.app.server_nonce, self.server_seq, ct, hdr[0])
        self.server_seq += 1
        return pt

def _recv_exact(sock, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        d = sock.recv(n - len(buf))
        if not d:
            break
        buf += d
    return buf

def handshake(host: str = "longcloud.weixin.com", port: int = 443, timeout: float = 8.0,
              verify_server: bool = True):
    import socket, secrets
    k1, k2 = gen_keypair(), gen_keypair()
    ch = build_client_hello(secrets.token_bytes(32), pub_uncompressed(k1), pub_uncompressed(k2))
    sock = socket.socket(); sock.settimeout(timeout); sock.connect((host, port))
    sock.sendall(ch)
    resp = b""; sock.settimeout(5)
    try:
        while True:
            d = sock.recv(8192)
            if not d:
                break
            resp += d
            recs0 = parse_records(resp)

            if len(recs0) >= 4:
                break
    except socket.timeout:
        pass
    recs = parse_records(resp)
    if not recs or recs[0][0] != RECORD_HANDSHAKE:
        raise RuntimeError(f"未收到 ServerHello: {resp[:16].hex()}")
    sh = recs[0][1]
    com_key = compute_share_key(k1, parse_server_keyshare(sh))
    hsk = handshake_keys(com_key, ch[5:] + sh)

    plain = []
    for seq, (typ, pl) in enumerate(recs[1:], 1):
        plain.append(decrypt_record(hsk.server_key, hsk.server_nonce, seq, pl))

    transcript = ch[5:] + sh
    sf_verify = None
    for p in plain:
        mtype = p[4]
        if mtype == 0x14:
            sf_verify = p[7:7 + HASH_LEN]
            break
        transcript += p
    if verify_server:
        if sf_verify is None:
            raise RuntimeError("未找到 ServerFinished")
        exp = server_finished_expected(com_key, transcript)
        if exp != sf_verify:
            raise RuntimeError(f"ServerFinished 校验失败 exp={exp.hex()} got={sf_verify.hex()}")

    cf = client_finished(com_key, transcript)
    cf_ct = encrypt_record(hsk.client_key, hsk.client_nonce, 1, wrap_handshake_msg(0x14, cf), RECORD_HANDSHAKE)
    sock.sendall(build_record(RECORD_HANDSHAKE, cf_ct))

    app = app_keys(com_key, transcript)
    sess = MMTLSSession(sock, app)
    sess.client_seq = 2
    sess.server_seq = len(plain) + 1

    sess.com_key = com_key
    sess.hs_messages = plain
    sess.transcript_before_sf = transcript
    sess.client_hello = ch
    sess.server_hello = sh
    return sess

def resumption_handshake_hash(client_hello_record: bytes, server_hello: bytes, hs_messages) -> bytes:
    acc = client_hello_record[5:] + server_hello
    for p in hs_messages:
        if len(p) >= 5 and p[4] == 0x04:
            break
        acc += p
    return hashlib.sha256(acc).digest()

def resumption_secret(com_key: bytes, handshake_hash: bytes, psk_type: int = 1) -> bytes:
    label = "PSK_ACCESS" if psk_type == 1 else "PSK_REFRESH"
    return hkdf_expand(com_key, label.encode() + handshake_hash, 32)

def access_psk_secret(sess) -> bytes:
    hh = resumption_handshake_hash(sess.client_hello, sess.server_hello, sess.hs_messages)
    return resumption_secret(sess.com_key, hh, psk_type=1)

HS_TYPE_CLIENT_HELLO = 1
HS_TYPE_SERVER_HELLO = 2
HS_TYPE_NEW_SESSION_TICKET = 4
HS_TYPE_ENCRYPTED_EXT = 8
HS_TYPE_CERT_VERIFY = 0x0F
HS_TYPE_FINISHED = 0x14

def shortcloud_handshake_keys(psk_access: bytes, transcript: bytes) -> "TrafficKeys":
    return TrafficKeys(psk_access, "handshake key expansion", hashlib.sha256(transcript).digest())

def shortcloud_app_keys(psk_access: bytes, transcript: bytes) -> "TrafficKeys":
    exp = expanded_secret(psk_access, transcript)
    return TrafficKeys(exp, "application data key expansion", hashlib.sha256(transcript).digest())

def build_psk_client_hello(client_random: bytes, psk: "MmtlsPsk", timestamp: Optional[int] = None) -> bytes:
    import time as _t
    magic = b"\x01\x03\xf1"
    ciphers = bytes([1]) + struct.pack(">H", 0x00A8)
    ts = struct.pack(">I", timestamp if timestamp is not None else int(_t.time()))
    psk_ext_payload = struct.pack(">H", 15) + b"\x01" + serialize_mmtls_psk(psk)
    psk_ext = struct.pack(">I", len(psk_ext_payload)) + psk_ext_payload
    ext_block = bytes([1]) + psk_ext
    body = magic + ciphers + client_random + ts + struct.pack(">I", len(ext_block)) + ext_block
    payload = struct.pack(">I", len(body)) + body

    return build_record(RECORD_HANDSHAKE_0RTT, payload)

def shortcloud_handshake(access_psk_identity: "MmtlsPsk", psk_access_secret: bytes,
                         host: str = "shortcloud.weixin.com", port: int = 443, timeout: float = 8.0):
    import socket, secrets
    cr = secrets.token_bytes(32)
    ch = build_psk_client_hello(cr, access_psk_identity)
    sock = socket.socket(); sock.settimeout(timeout); sock.connect((host, port))
    sock.sendall(ch)
    resp = b""; sock.settimeout(5)
    try:
        while True:
            d = sock.recv(8192)
            if not d:
                break
            resp += d
            if len(parse_records(resp)) >= 2:
                break
    except socket.timeout:
        pass
    recs = parse_records(resp)
    dbg = {"ch_len": len(ch), "resp_len": len(resp),
           "records": [(t, len(pl)) for t, pl in recs], "raw_head": resp[:32].hex()}
    if not recs or recs[0][0] != RECORD_HANDSHAKE:
        return sock, None, dbg
    sh = recs[0][1]
    transcript = ch[5:] + sh
    app = shortcloud_app_keys(psk_access_secret, transcript)
    dbg["server_hello_head"] = sh[:16].hex()
    return sock, app, dbg

def parse_handshake_messages(hs_messages):
    out = []
    for p in hs_messages:
        if len(p) < 5:
            continue
        innerlen = struct.unpack(">I", p[:4])[0]
        mtype = p[4]
        content = p[5:4 + innerlen]
        out.append((mtype, content))
    return out

@dataclass
class MmtlsPsk:
    psk_type: int
    ticket_lifetime_hint: int
    mac_value: bytes
    key_version: int
    iv: bytes
    encrypted_ticket: bytes
    raw: bytes = dc_field(repr=False, default=b"")

def serialize_mmtls_psk(psk: "MmtlsPsk") -> bytes:
    body = (bytes([psk.psk_type])
            + struct.pack(">I", psk.ticket_lifetime_hint)
            + struct.pack(">H", len(psk.mac_value)) + psk.mac_value
            + struct.pack(">I", psk.key_version)
            + struct.pack(">H", len(psk.iv)) + psk.iv
            + struct.pack(">H", len(psk.encrypted_ticket)) + psk.encrypted_ticket)
    return struct.pack(">I", len(body)) + body

def _parse_one_psk(b, p):
    start = p
    msg_len = struct.unpack(">I", b[p:p + 4])[0]; p += 4
    end = p + msg_len
    psk_type = b[p]; p += 1
    lifetime = struct.unpack(">I", b[p:p + 4])[0]; p += 4
    mlen = struct.unpack(">H", b[p:p + 2])[0]; p += 2
    mac = b[p:p + mlen]; p += mlen
    keyver = struct.unpack(">I", b[p:p + 4])[0]; p += 4
    ivlen = struct.unpack(">H", b[p:p + 2])[0]; p += 2
    iv = b[p:p + ivlen]; p += ivlen
    tlen = struct.unpack(">H", b[p:p + 2])[0]; p += 2
    tkt = b[p:p + tlen]; p += tlen
    return MmtlsPsk(psk_type, lifetime, mac, keyver, iv, tkt, raw=b[start:end]), end

def parse_new_session_ticket(hs_messages):
    for mtype, content in parse_handshake_messages(hs_messages):
        if mtype == 0x04:
            count = content[0]; p = 1; out = []
            for _ in range(count):
                psk, p = _parse_one_psk(content, p)
                out.append(psk)
            return out
    return []

def access_psk(hs_messages):
    for psk in parse_new_session_ticket(hs_messages):
        if psk.psk_type == 1:
            return psk
    return None
