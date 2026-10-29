"""云端服务：FastAPI + WebSocket + 多租户。

一个进程服务 N 个直播间，每个租户独立的 Pipeline / 知识库 / 提词队列 / 留痕。
除 GET / 与 /api/health 外全部需要 token。

  GET  /                 H5 助播端（静态壳，数据在鉴权后的 WS 里）
  WS   /ws?t=<token>     实时通道：提词 + 助播语音 + 直播间状态
  GET  /api/stats        本场统计
  GET  /api/pending      待主播跟进清单
  POST /api/danmaku      灌入一条弹幕（入队返回，不阻塞事件循环）
  POST /api/announce     触发 AI 声明口播
  GET  /api/kb           读取本店知识库（手机设置页用）
  POST /api/kb           保存知识库 → 入队热更新 + 预合成语音
  POST /api/mute         一键静音（店主自己按）
  GET  /api/usage        本店本月累计用量
  GET  /api/health       公开健康探针（含真实连接状态）

管理 API（admin token，仅走 Header，禁止 URL 传参）：
  GET  /api/admin/tenants
  POST /api/admin/tenants
  DELETE /api/admin/tenants/{tenant_id}
  POST /api/admin/rotate/{tenant_id}
  POST /api/admin/mute/{tenant_id}
"""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from src import accounts  # noqa: E402
from src.auth import (
    auth_enabled,
    extract_token,
    load_admin_token,
    load_tenants,
    new_token,
    verify,
)  # noqa: E402
from src.config import load_config  # noqa: E402
from src.danmaku.base import Danmaku  # noqa: E402
from src.kb_store import KBStore, compile_faqs  # noqa: E402
from src.ratelimit import RateLimiter  # noqa: E402
from src.rooms import RoomRegistry  # noqa: E402

ROOT = Path(__file__).resolve().parent
cfg = load_config(os.environ.get("LIVE_COPILOT_CONFIG"))
TENANTS = load_tenants(cfg)
AUTH_ON = auth_enabled(TENANTS)
ADMIN_TOKEN = load_admin_token(cfg)  # 为空则管理 API 全部禁用
kb_store = KBStore()
registry = RoomRegistry(cfg, TENANTS, kb_store)

# 鉴权失败限流：扫描器 20 次/分钟就封 2 分钟，避免日志被刷爆
AUTH_FAILS = RateLimiter(max_hits=20, window_sec=60.0, block_sec=120.0)

# nginx 反代注入的免 token 通道（配合 nginx basic auth 使用）。
# 用途：店主自己测试时地址栏不用带 43 位 token；公网客户仍然用 ?t=<token>。
PROXY_TOKEN = (os.environ.get("LIVE_COPILOT_PROXY_TOKEN") or "").strip()
PROXY_TENANT = (os.environ.get("LIVE_COPILOT_PROXY_TENANT") or "").strip()

# ---- 配置与启动 ----
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8010"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    registry.bind_loop(asyncio.get_running_loop())
    print(
        f"[auth] 租户鉴权 "
        f"{'已启用（%d 个租户）' % len(TENANTS) if AUTH_ON else '未启用——公网部署请务必配置 tenants.yaml！'}"
    )
    print(f"[admin] 管理 API {'已启用' if ADMIN_TOKEN else '未启用（未配置 admin token）'}")
    for tid, r in registry.rooms.items():
        print(
            f"[rooms] {tid} | {r.name} | 平台={r.platform} | AI出声={r.pipe.assist_voice} "
            f"| 大脑={r.brain_kind} | 房间号={r.room_id or '未填'}"
        )
    if cfg.get("source") == "ws" and cfg.get("ws_url"):
        started = registry.start_all_pumps()
        print(f"[rooms] 采集启动情况：{started}")
    # 维护线程：每 6 小时清理一次过期留痕和 TTS 缓存，避免小磁盘写满
    t = threading.Thread(target=_maintenance_loop, daemon=True, name="maintenance")
    t.start()
    yield
    registry.stop_all()
    print("[server] 已关闭所有房间与采集连接")


app = FastAPI(title="AI 助播", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


# ---- 中间件：无缓存 + IP 黑名单 + 登录门禁 ----
PUBLIC_PATHS = {"/login", "/api/login", "/api/register", "/api/health", "/favicon.ico"}


def _is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith("/static/")


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    ip = _client_ip(request)
    if AUTH_FAILS.blocked(f"{ip}:auth"):
        return JSONResponse({"ok": False, "error": "rate limited"}, status_code=429)
    path = request.url.path
    if not _is_public(path) and not _authed(request):
        if path.startswith("/api"):
            return _deny(request)
        return RedirectResponse("/login", status_code=302)
    resp = await call_next(request)
    # HTML 和 API 一律 no-store，防止 token 被缓存到别人浏览器
    if path == "/" or path.startswith("/api"):
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


def _client_ip(request: Request) -> str:
    # 优先 X-Forwarded-For（nginx 反代场景）；没配反代时拿直连 IP
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "?"


# ---- 鉴权 ----
def _deny(request: Request) -> JSONResponse:
    ip = _client_ip(request)
    AUTH_FAILS.hit(f"{ip}:auth")
    print(f"[auth] 拒绝未授权访问 {request.method} {request.url.path} from {ip}")
    return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)


def _resolve(request: Request):
    """登录会话 / nginx 内部令牌 / URL token 三种渠道 → (tenant, room)。"""
    if not AUTH_ON:
        room = next(iter(registry.rooms.values()), None)
        return ({"id": "default", "name": cfg["room"]["name"], "room": ""}, room) if room else (None, None)
    meta = _session_tenant(request) or _proxy_meta(request.headers)
    if meta:
        return meta, registry.get(meta["id"])
    tenant = verify(TENANTS, extract_token(request.headers, request.query_params))
    if not tenant:
        return None, None
    return tenant, registry.get(tenant["id"])


def _tenant_by_id(tid: str) -> dict | None:
    for meta in TENANTS.values():
        if meta.get("id") == tid:
            return meta
    return None


def _session_tenant(req) -> dict | None:
    """从签名 cookie 解析登录用户（Request 和 WebSocket 都适用）。"""
    sid = req.cookies.get(accounts.COOKIE)
    user = accounts.parse_session(sid) if sid else None
    if not user:
        return None
    rec = accounts.get(user)
    if not rec:
        return None
    return _tenant_by_id(rec.get("tenant_id") or user)


def _authed(request: Request) -> bool:
    if not AUTH_ON:
        return True
    if _session_tenant(request) or _proxy_meta(request.headers):
        return True
    return bool(verify(TENANTS, extract_token(request.headers, request.query_params)))


def _proxy_meta(headers) -> dict | None:
    """校验 nginx 注入的 X-Proxy-Token，命中则免 ?t= 直接放行默认租户。"""
    if not PROXY_TOKEN:
        return None
    got = (headers.get("x-proxy-token") or "").strip()
    if not got or not hmac.compare_digest(got, PROXY_TOKEN):
        return None
    for meta in TENANTS.values():
        if meta["id"] == PROXY_TENANT:
            return meta
    return next(iter(TENANTS.values()), None)


def _admin_header_token(request: Request) -> str | None:
    """管理 token 只允许走 Header，禁止 URL 传参（URL 会进访问日志和浏览器历史）。"""
    for h in ("x-admin-token", "x-token", "authorization"):
        v = request.headers.get(h)
        if not v:
            continue
        if h == "authorization" and v.lower().startswith("bearer "):
            return v[7:].strip()
        return v.strip()
    return None


def _admin_ok(request: Request) -> bool:
    if not ADMIN_TOKEN:
        return False
    from src.auth import verify_admin
    return verify_admin(ADMIN_TOKEN, _admin_header_token(request))


def _admin_deny(request: Request) -> JSONResponse:
    ip = _client_ip(request)
    AUTH_FAILS.hit(f"{ip}:admin")
    print(f"[admin] 拒绝管理操作 {request.method} {request.url.path} from {ip}")
    return JSONResponse({"ok": False, "error": "admin unauthorized"}, status_code=403)


# ---- 公开端点 ----
@app.get("/")
async def index() -> FileResponse:
    return FileResponse(ROOT / "static" / "assist.html")


@app.get("/login")
async def login_page() -> FileResponse:
    return FileResponse(ROOT / "static" / "login.html")


# ---- 账号：注册 / 登录 / 登出 / 我是谁 ----
def _set_session(resp, username: str, request: Request) -> None:
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    resp.set_cookie(
        accounts.COOKIE,
        accounts.make_session(username),
        max_age=accounts.SESSION_TTL,
        httponly=True,
        samesite="lax",
        secure=(proto == "https"),
        path="/",
    )


def _me_payload(meta: dict, room) -> dict:
    prof = room.profile if room else {}
    return {
        "ok": True,
        "username": meta.get("username") or meta["id"],
        "tenant": meta["id"],
        "studio_name": prof.get("studio_name") or meta.get("name", ""),
        "assistant_name": prof.get("assistant_name") or "小陶",
        "room_id": room.room_id if room else "",
        "kb_filled": bool(room and (prof.get("shipping") or prof.get("products"))),
        "muted": room.pipe.muted if room else False,
        "live": room.live if room else None,
        "connected": bool(room and room._src and room._src.connected),
    }


@app.post("/api/register")
async def api_register(request: Request, payload: dict) -> JSONResponse:
    """注册 = 建账号 + 建租户（一个账号一个直播间）。"""
    if not AUTH_ON:
        return JSONResponse({"ok": False, "error": "服务未启用多租户，请配置 tenants.yaml"}, status_code=400)
    username = accounts.normalize_username(payload.get("username"))
    password = payload.get("password") or ""
    err = accounts.check_username(username) or accounts.check_password(password)
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    if accounts.get(username):
        return JSONResponse({"ok": False, "error": "这个用户名已经被注册了"}, status_code=409)
    if _tenant_by_id(username):
        return JSONResponse({"ok": False, "error": "这个用户名已被占用，换一个吧"}, status_code=409)

    studio = (payload.get("studio_name") or "").strip()[:40]
    token = new_token()
    meta = {
        "id": username,
        "name": studio or username,
        "room": "",
        "platform": "douyin",
        "token": token,
        "username": username,
        "assist_voice": True,
    }
    accounts.create(username, password, username, studio)
    TENANTS[token] = meta
    registry.register_live(meta)
    _write_tenants_file()
    print(f"[account] 注册 {username}（{studio or '未填店名'}）")
    resp = JSONResponse({"ok": True, "username": username, "tenant": username})
    _set_session(resp, username, request)
    return resp


@app.post("/api/login")
async def api_login(request: Request, payload: dict) -> JSONResponse:
    username = accounts.normalize_username(payload.get("username"))
    rec = accounts.authenticate(username, payload.get("password") or "")
    if not rec:
        AUTH_FAILS.hit(f"{_client_ip(request)}:auth")
        return JSONResponse({"ok": False, "error": "用户名或密码不对"}, status_code=401)
    meta = _tenant_by_id(rec.get("tenant_id") or username)
    if not meta:
        return JSONResponse({"ok": False, "error": "账号没有关联直播间，请联系管理员"}, status_code=500)
    resp = JSONResponse({"ok": True, "username": username})
    _set_session(resp, username, request)
    return resp


@app.post("/api/logout")
async def api_logout() -> JSONResponse:
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(accounts.COOKIE, path="/")
    return resp


@app.get("/api/me")
async def api_me(request: Request) -> JSONResponse:
    tenant, room = _resolve(request)
    if not room:
        return _deny(request)
    return JSONResponse(_me_payload(tenant, room))


@app.get("/api/health")
async def health() -> JSONResponse:
    rooms = [r.state() for r in registry.rooms.values()]
    # 真实健康：任何房间采集已连上 → ok=true；全断且应该有采集时 → degraded
    any_connected = any(r["connected"] for r in rooms)
    any_pump = any(r["room_id_set"] for r in rooms)
    ok = any_connected or not any_pump
    return JSONResponse(
        {
            "ok": ok,
            "rooms_count": len(rooms),
            "rooms": rooms,
            "auth": AUTH_ON,
        }
    )


# ---- 租户端点（H5 用） ----
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    token = ws.query_params.get("t") or ws.query_params.get("token")
    ip = _client_ip(ws)
    if AUTH_ON:
        tenant = _session_tenant(ws) or _proxy_meta(ws.headers) or verify(TENANTS, token)
        if not tenant:
            if AUTH_FAILS.hit(f"{ip}:auth"):
                print(f"[auth] IP {ip} 触发鉴权限流")
            print(f"[auth] 拒绝 WS 连接 from {ip}")
            await ws.close(code=4401, reason="unauthorized")
            return
        AUTH_FAILS.clear(f"{ip}:auth")
    else:
        tenant = {"id": "default", "name": cfg["room"]["name"]}
    room = registry.get(tenant["id"])
    if not room:
        await ws.close(code=4404, reason="no such room")
        return

    await ws.accept()
    q = room.sink.register()
    try:
        await ws.send_text(
            json.dumps(
                {
                    "type": "hello",
                    "tenant": tenant["id"],
                    "username": tenant.get("username") or tenant["id"],
                    "studio_name": room.profile.get("studio_name") or room.name,
                    "room": room.name,
                    "platform": room.platform,
                    "assist_voice": room.pipe.assist_voice,
                    "assistant_name": room.profile.get("assistant_name") or "小陶",
                    "live": room.live,
                    "connected": room._src.connected if room._src else False,
                    "muted": room.pipe.muted,
                    "room_id_set": bool(room.room_id),
                    "pending": room.pipe.pending_host,
                    "stats": room.pipe.stats,
                    "kb_filled": bool(room.profile.get("shipping") or room.profile.get("products")),
                },
                ensure_ascii=False,
            )
        )
        while True:
            msg = await q.get()
            await ws.send_text(json.dumps(msg, ensure_ascii=False))
    except WebSocketDisconnect:
        pass
    finally:
        room.sink.unregister(q)


@app.get("/api/stats")
async def stats(request: Request) -> JSONResponse:
    tenant, room = _resolve(request)
    if not room:
        return _deny(request)
    tts_stats = room.tts.stats() if hasattr(room.tts, "stats") else {}
    return JSONResponse(
        {"stats": room.pipe.stats, "tts": tts_stats, "log": str(room.pipe.log_path), "room": room.name}
    )


@app.get("/api/pending")
async def pending(request: Request) -> JSONResponse:
    tenant, room = _resolve(request)
    if not room:
        return _deny(request)
    return JSONResponse({"pending": room.pipe.pending_host})


@app.get("/api/report")
async def report(request: Request, session: str = "") -> JSONResponse:
    """效果看板：AI 承接率、决策延迟、观众在问什么、知识库缺口。"""
    tenant, room = _resolve(request)
    if not room:
        return _deny(request)
    from src.analytics import analyze

    if session:
        # 只允许读自己租户目录下的文件，防目录穿越
        safe = re.sub(r"[^A-Za-z0-9_.-]", "", session)
        if safe in ("", ".", ".."):
            return JSONResponse({"ok": False, "error": "session 参数无效"}, status_code=400)
        path = room.pipe.log_path.parent / safe
    else:
        path = room.pipe.log_path
    data = analyze(path)
    data["room"] = room.name
    return JSONResponse(data)


@app.get("/api/sessions")
async def sessions(request: Request) -> JSONResponse:
    tenant, room = _resolve(request)
    if not room:
        return _deny(request)
    d = room.pipe.log_path.parent
    files = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]
    return JSONResponse(
        {
            "current": room.pipe.log_path.name,
            "sessions": [
                {"name": f.name, "size": f.stat().st_size, "mtime": int(f.stat().st_mtime)}
                for f in files
            ],
        }
    )


@app.post("/api/danmaku")
async def push_danmaku(request: Request, payload: dict) -> JSONResponse:
    """弹幕入队，HTTP 立刻返回，不阻塞事件循环。debug 用 ?sync=1 走同步路径。"""
    tenant, room = _resolve(request)
    if not room:
        return _deny(request)
    kind = (payload.get("kind") or "chat").strip()
    text = (payload.get("text") or "").strip()
    if not text and kind == "chat":
        return JSONResponse({"ok": False, "error": "text 为空"}, status_code=400)
    dm = Danmaku(user=(payload.get("user") or "观众").strip(), text=text,
                 kind=kind, extra=payload.get("extra") or {})
    if request.query_params.get("sync") == "1":
        action, spoken = room.pipe.handle(dm)
        return JSONResponse({"ok": True, "action": action, "spoken": spoken, "sync": True})
    queued = room.enqueue(dm)
    return JSONResponse({"ok": True, "queued": queued, "depth": room.queue_depth()})


@app.post("/api/announce")
async def announce(request: Request) -> JSONResponse:
    tenant, room = _resolve(request)
    if not room:
        return _deny(request)
    text = cfg["disclosure"]["text"]
    # 通过房间队列调用，避免阻塞事件循环
    room.submit(lambda: room.pipe.announce(text))
    return JSONResponse({"ok": True, "queued": True})


@app.get("/api/kb")
async def get_kb(request: Request) -> JSONResponse:
    tenant, room = _resolve(request)
    if not room:
        return _deny(request)
    return JSONResponse({"profile": room.profile, "room_id": room.room_id})


@app.post("/api/kb")
async def save_kb(request: Request, payload: dict) -> JSONResponse:
    """店主在手机上保存知识库 → 入队热更新 + 预合成语音，HTTP 立刻返回。

    预合成 TTS 可能耗时几十秒，走房间 worker，不在请求里同步做。
    """
    tenant, room = _resolve(request)
    if not room:
        return _deny(request)
    profile = kb_store.save(room.id, payload.get("profile") or {}) if "profile" in payload else room.profile
    # compile_faqs 只算数量，不重复 apply（kb_store.save 订阅回调已经把 apply 入队了）
    faqs = compile_faqs(profile)

    # 房间号可在设置页填写，填了就立刻拉起采集（这是即时操作，不等队列）
    new_room_id = str(payload.get("room_id") or "").strip()
    pumped = None
    if new_room_id and new_room_id != room.room_id:
        room.room_id = new_room_id
        room.stop_pump()
        _persist_room_id(room.id, new_room_id)
        pumped = room.start_pump(cfg.get("ws_url", "")) if cfg.get("source") == "ws" else False
    return JSONResponse({
        "ok": True,
        "faqs": len(faqs),
        "prewarm_queued": True,
        "room_id": room.room_id,
        "pump_started": pumped,
        "digest": kb_store._updated.get(room.id, 0),
    })


@app.post("/api/room")
async def api_room(request: Request, payload: dict) -> JSONResponse:
    """只改直播间号（每次开播只需这一步）。"""
    tenant, room = _resolve(request)
    if not room:
        return _deny(request)
    rid = re.sub(r"[^0-9A-Za-z_-]", "", str(payload.get("room_id") or ""))[:32]
    if not rid:
        return JSONResponse({"ok": False, "error": "直播间号不能为空"}, status_code=400)
    changed = rid != room.room_id
    if changed:
        room.room_id = rid
        room.stop_pump()
        _persist_room_id(room.id, rid)
    alive = bool(room._pump and room._pump.is_alive())
    pumped = alive
    if cfg.get("source") == "ws" and not alive:
        pumped = room.start_pump(cfg.get("ws_url", ""))
    print(f"[room] {room.id} → {rid}（changed={changed}, pump={pumped}）")
    return JSONResponse({"ok": True, "room_id": rid, "changed": changed, "pump_started": pumped})


def _persist_room_id(tenant_id: str, room_id: str) -> None:
    """把房间号同时写回 tenants.yaml 和内存 TENANTS，重启后仍在，避免被其他 admin 操作覆盖回去。"""
    import yaml

    # 同步内存：防止 admin_add/remove/rotate 触发 _write_tenants_file 时把 room_id 写回旧值
    for meta in TENANTS.values():
        if meta.get("id") == tenant_id:
            meta["room"] = room_id

    f = ROOT / "tenants.yaml"
    if not f.exists():
        return
    try:
        data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        for e in data.get("tenants", []):
            if str(e.get("id")) == tenant_id:
                e["room"] = room_id
        f.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        print(f"[kb] 房间号已持久化：{tenant_id} → {room_id}")
    except Exception as e:  # noqa: BLE001
        print(f"[kb] 房间号持久化失败（本次会话仍有效）：{e}")


@app.post("/api/mute")
async def mute_self(request: Request) -> JSONResponse:
    """店主自己一键静音 AI 出声（出事故时立刻用）。"""
    tenant, room = _resolve(request)
    if not room:
        return _deny(request)
    body = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        pass
    muted = bool(body.get("muted", True))
    room.set_muted(muted)
    print(f"[mute] {room.id} muted={muted}")
    return JSONResponse({"ok": True, "muted": muted})


@app.get("/api/usage")
async def usage(request: Request) -> JSONResponse:
    tenant, room = _resolve(request)
    if not room:
        return _deny(request)
    return JSONResponse(_usage_for(room))


def _usage_for(room) -> dict:
    u = dict(room.pipe.usage)
    tts_stats = room.tts.stats() if hasattr(room.tts, "stats") else {}
    return {
        "room": room.id,
        "llm": {
            "calls": u.get("llm_calls", 0),
            "errors": u.get("llm_errors", 0),
            "prompt_tokens": u.get("prompt_tokens", 0),
            "completion_tokens": u.get("completion_tokens", 0),
        },
        "tts": {
            "requests": u.get("tts_synth", 0),
            "chars": u.get("tts_chars", 0),
            "hit_rate": tts_stats.get("hit_rate", 0),
        },
        "danmaku": room.pipe.stats.get("danmaku", 0),
        "dropped": u.get("dropped", 0),
    }


# ============ 管理 API：让运营者一条命令加客户，不用 SSH ============


def _write_tenants_file() -> None:
    """把内存中的租户表落盘，重启后仍在。room_id 优先取内存 TENANTS，缺失时回退到 registry。"""
    import yaml

    entries = []
    seen = set()
    for tok, meta in TENANTS.items():
        if meta["id"] in seen:
            continue
        seen.add(meta["id"])
        room_id = str(meta.get("room") or "").strip()
        # 兜底：内存 meta 没 room_id 时去 registry 取（避免 admin add/remove 清掉刚填的号）
        if not room_id:
            rm = registry.get(meta["id"])
            if rm:
                room_id = rm.room_id
        e = {
            "id": meta["id"],
            "name": meta["name"],
            "room": room_id,
            "platform": meta.get("platform") or "douyin",
            "token": tok,
        }
        if meta.get("voice"):
            e["voice"] = meta["voice"]
        if "assist_voice" in meta:
            e["assist_voice"] = bool(meta["assist_voice"])
        entries.append(e)
    f = ROOT / "tenants.yaml"
    f.write_text(
        "# 租户表（含密钥，勿提交公开仓库）\n"
        + yaml.safe_dump({"tenants": entries}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    os.chmod(f, 0o600)


@app.get("/api/admin/tenants")
async def admin_list(request: Request) -> JSONResponse:
    if not ADMIN_TOKEN or not _admin_ok(request):
        return _admin_deny(request)
    out = []
    for tok, meta in TENANTS.items():
        room = registry.get(meta["id"])
        out.append(
            {
                "id": meta["id"],
                "name": meta["name"],
                "platform": meta.get("platform") or "douyin",
                "room": room.room_id if room else meta.get("room", ""),
                "assist_voice": room.pipe.assist_voice if room else None,
                "muted": room.pipe.muted if room else False,
                "live": room.live if room else None,
                "connected": bool(room and room._src and room._src.connected),
                "kb_filled": bool(room and (room.profile.get("shipping") or room.profile.get("products"))),
                "stats": room.pipe.stats if room else {},
                "usage": _usage_for(room) if room else None,
                "url": f"?t={tok}",
            }
        )
    return JSONResponse({"ok": True, "tenants": out, "rooms": len(registry.rooms)})


@app.post("/api/admin/tenants")
async def admin_add(request: Request, payload: dict) -> JSONResponse:
    """运行时新增客户：立即生效，无需重启服务。"""
    if not ADMIN_TOKEN or not _admin_ok(request):
        return _admin_deny(request)
    from src.auth import new_token

    tid = re.sub(r"[^A-Za-z0-9_.-]", "", str(payload.get("id") or "").strip())
    name = str(payload.get("name") or "").strip()
    if not tid or not name:
        return JSONResponse({"ok": False, "error": "id 和 name 必填"}, status_code=400)
    if tid in registry.rooms:
        return JSONResponse({"ok": False, "error": f"租户 {tid} 已存在"}, status_code=409)

    platform = str(payload.get("platform") or "douyin").strip()
    if platform not in ("douyin", "channels"):
        platform = "douyin"
    token = new_token()
    meta = {
        "id": tid,
        "name": name,
        "room": str(payload.get("room") or "").strip(),
        "platform": platform,
        "token": token,
    }
    if str(payload.get("voice") or "").strip():
        meta["voice"] = str(payload["voice"]).strip()
    if "assist_voice" in payload:
        meta["assist_voice"] = bool(payload["assist_voice"])

    TENANTS[token] = meta
    room = registry.register_live(meta)
    _write_tenants_file()
    print(f"[admin] 新增租户 {tid}（{name}，平台={platform}，AI出声={room.pipe.assist_voice}）")
    return JSONResponse(
        {
            "ok": True,
            "id": tid,
            "token": token,
            "assist_voice": room.pipe.assist_voice,
            "pump_started": bool(room._pump and room._pump.is_alive()),
            "note": "视频号平台已自动关闭 AI 出声" if platform == "channels" and not room.pipe.assist_voice else None,
        }
    )


@app.delete("/api/admin/tenants/{tenant_id}")
async def admin_remove(request: Request, tenant_id: str) -> JSONResponse:
    if not ADMIN_TOKEN or not _admin_ok(request):
        return _admin_deny(request)
    target = None
    for tok, meta in list(TENANTS.items()):
        if meta["id"] == tenant_id:
            target = tok
            break
    if not target:
        return JSONResponse({"ok": False, "error": "租户不存在"}, status_code=404)
    TENANTS.pop(target, None)
    registry.remove(tenant_id)
    _write_tenants_file()
    print(f"[admin] 删除租户 {tenant_id}")
    return JSONResponse({"ok": True, "removed": tenant_id})


@app.post("/api/admin/rotate/{tenant_id}")
async def admin_rotate(request: Request, tenant_id: str) -> JSONResponse:
    """重置某客户的访问链接（链接泄露时用）。"""
    if not ADMIN_TOKEN or not _admin_ok(request):
        return _admin_deny(request)
    from src.auth import new_token

    old = None
    for tok, meta in TENANTS.items():
        if meta["id"] == tenant_id:
            old = tok
            break
    if not old:
        return JSONResponse({"ok": False, "error": "租户不存在"}, status_code=404)
    meta = TENANTS.pop(old)
    token = new_token()
    meta["token"] = token
    TENANTS[token] = meta
    _write_tenants_file()
    return JSONResponse({"ok": True, "id": tenant_id, "token": token})


@app.post("/api/admin/mute/{tenant_id}")
async def admin_mute(request: Request, tenant_id: str) -> JSONResponse:
    if not ADMIN_TOKEN or not _admin_ok(request):
        return _admin_deny(request)
    room = registry.get(tenant_id)
    if not room:
        return JSONResponse({"ok": False, "error": "租户不存在"}, status_code=404)
    body = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        pass
    muted = bool(body.get("muted", True))
    room.set_muted(muted)
    print(f"[admin] {tenant_id} muted={muted}")
    return JSONResponse({"ok": True, "muted": muted})


# ============ 后台维护 ============


def _maintenance_loop() -> None:
    """每 6 小时清理过期留痕 + TTS 缓存，避免小磁盘写满。"""
    interval = float(os.environ.get("MAINT_INTERVAL_HOURS", "6")) * 3600
    sessions_days = int(os.environ.get("SESSIONS_KEEP_DAYS", "30"))
    tts_max_bytes = int(os.environ.get("TTS_CACHE_MAX_MB", "512")) * 1024 * 1024
    tts_max_files = int(os.environ.get("TTS_CACHE_MAX_FILES", "2000"))
    # 启动时先等 30 秒，让房间完成启动
    time.sleep(30)
    while True:
        try:
            _cleanup_sessions(sessions_days)
            _cleanup_tts(tts_max_bytes, tts_max_files)
        except Exception as e:  # noqa: BLE001
            print(f"[maint] 清理失败：{e}")
        time.sleep(interval)


def _cleanup_sessions(max_days: int) -> None:
    cutoff = time.time() - max_days * 86400
    sessions_dir = ROOT / "sessions"
    if not sessions_dir.exists():
        return
    removed = 0
    for f in sessions_dir.rglob("*.jsonl"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        except OSError:
            continue
    if removed:
        print(f"[maint] 清理过期留痕 {removed} 个（>{max_days}天）")


def _cleanup_tts(max_bytes: int, max_files: int) -> None:
    removed = 0
    for tts in registry._tts_cache.values() if hasattr(registry, "_tts_cache") else []:
        if hasattr(tts, "cleanup"):
            removed += tts.cleanup(max_bytes=max_bytes, max_files=max_files)
    if removed:
        print(f"[maint] 清理 TTS 缓存 {removed} 个")


if __name__ == "__main__":
    import uvicorn

    # uvicorn 默认单 worker；本服务是 IO 密集 + 内存共享，多 worker 没意义。
    uvicorn.run(
        app,
        host=HOST,
        port=PORT,
        log_level="info",
        forwarded_allow_ips="*",  # nginx 反代时识别真 IP
    )