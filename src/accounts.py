"""本地账号体系：注册 / 登录 / 会话。

设计取舍：
- 不引入数据库依赖，账号存 data/accounts.json（600 权限，已 gitignore）
- 密码用 pbkdf2-hmac-sha256 + 每用户随机盐，12 万次迭代
- 会话是签名 cookie（HMAC-SHA256），无状态，服务重启不掉线
- 一个账号 = 一个租户（一个直播间），注册时自动建租户

后续要接短信验证码 / 微信登录时，只需要替换 authenticate 与注册入口。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
ACCOUNTS = DATA / "accounts.json"
SECRET_FILE = DATA / "session_secret"

SESSION_TTL = 30 * 86400  # 30 天
COOKIE = "lc_sid"

_LOCK = threading.RLock()
_PBKDF2_ROUNDS = 120_000
_USER_RE = re.compile(r"^[a-z0-9_-]{3,24}$")


# ---------------- 基础 ----------------
def _secret() -> bytes:
    DATA.mkdir(parents=True, exist_ok=True)
    if not SECRET_FILE.exists():
        SECRET_FILE.write_text(secrets.token_urlsafe(48), encoding="utf-8")
        SECRET_FILE.chmod(0o600)
    return SECRET_FILE.read_bytes()


def _load() -> dict:
    if not ACCOUNTS.exists():
        return {"users": {}}
    try:
        return json.loads(ACCOUNTS.read_text(encoding="utf-8")) or {"users": {}}
    except (json.JSONDecodeError, OSError):
        return {"users": {}}


def _save(data: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = ACCOUNTS.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(ACCOUNTS)


def hash_password(pw: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), _PBKDF2_ROUNDS)
    return f"pbkdf2${salt}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        algo, salt, digest = stored.split("$", 2)
    except ValueError:
        return False
    if algo != "pbkdf2":
        return False
    return hmac.compare_digest(hash_password(pw, salt), stored)


# ---------------- 校验 ----------------
def normalize_username(raw: str) -> str:
    return (raw or "").strip().lower()


def check_username(raw: str) -> str | None:
    u = normalize_username(raw)
    if not _USER_RE.match(u):
        return "用户名 3~24 位，只能是字母、数字、下划线或短横线"
    return None


def check_password(pw: str) -> str | None:
    if len(pw or "") < 6:
        return "密码至少 6 位"
    if len(pw) > 128:
        return "密码太长"
    return None


# ---------------- 账号 ----------------
def get(username: str) -> dict | None:
    with _LOCK:
        return _load()["users"].get(normalize_username(username))


def create(username: str, password: str, tenant_id: str, studio_name: str = "") -> dict:
    """创建账号。tenant_id 与账号绑定（一个账号一个直播间）。"""
    u = normalize_username(username)
    with _LOCK:
        data = _load()
        if u in data["users"]:
            raise ValueError("这个用户名已经被注册了")
        rec = {
            "username": u,
            "password": hash_password(password),
            "tenant_id": tenant_id,
            "studio_name": studio_name,
            "created": int(time.time()),
        }
        data["users"][u] = rec
        _save(data)
        return rec


def authenticate(username: str, password: str) -> dict | None:
    rec = get(username)
    if not rec:
        return None
    if not verify_password(password or "", rec.get("password", "")):
        return None
    return rec


def set_password(username: str, password: str) -> bool:
    u = normalize_username(username)
    with _LOCK:
        data = _load()
        if u not in data["users"]:
            return False
        data["users"][u]["password"] = hash_password(password)
        _save(data)
        return True


def update(username: str, **fields) -> None:
    u = normalize_username(username)
    with _LOCK:
        data = _load()
        if u in data["users"]:
            data["users"][u].update(fields)
            _save(data)


# ---------------- 会话 ----------------
def make_session(username: str) -> str:
    u = normalize_username(username)
    exp = int(time.time()) + SESSION_TTL
    payload = f"{u}|{exp}"
    sig = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()[:40]
    return f"{payload}|{sig}"


def parse_session(value: str | None) -> str | None:
    """校验签名与过期时间，返回用户名。"""
    if not value:
        return None
    parts = value.split("|")
    if len(parts) != 3:
        return None
    u, exp, sig = parts
    payload = f"{u}|{exp}"
    want = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()[:40]
    if not hmac.compare_digest(sig, want):
        return None
    try:
        if int(exp) < time.time():
            return None
    except ValueError:
        return None
    return u