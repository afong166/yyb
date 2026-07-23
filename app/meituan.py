"""Built-in project: 美团小程序 weappsilentlogin code 登录（纯算 mtgsig）。

把独立脚本「美团Code登录.py」移植到 app 的内置项目模型：用户选一个已绑定的微信账号，
后端用 codebridge 取美团小程序的 wx.login code，本地纯算生成 mtgsig / siua / dfpid，
POST open.meituan.com/user/v1/weappsilentlogin 换取 userId / token / openId / unionId。

mtgsig / siua / dfpid 的算法（JSGuard 还原）忠实照搬原脚本；仅把取码从外部服务换成内部
codebridge，并把 urllib 请求换成 httpx 异步（支持账号绑定的地区代理）。
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
import random
import re
import secrets
import struct
import time
import urllib.parse
import zlib
from collections import OrderedDict
from typing import Any, Dict, Iterable, List, MutableMapping, Optional, Sequence, Tuple

import httpx

# JSGuard 自定义 Base64 字母表，对应 jsguard.js 里的 Tn。
MTG_BASE64_ALPHABET = "ZmserbBoHQtNP+wOcza/LpngG8yJq42KWYj0DSfdikx3VT16IlUAFM97hECvuRX5"

# jsguard.js 里的 ge() 使用的 MurmurHash2 常量。
MURMUR_M = 1540483477

MEITUAN_APPID = "wxde8ac0a21135c07d"
LOGIN_URL = "https://open.meituan.com/user/v1/weappsilentlogin"


class _Undefined:
    pass


UNDEFINED = _Undefined()


def js_json(value: Any) -> str:
    """按 JSON.stringify 的常见输出格式生成紧凑 JSON。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def js_str(value: Any) -> str:
    """模拟 JS String(value) 的常用分支，避免 Python True/None 格式和 JS 不一致。"""
    if value is UNDEFINED:
        return "undefined"
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (dict, list, tuple)):
        if isinstance(value, dict):
            return "[object Object]"
        return ",".join(js_str(x) for x in value)
    return str(value)


def js_encode_uri_component(value: Any) -> str:
    """模拟 encodeURIComponent，保留 JS 默认安全字符 -_.!~*'()。"""
    return urllib.parse.quote(js_str(value), safe="-_.!~*'()")


def mtg_fixed_encode(value: Any) -> str:
    """jsguard.js 的 fe()：encodeURIComponent 后再转义 ! ' ( ) *。"""
    return urllib.parse.quote(js_str(value), safe="-_.~")


def bytes_from_js_uri(value: Any) -> List[int]:
    """jsguard.js 的 ce()：encodeURIComponent 后把 %XX 还原成字节。"""
    encoded = js_encode_uri_component(value)
    out: List[int] = []
    i = 0
    while i < len(encoded):
        if encoded[i] == "%":
            out.append(int(encoded[i + 1 : i + 3], 16))
            i += 3
        else:
            out.append(ord(encoded[i]))
            i += 1
    return out


def parse_query_like_js(query: str, missing_as_undefined: bool = False) -> List[List[str]]:
    """模拟 ne()：split('&') 再 split('=')，只取 split 后的第 2 段作为 value。"""
    if not query:
        return []
    result: List[List[str]] = []
    for item in query.split("&"):
        parts = item.split("=")
        if len(parts) < 1:
            continue
        key = urllib.parse.unquote(parts[0].replace("+", " "))
        if len(parts) == 1:
            result.append([key, "undefined" if missing_as_undefined else ""])
        else:
            value = urllib.parse.unquote(parts[1].replace("+", " "))
            result.append([key, value])
    return result


def push_encoded_pairs(target: List[List[str]], source: Any, object_mode: bool = False) -> None:
    """模拟 te()：把 query 数组或 data 对象转成签名前的 key/value。"""
    if object_mode:
        if not isinstance(source, MutableMapping):
            return
        for key, value in source.items():
            if value is UNDEFINED:
                target.append([mtg_fixed_encode(key), "undefined"])
            elif value is None:
                target.append([mtg_fixed_encode(key), "null"])
            elif isinstance(value, (dict, list, tuple)):
                target.append([mtg_fixed_encode(key), mtg_fixed_encode(js_json(value))])
            else:
                target.append([mtg_fixed_encode(key), mtg_fixed_encode(value)])
    else:
        for key, value in source or []:
            target.append([mtg_fixed_encode(key), mtg_fixed_encode(value)])


def parse_url_by_jsguard_regex(url: str) -> Tuple[str, str]:
    """按 jsguard.js 的 URL 正则取 path/query，保持它只签 path 不签 host。"""
    matched = re.match(
        r"^(?:([A-Za-z]+):)?(\/{0,3})([0-9.\-A-Za-z]+)(?::(\d+))?(?:\/([^?#]*))?(?:\?([^#]*))?(?:#(.*))?$",
        url or "",
    )
    path = "/"
    query = ""
    if matched:
        if matched.group(5):
            path += matched.group(5)
        if matched.group(6):
            query = matched.group(6)
    return path, query


def build_signed_request_bytes(
    method: str,
    url: str,
    data: Any = None,
    header: Optional[Dict[str, Any]] = None,
) -> Tuple[List[int], Dict[str, Any]]:
    """还原 mtgsig 的请求规范化逻辑，输出参与 ge() 的字节数组。"""
    method = (method or "GET").upper()
    header = header or {}
    path, query = parse_url_by_jsguard_regex(url or "")
    query_pairs = parse_query_like_js(query)

    signed_pairs: List[List[str]] = []
    form_body = ""

    is_form = False
    if method != "GET":
        for key, value in header.items():
            if key.lower() == "content-type" and value and str(value).lower().startswith("application/x-www-form-urlencoded"):
                is_form = True
                break

    if method == "GET":
        if isinstance(data, MutableMapping) and len(data) > 0:
            push_encoded_pairs(signed_pairs, data, object_mode=True)
            if query_pairs:
                rest: "OrderedDict[str, Any]" = OrderedDict()
                for key, value in parse_query_like_js(query, missing_as_undefined=True):
                    if key not in data:
                        rest[key] = value
                push_encoded_pairs(signed_pairs, rest, object_mode=True)
        else:
            push_encoded_pairs(signed_pairs, query_pairs, object_mode=False)
    else:
        push_encoded_pairs(signed_pairs, query_pairs, object_mode=False)
        if is_form:
            if isinstance(data, str):
                form_body = data
            elif isinstance(data, MutableMapping):
                temp = []
                for key, value in data.items():
                    temp.append(js_encode_uri_component(key) + "=" + js_encode_uri_component(value))
                form_body = "&".join(temp)

    signed_pairs.sort(key=lambda item: (item[0], item[1]))
    normalized_query = "&".join([key + "=" + value for key, value in signed_pairs])
    signed_bytes = bytes_from_js_uri(method + " " + path + " " + normalized_query)

    if (not is_form) and method != "GET" and data is not None:
        body = data if isinstance(data, str) else js_json(data)
        signed_bytes.extend(bytes_from_js_uri(body)[:16200])

    if form_body:
        signed_bytes.extend(bytes_from_js_uri(form_body)[:16200])

    debug = {
        "method": method,
        "path": path,
        "query": normalized_query,
        "is_form": is_form,
        "form_body": form_body,
        "signed_len": len(signed_bytes),
    }
    return signed_bytes, debug


def u32(value: int) -> int:
    return value & 0xFFFFFFFF


def int_to_be4(value: int) -> List[int]:
    """jsguard.js 的 ie()：uint32 转大端 4 字节。"""
    value = u32(value)
    return [(value >> 24) & 255, (value >> 16) & 255, (value >> 8) & 255, value & 255]


def hex_to_bytes_loose(hex_text: str) -> List[int]:
    """jsguard.js 的 re()：每 2 个 hex 转 1 字节；奇数长度最后 1 位也 parseInt。"""
    return [int(hex_text[i : i + 2], 16) for i in range(0, len(hex_text), 2)]


def bytes_to_hex(byte_values: Iterable[int]) -> str:
    return "".join(f"{item & 255:02x}" for item in byte_values)


def js_multiply_m(value: int) -> int:
    """模拟 JS 里 1540483477 的 32 位乘法拆半实现。"""
    value = u32(value)
    return u32(MURMUR_M * (value & 0xFFFF) + (((MURMUR_M * ((value >> 16) & 0xFFFF)) & 0xFFFF) << 16))


def mtg_hash_ge(byte_values: Sequence[int], seed: int) -> int:
    """还原 jsguard.js 的 ge()，类似 MurmurHash2 但末尾额外 xor 常量。"""
    remain = len(byte_values)
    value = u32(seed ^ remain)
    index = 0

    while remain >= 4:
        block = (
            (byte_values[index] & 255)
            | ((byte_values[index + 1] & 255) << 8)
            | ((byte_values[index + 2] & 255) << 16)
            | ((byte_values[index + 3] & 255) << 24)
        )
        block = js_multiply_m(block)
        value = u32(js_multiply_m(value) ^ js_multiply_m(block ^ (block >> 24)))
        remain -= 4
        index += 4

    if remain == 3:
        value ^= (byte_values[index + 2] & 255) << 16
    if remain >= 2:
        value ^= (byte_values[index + 1] & 255) << 8
    if remain >= 1:
        value = js_multiply_m(value ^ (byte_values[index] & 255))

    value = js_multiply_m(value ^ (value >> 13))
    return u32((value ^ (value >> 15)) ^ MURMUR_M)


def mtg_crc32_gn(byte_values: Sequence[int]) -> int:
    """还原 jsguard.js 的 Gn()：CRC32 表算法，但最终 xor 常量是 0x12477cdf。"""
    table: List[int] = []
    for item in range(256):
        value = item
        for _ in range(8):
            value = ((value >> 1) ^ 0xEDB88320) if (value & 1) else (value >> 1)
        table.append(u32(value))

    crc = 0xFFFFFFFF
    for item in byte_values:
        crc = u32(table[(crc ^ (item & 255)) & 255] ^ (crc >> 8))
    return u32(0x12477CDF ^ crc)


def md5_word_array(byte_values: Sequence[int]) -> List[int]:
    """还原 Ae.md5Array() 的结果：MD5 digest 按 little-endian 拆 4 个 uint32。"""
    digest = hashlib.md5(bytes(byte_values)).digest()
    return list(struct.unpack("<4I", digest))


def md5_hex_from_words(words: Sequence[int]) -> str:
    """还原 Ae.md5ToHex()：每个 uint32 按 little-endian 输出 hex。"""
    out: List[int] = []
    for word in words:
        word = u32(word)
        out.extend([word & 255, (word >> 8) & 255, (word >> 16) & 255, (word >> 24) & 255])
    return bytes_to_hex(out)


def mtg_custom_base64(byte_values: Sequence[int]) -> str:
    """还原 mtgsig a5 使用的自定义 Base64。"""
    result: List[str] = []
    full_len = len(byte_values) - len(byte_values) % 3
    for index in range(0, full_len, 3):
        block = ((byte_values[index] & 255) << 16) + ((byte_values[index + 1] & 255) << 8) + (byte_values[index + 2] & 255)
        result.append(
            MTG_BASE64_ALPHABET[(block >> 18) & 63]
            + MTG_BASE64_ALPHABET[(block >> 12) & 63]
            + MTG_BASE64_ALPHABET[(block >> 6) & 63]
            + MTG_BASE64_ALPHABET[block & 63]
        )

    remain = len(byte_values) - full_len
    if remain == 1:
        one = byte_values[-1] & 255
        result.append(MTG_BASE64_ALPHABET[one >> 2] + MTG_BASE64_ALPHABET[(one << 4) & 63] + "==")
    elif remain == 2:
        two = ((byte_values[-2] & 255) << 8) + (byte_values[-1] & 255)
        result.append(
            MTG_BASE64_ALPHABET[two >> 10]
            + MTG_BASE64_ALPHABET[(two >> 4) & 63]
            + MTG_BASE64_ALPHABET[(two << 2) & 63]
            + "="
        )
    return "".join(result)


def mtg_rc4_variant(key: Sequence[int], text: str) -> List[int]:
    """还原 a5 内层 RC4 变体：KSA 多加固定 31。"""
    state = list(range(256))
    j = 0
    for i in range(256):
        j = (j + state[i] + (key[i % len(key)] & 255) + 31) % 256
        state[i], state[j] = state[j], state[i]

    i = 0
    j = 0
    out: List[int] = []
    for ch in text:
        i = (i + 1) % 256
        j = (j + state[i]) % 256
        state[i], state[j] = state[j], state[i]
        out.append(ord(ch) ^ state[(state[i] + state[j]) % 256])
    return out


def build_qn(
    appid: str,
    openid: str = "",
    timestamp_ms: Optional[int] = None,
    init_timestamp_ms: Optional[int] = None,
    seq: int = 1,
    route: str = "",
    b9: str = "00102",
    b11: str = "",
    b10: Any = UNDEFINED,
    account_info: Any = UNDEFINED,
) -> "OrderedDict[str, Any]":
    """按 JS 插入顺序构造 JSON.stringify(qn) 的对象。"""
    now_ms = int(time.time() * 1000) if timestamp_ms is None else int(timestamp_ms)
    init_ms = now_ms if init_timestamp_ms is None else int(init_timestamp_ms)
    qn: "OrderedDict[str, Any]" = OrderedDict()
    qn["b7"] = init_ms // 1000
    if account_info is UNDEFINED:
        account_info = {"miniProgram": {"appId": appid or ""}}
    if account_info is not None:
        qn["b1"] = account_info
    qn["b6"] = openid or ""
    qn["b8"] = int(seq)
    qn["b12"] = appid or ""
    if b11:
        qn["b11"] = b11
    qn["b2"] = route or ""
    qn["b9"] = b9
    if b10 is not UNDEFINED:
        qn["b10"] = b10
    return qn


def build_mtgsig(
    method: str,
    url: str,
    data: Any = None,
    header: Optional[Dict[str, Any]] = None,
    *,
    appid: str = MEITUAN_APPID,
    openid: str = "",
    dfpid: str = "",
    siua: str = "",
    timestamp_ms: Optional[int] = None,
    init_timestamp_ms: Optional[int] = None,
    seq: int = 1,
    route: str = "",
    env_code: int = 119,
    b9: str = "00102",
    b11: str = "",
    b10: Any = UNDEFINED,
    account_info: Any = UNDEFINED,
) -> Tuple["OrderedDict[str, Any]", Dict[str, Any]]:
    """生成 mtgsig 对象，并返回调试信息。"""
    if timestamp_ms is None:
        timestamp_ms = int(time.time() * 1000)
    timestamp_ms = int(timestamp_ms)
    if not dfpid:
        dfpid = make_dfpid(timestamp_ms=timestamp_ms)

    request_bytes, req_debug = build_signed_request_bytes(method, url, data, header)
    qn = build_qn(
        appid=appid,
        openid=openid,
        timestamp_ms=timestamp_ms,
        init_timestamp_ms=init_timestamp_ms,
        seq=seq,
        route=route,
        b9=b9,
        b11=b11,
        b10=b10,
        account_info=account_info,
    )
    qn_text = js_json(qn)

    timestamp_low = u32(timestamp_ms)
    timestamp_bytes = int_to_be4(timestamp_low)
    md5_seed = hashlib.md5(bytes(bytes_from_js_uri(siua) + timestamp_bytes)).hexdigest()
    key_bytes = hex_to_bytes_loose(md5_seed[:15])
    key_bytes[7] = (int(env_code) ^ mtg_crc32_gn(timestamp_bytes)) & 255
    key_bytes.extend(timestamp_bytes)
    key_bytes.extend(int_to_be4(mtg_crc32_gn(key_bytes)))

    a5 = mtg_custom_base64(key_bytes + mtg_rc4_variant(key_bytes, qn_text))

    request_hash = mtg_hash_ge(request_bytes, timestamp_ms)
    a5_hash = mtg_hash_ge(bytes_from_js_uri(a5), timestamp_ms)
    a4_mix_words = [
        request_hash,
        a5_hash,
        u32(request_hash ^ timestamp_low),
        u32(request_hash ^ a5_hash ^ timestamp_low),
    ]
    a4 = bytes_to_hex(int_to_be4(request_hash) + int_to_be4(a5_hash) + hex_to_bytes_loose(md5_hex_from_words(a4_mix_words)))

    a1 = "1.2"
    x0 = 3
    d1_raw = a1 + str(timestamp_ms) + dfpid + a4 + str(u32(a5_hash)) + md5_seed + appid
    d1_words = md5_word_array(bytes_from_js_uri(d1_raw))
    rotate_like = u32((timestamp_low << x0) | (timestamp_low << (32 - x0)))
    d1_words[0] = u32(d1_words[0] ^ rotate_like)
    d1_words[1] = u32(d1_words[1] ^ a5_hash)
    d1_words[2] = u32(d1_words[2] ^ a5_hash ^ rotate_like)
    d1_words[3] = u32(d1_words[3] ^ d1_words[0])
    d1 = md5_hex_from_words(d1_words)

    mtgsig: "OrderedDict[str, Any]" = OrderedDict()
    mtgsig["a1"] = a1
    mtgsig["a2"] = timestamp_ms
    mtgsig["a3"] = dfpid
    mtgsig["a4"] = a4
    mtgsig["a5"] = a5
    mtgsig["a6"] = siua
    mtgsig["a7"] = appid
    mtgsig["x0"] = x0
    mtgsig["d1"] = d1

    debug = {
        "request": req_debug,
        "qn": qn,
        "qn_json": qn_text,
        "key_hex": bytes_to_hex(key_bytes),
        "request_hash": request_hash,
        "a5_hash": a5_hash,
        "md5_seed": md5_seed,
        "d1_raw": d1_raw,
    }
    return mtgsig, debug


def build_windows_system_object(
    *,
    model: str = "microsoft",
    brand: str = "microsoft",
    platform: str = "windows",
    system: str = "Windows Unknown x64",
    version: str = "4.1.11.24",
    sdk_version: str = "3.16.1",
    language: str = "zh_CN",
    network_type: str = "wifi",
    screen_width: int = 414,
    screen_height: int = 780,
    window_width: int = 414,
    window_height: int = 780,
    pixel_ratio: int = 1,
    scene: int = 1256,
    route: str = "index/pages/mt/mt",
) -> "OrderedDict[str, Any]":
    """构造 JSGuard 常见 Windows 小程序指纹对象，用于本地 dfpid 和 siua。"""
    return OrderedDict(
        [
            ("accelerometer", []),
            ("albumAuthorized", True),
            ("BatteryInfo", OrderedDict([("errMsg", "getBatteryInfo:ok"), ("isCharging", True), ("level", 100)])),
            ("batteryLevel", None),
            ("Beacons", None),
            ("benchmarkLevel", -1),
            ("bluetoothEnabled", False),
            ("brand", brand),
            ("brightness", 0.5),
            ("cameraAuthorized", True),
            ("compass", []),
            ("deviceOrientation", None),
            ("devicePixelRatio", pixel_ratio),
            ("enableDebug", False),
            ("errMsg", "getSystemInfo:ok"),
            ("fontSizeSetting", None),
            ("language", language),
            ("LaunchOptionsSync", OrderedDict([("path", route), ("scene", scene)])),
            ("locationAuthorized", True),
            ("locationEnabled", True),
            ("locationReducedAccuracy", None),
            ("microphoneAuthorized", True),
            ("model", model),
            ("networkType", network_type),
            ("notificationAlertAuthorized", None),
            ("notificationAuthorized", True),
            ("notificationBadgeAuthorized", None),
            ("notificationSoundAuthorized", None),
            ("pixelRatio", pixel_ratio),
            ("platform", platform),
            ("safeArea", OrderedDict([("left", 0), ("right", screen_width), ("top", 0), ("bottom", screen_height), ("width", screen_width), ("height", screen_height)])),
            ("screenHeight", screen_height),
            ("screenTop", None),
            ("screenWidth", screen_width),
            ("SDKVersion", sdk_version),
            ("statusBarHeight", 20),
            ("system", system),
            ("version", version),
            ("wifiEnabled", True),
            ("WifiInfo", None),
            ("windowHeight", window_height),
            ("windowWidth", window_width),
            ("screenRecord", None),
            ("isPrivacy", 1),
            ("hasSystemProxy", -1),
            ("captureRecord", "[]"),
        ]
    )


def make_jsguard_random_letters() -> str:
    """按 JSGuard 里 An() 的 Math.random 表达式生成 7 位本地随机大写串。"""
    letters: List[str] = []
    for _ in range(7):
        letters.append(chr(random.randrange(25) | ord("A")))
    return "".join(letters)


def make_local_dfpid(
    timestamp_ms: Optional[int] = None,
    *,
    openid: str = "",
    system_object: Optional["OrderedDict[str, Any]"] = None,
    random_letters: Optional[str] = None,
) -> str:
    """纯算法生成 JSGuard 本地 dfpid/localId，对应 jsguard.js 的 An()。"""
    now_ms = int(time.time() * 1000) if timestamp_ms is None else int(timestamp_ms)
    timestamp_sec = round(now_ms / 1000)
    if random_letters is None:
        random_letters = make_jsguard_random_letters()
    else:
        random_letters = (random_letters.upper() + "AAAAAAA")[:7]
    if system_object is None:
        system_object = build_windows_system_object()

    md5_input = OrderedDict(
        [
            ("model", system_object.get("model")),
            ("system", system_object),
            ("timestamp", timestamp_sec),
            ("openid", openid or ""),
        ]
    )
    digest = hashlib.md5(js_json(md5_input).encode("utf-8")).hexdigest()
    base = f"{now_ms}{random_letters}{digest}"
    crc_prefix = str(zlib.crc32(base.encode("utf-8")) & 0xFFFFFFFF)[:4]
    return base + crc_prefix


def make_dfpid(
    timestamp_ms: Optional[int] = None,
    seed: str = "PYMTGSIG",
    *,
    openid: str = "",
    system_object: Optional["OrderedDict[str, Any]"] = None,
) -> str:
    """兼容旧调用名；现在返回 JSGuard An() 风格的纯算法本地 dfpid。"""
    letters = (seed.upper().replace("_", "") + "AAAAAAA")[:7]
    return make_local_dfpid(timestamp_ms=timestamp_ms, openid=openid, system_object=system_object, random_letters=letters)


def make_session_id(platform: str = "windows", mmp: bool = False) -> str:
    """纯算法生成 JSGuard sessionId，对应 Ze.getSessionId()。"""
    hex_chars = list("0123456789abcdef")
    values = [secrets.choice(hex_chars) for _ in range(36)]
    values[14] = "4"
    values[19] = hex_chars[(int(values[19], 16) & 3) | 8]
    values[8] = values[13] = values[18] = values[23] = ""
    raw_uuid32 = "".join(values)
    if mmp:
        return raw_uuid32 + "55"
    platform_index = {
        "android": 0,
        "ios": 1,
        "devtools": 2,
        "windows": 3,
        "mac": 4,
        "ohos": 5,
    }.get((platform or "").lower(), 9)
    return raw_uuid32 + "0" + str(platform_index)


def aes_cbc_pkcs7_encrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    """AES-CBC-PKCS7 加密；优先用 pycryptodome，缺失时回退 cryptography。"""
    pad_len = 16 - (len(data) % 16)
    padded = data + bytes([pad_len]) * pad_len
    try:
        from Crypto.Cipher import AES  # type: ignore

        return AES.new(key, AES.MODE_CBC, iv).encrypt(padded)
    except Exception:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes  # type: ignore

        encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        return encryptor.update(padded) + encryptor.finalize()


def build_dfp_system_array(system_object: "OrderedDict[str, Any]") -> List[Any]:
    """按 vt.system 固定字段顺序，把 system 对象压成 siua 里的数组结构。"""
    fields = [
        "accelerometer", "albumAuthorized", "BatteryInfo", "batteryLevel", "Beacons",
        "benchmarkLevel", "bluetoothEnabled", "brand", "brightness", "cameraAuthorized",
        "compass", "deviceOrientation", "devicePixelRatio", "enableDebug", "errMsg",
        "fontSizeSetting", "language", "LaunchOptionsSync", "locationAuthorized",
        "locationEnabled", "locationReducedAccuracy", "microphoneAuthorized", "model",
        "networkType", "notificationAlertAuthorized", "notificationAuthorized",
        "notificationBadgeAuthorized", "notificationSoundAuthorized", "pixelRatio",
        "platform", "safeArea", "screenHeight", "screenTop", "screenWidth", "SDKVersion",
        "statusBarHeight", "system", "version", "wifiEnabled", "WifiInfo", "windowHeight",
        "windowWidth", "screenRecord", "isPrivacy", "hasSystemProxy", "captureRecord",
    ]
    battery_fields = ["errMsg", "isCharging", "level"]
    safe_area_fields = ["left", "right", "top", "bottom", "width", "height"]
    wifi_fields = ["SSID", "BSSID", "autoJoined", "signalStrength", "justJoined", "secure", "frequency"]

    result: List[Any] = []
    for field in fields:
        value = system_object.get(field)
        if isinstance(value, str) and field in {"BatteryInfo", "safeArea", "WifiInfo", "LaunchOptionsSync"}:
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                pass
        if field == "LaunchOptionsSync" and isinstance(value, MutableMapping):
            result.append(js_json(OrderedDict([("path", value.get("path")), ("scene", value.get("scene"))])))
        elif field == "BatteryInfo" and isinstance(value, MutableMapping):
            result.append([value.get(item) for item in battery_fields])
        elif field == "safeArea" and isinstance(value, MutableMapping):
            result.append([value.get(item) for item in safe_area_fields])
        elif field == "WifiInfo" and isinstance(value, MutableMapping):
            result.append([value.get(item) for item in wifi_fields])
        else:
            result.append(value)
    return result


def build_siua(
    *,
    appid: str = MEITUAN_APPID,
    openid: str = "",
    dfpid: str = "",
    localid: str = "",
    filetime_ms: Optional[int] = None,
    timestamp_ms: Optional[int] = None,
    session_id: str = "",
    route: str = "index/pages/mt/mt",
    scene: int = 1256,
    platform: str = "windows",
    ext: Optional[List[Any]] = None,
    system_object: Optional["OrderedDict[str, Any]"] = None,
) -> str:
    """生成 a6/siua：w1.6 + AES-CBC(gzip(JSON数组))。"""
    now_ms = int(time.time() * 1000) if timestamp_ms is None else int(timestamp_ms)
    if filetime_ms is None:
        filetime_ms = now_ms
    if system_object is None:
        system_object = build_windows_system_object(route=route, scene=scene, platform=platform)
    if not localid:
        localid = make_local_dfpid(timestamp_ms=filetime_ms, openid=openid, system_object=system_object)
    if not dfpid:
        dfpid = localid
    if not session_id:
        session_id = make_session_id(platform=platform)
    if ext is None:
        ext = [0, 1, 2, 0, 4]
    system_array = build_dfp_system_array(system_object)

    plain_array = [
        appid,
        dfpid,
        int(filetime_ms),
        "2.5.0",
        localid,
        system_array,
        round(now_ms / 1000),
        ext,
        session_id,
    ]
    plain = js_json(plain_array).encode("utf-8")
    gz = gzip.compress(plain, compresslevel=6, mtime=now_ms // 1000)
    encrypted = aes_cbc_pkcs7_encrypt(gz, b"z7Jut6Ywr2Pe5Nhx", b"0807060504030201")
    return "w1.6" + base64.b64encode(encrypted).decode("ascii")


def build_pure_identity(
    *,
    appid: str = MEITUAN_APPID,
    openid: str = "",
    timestamp_ms: Optional[int] = None,
    filetime_ms: Optional[int] = None,
    route: str = "index/pages/mt/mt",
    scene: int = 1256,
    platform: str = "windows",
    random_letters: Optional[str] = None,
    session_id: str = "",
) -> "OrderedDict[str, Any]":
    """一次性生成纯本地 dfpid/localId/sessionId/siua，避免 dfpid 和 siua 不一致。"""
    now_ms = int(time.time() * 1000) if timestamp_ms is None else int(timestamp_ms)
    file_ms = now_ms if filetime_ms is None else int(filetime_ms)
    system_object = build_windows_system_object(route=route, scene=scene, platform=platform)
    localid = make_local_dfpid(
        timestamp_ms=file_ms,
        openid=openid,
        system_object=system_object,
        random_letters=random_letters,
    )
    sid = session_id or make_session_id(platform=platform)
    siua = build_siua(
        appid=appid,
        openid=openid,
        dfpid=localid,
        localid=localid,
        filetime_ms=file_ms,
        timestamp_ms=now_ms,
        session_id=sid,
        route=route,
        scene=scene,
        platform=platform,
        system_object=system_object,
    )
    return OrderedDict(
        [
            ("dfpid", localid),
            ("localid", localid),
            ("filetime_ms", file_ms),
            ("timestamp_ms", now_ms),
            ("session_id", sid),
            ("siua", siua),
            ("system_object", system_object),
        ]
    )


# ============================== 登录主流程 ==============================

def _login_params() -> "OrderedDict[str, Any]":
    return OrderedDict(
        [
            ("sdkVersion", "4.1.11.24"),
            ("utm_medium", "windows"),
            ("sdkType", "wxmp"),
            ("login_sdk_version", "6.18.4"),
            ("appName", "group"),
            ("risk_app", "214"),
            ("risk_partner", "0"),
            ("risk_platform", "13"),
            ("risk_smsPrefixId", "0"),
            ("risk_smsTemplateId", "0"),
            ("version_name", "10.26.1"),
        ]
    )


async def meituan_code_login(code: str, proxy_url: str = "") -> dict:
    """用 wx.login code 换美团 userId/token/openId/unionId。"""
    appid = MEITUAN_APPID
    params = _login_params()
    payload: "OrderedDict[str, Any]" = OrderedDict(
        [
            ("code", code),
            ("device_type", "microsoft"),
            ("device_os", "微信小程序"),
        ]
    )
    query = urllib.parse.urlencode(params)
    full_url = LOGIN_URL + "?" + query

    identity = build_pure_identity(appid=appid, route="index/pages/mt/mt", scene=1256)
    sign_header = OrderedDict([("content-type", "application/x-www-form-urlencoded")])
    mtgsig, _debug = build_mtgsig(
        "POST", full_url, payload, sign_header,
        appid=appid, openid="",
        dfpid=identity["dfpid"], siua=identity["siua"],
        timestamp_ms=identity["timestamp_ms"], init_timestamp_ms=identity["filetime_ms"],
        seq=1, route="index/pages/mt/mt", env_code=119, b9="00102", b11="",
    )

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/132.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
            "MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090a13) "
            "UnifiedPCWindowsWechat(0xf2541b18) XWEB/20005"
        ),
        "Content-Type": "application/x-www-form-urlencoded",
        "mtgsig": js_json(mtgsig),
        "Referer": f"https://servicewechat.com/{appid}/1555/page-frame.html",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    body = urllib.parse.urlencode(payload).encode("utf-8")

    kw = {"proxy": proxy_url} if proxy_url else {}
    async with httpx.AsyncClient(timeout=25.0, verify=False, **kw) as cli:
        r = await cli.post(full_url, content=body, headers=headers)
        try:
            data = r.json()
        except Exception:
            return {"ok": False, "stage": "weappsilentlogin",
                    "error": f"登录返回非 JSON：HTTP {r.status_code} {r.text[:160]}"}

    inner = data.get("data") or {}
    user_id = inner.get("userId")
    token = inner.get("token") or ""
    open_id = data.get("openId") or ""
    union_id = data.get("unionId") or ""
    if not token:
        msg = data.get("msg") or data.get("message")
        if not msg and open_id:
            # 美团解析出了 openId/unionId（这一步走微信，和签名无关），却没下发 token —— 多为风控拦截：
            # 常见于经代理/异地 IP 出网、或该微信号未在美团注册激活。
            msg = ("美团已返回账号身份（openId/unionId）但未下发 token，通常是风控拦截：建议改直连或换 IP "
                   "重试（本项目默认直连，若你填了 SOCKS5 代理可先清空），或先用真机在美团小程序登录激活后再试。")
        return {"ok": False, "stage": "weappsilentlogin",
                "error": msg or json.dumps(data, ensure_ascii=False)[:200],
                "openId": open_id, "unionId": union_id, "raw": data}
    return {
        "ok": True,
        "stage": "meituan",
        "userId": user_id,
        "token": token,
        "mtToken": token,
        "openId": open_id,
        "unionId": union_id,
        "account": str(user_id or open_id),
        # 复用内置项目的提交/结果字段：submit 时写入的环境变量值 = token
        "cookie": token,
        "code": code,
    }


async def run_meituan_code_login(user_id: int, openid: str, appid: str, proxy_url: str = "") -> dict:
    from .codebridge import get_code_for_openid

    login_appid = appid or MEITUAN_APPID
    # 美团 weappsilentlogin 非地域限制，且对代理/异地 IP 风控较严（经代理常只回身份不发 token）。
    # 因此默认直连（与已验证可用的独立脚本一致），只有用户显式填了 proxyUrl 才走代理，
    # 不像脉动那样回退到账号绑定的地区代理。
    proxy = (proxy_url or "").strip()

    cr = await get_code_for_openid(openid, login_appid)
    if not cr.get("success") or not cr.get("code"):
        return {"ok": False, "stage": "get-code", "error": cr.get("error") or "failed to get Meituan code"}

    login = await meituan_code_login(cr["code"], proxy)
    if not login.get("ok"):
        login.setdefault("code", cr["code"])
    return login
