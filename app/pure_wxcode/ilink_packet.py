"""iLink 包(WPKGHeader)构造与响应解析。"""
from __future__ import annotations

import struct
from typing import Dict, List, Tuple

MAGIC = 0x1110
VERSION = 0x076D
ILINK_APPID = "ilinkapp_060000b7b93f6c"
CMDID_MANUALAUTH = 3453
CMDID_APPSESSION = 3288

CRYPTO_RAW, CRYPTO_AESGCM, CRYPTO_HYBRID = 0, 16, 17
COMPRESS_NONE, COMPRESS_ZLIB, COMPRESS_LZ4 = 0, 1, 4

def enc_varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("varint expects unsigned")
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)

def build_wpkg_header(numeric: List[Tuple[int, int]], strings: List[Tuple[int, bytes]]) -> bytes:
    body = bytearray()
    body += enc_varint(1)
    for fid, val in numeric:
        body += enc_varint(fid) + enc_varint(val)
    body += enc_varint(0)
    for fid, data in strings:
        body += enc_varint(fid) + enc_varint(len(data)) + data
    body += enc_varint(0)

    body += enc_varint(len(body))
    return bytes(body)

def build_ilink_packet(cmdid: int, body: bytes, *, crypto: int, compress: int,
                       extra_numeric: Dict[int, int] = None,
                       extra_strings: Dict[int, bytes] = None,
                       ilink_appid: str = ILINK_APPID) -> bytes:

    nums = {1: 1, 2: 0, 3: 0, 4: 0, 5: 0x80101, 6: 11, 7: 0, 8: 0, 9: 0, 10: 1,
            11: 0, 12: 0, 13: 0, 17: 0, 18: 1, 20: 1504, 21: 0, 22: 0, 23: 0,
            25: crypto, 26: compress, 28: 1, 29: 1, 30: 0}
    if extra_numeric:
        nums.update(extra_numeric)
    numeric = sorted(nums.items())
    strs = {14: b"", 24: ilink_appid.encode(), 27: b""}
    if extra_strings:
        strs.update(extra_strings)
    strings = sorted(strs.items())
    header = build_wpkg_header(numeric, strings)
    inner = header + body
    total = 16 + len(inner)
    prefix = struct.pack(">IHHII", total, MAGIC, VERSION, cmdid, 0)
    return prefix + inner

def parse_ilink_response(raw: bytes) -> Dict:
    total, magic, ver, cmdid, zero = struct.unpack(">IHHII", raw[:16])
    pos = 16

    def rv(p):
        out = 0; sh = 0
        while True:
            c = raw[p]; p += 1
            out |= (c & 0x7F) << sh
            if c < 0x80:
                return out, p
            sh += 7
    version, pos = rv(pos)
    numeric = {}
    while True:
        fid, pos = rv(pos)
        if fid == 0:
            break
        val, pos = rv(pos)
        numeric[fid] = val
    strings = {}
    while True:
        fid, pos = rv(pos)
        if fid == 0:
            break
        ln, pos = rv(pos)
        strings[fid] = raw[pos:pos + ln]; pos += ln
    marker, pos = rv(pos)
    body = raw[pos:]
    return {"cmdid": cmdid, "link_err": numeric.get(4, 0), "crypto": numeric.get(7, 0),
            "compress": numeric.get(8, 0), "numeric": numeric, "strings": strings, "body": body}
