#!/usr/bin/env python3
"""脉动小程序 Turing deviceToken V3 纯 HTTP 协议实现。

协议来源：当前解包目录内的 Turing Core 2.0.3 插件。
本文件不启动微信、不调用浏览器，只复现 ticket -> risk -> V3 封包流程。
Turing 请求使用 httpx，以便和脉动业务请求复用同一地区代理。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import secrets
import struct
import sys
import threading
import time
import uuid
import zlib
from pathlib import Path
from typing import Any

import httpx


CHANNEL = "109092"
WX_APPID = "wxef2336428c3873d2"
TICKET_URL = "https://browsertdidticket.m.qq.com"
RISK_URL = "https://flysec.m.qq.com/jprx/1941"
INVALID_REQUEST_RET = -17
INVALID_RISK_TOKEN_RETS = (4, 6)
EXTERNAL_ERROR_RETS = frozenset((-1, -2, -3, -4, -5, -8, -9, -13, -14, -16, -17, -18, -19))
SDK_INFO = {
    "buildno": 20003,
    "sdkver": "2.0.3",
    "lc": "021834179857A415",
    "channel": CHANNEL,
    "platform": 5,
}
PACKET_KEY = b"01303975070694866490574863106155"
V3_KEY_PREFIX = bytes((77, 35, 78, 120, 90, 98, 64, 116))

DFP_FEATURE_KEYS = (
    1,
    2,
    4,
    43,
    101,
    103,
    104,
    105,
    106,
    107,
    108,
    109,
    110,
    111,
    112,
    113,
    114,
    115,
    116,
    117,
    118,
    119,
    126,
    127,
    128,
    129,
    130,
    1000,
    1001,
    1002,
    1003,
    1006,
    1007,
)
RISK_FEATURE_KEYS = (
    1,
    2,
    4,
    43,
    101,
    104,
    105,
    107,
    119,
    121,
    122,
    123,
    124,
    127,
    128,
    129,
    130,
)

DEFAULT_PROFILE: dict[str, Any] = {
    "app_version": "",
    "env_version": "release",
    "network_type": "wifi",
    "local_ip": "",
    "plugin_login_code": "",
    "request_package_name": "",
    "system_info": {
        "SDKVersion": "3.14.2",
        "brand": "iPhone",
        "model": "iPhone 15 Pro<iPhone16,1>",
        "screenHeight": 844,
        "screenWidth": 390,
        "system": "iOS 17.0",
        "language": "zh_CN",
        "version": "8.0.73",
        "platform": "ios",
        "statusBarHeight": 47,
        "benchmarkLevel": 1,
        "albumAuthorized": False,
        "cameraAuthorized": True,
        "locationAuthorized": False,
        "microphoneAuthorized": False,
        "notificationAuthorized": True,
        "bluetoothEnabled": True,
        "locationEnabled": True,
        "wifiEnabled": True,
        "enableDebug": False,
        "fontSizeSetting": 16,
    },
}

DEFAULT_STATE_PATH = Path(__file__).resolve().parents[1] / "data" / "turing" / "default.json"
TLS_VERIFY = os.environ.get("MAIDONG_TLS_VERIFY", "1").strip().lower() not in ("0", "false", "no", "off")
PACKAGE_VERSION = os.environ.get("MAIDONG_PACKAGE_VERSION", "55")
_STATE_LOCKS: dict[str, threading.Lock] = {}
_STATE_LOCKS_GUARD = threading.Lock()


class TuringProtocolError(RuntimeError):
    """Turing 协议请求或响应不符合预期。"""


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _json_bytes(data: Any) -> bytes:
    # JSON.stringify 默认无空格，并直接保留 Unicode 字符。
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _uint32(value: int) -> int:
    return value & 0xFFFFFFFF


def _xxtea_encrypt(data: bytes, key: bytes) -> bytes:
    """复现插件 xxtea_new.encryptUint8Array。"""
    if not data:
        return data

    padded = data + b"\x00" * ((4 - len(data) % 4) % 4)
    values = list(struct.unpack(f"<{len(padded) // 4}I", padded))
    values.append(len(data))

    key_padded = key[:16] + b"\x00" * max(0, 16 - len(key[:16]))
    key_words = list(struct.unpack("<4I", key_padded))
    rounds = 6 + 52 // len(values)
    total = 0
    z_value = values[-1]

    while rounds > 0:
        total = _uint32(total + 0x9E3779B9)
        selector = (total >> 2) & 3
        for index in range(len(values) - 1):
            y_value = values[index + 1]
            mix = (
                ((z_value >> 5 ^ _uint32(y_value << 2)) + (y_value >> 3 ^ _uint32(z_value << 4)))
                ^ ((total ^ y_value) + (key_words[(index & 3) ^ selector] ^ z_value))
            )
            values[index] = _uint32(values[index] + mix)
            z_value = values[index]

        y_value = values[0]
        last = len(values) - 1
        mix = (
            ((z_value >> 5 ^ _uint32(y_value << 2)) + (y_value >> 3 ^ _uint32(z_value << 4)))
            ^ ((total ^ y_value) + (key_words[(last & 3) ^ selector] ^ z_value))
        )
        values[last] = _uint32(values[last] + mix)
        z_value = values[last]
        rounds -= 1

    return struct.pack(f"<{len(values)}I", *values)


def _xxtea_decrypt(data: bytes, key: bytes) -> bytes:
    """仅用于自测和诊断，业务请求生成只使用加密。"""
    if not data or len(data) % 4 != 0:
        return data

    values = list(struct.unpack(f"<{len(data) // 4}I", data))
    key_padded = key[:16] + b"\x00" * max(0, 16 - len(key[:16]))
    key_words = list(struct.unpack("<4I", key_padded))
    rounds = 6 + 52 // len(values)
    total = _uint32(0x9E3779B9 * rounds)
    y_value = values[0]

    while total != 0:
        selector = (total >> 2) & 3
        for index in range(len(values) - 1, 0, -1):
            z_value = values[index - 1]
            mix = (
                ((z_value >> 5 ^ _uint32(y_value << 2)) + (y_value >> 3 ^ _uint32(z_value << 4)))
                ^ ((total ^ y_value) + (key_words[(index & 3) ^ selector] ^ z_value))
            )
            values[index] = _uint32(values[index] - mix)
            y_value = values[index]

        z_value = values[-1]
        mix = (
            ((z_value >> 5 ^ _uint32(y_value << 2)) + (y_value >> 3 ^ _uint32(z_value << 4)))
            ^ ((total ^ y_value) + (key_words[selector] ^ z_value))
        )
        values[0] = _uint32(values[0] - mix)
        y_value = values[0]
        total = _uint32(total - 0x9E3779B9)

    plain = struct.pack(f"<{len(values)}I", *values)
    size = values[-1]
    if size < len(plain) - 7 or size > len(plain) - 4:
        raise TuringProtocolError("XXTEA 明文长度校验失败")
    return plain[:size]


def _murmur_hash2(data: bytes, seed: int = 256) -> int:
    value = _uint32(seed ^ len(data))
    offset = 0
    remaining = len(data)

    while remaining >= 4:
        block = int.from_bytes(data[offset : offset + 4], "little")
        block = _uint32(block * 0x5BD1E995)
        block ^= block >> 24
        block = _uint32(block * 0x5BD1E995)
        value = _uint32(value * 0x5BD1E995) ^ block
        offset += 4
        remaining -= 4

    if remaining == 3:
        value ^= data[offset] | data[offset + 1] << 8
        value ^= data[offset + 2] << 16
        value = _uint32(value * 0x5BD1E995)
    elif remaining == 2:
        value ^= data[offset] | data[offset + 1] << 8
        value = _uint32(value * 0x5BD1E995)
    elif remaining == 1:
        value ^= data[offset]
        value = _uint32(value * 0x5BD1E995)

    value ^= value >> 13
    value = _uint32(value * 0x5BD1E995)
    value ^= value >> 15
    return _uint32(value)


def _js_rand_step(seed: int) -> int:
    # 首轮 seed 是 13 位毫秒时间戳，必须经过 IEEE-754 Number 再做 ToInt32。
    number = float(seed) * 214013.0 + 2531011.0
    if not math.isfinite(number):
        return 0
    integer = math.trunc(number) % 0x100000000
    signed = integer if integer < 0x80000000 else integer - 0x100000000
    return (signed >> 16) & 0x7FFF


def _feature_signature(timestamp: int, features: dict[str, str]) -> str:
    values = [str(features[key]) for key in features]
    seed = timestamp
    for _ in range(len(values) // 4 + 1):
        seed = _js_rand_step(seed)
        values.append(format(seed, "x"))
    return "01" + format(_murmur_hash2("".join(values).encode("utf-8")), "x")


def _encrypt_text(text: str, key: str, append_timestamp: int | None = None) -> str:
    if append_timestamp is not None:
        text = f"{text}_{append_timestamp}"
    cipher = _xxtea_encrypt(text.encode("utf-8"), key.encode("utf-8"))
    return base64.b64encode(cipher).decode("ascii")


def _build_features(
    keys: tuple[int, ...],
    profile: dict[str, Any],
    device_uuid: str,
    ticket: str,
    openid: str,
    timestamp: int,
) -> dict[str, str]:
    features = {str(key): "" for key in keys}
    system = profile["system_info"]

    # field 1 每次请求都会加入当前时间后再用固定 key 加密。
    features["1"] = _encrypt_text(device_uuid, PACKET_KEY.decode("ascii"), timestamp)
    features["2"] = ticket

    values: dict[int, str] = {
        4: str(system.get("platform", "")),
        43: str(profile.get("network_type", "")),
        101: openid,
        103: str(system.get("SDKVersion", "")),
        104: str(system.get("brand", "")),
        105: str(system.get("model", "")),
        106: (
            f"{system.get('screenHeight')}*{system.get('screenWidth')}"
            if system.get("screenHeight") is not None and system.get("screenWidth") is not None
            else ""
        ),
        107: str(system.get("system", "")),
        108: str(system.get("language", "")),
        111: str(system.get("version", "")),
        116: str(system.get("statusBarHeight", "")),
        117: str(system.get("benchmarkLevel", "")),
        124: str(system.get("enableDebug", "")).lower(),
        126: str(system.get("fontSizeSetting", "")),
        127: str(profile.get("app_version", "")),
        128: str(profile.get("local_ip", "")),
        129: str(profile.get("env_version", "release")),
        130: str(profile.get("plugin_login_code", "")),
        121: str(profile.get("longitude_latitude", "")),
        123: str(profile.get("wifi_bssid", "")),
    }

    auth_names = (
        "albumAuthorized",
        "cameraAuthorized",
        "locationAuthorized",
        "microphoneAuthorized",
        "notificationAuthorized",
        "bluetoothEnabled",
        "locationEnabled",
        "wifiEnabled",
    )
    values[118] = ":".join("1" if system.get(name) else "0" for name in auth_names)

    for key, value in values.items():
        if str(key) in features:
            features[str(key)] = value
    return features


def _build_payload(
    kind: str,
    profile: dict[str, Any],
    device_uuid: str,
    openid: str,
    ticket: str = "",
    timestamp: int | None = None,
) -> dict[str, Any]:
    timestamp = timestamp or _now_ms()
    keys = DFP_FEATURE_KEYS if kind == "ticket" else RISK_FEATURE_KEYS
    features = _build_features(keys, profile, device_uuid, ticket, openid, timestamp)
    statistics = {"10": _feature_signature(timestamp, features), "11": str(uuid.uuid4())}
    product_info = {"clientVer": "", "requestPackageName": profile.get("request_package_name", "")}
    client_info = {
        "requestSeq": "",
        "metaData": "",
        "channel": "",
        "buildNo": 0,
        "version": "",
        "lc": "",
        "extraInfo": "",
        "wx_appid": WX_APPID,
        "appid": "",
        "type": 0,
    }

    if kind == "ticket":
        return {
            "timestamp": timestamp,
            "featureObj": features,
            "requireType": 0,
            "sdkInfo": dict(SDK_INFO),
            "productInfo": product_info,
            "clientInfo": client_info,
            "statisticsInfo": statistics,
            "extraIds": {"1": ""},
        }
    return {
        "timestamp": timestamp,
        "sdkInfo": dict(SDK_INFO),
        "deviceObj": features,
        "productInfo": product_info,
        "clientInfo": client_info,
        "statisticsInfo": statistics,
        "extraIds": {"1": ""},
    }


def _build_packet(
    payload: dict[str, Any],
    device_uuid: str,
    cached_token: str = "",
    timestamp: int | None = None,
) -> dict[str, Any]:
    timestamp = timestamp or _now_ms()
    middle_key = base64.b64encode(_xxtea_encrypt(device_uuid.encode("ascii"), PACKET_KEY)).decode("ascii")
    content = base64.b64encode(_xxtea_encrypt(_json_bytes(payload), middle_key.encode("ascii"))).decode("ascii")

    if cached_token:
        request_token = cached_token
        request_type = "1"
    else:
        utc8_day_start = timestamp - (timestamp + 28_800_000) % 86_400_000
        request_token = _encrypt_text(middle_key, str(utc8_day_start))
        request_type = "0"

    return {
        "req": {
            "content": content,
            "channel": CHANNEL,
            "token": request_token,
            "version": "1",
            "type": request_type,
            "timestamp": str(timestamp),
        }
    }


def _post_json(
    url: str,
    payload: dict[str, Any],
    timeout: float,
    proxy_url: str = "",
) -> dict[str, Any]:
    client_args: dict[str, Any] = {
        "timeout": timeout,
        "verify": TLS_VERIFY,
        "headers": {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 Mobile/15E148 "
                "MicroMessenger/8.0.73 MiniProgramEnv/iOS"
            ),
            "Referer": f"https://servicewechat.com/{WX_APPID}/{PACKAGE_VERSION}/page-frame.html",
        },
    }
    if proxy_url:
        client_args["proxy"] = proxy_url
    try:
        with httpx.Client(**client_args) as client:
            response = client.post(url, content=_json_bytes(payload))
            response.raise_for_status()
            raw = response.content
    except httpx.HTTPStatusError as exc:
        raise TuringProtocolError(
            f"{url} HTTP {exc.response.status_code}，响应长度={len(exc.response.content)}"
        ) from exc
    except httpx.HTTPError as exc:
        raise TuringProtocolError(f"{url} 网络失败: {type(exc).__name__}") from exc

    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TuringProtocolError(f"{url} 返回非 JSON，长度={len(raw)}") from exc
    if not isinstance(result, dict):
        raise TuringProtocolError(f"{url} 返回类型异常: {type(result).__name__}")
    return result


def _extract_resp(response: dict[str, Any], endpoint: str, require_outer_ret: bool) -> dict[str, Any]:
    outer_ret = response.get("ret")
    if require_outer_ret and outer_ret != 0:
        raise TuringProtocolError(f"{endpoint} 外层响应失败: ret={outer_ret}")
    if not require_outer_ret and outer_ret not in (None, 0):
        raise TuringProtocolError(f"{endpoint} 外层响应失败: ret={outer_ret}")
    data = response.get("data")
    resp = data.get("resp") if isinstance(data, dict) else None
    if not isinstance(resp, dict):
        raise TuringProtocolError(f"{endpoint} 响应缺少 data.resp 对象")
    return resp


def _ms_bytes(timestamp: int) -> bytes:
    # 插件用 JS >>56 等位移，移位数按 32 取模，因此低 32 位会重复一次。
    low32 = (timestamp & 0xFFFFFFFF).to_bytes(4, "big")
    return low32 + low32


def build_v3_device_token(
    raw_device_token: str,
    timestamp: int | None = None,
    *,
    ret: int = 0,
    msg: str = "",
    raw_payload: dict[str, Any] | None = None,
    sign_key: str = "",
    request_package_name: str = "",
) -> str:
    """把服务端 msgBlock 或内联 risk payload 封装成最终 V3 token。"""
    timestamp = timestamp or _now_ms()
    payload = {
        "features": {"1": "", "2": "", "3": CHANNEL, "4": request_package_name},
        "timestamp": timestamp,
        "flags": 0,
        "deviceToken": raw_device_token,
        "raw": raw_payload or {},
        "ret": ret,
        "msg": msg,
        "signKey": sign_key,
    }
    plain = bytes((SDK_INFO["platform"],)) + _json_bytes(payload)
    compressed = zlib.compress(plain)
    time_bytes = _ms_bytes(timestamp)
    cipher = _xxtea_encrypt(compressed, V3_KEY_PREFIX + time_bytes)
    return "v3:" + base64.b64encode(bytes((2,)) + time_bytes + cipher).decode("ascii")


def decode_v3_device_token(token: str) -> dict[str, Any]:
    """解开本实现生成的 V3 token，供单测和排错使用。"""
    if not token.startswith("v3:"):
        raise TuringProtocolError("deviceToken 缺少 v3: 前缀")
    envelope = base64.b64decode(token[3:], validate=True)
    if len(envelope) < 13 or envelope[0] != 2:
        raise TuringProtocolError("deviceToken V3 envelope 无效")
    time_bytes = envelope[1:9]
    plain = _xxtea_decrypt(envelope[9:], V3_KEY_PREFIX + time_bytes)
    unpacked = zlib.decompress(plain)
    if not unpacked or unpacked[0] != SDK_INFO["platform"]:
        raise TuringProtocolError("deviceToken platform 字节无效")
    result = json.loads(unpacked[1:].decode("utf-8"))
    if not isinstance(result, dict):
        raise TuringProtocolError("deviceToken payload 不是对象")
    return result


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"uuid": uuid.uuid4().hex}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TuringProtocolError(f"状态文件读取失败: {path}") from exc
    if not isinstance(data, dict):
        raise TuringProtocolError(f"状态文件根节点必须是对象: {path}")
    device_uuid = str(data.get("uuid") or "")
    if len(device_uuid) != 32 or any(char not in "0123456789abcdefABCDEF" for char in device_uuid):
        # UUID 就是设备身份；损坏后必须丢弃所有绑定旧设备的可重放缓存。
        data = {"uuid": uuid.uuid4().hex}
        _save_state(path, data)
    return data


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{secrets.token_hex(4)}.tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def _merge_profile(profile_path: Path | None, plugin_login_code: str) -> dict[str, Any]:
    profile = json.loads(json.dumps(DEFAULT_PROFILE))
    if profile_path:
        try:
            custom = json.loads(profile_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TuringProtocolError(f"设备配置读取失败: {profile_path}") from exc
        if not isinstance(custom, dict):
            raise TuringProtocolError("设备配置根节点必须是对象")
        custom_system = custom.pop("system_info", {})
        profile.update(custom)
        if isinstance(custom_system, dict):
            profile["system_info"].update(custom_system)
    if plugin_login_code:
        profile["plugin_login_code"] = plugin_login_code
    return profile


def _generate_device_token_unlocked(
    openid: str,
    *,
    state_path: str | Path = DEFAULT_STATE_PATH,
    profile: dict[str, Any] | None = None,
    force_refresh: bool = False,
    timeout: float = 20.0,
    proxy_url: str = "",
    debug: bool = False,
) -> dict[str, Any]:
    """生成可直接放入业务请求 header 的 deviceToken。"""
    if not openid.strip():
        raise ValueError("openid 不能为空")

    path = Path(state_path)
    state = _load_state(path)
    current_profile = profile or json.loads(json.dumps(DEFAULT_PROFILE))
    now = _now_ms()

    # 不落盘保存明文 openid，只用摘要隔离不同账号的缓存。
    openid_hash = hashlib.sha256(openid.encode("utf-8")).hexdigest()
    if state.get("openid_hash") not in (None, openid_hash):
        state = {"uuid": state["uuid"]}
    state["openid_hash"] = openid_hash
    context_hash = hashlib.sha256(
        _json_bytes(current_profile) + b"\x00" + proxy_url.encode("utf-8")
    ).hexdigest()
    if state.get("context_hash") not in (None, context_hash):
        state = {"uuid": state["uuid"], "openid_hash": openid_hash}
    state["context_hash"] = context_hash
    if force_refresh:
        for key in (
            "ticket",
            "ticket_expires_at",
            "turing_token",
            "risk_token",
            "raw_device_token",
            "raw_expires_at",
            "sign_key",
            "ctrl_flags",
        ):
            state.pop(key, None)

    cached_raw = state.get("raw_device_token")
    if (
        not force_refresh
        and isinstance(cached_raw, str)
        and cached_raw
        and now < _as_int(state.get("raw_expires_at"))
    ):
        token = build_v3_device_token(
            cached_raw,
            request_package_name=str(current_profile.get("request_package_name", "")),
        )
        return {"deviceToken": token, "source": "cache", "ret": 0}

    ticket = str(state.get("ticket", ""))
    if force_refresh or not ticket or now >= _as_int(state.get("ticket_expires_at")):
        ticket_payload = _build_payload("ticket", current_profile, state["uuid"], openid)
        cached_turing_token = str(state.get("turing_token", ""))
        ticket_packet = _build_packet(ticket_payload, state["uuid"], cached_turing_token)
        ticket_response = _post_json(TICKET_URL, ticket_packet, timeout, proxy_url)
        ticket_resp = _extract_resp(ticket_response, "ticket", True)
        if _as_int(ticket_resp.get("ret"), -1) == INVALID_REQUEST_RET and cached_turing_token:
            state.pop("turing_token", None)
            _save_state(path, state)
            ticket_packet = _build_packet(ticket_payload, state["uuid"])
            ticket_response = _post_json(TICKET_URL, ticket_packet, timeout, proxy_url)
            ticket_resp = _extract_resp(ticket_response, "ticket 无缓存重试", True)
        ticket_ret = _as_int(ticket_resp.get("ret"), -1)
        if ticket_ret < 0 or not isinstance(ticket_resp.get("ticketID"), str) or not ticket_resp["ticketID"]:
            if ticket_ret == INVALID_REQUEST_RET:
                state.pop("turing_token", None)
                _save_state(path, state)
            raise TuringProtocolError(f"ticket 业务响应失败: ret={ticket_ret} err={ticket_resp.get('err', '')}")

        ticket = str(ticket_resp["ticketID"])
        ticket_ttl = max(0, _as_int(ticket_resp.get("overtime")))
        state["ticket"] = ticket
        state["ticket_expires_at"] = _now_ms() + ticket_ttl * 1000
        if ticket_resp.get("token"):
            state["turing_token"] = str(ticket_resp["token"])
        extra_ids = ticket_resp.get("extraIds") or []
        state["tdid"] = str(extra_ids[0]) if isinstance(extra_ids, list) and extra_ids else ""
        _save_state(path, state)
        if debug:
            print(f"[Turing] ticket 成功，ticket_len={len(ticket)}，ttl={ticket_ttl}s", file=sys.stderr)

    risk_payload = _build_payload("risk", current_profile, state["uuid"], openid, ticket)
    risk_packet = _build_packet(risk_payload, state["uuid"], str(state.get("risk_token", "")))
    risk_response = _post_json(RISK_URL, risk_packet, timeout, proxy_url)
    # risk 线上响应只有 data.resp，和 ticket 响应不同，外层可能没有 ret。
    risk_resp = _extract_resp(risk_response, "risk", False)

    risk_ret = _as_int(risk_resp.get("ret"), -1)
    raw_value = risk_resp.get("msgBlock")
    if raw_value is not None and not isinstance(raw_value, str):
        raise TuringProtocolError(f"risk msgBlock 类型异常: {type(raw_value).__name__}")
    raw_token = raw_value or ""
    if risk_resp.get("token"):
        state["risk_token"] = str(risk_resp["token"])
    # SDK 在 ret=4/6 时会无条件删除 rk；即使响应同时下发了新 token 也不能保留。
    if risk_ret in INVALID_RISK_TOKEN_RETS:
        state.pop("risk_token", None)
    state["ctrl_flags"] = risk_resp.get("ctrlFlags", "")

    if risk_ret in EXTERNAL_ERROR_RETS or risk_ret != 0:
        _save_state(path, state)
        raise TuringProtocolError(f"risk 业务响应失败: ret={risk_ret} err={risk_resp.get('err', '')}")

    if raw_token:
        raw_ttl = max(0, _as_int(risk_resp.get("overtime")))
        state["raw_device_token"] = raw_token
        state["raw_expires_at"] = _now_ms() + raw_ttl * 1000
        state["sign_key"] = str(risk_resp.get("signKey", ""))
        _save_state(path, state)
        token = build_v3_device_token(
            raw_token,
            ret=risk_ret,
            request_package_name=str(current_profile.get("request_package_name", "")),
        )
        if debug:
            print(f"[Turing] risk 成功，raw_len={len(raw_token)}，ttl={raw_ttl}s", file=sys.stderr)
        return {"deviceToken": token, "source": "risk", "ret": risk_ret}

    _save_state(path, state)
    raise TuringProtocolError(f"risk 未返回 msgBlock: ret={risk_ret} err={risk_resp.get('err', '')}")


def generate_device_token(
    openid: str,
    *,
    state_path: str | Path = DEFAULT_STATE_PATH,
    profile: dict[str, Any] | None = None,
    force_refresh: bool = False,
    timeout: float = 20.0,
    proxy_url: str = "",
    debug: bool = False,
) -> dict[str, Any]:
    """按状态文件串行生成 token，避免同一设备并发触发 Turing 次数限制。"""
    path = Path(state_path).resolve()
    lock_key = str(path).casefold()
    with _STATE_LOCKS_GUARD:
        lock = _STATE_LOCKS.setdefault(lock_key, threading.Lock())
    with lock:
        return _generate_device_token_unlocked(
            openid,
            state_path=path,
            profile=profile,
            force_refresh=force_refresh,
            timeout=timeout,
            proxy_url=proxy_url,
            debug=debug,
        )


def _self_test() -> None:
    fixed_timestamp = 1_720_000_000_123
    raw_token = "RAW_DEVICE_TOKEN_TEST_001"
    token = build_v3_device_token(raw_token, fixed_timestamp)
    decoded = decode_v3_device_token(token)
    expected_json = (
        '{"features":{"1":"","2":"","3":"109092","4":""},'
        '"timestamp":1720000000123,"flags":0,'
        '"deviceToken":"RAW_DEVICE_TOKEN_TEST_001","raw":{},'
        '"ret":0,"msg":"","signKey":""}'
    )
    if _json_bytes(decoded).decode("utf-8") != expected_json:
        raise AssertionError("V3 payload 与原 SDK oracle 不一致")
    if _ms_bytes(fixed_timestamp).hex() != "77fd307b77fd307b":
        raise AssertionError("JS 时间戳位移兼容失败")
    print("self-test: ok")


def main() -> int:
    parser = argparse.ArgumentParser(description="脉动 Turing deviceToken V3 纯协议生成器")
    parser.add_argument("--openid", default=os.getenv("MAIDONG_OPENID", ""), help="业务登录响应里的 extend.openId")
    parser.add_argument(
        "--state",
        type=Path,
        default=DEFAULT_STATE_PATH,
        help="UUID/ticket/token 缓存文件",
    )
    parser.add_argument("--profile", type=Path, help="可选设备特征 JSON")
    parser.add_argument(
        "--plugin-login-code",
        default=os.getenv("TURING_PLUGIN_LOGIN_CODE", ""),
        help="可选 wx.pluginLogin code；不传时字段 130 为空",
    )
    parser.add_argument("--force-refresh", action="store_true", help="忽略本地 ticket/raw token 缓存")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--proxy", default="", help="可选 HTTP/SOCKS5 代理")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    try:
        if args.self_test:
            _self_test()
            return 0
        profile = _merge_profile(args.profile, args.plugin_login_code)
        result = generate_device_token(
            args.openid,
            state_path=args.state,
            profile=profile,
            force_refresh=args.force_refresh,
            timeout=args.timeout,
            proxy_url=args.proxy,
            debug=args.debug,
        )
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0
    except (OSError, ValueError, TuringProtocolError, AssertionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
