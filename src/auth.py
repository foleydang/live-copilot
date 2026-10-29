"""租户鉴权。

当前服务在公网 IP 上，无鉴权时任何人都能：看提词内容、灌假弹幕、触发 AI 声明。
接第二个客户之前必须先堵上。

模型：一个 token 对应一个租户（直播间）。token 通过 URL 传给 H5，
H5 存 localStorage，之后 WS 与 API 都带上。
"""
from __future__ import annotations

import hmac
import secrets
from pathlib import Path

from .config import ROOT

import yaml


def load_tenants(cfg: dict, tenants_file: "str | Path | None" = None) -> dict[str, dict]:
    """返回 {token: {"id":..., "name":..., "room":..., "platform":...}}。

    配置来源（优先级从高到低）：
    1. cfg["auth"]["tenants"] 列表
    2. tenants.yaml 文件（便于加客户时不改主配置）

    tenants_file 参数供测试注入，避免依赖真实文件系统状态。
    """
    tenants: dict[str, dict] = {}
    entries = (cfg.get("auth") or {}).get("tenants") or []
    if not entries:
        f = Path(tenants_file) if tenants_file is not None else ROOT / "tenants.yaml"
        if f.exists():
            entries = (yaml.safe_load(f.read_text(encoding="utf-8")) or {}).get("tenants", [])
    for e in entries:
        tok = str(e.get("token") or "").strip()
        if not tok:
            continue
        meta = {
            "id": str(e.get("id") or e.get("name") or "default"),
            "name": str(e.get("name") or e.get("id") or "直播间"),
            "room": str(e.get("room") or ""),
            # platform 必须透传：视频号店靠它触发"AI 不出声"的合规默认值。
            # 早先这里漏了 platform，导致视频号店静默退回全局 douyin 配置。
            "platform": str(e.get("platform") or ""),
            "voice": str(e.get("voice") or ""),
        }
        if "assist_voice" in e:  # 仅显式配置时才覆盖平台默认
            meta["assist_voice"] = bool(e["assist_voice"])
        tenants[tok] = meta
    return tenants


def auth_enabled(tenants: dict[str, dict]) -> bool:
    """始终启用登录系统（accounts 模块存在即多租户模式）。"""
    return True


def verify(tenants: dict[str, dict], token: str | None) -> dict | None:
    """常量时间比较，避免 token 被时序侧信道猜出。"""
    if not token:
        return None
    for tok, meta in tenants.items():
        if hmac.compare_digest(tok, str(token)):
            return meta
    return None


def new_token() -> str:
    return secrets.token_urlsafe(24)


def load_admin_token(cfg: dict) -> str:
    """管理令牌：用于远程加客户，让运营者不必 SSH 上服务器。

    来源优先级：环境变量 LIVE_COPILOT_ADMIN_TOKEN > admin.yaml > cfg["auth"]["admin_token"]
    为空则禁用管理 API（更安全，只能 SSH 改文件）。
    """
    import os

    tok = (os.environ.get("LIVE_COPILOT_ADMIN_TOKEN")
           or os.environ.get("AI_ZHUBO_ADMIN_TOKEN", "")).strip()
    if tok:
        return tok
    f = ROOT / "admin.yaml"
    if f.exists():
        try:
            tok = str((yaml.safe_load(f.read_text(encoding="utf-8")) or {}).get("admin_token") or "").strip()
            if tok:
                return tok
        except yaml.YAMLError:
            pass
    return str((cfg.get("auth") or {}).get("admin_token") or "").strip()


def verify_admin(admin_token: str, given: str | None) -> bool:
    if not admin_token or not given:
        return False
    return hmac.compare_digest(admin_token, str(given))


def extract_token(headers, query_params) -> str | None:
    """依次从 X-Token 头、Authorization Bearer、query 参数 t/token 取。"""
    for h in ("x-token", "x-auth-token"):
        v = headers.get(h)
        if v:
            return str(v).strip()
    auth = headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip()
    for q in ("t", "token"):
        v = query_params.get(q)
        if v:
            return str(v).strip()
    return None
