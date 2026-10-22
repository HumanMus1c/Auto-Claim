"""Trae 多账号签到 / 用量统计后端。

主要能力移植自 star620/TraeTools（MIT）：
  - 从本机 Trae 桌面客户端读取登录态（storage.json / leveldb，只读）
  - Cloud-IDE-JWT / X-Cloudide-Session 识别、清洗与静默续期
  - 多账号签到状态、每日领取、剩余积分、账号资料、学生认证
  - 按会话的用量统计（模型 / Token / 缓存 / 积分）与积分过期提醒
  - 签到历史 SQLite、每日积分快照、飞书机器人推送

和上游 C# 版保持相同的接口与风控口径：
  POST /trae/api/v2/ug/checkin_credits/status
  POST /trae/api/v2/ug/checkin_credits/claim
  POST /cloudide/api/v3/common/GetUserToken
  POST /trae/api/v2/pay/user_current_entitlement_list
  POST /cloudide/api/v3/trae/GetUserInfo
  POST /trae/api/v3/student_verification/status
  POST /trae/api/v1/pay/query_user_usage_group_by_session
  POST /trae/api/v2/pay/expired_ents

x-device-id 必须是 16 位数字（Aha 设备号）；用 UUID 会触发 9074。

本模块只使用 Python 标准库；敏感字段在 Windows 上用当前用户的 DPAPI
加密落盘。所有公开数据都不含 Token / Session 原文。

CLI:
  python trae_signin.py status
  python trae_signin.py auto
  python trae_signin.py silent      # 计划任务用，结果写 trae_signin.log
  python trae_signin.py doctor
  python trae_signin.py usage
  python trae_signin.py import-local

退出码：0 成功/已签到；1 部分或全部失败；2 没有账号/登录态或参数错误。
"""

import base64
import binascii
import ctypes
import hashlib
import json
import os
import random
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

VERSION = "1.0.0"

BASE_URL = "https://api.trae.cn"
TZ_CN = timezone(timedelta(hours=8))
REQUEST_TIMEOUT = 30
MAX_RESPONSE_BYTES = 4 << 20
BUDGET_SECONDS = 600.0
MAX_USAGE_PAGES = 8
USAGE_PAGE_SIZE = 50
DEFAULT_USAGE_DAYS = 7
EXPIRE_WARN_DAYS = 7

CLIENT_DIRS = (
    "Trae CN",
    "TRAE CN",
    "TRAE SOLO CN",
    "TRAE SOLO",
    "TRAE",
    "Trae",
)
STORAGE_REL = ("User", "globalStorage", "storage.json")
LEVELDB_RELS = ("Local Storage\\leveldb", "Session Storage")
MAX_SCAN_BYTES = 24 * 1024 * 1024
MAX_SCAN_FILES = 200

JWT_RE = re.compile(
    r"eyJ[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}")
DEVICE_RE = re.compile(r"iCubeAuthInfo://icube-dc:(\d{16})(?!\d)")
SESSION_NAME = "X-Cloudide-Session"
MIN_TOKEN_LENGTH = 40
MIN_SESSION_LENGTH = 16

# Trae 把登录态加密写在 storage.json 的 iCubeAuthInfo://icube.cloudide。
# 格式：6 字节头 + 32 字节密钥材料 + AES-128-CBC/PKCS#7 密文；
# 明文是 SHA-512(JSON) + JSON。密钥和 IV 由密钥材料经 SHA-512 派生。
ICUBE_AUTH_KEY = "iCubeAuthInfo://icube.cloudide"
_ICUBE_HEADER = b"tc\x05\x10\x00\x00"
_ICUBE_XOR = bytes.fromhex(
    "4dd4c2e6b83162090e52b3c7a6733ba41cb2462b829ab58a196b39db57177524"
    "f49baf7f08e8d68d26a72e37c1a95a2f1f05a51892aef2949732b62a38aadd58")
_BCRYPT_BLOCK_PADDING = 0x00000001

FAILURE_RESULTS = (
    "ERROR", "NETWORK", "TIMEOUT", "NO_AUTH", "AUTH_ERROR",
    "AUTH_REJECTED", "NO_ACCOUNT", "NO_CLIENT", "FORBIDDEN", "PARTIAL",
)
SUCCESS_RESULTS = ("OK", "ALREADY", "CLAIMED", "NO_CHANGE")

_LOCK = threading.RLock()
_DB_LOCK = threading.RLock()
_started_at = time.monotonic()
_sensitive = set()

_ACCOUNT_FIELDS = (
    "id", "name", "token", "session", "device_id", "account_uid",
    "token_updated_at", "last_checkin_date", "remaining_credits", "enabled",
    "is_member", "screen_name", "mobile_masked", "avatar_url", "is_student",
    "source", "region", "host", "created_at", "last_status", "last_usage_at",
)
_DEFAULT_SETTINGS = {
    "interval_seconds": 5,
    "feishu_webhook": "",
    "auto_enabled": True,
    "auto_time": "08:05",
    "usage_days": DEFAULT_USAGE_DAYS,
}


# ---------------------------------------------------------------- 路径

def _env_flag(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def data_dir():
    override = os.environ.get("TRAE_DATA_DIR")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    base = os.environ.get("APPDATA")
    if not base:
        base = os.path.join(str(Path.home()), "AppData", "Roaming")
    return os.path.join(base, "Auto-Claim", "trae")


def store_path():
    return os.path.join(data_dir(), "accounts.json")


def db_path():
    return os.path.join(data_dir(), "checkin.db")


def log_path():
    override = os.environ.get("TRAE_SIGNIN_LOG")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "trae_signin.log")


def _remaining():
    return BUDGET_SECONDS - (time.monotonic() - _started_at)


def _now():
    return datetime.now()


def _now_text():
    return _now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 敏感字段

if os.name == "nt":
    import ctypes.wintypes as _wt

    class _DataBlob(ctypes.Structure):
        _fields_ = [("cbData", _wt.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _dpapi(data, protect):
        src = _DataBlob(len(data), ctypes.create_string_buffer(data, len(data)))
        dst = _DataBlob()
        func = (ctypes.windll.crypt32.CryptProtectData if protect
                else ctypes.windll.crypt32.CryptUnprotectData)
        # CryptProtectData(pDataIn, description, entropy, reserved, prompt, flags, pDataOut)
        args = [ctypes.byref(src), None, None, None, None, 0, ctypes.byref(dst)]
        if not func(*args):
            raise OSError("DPAPI 操作失败")
        try:
            return ctypes.string_at(dst.pbData, dst.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(dst.pbData)


def _seal(value):
    """加密落盘。非 Windows 原样保存（仅用于测试/开发）。"""
    if value is None or value == "":
        return value
    if os.name != "nt":
        return value
    try:
        blob = _dpapi(str(value).encode("utf-8"), True)
        return "dpapi:" + base64.b64encode(blob).decode("ascii")
    except Exception:
        # DPAPI 失败时保留明文，避免直接丢凭据；下次加载仍可使用。
        return value


def _unseal(value):
    if value is None or value == "":
        return value
    if not isinstance(value, str) or not value.startswith("dpapi:"):
        return value
    if os.name != "nt":
        return None
    try:
        blob = base64.b64decode(value[6:])
        return _dpapi(blob, False).decode("utf-8")
    except Exception:
        return None


def _mask(value):
    if not value:
        return ""
    value = str(value)
    if len(value) <= 16:
        return "*" * 8
    return value[:6] + "…" + value[-6:]


def _mask_digits(value):
    if not value:
        return ""
    value = str(value)
    if len(value) <= 8:
        return "*" * len(value)
    return value[:4] + "…" + value[-4:]


# ---------------------------------------------------------------- JWT / 凭证

def _b64url_decode(segment):
    padded = segment.replace("-", "+").replace("_", "/")
    padding = len(padded) % 4
    if padding == 2:
        padded += "=="
    elif padding == 3:
        padded += "="
    return base64.b64decode(padded)


def jwt_payload(token):
    """解析 JWT payload；失败返回 None。"""
    if not token or not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) < 2:
        return None
    try:
        data = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
    except (ValueError, TypeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def jwt_uid(token):
    payload = jwt_payload(token)
    if not payload:
        return None
    data = payload.get("data")
    if isinstance(data, dict):
        uid = data.get("id")
        if isinstance(uid, str) and uid:
            return uid
    return None


def jwt_expiry(token):
    """解析 exp（根或 data.exp），返回本地 aware datetime；失败 None。"""
    payload = jwt_payload(token)
    if not payload:
        return None
    for source in (payload, payload.get("data")):
        if not isinstance(source, dict):
            continue
        raw = source.get("exp")
        try:
            seconds = int(raw)
        except (TypeError, ValueError):
            continue
        if 1_000_000_000 <= seconds <= 4_102_444_800:
            return datetime.fromtimestamp(seconds, timezone.utc)
    return None


def _expiry_text(token):
    expiry = jwt_expiry(token)
    if not expiry:
        return ""
    return expiry.astimezone().strftime("%Y-%m-%d %H:%M")


def _token_expired(token):
    expiry = jwt_expiry(token)
    return bool(expiry and expiry <= datetime.now(timezone.utc))


def normalize_credential(raw):
    """剥掉 Cookie / Authorization / 引号 / 换行等外壳。

    同时粘贴 Token 与 Cookie 时优先取 JWT：它本身就是完整登录态，
    不必再用会话多打一次 GetUserToken。
    """
    if not raw:
        return ""
    text = str(raw).strip()
    match = JWT_RE.search(text)
    if match:
        return match.group(0)

    idx = text.lower().find(SESSION_NAME.lower() + "=")
    if idx >= 0:
        text = text[idx + len(SESSION_NAME) + 1:]
        end = len(text)
        for ch in (";", "\r", "\n", " ", "\t", '"', "'"):
            pos = text.find(ch)
            if pos >= 0:
                end = min(end, pos)
        text = text[:end]
    text = text.strip().strip('"\'')
    for prefix in ("Authorization:", "Cookie:", "Set-Cookie:"):
        if text.lower().startswith(prefix.lower()):
            text = text[len(prefix):].strip()
    if text.lower().startswith("cloud-ide-jwt"):
        text = text[len("cloud-ide-jwt"):].lstrip(": ").strip()
    if text.lower().startswith("bearer "):
        text = text[7:].strip()
    text = text.replace("\r", "").replace("\n", "").replace("\t", "").strip()
    return text.strip('"\'')


def _looks_like_jwt(text):
    parts = text.split(".")
    if len(parts) != 3 or any(not part for part in parts):
        return False
    if any(not re.fullmatch(r"[A-Za-z0-9_\-]+", part) for part in parts):
        return False
    return isinstance(jwt_payload(text), dict)


def classify_credential(raw):
    text = normalize_credential(raw)
    if not text:
        return "UNKNOWN"
    if _looks_like_jwt(text) and len(text) >= MIN_TOKEN_LENGTH:
        return "TOKEN"
    if len(text) >= MIN_SESSION_LENGTH:
        return "SESSION"
    return "UNKNOWN"


# ---------------------------------------------------------------- 存储

def _valid_device_id(value):
    return bool(re.fullmatch(r"\d{16}", str(value or "")))


def _device_in_use(accounts, value, except_id=None):
    return any(a.get("device_id") == value and a.get("id") != except_id
               for a in accounts)


def _new_device_id(accounts, except_id=None):
    used = {a.get("device_id") for a in accounts
            if a.get("id") != except_id and a.get("device_id")}
    rng = random.SystemRandom()
    while True:
        value = str(rng.randint(1_000_000_000_000_000,
                                9_999_999_999_999_999))
        if value not in used:
            return value


def _normalize_date(value):
    if not value:
        return None
    text = str(value).strip()
    if len(text) >= 10:
        text = text[:10]
    try:
        datetime.strptime(text, "%Y-%m-%d")
        return text
    except ValueError:
        return None


def _normalize_region(value):
    """把登录态里的区域字段收敛成短标签；未知值保持可展示。"""
    if not isinstance(value, (str, int)):
        return ""
    text = str(value).strip().upper()
    if not text:
        return ""
    if text in ("CN", "CHN", "CHINA", "MAINLAND", "MAINLAND_CHINA"):
        return "CN"
    return text[:16]


def _normalize_host(value):
    if not isinstance(value, str):
        return ""
    text = value.strip().rstrip("/")
    if not re.fullmatch(r"https?://[^\s]{1,2048}", text):
        return ""
    return text


def _region_from_auth(info):
    """从 iCubeAuthInfo 解出的结构里读取区域，兼容 CN / SG 两套字段。"""
    if not isinstance(info, dict):
        return ""
    candidates = []
    user_region = info.get("userRegion")
    if isinstance(user_region, dict):
        candidates.extend((user_region.get("region"),
                           user_region.get("_aiRegion")))
    elif isinstance(user_region, (str, int)):
        candidates.append(user_region)
    candidates.extend((info.get("region"), info.get("loginRegion"),
                       info.get("AIRegion"), info.get("storeRegion")))
    for value in candidates:
        region = _normalize_region(value)
        if region:
            return region
    account = info.get("account")
    if isinstance(account, dict):
        for key in ("storeRegion", "region", "country"):
            region = _normalize_region(account.get(key))
            if region:
                return region
    host = (_normalize_host(info.get("host"))
            or _normalize_host(info.get("loginHost"))).lower()
    if "trae.cn" in host or host.endswith(".cn"):
        return "CN"
    if "trae.ai" in host or "growsg" in host:
        return "SG"
    return ""


def _infer_region(region, host, source):
    """旧账号没有区域字段时按客户端来源/域名兜底，避免显示成错误区域。"""
    region = _normalize_region(region)
    if region:
        return region
    host = (host or "").lower()
    if "trae.cn" in host or host.endswith(".cn"):
        return "CN"
    if "trae.ai" in host or "growsg" in host:
        return "SG"
    source = (source or "").lower()
    if "trae cn" in source or "solo cn" in source:
        return "CN"
    if re.search(r"(^|[\\/])trae([\\/]|$)", source):
        return "SG"
    return "CN"


def _normalize_account(raw, accounts):
    raw = raw if isinstance(raw, dict) else {}
    account = {key: raw.get(key) for key in _ACCOUNT_FIELDS}
    account["id"] = str(account.get("id") or uuid_hex())
    account["token"] = _unseal(account.get("token"))
    account["session"] = _unseal(account.get("session"))
    account["device_id"] = _unseal(account.get("device_id")) or ""
    account["name"] = (account.get("name") or "").strip() or None
    account["source"] = account.get("source") or ""
    account["host"] = _normalize_host(account.get("host"))
    account["region"] = _infer_region(account.get("region"),
                                      account["host"], account["source"])
    account["account_uid"] = account.get("account_uid") or jwt_uid(account["token"])
    account["last_checkin_date"] = _normalize_date(account.get("last_checkin_date"))
    account["token_updated_at"] = _normalize_text_time(
        account.get("token_updated_at"))
    account["created_at"] = _normalize_text_time(account.get("created_at"))
    account["last_usage_at"] = _normalize_text_time(account.get("last_usage_at"))
    try:
        account["remaining_credits"] = float(account.get("remaining_credits"))
    except (TypeError, ValueError):
        account["remaining_credits"] = -1.0
    account["enabled"] = bool(account.get("enabled", True))
    account["is_member"] = bool(account.get("is_member", False))
    account["is_student"] = bool(account.get("is_student", False))
    if not isinstance(account.get("last_status"), dict):
        account["last_status"] = {}
    if not _valid_device_id(account.get("device_id")):
        account["device_id"] = _new_device_id(accounts, except_id=account["id"])
    return account


def _normalize_text_time(value):
    if not value:
        return None
    text = str(value).strip()
    try:
        if len(text) >= 19:
            datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
            return text[:19]
        if len(text) >= 10:
            datetime.strptime(text[:10], "%Y-%m-%d")
            return text[:10] + " 00:00:00"
    except ValueError:
        return None
    return None


def uuid_hex():
    return "%032x" % random.SystemRandom().getrandbits(128)


def _fresh_store():
    return {
        "version": 1,
        "accounts": [],
        "active_id": None,
        "settings": dict(_DEFAULT_SETTINGS),
    }


def load_store():
    path = store_path()
    if not os.path.isfile(path):
        return _fresh_store()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        # 损坏的配置改名留档，避免下一次保存直接把用户数据覆盖
        try:
            os.replace(path, path + ".broken-" + time.strftime("%Y%m%d-%H%M%S"))
        except OSError:
            pass
        return _fresh_store()

    store = _fresh_store()
    if not isinstance(raw, dict):
        return store
    settings = raw.get("settings")
    if isinstance(settings, dict):
        for key in _DEFAULT_SETTINGS:
            if key in settings:
                store["settings"][key] = settings[key]
    store["settings"]["feishu_webhook"] = _unseal(
        store["settings"].get("feishu_webhook")) or ""
    try:
        interval = int(store["settings"].get("interval_seconds") or 5)
        store["settings"]["interval_seconds"] = max(0, min(120, interval))
    except (TypeError, ValueError):
        store["settings"]["interval_seconds"] = 5
    store["settings"]["auto_enabled"] = bool(
        store["settings"].get("auto_enabled", True))
    try:
        store["settings"]["usage_days"] = max(
            1, min(90, int(store["settings"].get("usage_days") or 7)))
    except (TypeError, ValueError):
        store["settings"]["usage_days"] = DEFAULT_USAGE_DAYS

    accounts = []
    for item in raw.get("accounts") or []:
        if not isinstance(item, dict):
            continue
        account = _normalize_account(item, accounts)
        accounts.append(account)
    # 设备号必须全局唯一，重复时给后出现的账号重新生成
    seen = set()
    for account in accounts:
        if account["device_id"] in seen:
            account["device_id"] = _new_device_id(accounts, except_id=account["id"])
        seen.add(account["device_id"])
    store["accounts"] = accounts
    active = raw.get("active_id")
    if active and any(a["id"] == active for a in accounts):
        store["active_id"] = active
    elif accounts:
        store["active_id"] = accounts[0]["id"]
    return store


def save_store(store):
    path = store_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    settings = dict(store.get("settings") or {})
    settings["feishu_webhook"] = _seal(settings.get("feishu_webhook"))
    payload = {
        "version": 1,
        "active_id": store.get("active_id"),
        "settings": settings,
        "accounts": [],
    }
    for account in store.get("accounts") or []:
        item = {key: account.get(key) for key in _ACCOUNT_FIELDS}
        item["token"] = _seal(item.get("token"))
        item["session"] = _seal(item.get("session"))
        item["device_id"] = _seal(item.get("device_id"))
        payload["accounts"].append(item)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def active_account(store):
    accounts = store.get("accounts") or []
    if not accounts:
        return None
    active_id = store.get("active_id")
    for account in accounts:
        if account["id"] == active_id:
            return account
    store["active_id"] = accounts[0]["id"]
    return accounts[0]


def find_account(store, account_id):
    for account in store.get("accounts") or []:
        if account["id"] == account_id:
            return account
    return None


# ---------------------------------------------------------------- 可视化字段

def _display_name(account):
    return (account.get("name")
            or account.get("screen_name")
            or "账号 " + account["id"][:6])


def public_account(account, store):
    token = account.get("token") or ""
    expiry = jwt_expiry(token)
    expiry_text = (expiry.astimezone().strftime("%Y-%m-%d %H:%M")
                   if expiry else "")
    remaining = account.get("remaining_credits")
    try:
        remaining = float(remaining)
    except (TypeError, ValueError):
        remaining = -1
    region = _infer_region(account.get("region"), account.get("host"),
                           account.get("source"))
    return {
        "id": account["id"],
        "name": account.get("name") or "",
        "screen_name": account.get("screen_name") or "",
        "display": _display_name(account),
        "source": account.get("source") or "",
        "region": region,
        "region_label": "国内" if region == "CN" else "国际",
        "region_kind": "cn" if region == "CN" else "intl",
        "host": account.get("host") or "",
        "enabled": bool(account.get("enabled", True)),
        "active": account["id"] == store.get("active_id"),
        "is_member": bool(account.get("is_member")),
        "is_student": bool(account.get("is_student")),
        "mobile_masked": account.get("mobile_masked") or "",
        "avatar_url": account.get("avatar_url") or "",
        "token_masked": _mask(token),
        "has_token": bool(token),
        "has_session": bool(account.get("session")),
        "token_expiry": expiry_text,
        "token_expired": bool(expiry and expiry <= datetime.now(timezone.utc)),
        "device_id_masked": _mask_digits(account.get("device_id")),
        "account_uid": account.get("account_uid") or "",
        "last_checkin": account.get("last_checkin_date"),
        "remaining": remaining if remaining >= 0 else None,
        "last_status": account.get("last_status") or {},
        "last_usage_at": account.get("last_usage_at"),
        "created_at": account.get("created_at"),
    }


def public_settings(store):
    settings = store.get("settings") or {}
    return {
        "interval_seconds": settings.get("interval_seconds", 5),
        "feishu_configured": bool(settings.get("feishu_webhook")),
        "auto_enabled": bool(settings.get("auto_enabled", True)),
        "auto_time": settings.get("auto_time") or "08:05",
        "usage_days": settings.get("usage_days") or DEFAULT_USAGE_DAYS,
    }


# ---------------------------------------------------------------- 本机客户端

def find_client_dirs():
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return []
    found = []
    seen = set()
    for name in CLIENT_DIRS:
        path = os.path.join(appdata, name)
        key = os.path.normcase(os.path.abspath(path))
        if key in seen or not os.path.isdir(path):
            continue
        seen.add(key)
        storage = os.path.join(path, *STORAGE_REL)
        leveldb = os.path.join(path, "Local Storage", "leveldb")
        found.append({
            "name": name,
            "path": path,
            "storage": os.path.isfile(storage),
            "leveldb": os.path.isdir(leveldb),
        })
    return found


def _safe_read(path, max_bytes=MAX_SCAN_BYTES, binary=False):
    try:
        with open(path, "rb") as handle:
            data = handle.read(max_bytes)
    except OSError:
        return ""
    if binary:
        return data.decode("latin-1", "ignore")
    return data.decode("utf-8", "replace")


def _extract_session(text):
    """在 storage.json / leveldb 文本里定位会话值。

    键和值之间可能是 JSON 的 `":"`、查询串的 `=`，也可能是 leveldb 的
    \\x00 分隔符；统一跳过非字母数字开头，再取第一个长串。之前把 `:`
    算作合法值字符，遇到 JSON 会只截到一个冒号，导致整条会话被丢掉。
    """
    idx = text.lower().find(SESSION_NAME.lower())
    if idx < 0:
        return None
    tail = text[idx + len(SESSION_NAME):idx + len(SESSION_NAME) + 8192]
    match = re.search(r"[A-Za-z0-9][A-Za-z0-9._:+=%\-]{15,}", tail)
    return match.group(0) if match else None


def _extract_device_id(text):
    match = DEVICE_RE.search(text)
    return match.group(1) if match else None


def _aes_cbc_decrypt(key, iv, data):
    """AES-128-CBC 解密（Windows CNG，保持标准库依赖）。

    CNG 不接受通过 BCryptSetProperty 设置 IV；IV 必须随 BCryptDecrypt
    的 pbIV/cbIV 参数传入。使用 BCRYPT_BLOCK_PADDING 让 CNG 按 PKCS#7
    去掉填充，返回值即可直接做 SHA-512 校验。
    """
    if not hasattr(ctypes, "WinDLL"):
        raise OSError("AES-CBC 解密需要 Windows CNG")
    bcrypt = ctypes.WinDLL("bcrypt.dll")

    def check(status, action):
        if status != 0:
            raise OSError("%s 失败（NTSTATUS=0x%08x）"
                          % (action, status & 0xffffffff))

    algorithm = ctypes.c_void_p()
    key_handle = ctypes.c_void_p()
    try:
        check(bcrypt.BCryptOpenAlgorithmProvider(
            ctypes.byref(algorithm), "AES", None, 0), "BCryptOpenAlgorithmProvider")
        mode = "ChainingModeCBC"
        check(bcrypt.BCryptSetProperty(
            algorithm, "ChainingMode", mode, (len(mode) + 1) * 2, 0),
            "BCryptSetProperty")
        key_buffer = ctypes.create_string_buffer(key, len(key))
        check(bcrypt.BCryptGenerateSymmetricKey(
            algorithm, ctypes.byref(key_handle), None, 0,
            key_buffer, len(key), 0), "BCryptGenerateSymmetricKey")
        input_buffer = ctypes.create_string_buffer(data, len(data))
        iv_buffer = ctypes.create_string_buffer(iv, len(iv))
        output_buffer = ctypes.create_string_buffer(len(data))
        written = ctypes.c_ulong(0)
        check(bcrypt.BCryptDecrypt(
            key_handle, input_buffer, len(data), None,
            iv_buffer, len(iv), output_buffer, len(data),
            ctypes.byref(written), _BCRYPT_BLOCK_PADDING), "BCryptDecrypt")
        return output_buffer.raw[:written.value]
    finally:
        if key_handle.value:
            bcrypt.BCryptDestroyKey(key_handle)
        if algorithm.value:
            bcrypt.BCryptCloseAlgorithmProvider(algorithm, 0)


def _decrypt_icube_value(value):
    """解密一个 iCubeAuthInfo 值；格式不符或校验失败时返回 None。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        blob = base64.b64decode(value)
    except (ValueError, TypeError, binascii.Error):
        return None
    if (len(blob) < 38 + 16 or blob[:6] != _ICUBE_HEADER
            or (len(blob) - 38) % 16):
        return None
    material = blob[6:38]
    digest = hashlib.sha512(
        hashlib.sha512(material).digest() + _ICUBE_XOR).digest()
    try:
        plain = _aes_cbc_decrypt(digest[:16], digest[16:32], blob[38:])
    except (OSError, ValueError):
        return None
    if len(plain) < 64 or plain[:64] != hashlib.sha512(plain[64:]).digest():
        return None
    try:
        parsed = json.loads(plain[64:].decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _extract_icube_auth(text):
    """从未加密的 storage.json 中找到并解密 Trae 登录态。

    返回 token、account_uid 和账号展示字段；不返回 refresh token、
    设备私钥等敏感内容。
    """
    if not text or "iCubeAuthInfo://" not in text:
        return None
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    keys = []
    if isinstance(data.get(ICUBE_AUTH_KEY), str):
        keys.append(ICUBE_AUTH_KEY)
    for key in data:
        if (isinstance(key, str) and key.startswith("iCubeAuthInfo://icube.")
                and key not in keys
                and not key.startswith("iCubeAuthInfo://icube-dc")):
            keys.append(key)

    fallback = None
    for key in keys:
        info = _decrypt_icube_value(data.get(key))
        if not info:
            continue
        token = info.get("token")
        if not isinstance(token, str) or not _looks_like_jwt(token):
            continue
        account = info.get("account")
        account = account if isinstance(account, dict) else {}
        item = {
            "token": token,
            "account_uid": str(info.get("userId") or jwt_uid(token) or ""),
            "screen_name": _text(account.get("username")),
            "mobile_masked": _text(account.get("nonPlainTextMobile")),
            "avatar_url": _text(account.get("avatar_url")),
            "region": _region_from_auth(info),
            "host": (_normalize_host(info.get("host"))
                     or _normalize_host(info.get("loginHost"))),
        }
        if not _token_expired(token):
            return item
        fallback = fallback or item
    return fallback


def _has_icube_auth(text):
    return ('"iCubeAuthInfo://icube.cloudide"' in text
            or "iCubeAuthInfo://icube.cloudide" in text)


def _pick_token(tokens):
    """优先未过期且 exp 最晚的 token；没有 exp 的排最后。"""
    unique = list(dict.fromkeys(tokens))
    if not unique:
        return None
    now = datetime.now(timezone.utc)

    def key(token):
        expiry = jwt_expiry(token)
        if not expiry:
            return (0, 0)
        return (1 if expiry > now else 0, expiry.timestamp())

    return max(unique, key=key)


def _scan_client_dir(client):
    """扫描单个客户端目录，返回候选账号（不含网络请求结果）。"""
    root = client["path"]
    name = client["name"]
    tokens = []
    session = None
    device_id = None
    source = ""
    account_meta = None
    has_encrypted = False

    storage = os.path.join(root, *STORAGE_REL)
    if os.path.isfile(storage):
        text = _safe_read(storage, binary=False)
        device_id = _extract_device_id(text)
        has_encrypted = _has_icube_auth(text)
        account_meta = _extract_icube_auth(text)
        if account_meta:
            tokens.append(account_meta["token"])
            source = name + "\\storage.json"
        matches = JWT_RE.findall(text)
        if matches:
            tokens.extend(matches)
            source = name + "\\storage.json"
        session = _extract_session(text)

    scanned = 0
    for rel in LEVELDB_RELS:
        folder = os.path.join(root, *rel.split("\\"))
        if not os.path.isdir(folder):
            continue
        try:
            files = [
                os.path.join(folder, item)
                for item in os.listdir(folder)
                if os.path.isfile(os.path.join(folder, item))
            ]
        except OSError:
            continue
        files.sort(key=lambda p: os.path.getsize(p) if os.path.exists(p) else 0,
                   reverse=True)
        for file in files:
            if scanned >= MAX_SCAN_FILES:
                break
            scanned += 1
            text = _safe_read(file, binary=True)
            if not text:
                continue
            matches = JWT_RE.findall(text)
            if matches:
                tokens.extend(matches)
                if not source:
                    source = name + "\\" + os.path.basename(file)
            if not session:
                session = _extract_session(text)

    token = _pick_token(tokens)
    if token and _token_expired(token) and not session:
        return {
            "ok": False,
            "source": name,
            "message": "客户端 Token 已过期，且没有可续期的会话；请打开 Trae 重新登录",
        }
    if not token and not session:
        if has_encrypted:
            message = ("检测到 iCubeAuthInfo 加密登录态，但解密失败；"
                       "请升级 Auto-Claim 或手动添加 Token / 会话 Cookie")
        else:
            message = ("未读取到登录凭证（可能尚未登录，"
                       "或客户端存储格式已变化）")
        return {
            "ok": False,
            "source": name,
            "message": message,
        }
    meta = account_meta or {}
    region = _infer_region(meta.get("region"), meta.get("host"), name)
    return {
        "ok": True,
        "name": meta.get("screen_name") or None,
        "screen_name": meta.get("screen_name") or "",
        "mobile_masked": meta.get("mobile_masked") or "",
        "avatar_url": meta.get("avatar_url") or "",
        "token": token,
        "session": session,
        "device_id": device_id,
        "source": source or name,
        "account_uid": meta.get("account_uid") or jwt_uid(token),
        "region": region,
        "host": meta.get("host") or "",
        "client_name": name,
    }


# ---------------------------------------------------------------- HTTP

class TraeApi:
    def __init__(self, token=None, session=None, device_id=None,
                 base_url=None):
        self.token = token or ""
        self.session = session or ""
        self.device_id = device_id or ""
        self.base_url = (base_url or os.environ.get("TRAE_BASE_URL")
                         or BASE_URL).rstrip("/")
        self.last_error = ""

    # -- 基础请求 -------------------------------------------------

    def _request(self, path, method="POST", json_body=None, auth=False,
                 cookie=False, referer=False, retries=0):
        url = self.base_url + path
        headers = {
            "Accept": "application/json",
            "User-Agent": "TraeTools/1.0 (Auto-Claim)",
        }
        if auth:
            if not self.token:
                return 0, None, "缺少 Token"
            headers["Authorization"] = "Cloud-IDE-JWT " + self.token
            headers["X-User-Region"] = "cn"
            if self.device_id:
                headers["x-device-id"] = self.device_id
        if cookie and self.session:
            headers["Cookie"] = SESSION_NAME + "=" + self.session
        if referer:
            headers["Referer"] = "https://www.trae.cn/"
            headers["Origin"] = "https://www.trae.cn"

        data = None
        if json_body is not None:
            data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif method.upper() == "POST":
            data = b""

        delay = 0.8
        last_error = ""
        for attempt in range(retries + 1):
            request = urllib.request.Request(
                url, data=data, headers=headers, method=method.upper())
            try:
                with urllib.request.urlopen(request,
                                            timeout=REQUEST_TIMEOUT) as resp:
                    raw = resp.read(MAX_RESPONSE_BYTES)
                    return resp.status, _decode_body(raw), ""
            except urllib.error.HTTPError as exc:
                try:
                    raw = exc.read(MAX_RESPONSE_BYTES)
                except OSError:
                    raw = b""
                body = _decode_body(raw)
                # 4xx 不重试；429 和 5xx 才值得再试
                if exc.code == 429 or 500 <= exc.code < 600:
                    last_error = "HTTP %s" % exc.code
                else:
                    return exc.code, body, ""
            except (urllib.error.URLError, OSError) as exc:
                last_error = str(exc)
            if attempt < retries:
                if _remaining() <= delay + REQUEST_TIMEOUT:
                    break
                time.sleep(delay)
                delay *= 2
        self.last_error = last_error
        return 0, None, last_error

    # -- 签到 -----------------------------------------------------

    def status(self, retries=1):
        code, body, err = self._request(
            "/trae/api/v2/ug/checkin_credits/status",
            auth=True, json_body={}, retries=retries)
        if not isinstance(body, dict):
            self.last_error = err or ("HTTP %s 空响应" % code)
            return None
        return body

    def claim(self, retries=1):
        code, body, err = self._request(
            "/trae/api/v2/ug/checkin_credits/claim",
            auth=True, json_body={}, retries=retries)
        if not isinstance(body, dict):
            self.last_error = err or ("HTTP %s 空响应" % code)
            return None
        return body

    def get_user_token(self, session=None, retries=1):
        session = session or self.session
        if not session:
            return None
        saved = self.session
        self.session = session
        try:
            code, body, err = self._request(
                "/cloudide/api/v3/common/GetUserToken",
                auth=False, cookie=True, referer=True, retries=retries)
        finally:
            self.session = saved
        if not isinstance(body, dict):
            self.last_error = err or ("HTTP %s 空响应" % code)
            return None
        result = body.get("Result")
        token = result.get("Token") if isinstance(result, dict) else None
        if not isinstance(token, str) or not token.strip():
            self.last_error = "GetUserToken 响应缺少 Result.Token"
            return None
        return token.strip()

    # -- 积分 / 资料 ---------------------------------------------

    def remaining_credits(self):
        code, body, err = self._request(
            "/trae/api/v2/pay/user_current_entitlement_list",
            auth=True, json_body={}, retries=1)
        if not isinstance(body, dict):
            self.last_error = err or ("HTTP %s 空响应" % code)
            return -1
        packs = body.get("user_entitlement_pack_list")
        if not isinstance(packs, list):
            self.last_error = "响应缺少 user_entitlement_pack_list"
            return -1
        if not packs:
            self.last_error = "积分包列表为空，可能会话失效"
            return -1
        total = 0.0
        for pack in packs:
            if not isinstance(pack, dict):
                continue
            limit = 0.0
            info = pack.get("entitlement_base_info")
            if isinstance(info, dict):
                quota = info.get("quota")
                if isinstance(quota, dict):
                    limit = _to_float(quota.get("credits_limit"))
            used = _to_float((pack.get("usage") or {}).get("credits_amount")) \
                if isinstance(pack.get("usage"), dict) else 0.0
            total += max(0.0, limit - used)
        return total

    def profile(self):
        code, body, err = self._request(
            "/cloudide/api/v3/trae/GetUserInfo",
            auth=True, cookie=True, referer=True, json_body={}, retries=1)
        if not isinstance(body, dict):
            self.last_error = err or ("HTTP %s 空响应" % code)
            return None
        result = body.get("Result")
        if not isinstance(result, dict):
            self.last_error = "GetUserInfo 缺少 Result"
            return None
        return {
            "user_id": _text(result.get("UserID")),
            "screen_name": _text(result.get("ScreenName")),
            "mobile_masked": _text(result.get("NonPlainTextMobile")),
            "avatar_url": _text(result.get("AvatarUrl")),
            "tenant_id": _text(result.get("TenantID")),
        }

    def student_status(self):
        code, body, err = self._request(
            "/trae/api/v3/student_verification/status",
            auth=True, cookie=True, referer=True, json_body={}, retries=1)
        if not isinstance(body, dict):
            self.last_error = err or ("HTTP %s 空响应" % code)
            return 0
        try:
            return int(body.get("status") or 0)
        except (TypeError, ValueError):
            return 0

    # -- 用量 -----------------------------------------------------

    def usage_page(self, start_seconds, end_seconds, page_num,
                   page_size=USAGE_PAGE_SIZE, retries=1):
        code, body, err = self._request(
            "/trae/api/v1/pay/query_user_usage_group_by_session",
            auth=True, cookie=True, referer=True, retries=retries,
            json_body={
                "start_time": int(start_seconds),
                "end_time": int(end_seconds),
                "page_size": int(page_size),
                "page_num": int(page_num),
                "usage_type": [7],
            })
        if not isinstance(body, dict):
            self.last_error = err or ("HTTP %s 空响应" % code)
            return None
        return body

    def expired_ents(self):
        code, body, err = self._request(
            "/trae/api/v2/pay/expired_ents",
            auth=True, cookie=True, referer=True, json_body={}, retries=1)
        if not isinstance(body, dict):
            self.last_error = err or ("HTTP %s 空响应" % code)
            return []
        rows = body.get("expired_ent_list")
        if not isinstance(rows, list):
            return []
        result = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            item = {
                "name": _text(row.get("name")),
                "credits_limit": _to_float(row.get("credits_limit")),
                "expire_time_ms": _to_int(row.get("expire_time_ms")),
            }
            if item["name"] or item["credits_limit"] > 0:
                result.append(item)
        return result


def _decode_body(raw):
    text = raw.decode("utf-8", "replace")
    try:
        return json.loads(text)
    except ValueError:
        return text


def _to_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _text(value):
    return value if isinstance(value, str) else ""


def _status_ok(payload):
    try:
        return bool(payload) and int(payload.get("code") or 0) == 0
    except (TypeError, ValueError):
        return False


def _status_checked(payload):
    return bool(payload) and payload.get("checked_in") is True


def _gained(payload, is_member):
    if not _status_ok(payload) or not _status_checked(payload):
        return 0.0
    total = _to_float(payload.get("credits"))
    if is_member:
        total += _to_float(payload.get("extra_credits"))
    return total


# ---------------------------------------------------------------- SQLite

def _connect():
    path = db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _init_db(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS checkin_record (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            date        TEXT    NOT NULL,
            time        TEXT    NOT NULL,
            account_id  TEXT    NOT NULL,
            account_name TEXT   NOT NULL DEFAULT '',
            credits     REAL    NOT NULL DEFAULT 0,
            is_member   INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_checkin_date
            ON checkin_record(date);
        CREATE INDEX IF NOT EXISTS idx_checkin_account
            ON checkin_record(account_id);

        CREATE TABLE IF NOT EXISTS credits_snapshot (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            date        TEXT    NOT NULL,
            account_id  TEXT    NOT NULL,
            remaining   REAL    NOT NULL DEFAULT 0
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_snapshot_date_account
            ON credits_snapshot(date, account_id);

        CREATE TABLE IF NOT EXISTS usage_session (
            account_id       TEXT NOT NULL,
            session_key      TEXT NOT NULL,
            session_id       TEXT NOT NULL DEFAULT '',
            usage_time       INTEGER NOT NULL DEFAULT 0,
            model_name       TEXT NOT NULL DEFAULT '',
            mode             TEXT NOT NULL DEFAULT '',
            credits          REAL NOT NULL DEFAULT 0,
            cost_money       REAL NOT NULL DEFAULT 0,
            input_token      INTEGER NOT NULL DEFAULT 0,
            output_token     INTEGER NOT NULL DEFAULT 0,
            cache_read_token INTEGER NOT NULL DEFAULT 0,
            cache_write_token INTEGER NOT NULL DEFAULT 0,
            preview          TEXT NOT NULL DEFAULT '',
            fetched_at       TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (account_id, session_key)
        );
        CREATE INDEX IF NOT EXISTS idx_usage_account_time
            ON usage_session(account_id, usage_time);
    """)


def _db_call(fn):
    with _DB_LOCK:
        conn = _connect()
        try:
            _init_db(conn)
            result = fn(conn)
            conn.commit()
            return result
        finally:
            conn.close()


def record_checkin(account, when, credits, is_member=False):
    """记录一次签到。同一天同一账号只保留一条，积分取较大值。"""
    day = when.strftime("%Y-%m-%d")
    clock = when.strftime("%H:%M")
    account_id = account["id"]
    name = _display_name(account)
    gained = float(credits or 0)

    def run(conn):
        row = conn.execute(
            "SELECT id, credits FROM checkin_record "
            "WHERE date = ? AND account_id = ? LIMIT 1",
            (day, account_id)).fetchone()
        if row:
            if gained > float(row["credits"] or 0):
                conn.execute(
                    "UPDATE checkin_record SET credits = ?, is_member = ? "
                    "WHERE id = ?",
                    (gained, 1 if is_member else 0, row["id"]))
        else:
            conn.execute(
                "INSERT INTO checkin_record "
                "(date, time, account_id, account_name, credits, is_member) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (day, clock, account_id, name, gained,
                 1 if is_member else 0))

    _db_call(run)


def record_snapshot(account_id, remaining):
    if remaining is None or remaining < 0:
        return
    today = _now().strftime("%Y-%m-%d")

    def run(conn):
        conn.execute(
            "INSERT INTO credits_snapshot (date, account_id, remaining) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(date, account_id) DO UPDATE SET remaining = ?",
            (today, account_id, float(remaining), float(remaining)))

    _db_call(run)


def _signed_dates(account_id=None, days=180):
    since = (_now() - timedelta(days=days)).strftime("%Y-%m-%d")

    def run(conn):
        if account_id:
            rows = conn.execute(
                "SELECT DISTINCT date FROM checkin_record "
                "WHERE date >= ? AND account_id = ? ORDER BY date",
                (since, account_id)).fetchall()
        else:
            rows = conn.execute(
                "SELECT DISTINCT date FROM checkin_record "
                "WHERE date >= ? ORDER BY date", (since,)).fetchall()
        return [row["date"] for row in rows]

    return _db_call(run)


def _today_credits(account_id=None):
    today = _now().strftime("%Y-%m-%d")

    def run(conn):
        if account_id:
            row = conn.execute(
                "SELECT COALESCE(SUM(credits), 0) AS total FROM checkin_record "
                "WHERE date = ? AND account_id = ?",
                (today, account_id)).fetchone()
        else:
            row = conn.execute(
                "SELECT COALESCE(SUM(credits), 0) AS total FROM checkin_record "
                "WHERE date = ?", (today,)).fetchone()
        return float(row["total"] or 0)

    return _db_call(run)


def _records(account_id=None, limit=60):
    def run(conn):
        if account_id:
            rows = conn.execute(
                "SELECT date, time, account_name, credits, is_member "
                "FROM checkin_record WHERE account_id = ? "
                "ORDER BY date DESC, time DESC LIMIT ?",
                (account_id, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT date, time, account_name, credits, is_member "
                "FROM checkin_record ORDER BY date DESC, time DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(row) for row in rows]

    return _db_call(run)


def _daily_totals(days=30):
    since = (_now() - timedelta(days=days)).strftime("%Y-%m-%d")

    def run(conn):
        rows = conn.execute(
            "SELECT date, SUM(credits) AS total, COUNT(*) AS count "
            "FROM checkin_record WHERE date >= ? "
            "GROUP BY date ORDER BY date", (since,)).fetchall()
        return [{"date": row["date"],
                 "credits": float(row["total"] or 0),
                 "count": int(row["count"] or 0)} for row in rows]

    return _db_call(run)


def _streak(dates):
    if not dates:
        return 0
    signed = set(dates)
    cursor = date.today()
    if cursor.isoformat() not in signed:
        cursor -= timedelta(days=1)
    count = 0
    while cursor.isoformat() in signed:
        count += 1
        cursor -= timedelta(days=1)
    return count


def _week_progress(dates):
    today = date.today()
    start = today - timedelta(days=(today.weekday() + 1) % 7)
    signed = set(dates)
    return [(start + timedelta(days=i)).isoformat() in signed
            for i in range(7)]


def history_summary(account_id=None):
    dates = _signed_dates(account_id)
    today = _now().strftime("%Y-%m-%d")
    month = today[:7]
    month_dates = [d for d in dates if d.startswith(month)]
    return {
        "today": today,
        "signed_dates": dates,
        "month_signed_days": len(month_dates),
        "streak": _streak(dates),
        "today_credits": _today_credits(account_id),
        "today_checked": today in set(dates),
        "records": _records(account_id, limit=60),
        "daily_totals": _daily_totals(30),
        "week_progress": _week_progress(dates),
    }


# ---------------------------------------------------------------- 用量数据库

def _session_key(item):
    session_id = _text(item.get("session_id"))
    if session_id:
        return session_id
    raw = "%s|%s|%s|%s" % (
        item.get("usage_time"), item.get("model_name"),
        item.get("credits_float"), item.get("user_input_preview"))
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()


def merge_usage(account_id, items, fetched_at=None):
    if not items:
        return 0
    fetched_at = fetched_at or _now_text()

    def run(conn):
        count = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            key = _session_key(item)
            conn.execute(
                "INSERT INTO usage_session "
                "(account_id, session_key, session_id, usage_time, model_name, "
                " mode, credits, cost_money, input_token, output_token, "
                " cache_read_token, cache_write_token, preview, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(account_id, session_key) DO UPDATE SET "
                " usage_time = excluded.usage_time, "
                " model_name = excluded.model_name, mode = excluded.mode, "
                " credits = excluded.credits, cost_money = excluded.cost_money, "
                " input_token = excluded.input_token, "
                " output_token = excluded.output_token, "
                " cache_read_token = excluded.cache_read_token, "
                " cache_write_token = excluded.cache_write_token, "
                " preview = excluded.preview, fetched_at = excluded.fetched_at",
                (account_id, key, _text(item.get("session_id")),
                 _to_int(item.get("usage_time")), _text(item.get("model_name")),
                 _text(item.get("mode")), _to_float(item.get("credits_float")),
                 _to_float(item.get("cost_money_float")),
                 _to_int(item.get("input_token")),
                 _to_int(item.get("output_token")),
                 _to_int(item.get("cache_read_token")),
                 _to_int(item.get("cache_write_token")),
                 _text(item.get("user_input_preview"))[:300], fetched_at))
            count += 1
        return count

    return _db_call(run)


def _usage_rows(account_id, days):
    start = int((_now() - timedelta(days=days)).timestamp() * 1000)

    def run(conn):
        rows = conn.execute(
            "SELECT * FROM usage_session WHERE account_id = ? "
            "AND (usage_time >= ? OR usage_time = 0) "
            "ORDER BY usage_time DESC LIMIT 500",
            (account_id, start)).fetchall()
        return [dict(row) for row in rows]

    return _db_call(run)


def usage_summary(account_id, days=None):
    days = days or DEFAULT_USAGE_DAYS
    rows = _usage_rows(account_id, days)
    total_credits = sum(float(row.get("credits") or 0) for row in rows)
    input_token = sum(int(row.get("input_token") or 0) for row in rows)
    output_token = sum(int(row.get("output_token") or 0) for row in rows)
    cache_read = sum(int(row.get("cache_read_token") or 0) for row in rows)
    cache_write = sum(int(row.get("cache_write_token") or 0) for row in rows)
    base = input_token + cache_read
    cache_rate = (cache_read / base * 100) if base else 0

    models = {}
    for row in rows:
        name = row.get("model_name") or "未知模型"
        item = models.setdefault(name, {
            "model": name, "sessions": 0, "credits": 0.0,
            "input_token": 0, "output_token": 0,
            "cache_read_token": 0,
        })
        item["sessions"] += 1
        item["credits"] += float(row.get("credits") or 0)
        item["input_token"] += int(row.get("input_token") or 0)
        item["output_token"] += int(row.get("output_token") or 0)
        item["cache_read_token"] += int(row.get("cache_read_token") or 0)
    model_list = sorted(models.values(),
                        key=lambda item: (item["credits"],
                                          item["input_token"] + item["output_token"]),
                        reverse=True)
    for item in model_list:
        item["credits"] = round(item["credits"], 2)

    sessions = []
    for row in rows[:100]:
        usage_time = int(row.get("usage_time") or 0)
        # 上游接口同时出现过秒与毫秒；统一给前端毫秒。
        if 0 < usage_time < 10_000_000_000:
            usage_time *= 1000
        sessions.append({
            "session_id": row.get("session_id") or "",
            "usage_time": usage_time,
            "model": row.get("model_name") or "",
            "mode": row.get("mode") or "",
            "credits": round(float(row.get("credits") or 0), 2),
            "input_token": int(row.get("input_token") or 0),
            "output_token": int(row.get("output_token") or 0),
            "cache_read_token": int(row.get("cache_read_token") or 0),
            "cache_write_token": int(row.get("cache_write_token") or 0),
            "preview": row.get("preview") or "",
        })

    return {
        "days": days,
        "sessions_count": len(rows),
        "credits": round(total_credits, 2),
        "input_token": input_token,
        "output_token": output_token,
        "cache_read_token": cache_read,
        "cache_write_token": cache_write,
        "cache_rate": round(cache_rate, 1),
        "models": model_list,
        "sessions": sessions,
        "has_data": bool(rows),
    }


# ---------------------------------------------------------------- Token 处理

def _store_token(account, token, session=None):
    account["token"] = token
    account["account_uid"] = jwt_uid(token) or account.get("account_uid")
    account["token_updated_at"] = _now_text()
    if session is not None:
        account["session"] = session


def _prepare_api(store, account, need_status=True):
    """确保 token 可用；返回 (api, status)。status 仅 need_status 时查询。"""
    api = TraeApi(account.get("token"), account.get("session"),
                  account.get("device_id"))
    status = None
    if account.get("token"):
        if need_status:
            status = api.status()
            if _status_ok(status):
                return api, status
        elif not _token_expired(account["token"]):
            return api, None

    if account.get("session"):
        renewed = api.get_user_token(account["session"])
        if renewed:
            _store_token(account, renewed)
            save_store(store)
            api = TraeApi(renewed, account.get("session"),
                          account.get("device_id"))
            if need_status:
                status = api.status()
                if _status_ok(status):
                    return api, status
            else:
                return api, None
    return api, status


def _remember_status(account, payload):
    if not isinstance(payload, dict):
        return
    account["last_status"] = {
        "code": _to_int(payload.get("code"), -1),
        "checked_in": payload.get("checked_in") is True,
        "credits": _to_float(payload.get("credits")),
        "extra_credits": _to_float(payload.get("extra_credits")),
        "message": _text(payload.get("message")),
        "at": _now_text(),
    }


def _status_error(payload, api):
    if payload is None:
        return api.last_error or "签到接口无响应"
    message = _text(payload.get("message"))
    code = payload.get("code")
    if message:
        return "%s（code=%s）" % (message, code)
    return "签到接口返回 code=%s" % code


# ---------------------------------------------------------------- 账号操作

def _profile_refresh(store, account, force=True):
    api, status = _prepare_api(store, account, need_status=not force)
    if status is not None:
        _remember_status(account, status)
    if not account.get("token"):
        return False, "未登录"
    profile = api.profile()
    if not profile:
        return False, api.last_error or "资料接口无响应"
    account["screen_name"] = profile.get("screen_name") or ""
    account["mobile_masked"] = profile.get("mobile_masked") or ""
    account["avatar_url"] = profile.get("avatar_url") or ""
    if not account.get("account_uid") and profile.get("user_id"):
        account["account_uid"] = profile["user_id"]
    if not account.get("name") and profile.get("screen_name"):
        account["name"] = profile["screen_name"]
    is_student = api.student_status() == 1
    account["is_student"] = is_student
    save_store(store)
    return True, "资料已更新"


def _candidate_priority(item):
    """同一账号出现在多个客户端时，优先 Trae CN，其次带会话的候选。"""
    return (0 if item.get("client_name") == "Trae CN" else 1,
            0 if item.get("session") else 1)


def import_local():
    """扫描本机 Trae 客户端并合并账号。只读客户端文件。"""
    with _LOCK:
        clients = find_client_dirs()
        if not clients:
            return {
                "result": "NO_CLIENT",
                "report": "未找到本机 Trae 客户端目录；请先安装并登录 Trae 桌面端，"
                          "或使用「添加账号」粘贴 Token / 会话 Cookie",
                "clients": [],
                "needs_attention": True,
            }

        candidates = []
        skipped = []
        for client in clients:
            item = _scan_client_dir(client)
            if item.get("ok"):
                candidates.append(item)
            else:
                skipped.append("%s：%s" % (item.get("source"),
                                           item.get("message")))

        if not candidates:
            return {
                "result": "NO_AUTH",
                "report": "已找到客户端目录，但未读取到可用登录态："
                          + "；".join(skipped),
                "clients": clients,
                "needs_attention": True,
            }

        # 同一账号通常会在 Trae CN / TRAE SOLO CN / TRAE 各留一份登录态。
        # 按 account_uid 去重，避免重复导入和重复刷新。
        merged_candidates = {}
        for item in candidates:
            key = item.get("account_uid") or item.get("token")
            current = merged_candidates.get(key)
            if (current is None
                    or _candidate_priority(item) < _candidate_priority(current)):
                merged_candidates[key] = item
        candidates = list(merged_candidates.values())

        store = load_store()
        added = updated = 0
        changed_accounts = []
        for item in candidates:
            token = item.get("token")
            session = item.get("session")
            if not token and session:
                token = TraeApi(session=session).get_user_token(session)
                if token:
                    item["token"] = token
                    item["account_uid"] = jwt_uid(token)
            if not token:
                skipped.append("%s：会话换取 Token 失败" % item.get("source"))
                continue

            uid = item.get("account_uid") or jwt_uid(token)
            match = None
            for account in store["accounts"]:
                if uid and account.get("account_uid") == uid:
                    match = account
                    break
                if account.get("token") == token:
                    match = account
                    break
                if session and account.get("session") == session:
                    match = account
                    break

            if match:
                before = (match.get("token"), match.get("session"),
                          match.get("device_id"), match.get("name"),
                          match.get("screen_name"), match.get("mobile_masked"),
                          match.get("avatar_url"), match.get("region"),
                          match.get("host"))
                _store_token(match, token, session or match.get("session"))
                if (_valid_device_id(item.get("device_id"))
                        and not _device_in_use(store["accounts"],
                                               item["device_id"], match["id"])):
                    match["device_id"] = item["device_id"]
                if item.get("source"):
                    match["source"] = item["source"]
                match["account_uid"] = uid or match.get("account_uid")
                if item.get("name") and not match.get("name"):
                    match["name"] = item["name"]
                for field in ("screen_name", "mobile_masked", "avatar_url"):
                    if item.get(field) and not match.get(field):
                        match[field] = item[field]
                if item.get("region"):
                    match["region"] = item["region"]
                if item.get("host"):
                    match["host"] = item["host"]
                if before != (match.get("token"), match.get("session"),
                              match.get("device_id"), match.get("name"),
                              match.get("screen_name"),
                              match.get("mobile_masked"),
                              match.get("avatar_url"), match.get("region"),
                              match.get("host")):
                    updated += 1
                changed_accounts.append(match)
            else:
                account = {
                    "id": uuid_hex(),
                    "name": item.get("name") or None,
                    "token": token,
                    "session": session,
                    "device_id": item.get("device_id")
                    if (_valid_device_id(item.get("device_id"))
                        and not _device_in_use(store["accounts"],
                                               item["device_id"]))
                    else _new_device_id(store["accounts"]),
                    "account_uid": uid,
                    "token_updated_at": _now_text(),
                    "last_checkin_date": None,
                    "remaining_credits": -1.0,
                    "enabled": True,
                    "is_member": False,
                    "screen_name": item.get("screen_name") or "",
                    "mobile_masked": item.get("mobile_masked") or "",
                    "avatar_url": item.get("avatar_url") or "",
                    "is_student": False,
                    "source": item.get("source") or "",
                    "region": item.get("region") or "CN",
                    "host": item.get("host") or "",
                    "created_at": _now_text(),
                    "last_status": {},
                    "last_usage_at": None,
                }
                store["accounts"].append(account)
                changed_accounts.append(account)
                added += 1
        if not store.get("active_id") and store["accounts"]:
            store["active_id"] = store["accounts"][0]["id"]
        save_store(store)

        # 有会话时才尝试补齐学生认证等扩展资料；只有 Token 的本地账号
        # 展示字段已从 iCubeAuthInfo 解密得到，GetUserInfo 反而会返回 401。
        for account in changed_accounts:
            if not account.get("session"):
                continue
            try:
                _profile_refresh(store, account, force=False)
            except Exception:
                pass

        result = "OK" if (added or updated) else "NO_CHANGE"
        report = "已导入本机账号：新增 %d 个，更新 %d 个" % (added, updated)
        if skipped:
            report += "；跳过：" + "；".join(skipped)
        return {
            "result": result,
            "report": report,
            "added": added,
            "updated": updated,
            "clients": clients,
            "accounts": [public_account(a, store) for a in store["accounts"]],
            "active_id": store.get("active_id"),
        }


def add_account(raw, name=None):
    """从 Token 或会话 Cookie 添加一个账号。"""
    text = normalize_credential(raw)
    kind = classify_credential(text)
    if kind == "UNKNOWN":
        return {
            "ok": False,
            "result": "ERROR",
            "error": "无法识别的凭证。请粘贴 Cloud-IDE-JWT Token，"
                     "或 X-Cloudide-Session 会话 Cookie",
        }

    with _LOCK:
        token = text if kind == "TOKEN" else None
        session = text if kind == "SESSION" else None
        if kind == "SESSION":
            token = TraeApi(session=session).get_user_token(session)
            if not token:
                return {
                    "ok": False,
                    "result": "AUTH_ERROR",
                    "error": "会话 Cookie 换取 Token 失败（可能已过期），"
                             "请重新登录 Trae 后再复制",
                }
        if len(token or "") < MIN_TOKEN_LENGTH:
            return {"ok": False, "result": "ERROR",
                    "error": "Token 长度不足，请确认复制完整"}

        uid = jwt_uid(token)
        store = load_store()
        match = None
        for account in store["accounts"]:
            if uid and account.get("account_uid") == uid:
                match = account
                break
            if account.get("token") == token:
                match = account
                break
            if session and account.get("session") == session:
                match = account
                break
        if match:
            _store_token(match, token, session)
            match["account_uid"] = uid or match.get("account_uid")
            if name:
                match["name"] = name.strip()
            action = "updated"
        else:
            match = {
                "id": uuid_hex(),
                "name": (name or "").strip() or None,
                "token": token,
                "session": session,
                "device_id": _new_device_id(store["accounts"]),
                "account_uid": uid,
                "token_updated_at": _now_text(),
                "last_checkin_date": None,
                "remaining_credits": -1.0,
                "enabled": True,
                "is_member": False,
                "screen_name": "",
                "mobile_masked": "",
                "avatar_url": "",
                "is_student": False,
                "source": "manual",
                "region": "CN",
                "host": "",
                "created_at": _now_text(),
                "last_status": {},
                "last_usage_at": None,
            }
            store["accounts"].append(match)
            action = "added"
        if not store.get("active_id"):
            store["active_id"] = match["id"]
        save_store(store)
        _sensitive.add(token)
        if session:
            _sensitive.add(session)

        # 拉取昵称 / 学生认证；失败时账号仍然可用
        try:
            _profile_refresh(store, match, force=False)
        except Exception:
            pass
        return {
            "ok": True,
            "result": "OK",
            "report": "账号已%s" % ("更新" if action == "updated" else "添加"),
            "action": action,
            "account": public_account(match, store),
            "accounts": [public_account(a, store) for a in store["accounts"]],
            "active_id": store.get("active_id"),
        }


def remove_account(account_id):
    with _LOCK:
        store = load_store()
        before = len(store["accounts"])
        store["accounts"] = [a for a in store["accounts"]
                             if a["id"] != account_id]
        if len(store["accounts"]) == before:
            return {"ok": False, "error": "账号不存在"}
        if store.get("active_id") == account_id:
            store["active_id"] = (store["accounts"][0]["id"]
                                  if store["accounts"] else None)
        save_store(store)
        return {"ok": True, "report": "账号已删除",
                "accounts": [public_account(a, store)
                             for a in store["accounts"]],
                "active_id": store.get("active_id")}


def update_account(account_id, **fields):
    allowed = {"name", "enabled", "is_member"}
    with _LOCK:
        store = load_store()
        account = find_account(store, account_id)
        if account is None:
            return {"ok": False, "error": "账号不存在"}
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key == "name":
                account["name"] = (str(value).strip() or None) if value else None
            elif key == "enabled":
                account["enabled"] = bool(value)
            elif key == "is_member":
                account["is_member"] = bool(value)
        save_store(store)
        return {"ok": True, "report": "账号设置已保存",
                "account": public_account(account, store),
                "accounts": [public_account(a, store) for a in store["accounts"]],
                "active_id": store.get("active_id")}


def set_active(account_id):
    with _LOCK:
        store = load_store()
        if find_account(store, account_id) is None:
            return {"ok": False, "error": "账号不存在"}
        store["active_id"] = account_id
        save_store(store)
        return {"ok": True, "report": "已切换主号",
                "accounts": [public_account(a, store) for a in store["accounts"]],
                "active_id": account_id}


def renew_device(account_id):
    with _LOCK:
        store = load_store()
        account = find_account(store, account_id)
        if account is None:
            return {"ok": False, "error": "账号不存在"}
        old = account.get("device_id")
        account["device_id"] = _new_device_id(store["accounts"],
                                              except_id=account_id)
        while account["device_id"] == old:
            account["device_id"] = _new_device_id(store["accounts"],
                                                  except_id=account_id)
        save_store(store)
        return {"ok": True, "report": "设备号已更换",
                "account": public_account(account, store),
                "accounts": [public_account(a, store) for a in store["accounts"]],
                "active_id": store.get("active_id")}


def update_settings(payload):
    if not isinstance(payload, dict):
        return {"ok": False, "error": "设置内容无效"}
    with _LOCK:
        store = load_store()
        settings = store["settings"]
        if "feishu_webhook" in payload:
            settings["feishu_webhook"] = str(payload.get("feishu_webhook") or "").strip()
        if "interval_seconds" in payload:
            try:
                settings["interval_seconds"] = max(
                    0, min(120, int(payload.get("interval_seconds"))))
            except (TypeError, ValueError):
                return {"ok": False, "error": "签到间隔必须是数字"}
        if "auto_enabled" in payload:
            settings["auto_enabled"] = bool(payload.get("auto_enabled"))
        if "auto_time" in payload:
            value = str(payload.get("auto_time") or "").strip()
            try:
                parsed = datetime.strptime(value, "%H:%M")
            except ValueError:
                return {"ok": False, "error": "自动签到时间格式应为 HH:MM"}
            settings["auto_time"] = parsed.strftime("%H:%M")
        if "usage_days" in payload:
            try:
                settings["usage_days"] = max(
                    1, min(90, int(payload.get("usage_days"))))
            except (TypeError, ValueError):
                return {"ok": False, "error": "用量天数必须是数字"}
        save_store(store)
        return {"ok": True, "report": "设置已保存",
                "settings": public_settings(store)}


def refresh_account(account_id=None):
    """刷新一个或全部账号的状态与资料。"""
    with _LOCK:
        store = load_store()
        if account_id:
            accounts = [a for a in store["accounts"] if a["id"] == account_id]
        else:
            accounts = [a for a in store["accounts"] if a.get("enabled")]
        if not accounts:
            return {"result": "NO_ACCOUNT", "report": "没有可刷新的账号",
                    "accounts": [], "needs_attention": True}

        rows = []
        remaining_total = 0.0
        ok_count = 0
        checked = 0
        for index, account in enumerate(accounts):
            if index:
                time.sleep(0.4)
            try:
                api, status = _prepare_api(store, account)
                if not _status_ok(status):
                    message = _status_error(status, api)
                    _remember_status(account, status)
                    rows.append({
                        "id": account["id"],
                        "name": _display_name(account),
                        "ok": False,
                        "result": "AUTH_ERROR",
                        "message": message,
                        "checked_in": False,
                        "credits": 0,
                        "remaining": None,
                    })
                    continue
                _remember_status(account, status)
                checked_in = _status_checked(status)
                gained = _gained(status, account.get("is_member"))
                if checked_in:
                    checked += 1
                    now = _now()
                    account["last_checkin_date"] = now.strftime("%Y-%m-%d")
                    # 服务端已签到但本地没有记录时（例如换机器/清过数据），
                    # 顺手补一条历史；同一天重复刷新只会取较大积分，不会重复计数。
                    record_checkin(account, now, gained, account.get("is_member"))
                remaining = api.remaining_credits()
                if remaining >= 0:
                    account["remaining_credits"] = remaining
                    remaining_total += remaining
                    record_snapshot(account["id"], remaining)
                rows.append({
                    "id": account["id"],
                    "name": _display_name(account),
                    "ok": True,
                    "result": "ALREADY" if checked_in else "OK",
                    "message": _text(status.get("message")) or "已连接",
                    "checked_in": checked_in,
                    "credits": gained,
                    "remaining": remaining if remaining >= 0 else None,
                })
                ok_count += 1
            except Exception as exc:
                rows.append({
                    "id": account["id"],
                    "name": _display_name(account),
                    "ok": False,
                    "result": "ERROR",
                    "message": "%s: %s" % (type(exc).__name__, exc),
                    "checked_in": False,
                    "credits": 0,
                    "remaining": None,
                })
        save_store(store)
        result = "OK" if ok_count == len(rows) else "PARTIAL"
        report = "已刷新 %d/%d 个账号" % (ok_count, len(rows))
        if checked:
            report += "，今日已签到 %d 个" % checked
        return {
            "result": result,
            "report": report,
            "accounts": rows,
            "checked": checked,
            "ok_count": ok_count,
            "remaining_total": round(remaining_total, 2),
            "at": _now_text(),
        }


# ---------------------------------------------------------------- 签到

def _checkin_one(store, account, index, count, quiet=False):
    name = _display_name(account)
    if index:
        interval = int(store["settings"].get("interval_seconds") or 5)
        if interval > 0:
            time.sleep(interval + random.uniform(-0.5, 0.5) if interval > 1
                       else interval)
    if not account.get("token") and not account.get("session"):
        return {"id": account["id"], "name": name, "ok": False,
                "result": "NO_AUTH", "gained": 0, "message": "未登录"}

    today = _now().strftime("%Y-%m-%d")
    if account.get("last_checkin_date") == today:
        return {"id": account["id"], "name": name, "ok": True,
                "result": "ALREADY", "gained": 0, "message": "本地记录今日已签到"}

    api, status = _prepare_api(store, account)
    if not _status_ok(status):
        _remember_status(account, status)
        message = _status_error(status, api)
        if not account.get("session"):
            # 令牌失效又没有会话 Cookie：明确提示需要重新导入，
            # 不要让人误以为是网络抖动。
            message += "；没有会话 Cookie 可续期，请重新导入本机登录态"
        return {"id": account["id"], "name": name, "ok": False,
                "result": "AUTH_ERROR", "gained": 0, "message": message}

    _remember_status(account, status)
    if _status_checked(status):
        gained = _gained(status, account.get("is_member"))
        account["last_checkin_date"] = today
        record_checkin(account, _now(), gained, account.get("is_member"))
        remaining = api.remaining_credits()
        if remaining >= 0:
            account["remaining_credits"] = remaining
            record_snapshot(account["id"], remaining)
        save_store(store)
        return {"id": account["id"], "name": name, "ok": True,
                "result": "ALREADY", "gained": gained,
                "remaining": remaining if remaining >= 0 else None,
                "message": "服务器显示今日已签到"}

    claim = api.claim()
    if not _status_ok(claim):
        return {"id": account["id"], "name": name, "ok": False,
                "result": "ERROR", "gained": 0,
                "message": _status_error(claim, api)}

    after = api.status()
    gained = _gained(after or claim, account.get("is_member"))
    _remember_status(account, after or claim)
    account["last_checkin_date"] = today
    record_checkin(account, _now(), gained, account.get("is_member"))
    remaining = api.remaining_credits()
    if remaining >= 0:
        account["remaining_credits"] = remaining
        record_snapshot(account["id"], remaining)
    save_store(store)
    return {"id": account["id"], "name": name, "ok": True,
            "result": "CLAIMED", "gained": gained,
            "remaining": remaining if remaining >= 0 else None,
            "message": "签到成功"}


def run_auto(account_id=None):
    global _started_at
    with _LOCK:
        _started_at = time.monotonic()
        store = load_store()
        if account_id:
            accounts = [a for a in store["accounts"]
                        if a["id"] == account_id and a.get("enabled")]
        else:
            accounts = [a for a in store["accounts"] if a.get("enabled")]
        if not accounts:
            return {"result": "NO_ACCOUNT",
                    "report": "没有启用的 Trae 账号，请先导入或添加账号",
                    "accounts": [], "needs_attention": True}

        rows = []
        total_gained = 0.0
        checked = failed = already = 0
        remaining_total = 0.0
        for index, account in enumerate(accounts):
            try:
                row = _checkin_one(store, account, index, len(accounts))
                rows.append(row)
                total_gained += float(row.get("gained") or 0)
                if row.get("ok"):
                    checked += 1
                    if row.get("result") == "ALREADY":
                        already += 1
                else:
                    failed += 1
                if row.get("remaining") is not None:
                    remaining_total += float(row.get("remaining") or 0)
            except Exception as exc:
                failed += 1
                rows.append({
                    "id": account["id"],
                    "name": _display_name(account),
                    "ok": False,
                    "result": "ERROR",
                    "gained": 0,
                    "message": "%s: %s" % (type(exc).__name__, exc),
                })
        save_store(store)

        if failed and checked:
            result = "PARTIAL"
        elif failed:
            result = "ERROR"
        elif already == len(rows):
            result = "ALREADY"
        else:
            result = "OK"

        parts = ["成功 %d/%d 个账号" % (checked, len(rows))]
        if total_gained:
            parts.append("获得 %s 积分" % _format_number(total_gained))
        if already:
            parts.append("%d 个今日已签" % already)
        if failed:
            parts.append("%d 个失败" % failed)
        report = "，".join(parts)

        payload = {
            "result": result,
            "report": report,
            "accounts": rows,
            "total_gained": round(total_gained, 2),
            "checked": checked,
            "already": already,
            "failed": failed,
            "remaining_total": round(remaining_total, 2),
            "at": _now_text(),
            "needs_attention": result in FAILURE_RESULTS,
        }
        _notify(store, payload)
        return payload


def _format_number(value):
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if abs(number - round(number)) < 0.01:
        return str(int(round(number)))
    return ("%.2f" % number).rstrip("0").rstrip(".")


# ---------------------------------------------------------------- 用量

def _parse_usage_item(item):
    if not isinstance(item, dict):
        return None
    extra = item.get("extra_info")
    if not isinstance(extra, dict):
        extra = {}
    return {
        "session_id": _text(item.get("session_id")),
        "usage_time": _to_int(item.get("usage_time")),
        "model_name": _text(item.get("model_name")),
        "mode": _text(item.get("mode")),
        "credits_float": _to_float(
            item.get("credits_float", item.get("amount_float"))),
        "cost_money_float": _to_float(item.get("cost_money_float")),
        "user_input_preview": _text(item.get("user_input_preview")),
        "input_token": _to_int(extra.get("input_token")),
        "output_token": _to_int(extra.get("output_token")),
        "cache_read_token": _to_int(extra.get("cache_read_token")),
        "cache_write_token": _to_int(extra.get("cache_write_token")),
    }


def run_usage(account_id=None, days=None):
    global _started_at
    with _LOCK:
        _started_at = time.monotonic()
        store = load_store()
        account = (find_account(store, account_id) if account_id
                   else active_account(store))
        if account is None:
            return {"result": "NO_ACCOUNT",
                    "report": "还没有 Trae 账号，请先导入或添加账号",
                    "needs_attention": True}
        days = days or int(store["settings"].get("usage_days") or 7)
        days = max(1, min(90, int(days)))
        api, _status = _prepare_api(store, account, need_status=False)
        if not account.get("token"):
            return {"result": "NO_AUTH",
                    "report": "账号没有可用 Token，请重新导入登录态",
                    "needs_attention": True}

        end = _now()
        start = end - timedelta(days=days)
        items = []
        total = 0
        last_error = ""
        for page in range(1, MAX_USAGE_PAGES + 1):
            body = api.usage_page(start.timestamp(), end.timestamp(), page)
            if body is None:
                last_error = api.last_error or "用量接口无响应"
                break
            rows = body.get("user_usage_group_by_sessions")
            if not isinstance(rows, list):
                last_error = "响应缺少 user_usage_group_by_sessions"
                break
            total = _to_int(body.get("total"), total)
            parsed = [x for x in (_parse_usage_item(row) for row in rows) if x]
            items.extend(parsed)
            if len(rows) < USAGE_PAGE_SIZE or (total and len(items) >= total):
                break
            if _remaining() <= REQUEST_TIMEOUT:
                break

        if not items and last_error:
            # Token 可能只是本地没过期、服务端已失效：用会话换新后再试一次
            if account.get("session"):
                renewed = api.get_user_token(account["session"])
                if renewed:
                    _store_token(account, renewed)
                    save_store(store)
                    api = TraeApi(renewed, account.get("session"),
                                  account.get("device_id"))
                    body = api.usage_page(start.timestamp(), end.timestamp(), 1)
                    if body is not None:
                        rows = body.get("user_usage_group_by_sessions")
                        if isinstance(rows, list):
                            items = [x for x in (_parse_usage_item(row)
                                                 for row in rows) if x]
                            last_error = ""
        if not items and last_error:
            return {"result": "ERROR",
                    "report": "用量查询失败：%s" % last_error,
                    "needs_attention": True}

        merge_usage(account["id"], items)
        expired = api.expired_ents()
        account["last_usage_at"] = _now_text()
        save_store(store)
        summary = usage_summary(account["id"], days)
        summary["expiring"] = _expiring_items(expired)
        summary["account"] = {
            "id": account["id"],
            "name": _display_name(account),
        }
        summary["fetched"] = len(items)
        summary["at"] = _now_text()
        summary["result"] = "OK"
        summary["report"] = "已同步 %d 个会话（近 %d 天）" % (len(items), days)
        return summary


def _expiring_items(expired):
    now_ms = int(time.time() * 1000)
    limit_ms = now_ms + EXPIRE_WARN_DAYS * 24 * 3600 * 1000
    result = []
    for item in expired or []:
        expire = _to_int(item.get("expire_time_ms"))
        if expire and expire <= limit_ms:
            result.append({
                "name": item.get("name") or "积分包",
                "credits": round(_to_float(item.get("credits_limit")), 2),
                "expire_time": expire,
                "expired": expire <= now_ms,
            })
    result.sort(key=lambda item: item["expire_time"])
    return result


# ---------------------------------------------------------------- 通知

def _notify(store, payload):
    webhook = (store.get("settings") or {}).get("feishu_webhook")
    if not webhook:
        return False
    lines = ["Trae 每日签到", payload.get("report") or ""]
    for row in payload.get("accounts") or []:
        prefix = "✔" if row.get("ok") else "✖"
        extra = ""
        if row.get("gained"):
            extra = " +%s" % _format_number(row.get("gained"))
        lines.append("%s %s%s %s" % (prefix, row.get("name") or "账号",
                                     extra, row.get("message") or ""))
    return send_feishu(webhook, "\n".join(lines))


def send_feishu(webhook, text):
    if not webhook:
        return False
    body = json.dumps({"msg_type": "text",
                       "content": {"text": text}}, ensure_ascii=False)
    request = urllib.request.Request(
        webhook, data=body.encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(request, timeout=15) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.HTTPError, urllib.error.URLError, OSError):
        return False


# ---------------------------------------------------------------- 诊断 / 汇总

def doctor_payload(store=None):
    store = store or load_store()
    clients = find_client_dirs()
    accounts = []
    for account in store.get("accounts") or []:
        token = account.get("token") or ""
        expiry = jwt_expiry(token)
        accounts.append({
            "id": account["id"],
            "name": _display_name(account),
            "source": account.get("source") or "",
            "account_uid": account.get("account_uid") or "",
            "has_token": bool(token),
            "has_session": bool(account.get("session")),
            "token_masked": _mask(token),
            "token_expiry": (expiry.astimezone().strftime("%Y-%m-%d %H:%M")
                             if expiry else ""),
            "token_expired": bool(expiry and
                                  expiry <= datetime.now(timezone.utc)),
            "device_id_masked": _mask_digits(account.get("device_id")),
            "enabled": bool(account.get("enabled")),
        })
    result = "OK" if accounts else "NO_ACCOUNT"
    report = ("已配置 %d 个 Trae 账号" % len(accounts) if accounts
              else "尚未配置 Trae 账号")
    if clients:
        report += "；检测到客户端：" + "、".join(c["name"] for c in clients)
    else:
        report += "；本机未检测到 Trae 客户端目录"
    return {
        "result": result,
        "report": report,
        "needs_attention": not accounts,
        "version": VERSION,
        "accounts": accounts,
        "clients": clients,
        "store_path": store_path(),
        "db_path": db_path(),
        "log_path": log_path(),
        "data_dir": data_dir(),
        "settings": public_settings(store),
    }


def monitor(deep=False):
    with _LOCK:
        store = load_store()
        account = active_account(store)
        active_id = account["id"] if account else None
        if deep:
            refresh = refresh_account(None)
        else:
            refresh = None
        # refresh_account 内部重新 load_store，这里重新读一次以拿到最新字段
        store = load_store()
        account = active_account(store)
        active_id = account["id"] if account else store.get("active_id")
        history = history_summary(active_id)
        usage = usage_summary(active_id,
                              int(store["settings"].get("usage_days") or 7)) \
            if active_id else None
        return {
            "ok": True,
            "version": VERSION,
            "at": _now_text(),
            "accounts": [public_account(a, store) for a in store["accounts"]],
            "active_id": active_id,
            "settings": public_settings(store),
            "history": history,
            "usage": usage,
            "clients": find_client_dirs(),
            "doctor": doctor_payload(store),
            "refresh": refresh,
            "store_path": store_path(),
            "db_path": db_path(),
            "log_path": log_path(),
            "data_dir": data_dir(),
        }


# ---------------------------------------------------------------- 日志

def _rotate_log(path):
    try:
        if os.path.getsize(path) > 4 * 1024 * 1024:
            backup = path + ".1"
            try:
                os.replace(path, backup)
            except OSError:
                pass
    except OSError:
        pass


def append_log(payload, action="console"):
    if not isinstance(payload, dict):
        return
    text = json.dumps(payload, ensure_ascii=False, default=str)
    for secret in _sensitive:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    line = "[%s] %s\n" % (_now_text(), text)
    path = log_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _rotate_log(path)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        pass


def get_log(limit=200):
    path = log_path()
    try:
        stat = os.stat(path)
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines = [line.rstrip("\r\n") for line in handle]
    except OSError:
        return {"path": path, "at": None, "lines": []}
    return {
        "path": path,
        "at": time.strftime("%Y-%m-%d %H:%M:%S",
                            time.localtime(stat.st_mtime)),
        "lines": [line for line in lines if line.strip()][-limit:],
    }


def clear_log():
    try:
        open(log_path(), "w", encoding="utf-8").close()
        return True
    except OSError:
        return False


def emit(out, action, quiet=False):
    payload = json.dumps(out, ensure_ascii=False, default=str)
    for secret in _sensitive:
        if secret:
            payload = payload.replace(secret, "[REDACTED]")
    silent = str(action).startswith("silent")
    if not silent and sys.stdout is not None:
        try:
            print(payload)
            return
        except Exception:
            pass
    if quiet and not _env_flag("TRAE_SIGNIN_LOG_EMPTY"):
        return
    try:
        path = log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _rotate_log(path)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("[%s] %s\n" % (_now_text(), payload))
    except OSError:
        pass


# ---------------------------------------------------------------- CLI

def run(action, account_id=None, days=None):
    action = (action or "auto").lower()
    if action == "doctor":
        payload = doctor_payload()
    elif action == "status":
        payload = refresh_account(account_id)
    elif action in ("auto", "silent"):
        payload = run_auto(account_id)
    elif action == "usage":
        payload = run_usage(account_id, days)
    elif action in ("import-local", "import"):
        payload = import_local()
    elif action == "refresh":
        payload = refresh_account(account_id)
    else:
        payload = {"result": "ERROR",
                   "report": "未知命令：%s（可用：status / auto / silent / "
                             "doctor / usage / import-local）" % action,
                   "needs_attention": True}
    quiet = action == "silent" and payload.get("result") in SUCCESS_RESULTS
    emit(payload, action, quiet=quiet)
    if payload.get("result") in ("OK", "ALREADY", "CLAIMED", "NO_CHANGE"):
        return 0
    if payload.get("result") in ("NO_ACCOUNT", "NO_AUTH", "NO_CLIENT"):
        return 2
    return 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    action = argv[0] if argv else "auto"
    days = None
    if "--days" in argv:
        try:
            days = int(argv[argv.index("--days") + 1])
        except (IndexError, ValueError):
            days = None
    try:
        return run(action, days=days)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        payload = {"result": "ERROR",
                   "report": "脚本运行异常（%s）" % type(exc).__name__,
                   "needs_attention": True}
        emit(payload, action)
        return 2


if __name__ == "__main__":
    sys.exit(main())
