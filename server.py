"""云端服务：FastAPI + WebSocket + 多租户。

一个进程服务 N 个直播间，每个账号（= 一个租户）独立的 Pipeline / 知识库 / 提词队列。
唯一的身份来源是登录 cookie（src/accounts.py），没有 token，也没有 tenants.yaml。

  GET  /login            登录 / 注册页
  GET  /                 助播台（需登录）
  WS   /ws               实时通道：提词 + 助播语音 + 直播间状态（cookie 鉴权）
  GET  /api/me           当前账号与直播间状态
  GET  /api/stats        本场统计
  GET  /api/pending      待主播跟进清单
  POST /api/danmaku      灌入一条弹幕（入队返回，不阻塞事件循环）
  POST /api/announce     触发 AI 声明口播
  GET  /api/kb           读取本店知识库
  POST /api/kb           保存知识库 → 入队热更新 + 预合成语音
  POST /api/room         只改直播间号（每次开播唯一要改的）
  POST /api/mute         一键静音
  GET  /api/usage        本店本月累计用量
  GET  /api/health       公开健康探针（含真实连接状态）
"""
from __future__ import annotations

import asyncio
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
from src.config import load_config  # noqa: E402
from src.danmaku.base import Danmaku  # noqa: E402
from src.kb_store import KBStore, compile_faqs  # noqa: E402
from src.ratelimit import RateLimiter  # noqa: E402
from src.rooms import RoomRegistry  # noqa: E402

ROOT = Path(__file__).resolve().parent
cfg = load_config(os.environ.get("LIVE_COPILOT_CONFIG"))
kb_store = KBStore()
registry = RoomRegistry(cfg, {}, kb_store)

# 鉴权失败限流：扫描器 20 次/分钟就封 2 分钟，避免日志被刷爆
AUTH_FAILS = RateLimiter(max_hits=20, window_sec=60.0, block_sec=120.0)

# ---- 配置与启动 ----
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8010"))


def _meta_from_account(username: str, rec: dict) -> dict:
    """账号记录 → 房间元信息。账号即租户，没有第二套映射。"""
    return {
        "id": username,
        "name": rec.get("studio_name") or username,
        "room": str(rec.get("room_id") or ""),
        "platform": "douyin",
        "username": username,
        "assist_voice": True,
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    registry.bind_loop(asyncio.get_running_loop())
    # 从账号表恢复所有房间（账号即租户）
    users = accounts._load().get("users", {})
    for username, rec in users.items():
        if username not in registry.rooms:
            registry.add(_meta_from_account(username, rec))
    print(f"[accounts] 已加载 {len(users)} 个账号")
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


def _ensure_room(username: str, rec: dict):
    """拿到（必要时创建）某个账号对应的房间。重启后/老账号都能自愈。"""
    room = registry.get(username)
    if room:
        return room
    return registry.register_live(_meta_from_account(username, rec))


def _resolve(request: Request):
    """登录 cookie → (tenant, room)。只有一条鉴权路径。"""
    meta = _session_tenant(request)
    if not meta:
        return None, None
    rec = accounts.get(meta["id"]) or {}
    return meta, _ensure_room(meta["id"], rec)


def _session_tenant(req) -> dict | None:
    """从签名 cookie 解析登录用户（Request 和 WebSocket 都适用）。"""
    sid = req.cookies.get(accounts.COOKIE)
    user = accounts.parse_session(sid) if sid else None
    if not user:
        return None
    rec = accounts.get(user)
    if not rec:
        return None
    return _meta_from_account(user, rec)


def _authed(request: Request) -> bool:
    return _session_tenant(request) is not None





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
    username = accounts.normalize_username(payload.get("username"))
    password = payload.get("password") or ""
    err = accounts.check_username(username) or accounts.check_password(password)
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    if accounts.get(username):
        return JSONResponse({"ok": False, "error": "这个用户名已经被注册了"}, status_code=409)

    studio = (payload.get("studio_name") or "").strip()[:40]
    accounts.create(username, password, username, studio)
    meta = _meta_from_account(username, accounts.get(username))
    registry.register_live(meta)
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
    _ensure_room(username, rec)
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
        }
    )


# ---- 租户端点（H5 用） ----
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    ip = _client_ip(ws)
    tenant = _session_tenant(ws)
    if not tenant:
        if AUTH_FAILS.hit(f"{ip}:auth"):
            print(f"[auth] IP {ip} 触发鉴权限流")
        print(f"[auth] 拒绝 WS 连接 from {ip}")
        await ws.close(code=4401, reason="unauthorized")
        return
    AUTH_FAILS.clear(f"{ip}:auth")
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
    # 店名/助手名两处同源：账号里也存一份，首页顶部和登录后立刻一致
    acct = accounts.get(room.id)
    if acct is not None and profile.get("studio_name"):
        accounts.update(room.id, studio_name=profile["studio_name"])
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
    """把房间号写回账号记录，重启后仍在。"""
    accounts.update(tenant_id, room_id=room_id)
    print(f"[room] 房间号已持久化：{tenant_id} → {room_id}")


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