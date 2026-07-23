"""HybridEcdh(crypto17) 加解密：cmd3453 登录 body 加密原语。"""
from __future__ import annotations

import hashlib
import hmac
import struct
from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric import ec, utils as asym_utils
from cryptography.hazmat.primitives.asymmetric.ec import ECDSA
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from cryptography.hazmat.primitives.hashes import SHA256 as _HashSHA256, Hash as _Hash
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidSignature, InvalidTag
import os as _os

NID_SECP256R1 = 415

SHARED_KEY_LEN = 24
BODY_KEY_LEN = 24
HASH_NARROW = True

_GCM_IV_LEN = 12
_GCM_TAG_LEN = 16

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

def lz4_literal_block(data: bytes) -> bytes:
    out = bytearray()
    n = len(data)
    if n < 15:
        out.append(n << 4)
    else:
        out.append(0xF0)
        rem = n - 15
        while rem >= 255:
            out.append(255); rem -= 255
        out.append(rem)
    out.extend(data)
    return bytes(out)

def _hash_str(s: str) -> bytes:
    return s.encode("utf-8") if HASH_NARROW else s.encode("utf-16-le")

def _sha256(*parts: bytes) -> bytes:
    h = hashlib.sha256()
    for p in parts:
        h.update(p)
    return h.digest()

def ecdh_shared(client_priv: ec.EllipticCurvePrivateKey, server_pub: bytes) -> bytes:
    pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), server_pub)
    return hashlib.sha256(client_priv.exchange(ec.ECDH(), pub)).digest()

def _aesgcm(key: bytes, aad: bytes, plaintext: bytes) -> bytes:
    iv = _os.urandom(_GCM_IV_LEN)
    ct_tag = AESGCM(key).encrypt(iv, plaintext, aad)
    return ct_tag[:-_GCM_TAG_LEN] + iv + ct_tag[-_GCM_TAG_LEN:]

def _aesgcm_dec(key: bytes, aad: bytes, blob: bytes) -> bytes:
    if len(blob) < _GCM_IV_LEN + _GCM_TAG_LEN:
        raise ValueError(f"gcm blob too short: {len(blob)} < {_GCM_IV_LEN + _GCM_TAG_LEN}")
    ct = blob[:-(_GCM_IV_LEN + _GCM_TAG_LEN)]
    iv = blob[-(_GCM_IV_LEN + _GCM_TAG_LEN):-_GCM_TAG_LEN]
    tag = blob[-_GCM_TAG_LEN:]
    return AESGCM(key).decrypt(iv, ct + tag, aad)

def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    out, t, i = b"", b"", 0
    while len(out) < length:
        i += 1
        t = hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        out += t
    return out[:length]

@dataclass
class HybridEcdhSession:
    client_priv: ec.EllipticCurvePrivateKey
    client_pub: bytes
    body: bytes
    shared: bytes

def _build_request_body(priv: ec.EllipticCurvePrivateKey,
                        server_static_pub: bytes, plaintext: bytes) -> tuple[bytes, bytes, bytes]:
    client_pub = priv.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    shared = ecdh_shared(priv, server_static_pub)
    K = _os.urandom(32)
    nid_str = _hash_str(str(NID_SECP256R1))
    one = _hash_str("1")
    h1 = _sha256(one, nid_str, client_pub)
    encK1 = _aesgcm(shared[:SHARED_KEY_LEN], h1, K)
    body_km = _hkdf(b"security hdkf expand", K, h1, 56)
    h2 = _sha256(one, nid_str, client_pub, encK1)
    field5 = _aesgcm(body_km[:BODY_KEY_LEN], h2, lz4_literal_block(plaintext))

    field2 = _pb_varint(1, NID_SECP256R1) + _pb_len(2, client_pub)
    body = (_pb_varint(1, 1) + _pb_len(2, field2) + _pb_len(3, encK1) + _pb_len(5, field5))
    return body, client_pub, shared

def encrypt_body(server_static_pub: bytes, plaintext: bytes) -> bytes:
    priv = ec.generate_private_key(ec.SECP256R1())
    body, _client_pub, _shared = _build_request_body(priv, server_static_pub, plaintext)
    return body

def encrypt_body_ex(server_static_pub: bytes, plaintext: bytes) -> HybridEcdhSession:
    priv = ec.generate_private_key(ec.SECP256R1())
    body, client_pub, shared = _build_request_body(priv, server_static_pub, plaintext)
    return HybridEcdhSession(client_priv=priv, client_pub=client_pub, body=body, shared=shared)

AUTOAUTH_SHARED_KEY_LEN = 24
AUTOAUTH_BODY_KEY_LEN = 24

def _build_autoauth_body(priv: ec.EllipticCurvePrivateKey,
                         server_static_pub: bytes, plaintext: bytes,
                         autoauth_enc_key: bytes | None,
                         *, shared_key_len: int = AUTOAUTH_SHARED_KEY_LEN,
                         body_key_len: int = AUTOAUTH_BODY_KEY_LEN,
                         hash_narrow: bool = True) -> tuple[bytes, bytes, bytes]:
    def hstr(s: str) -> bytes:
        return s.encode("utf-8") if hash_narrow else s.encode("utf-16-le")

    client_pub = priv.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    shared = ecdh_shared(priv, server_static_pub)
    K = _os.urandom(32)
    one, nid_str = hstr("1"), hstr(str(NID_SECP256R1))
    h1 = _sha256(one, nid_str, client_pub)

    encK1 = _aesgcm(shared[:shared_key_len], h1, K)
    encK2 = b""
    if autoauth_enc_key:
        encK2 = _aesgcm(autoauth_enc_key, h1, K)

    body_km = _hkdf(b"security hdkf expand", K, h1, 56)
    h2 = _sha256(one, nid_str, client_pub, encK1, encK2)
    field5 = _aesgcm(body_km[:body_key_len], h2, lz4_literal_block(plaintext))

    sec_ecdh = _pb_varint(1, NID_SECP256R1) + _pb_len(2, client_pub)
    body = _pb_varint(1, 1) + _pb_len(2, sec_ecdh) + _pb_len(3, encK1)
    if encK2:
        body += _pb_len(4, encK2)
    body += _pb_len(5, field5)
    return body, client_pub, shared

def encrypt_body_autoauth(server_static_pub: bytes, plaintext: bytes,
                          autoauth_enc_key: bytes | None,
                          *, shared_key_len: int = AUTOAUTH_SHARED_KEY_LEN,
                          body_key_len: int = AUTOAUTH_BODY_KEY_LEN,
                          hash_narrow: bool = True) -> HybridEcdhSession:
    priv = ec.generate_private_key(ec.SECP256R1())
    body, client_pub, shared = _build_autoauth_body(
        priv, server_static_pub, plaintext, autoauth_enc_key,
        shared_key_len=shared_key_len, body_key_len=body_key_len, hash_narrow=hash_narrow)
    return HybridEcdhSession(client_priv=priv, client_pub=client_pub, body=body, shared=shared)

def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    out, shift = 0, 0
    while True:
        b = buf[pos]; pos += 1
        out |= (b & 0x7F) << shift
        if b < 0x80:
            return out, pos
        shift += 7

def _parse_protobuf(buf: bytes) -> dict[int, list]:
    fields: dict[int, list] = {}
    pos, n = 0, len(buf)
    while pos < n:
        tag, pos = _read_varint(buf, pos)
        fno, wt = tag >> 3, tag & 0x7
        if wt == 0:
            val, pos = _read_varint(buf, pos)
        elif wt == 2:
            ln, pos = _read_varint(buf, pos)
            val = buf[pos:pos + ln]; pos += ln
        elif wt == 5:
            val = buf[pos:pos + 4]; pos += 4
        elif wt == 1:
            val = buf[pos:pos + 8]; pos += 8
        else:
            raise ValueError(f"unsupported wire type {wt} at field {fno}")
        fields.setdefault(fno, []).append(val)
    return fields

@dataclass
class HybridEcdhResponse:
    sec_ecdh_key: bytes
    server_eph_pub: bytes
    nid: int
    ct_blob: bytes
    signature: bytes
    cred_type: int

def parse_response(response_bytes: bytes) -> HybridEcdhResponse:
    top = _parse_protobuf(response_bytes)
    if 1 not in top:
        raise ValueError("HybridEcdhResponse missing field1 (SecECDHKey)")
    sec_ecdh_key = top[1][-1]
    sub = _parse_protobuf(sec_ecdh_key)
    nid = sub.get(1, [NID_SECP256R1])[-1]
    if 2 not in sub:
        raise ValueError("SecECDHKey missing field2 (server_eph_pub)")
    server_eph_pub = sub[2][-1]
    ct_blob = top[2][-1] if 2 in top else b""
    signature = top[3][-1] if 3 in top else b""
    cred_type = top[4][-1] if 4 in top else 1
    return HybridEcdhResponse(
        sec_ecdh_key=sec_ecdh_key, server_eph_pub=server_eph_pub, nid=nid,
        ct_blob=ct_blob, signature=signature, cred_type=cred_type,
    )

def ecdsa_verify(server_static_verifykey: bytes, message: bytes, signature_der: bytes) -> bool:
    if not server_static_verifykey or not signature_der:
        return False
    try:
        if len(server_static_verifykey) == 65 and server_static_verifykey[0] == 0x04:
            vk = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), server_static_verifykey)
        else:
            from cryptography.hazmat.primitives.serialization import load_der_public_key
            vk = load_der_public_key(server_static_verifykey)
        digest = hashlib.sha256(message).digest()
        vk.verify(signature_der, digest, ECDSA(asym_utils.Prehashed(_HashSHA256())))
        return True
    except InvalidSignature:
        return False
    except Exception:

        return False

def _decrypt_aad(nid: int, server_eph_pub: bytes, cred_type: int) -> bytes:
    nid_str = _hash_str(str(nid))
    cred_str = _hash_str(str(cred_type))
    return _sha256(nid_str, server_eph_pub, cred_str)

def decrypt_body(client_ephemeral_priv: ec.EllipticCurvePrivateKey,
                 response_bytes: bytes,
                 *, server_static_verifykey: bytes | None = None,
                 require_signature: bool = False) -> bytes:

    resp = parse_response(response_bytes)

    if server_static_verifykey is not None:
        ok = ecdsa_verify(server_static_verifykey, resp.ct_blob, resp.signature)
        if not ok and require_signature:
            raise InvalidSignature("HybridEcdhResponse EcdsaVerify failed")

    shared = ecdh_shared(client_ephemeral_priv, resp.server_eph_pub)

    h = _decrypt_aad(resp.nid, resp.server_eph_pub, resp.cred_type)

    plaintext = _aesgcm_dec(shared[:SHARED_KEY_LEN], h, resp.ct_blob)
    return plaintext

def _build_response_body_for_test(client_pub: bytes, plaintext: bytes,
                                  *, cred_type: int = 1,
                                  signer_priv: ec.EllipticCurvePrivateKey | None = None) -> bytes:
    server_eph_priv = ec.generate_private_key(ec.SECP256R1())
    server_eph_pub = server_eph_priv.public_key().public_bytes(
        Encoding.X962, PublicFormat.UncompressedPoint)
    shared = ecdh_shared(server_eph_priv, client_pub)
    h = _decrypt_aad(NID_SECP256R1, server_eph_pub, cred_type)
    ct_blob = _aesgcm(shared[:SHARED_KEY_LEN], h, plaintext)
    if signer_priv is not None:
        digest = hashlib.sha256(ct_blob).digest()
        signature = signer_priv.sign(digest, ECDSA(asym_utils.Prehashed(_HashSHA256())))
    else:
        signature = b""
    sec_ecdh_key = _pb_varint(1, NID_SECP256R1) + _pb_len(2, server_eph_pub)
    body = _pb_len(1, sec_ecdh_key) + _pb_len(2, ct_blob)
    if signature:
        body += _pb_len(3, signature)
    body += _pb_varint(4, cred_type)
    return body

def _selftest() -> None:
    print("=== hybrid_ecdh 自检往返测试 ===")

    server_static_priv = ec.generate_private_key(ec.SECP256R1())
    server_static_pub = server_static_priv.public_key().public_bytes(
        Encoding.X962, PublicFormat.UncompressedPoint)
    server_sign_priv = ec.generate_private_key(ec.SECP256R1())
    server_sign_pub = server_sign_priv.public_key().public_bytes(
        Encoding.X962, PublicFormat.UncompressedPoint)

    req_plaintext = b"\x0a\x05hello\x10\x2a"
    sess = encrypt_body_ex(server_static_pub, req_plaintext)
    assert len(sess.client_pub) == 65 and sess.client_pub[0] == 0x04
    print(f"[1] encrypt_body_ex ok, body={len(sess.body)}B, client_pub=65B, 保留临时私钥")

    resp_plaintext = b"\x0a\x0cdecrypted_ok\x18\x01"
    resp_body = _build_response_body_for_test(
        sess.client_pub, resp_plaintext, cred_type=1, signer_priv=server_sign_priv)
    print(f"[2] 模拟服务端响应 body={len(resp_body)}B")

    got = decrypt_body(sess.client_priv, resp_body,
                       server_static_verifykey=server_sign_pub, require_signature=True)
    assert got == resp_plaintext, f"明文不一致: {got!r} != {resp_plaintext!r}"
    print(f"[3] decrypt_body (验签通过) ok, 明文逐字节匹配: {got!r}")

    resp_body2 = _build_response_body_for_test(sess.client_pub, resp_plaintext, cred_type=2)
    got2 = decrypt_body(sess.client_priv, resp_body2)
    assert got2 == resp_plaintext
    print(f"[4] decrypt_body (无验签, cred_type=2) ok, 明文匹配")

    wrong_vk = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        Encoding.X962, PublicFormat.UncompressedPoint)
    try:
        decrypt_body(sess.client_priv, resp_body,
                     server_static_verifykey=wrong_vk, require_signature=True)
        raise AssertionError("应当抛 InvalidSignature")
    except InvalidSignature:
        print("[5] 错误验签公钥 → 正确抛 InvalidSignature")

    wrong_priv = ec.generate_private_key(ec.SECP256R1())
    try:
        decrypt_body(wrong_priv, resp_body)
        raise AssertionError("应当因 GCM tag 失败抛 InvalidTag")
    except InvalidTag:
        print("[6] 错误临时私钥 → 正确抛 InvalidTag (GCM tag mismatch)")

    parsed = parse_response(resp_body)
    assert parsed.nid == NID_SECP256R1
    assert len(parsed.server_eph_pub) == 65 and parsed.server_eph_pub[0] == 0x04
    assert parsed.cred_type == 1
    print(f"[7] parse_response ok: nid={parsed.nid}, server_eph_pub=65B, "
          f"cred_type={parsed.cred_type}, ct_blob={len(parsed.ct_blob)}B, sig={len(parsed.signature)}B")

    print("=== 全部自检通过 ✅ ===")

if __name__ == "__main__":
    _selftest()
