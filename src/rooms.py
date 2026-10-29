"""多租户房间注册表：一个进程服务 N 个直播间。

每个租户拥有独立的：Pipeline、大脑、知识库、留痕目录、WebSink（提词队列）、采集器连接。
这样 A 店的提词绝不会出现在 B 店的手机上。

线程模型（关键）：
- 每个房间一条 **串行 worker 队列**，弹幕只入队、由单线程顺序处理。
  LLM/TTS 都是阻塞调用，串行化后既不会拖垮 asyncio 事件循环，也不会丢统计、乱序提词。
- 采集线程只负责读 WS → 入队；HTTP 接口只负责入队 → 立刻返回。
- 队列满时丢弃最旧的一条并计数，绝不阻塞采集（直播宁可漏一条，不能卡住）。

关键事实：一个 douyinLive 实例可服务多个房间——房间号在 WS 路径里
（ws://127.0.0.1:1088/ws/{room}），所以采集器只需一个进程，每租户一条连接。
"""
from __future__ import annotations

import copy
import queue
import re
import threading
import time
from pathlib import Path

from .brain.local_rules import RuleBrain
from .config import ROOT
from .danmaku.ws_source import WsSource
from .kb_store import compile_faqs, compile_tone, kb_digest
from .output.sinks import WebSink
from .pipeline import Pipeline
from .voice.tts import make_tts

_ROOM_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_QUEUE_MAX = 500


def _build_brain(cfg: dict, room_name: str, faqs: list[dict], tone: str, assistant: str):
    llm_cfg = cfg.get("llm", {})
    if llm_cfg.get("enabled", True):
        try:
            from .brain.llm_client import LlmBrain

            brain = LlmBrain(
                room_name,
                llm_cfg,
                max_len=int(cfg.get("routing", {}).get("max_reply_len", 60)),
                faqs=faqs,
                tone=tone,
                assistant_name=assistant,
            )
            if brain.api_key:
                return brain, "llm"
        except Exception as e:  # noqa: BLE001  依赖缺失/初始化失败都不应阻断直播
            print(f"[rooms:{room_name}] LLM 不可用（{e}），降级本地规则")
    return RuleBrain(room_name, faqs), "rules"


class Room:
    def __init__(self, tenant: dict, cfg: dict, kb_store, tts_cache: dict, cache_root: Path):
        self.tenant = tenant
        self.id = tenant["id"]
        self.name = tenant.get("name") or self.id
        self.room_id = str(tenant.get("room") or "").strip()
        self.platform = tenant.get("platform") or cfg.get("room", {}).get("platform", "douyin")
        self.kb_store = kb_store

        # 租户级配置覆盖：房间名、平台（决定 AI 是否允许出声）、留痕目录
        self.cfg = copy.deepcopy(cfg)
        self.cfg.setdefault("room", {})["name"] = self.name
        self.cfg["room"]["platform"] = self.platform
        # AI 是否出声：租户显式配置 > 平台默认。
        # 注意不能用 setdefault —— 全局 config.yaml 里的 output.assist_voice
        # 会让视频号店的静音默认值失效，那是最危险的合规失效。
        out = self.cfg.setdefault("output", {})
        if "assist_voice" in tenant:
            out["assist_voice"] = bool(tenant["assist_voice"])
        else:
            out["assist_voice"] = self.platform != "channels"

        self.profile = kb_store.get(self.id)
        faqs = compile_faqs(self.profile)
        self.brain, self.brain_kind = _build_brain(
            self.cfg, self.name, faqs, compile_tone(self.profile),
            self.profile.get("assistant_name") or "小陶",
        )
        self.sink = WebSink()
        self.tts = self._make_tts(tts_cache, cache_root)
        self.pipe = Pipeline(
            self.cfg, self.brain, self.tts, self.sink,
            log_dir=str(cache_root.parent / "sessions" / self.id),
        )

        # ---- 串行 worker ----
        self._q: queue.Queue = queue.Queue(maxsize=_QUEUE_MAX)
        self._stop_worker = threading.Event()
        self._worker = threading.Thread(target=self._consume, name=f"room-{self.id}", daemon=True)
        self._worker.start()

        # ---- 采集 ----
        self._pump_stop: threading.Event | None = None
        self._pump: threading.Thread | None = None
        self._src: WsSource | None = None
        self.base_ws_url = ""
        self.live: bool | None = None
        self.last_status: dict = {}
        self.status_at = 0.0

    # ---- TTS ----
    def _make_tts(self, tts_cache: dict, cache_root: Path):
        """按音色复用 TTS 实例；缓存目录全局共享（key 含音色，不会串）。"""
        voice = self.tenant.get("voice") or self.cfg.get("tts", {}).get("voice", "longxiaochun")
        if voice not in tts_cache:
            cfg = dict(self.cfg.get("tts", {}))
            cfg["voice"] = voice
            tts_cache[voice] = make_tts(cfg, cache_dir=cache_root / "tts")
        return tts_cache[voice]

    # ---- 串行处理 ----
    def _consume(self) -> None:
        while not self._stop_worker.is_set():
            try:
                item = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                break
            try:
                if callable(item):
                    item()
                else:
                    self.pipe.handle(item)
            except Exception as e:  # noqa: BLE001  单条弹幕失败不能拖垮整个房间
                print(f"[rooms:{self.id}] 处理失败：{e}")
            finally:
                self._q.task_done()

    def enqueue(self, dm) -> bool:
        """入队即返回。队列满时丢最旧一条（直播不能卡），返回是否丢过数据由 dropped 体现。"""
        try:
            self._q.put_nowait(dm)
            return True
        except queue.Full:
            try:
                self._q.get_nowait()
                self._q.task_done()
                self.pipe.usage["dropped"] = self.pipe.usage.get("dropped", 0) + 1
                self._q.put_nowait(dm)
                return True
            except (queue.Empty, queue.Full):
                self.pipe.usage["dropped"] = self.pipe.usage.get("dropped", 0) + 1
                return False

    def submit(self, fn) -> bool:
        """把后台任务（如知识库热更新、预合成）丢给同一串行队列。"""
        try:
            self._q.put_nowait(fn)
            return True
        except queue.Full:
            return False

    def queue_depth(self) -> int:
        return self._q.qsize()

    # ---- 采集器 ----
    def _valid_room(self) -> bool:
        return bool(_ROOM_ID_RE.match(self.room_id))

    def start_pump(self, base_ws_url: str) -> bool:
        """连上游采集器。没填房间号则跳过（仍可用 H5 手动灌弹幕联调）。"""
        if not self.room_id:
            print(f"[rooms:{self.id}] 未填直播间号，暂不启动采集（可在 H5 设置页填写）")
            return False
        if not self._valid_room():
            print(f"[rooms:{self.id}] 直播间号格式可疑：{self.room_id!r}，暂不启动采集")
            self.sink.emit_status({"live": None, "code": "BAD_ROOM_ID", "text": "直播间号格式不对"})
            return False
        self.base_ws_url = base_ws_url
        url = base_ws_url.rstrip("/")
        if not url.endswith(self.room_id):
            url = f"{url.split('/ws/')[0]}/ws/{self.room_id}" if "/ws/" in url else f"{url}/ws/{self.room_id}"
        if self._pump and self._pump.is_alive():
            return True
        self._pump_stop = threading.Event()
        self._pump = threading.Thread(target=self._run_pump, args=(url,), name=f"pump-{self.id}", daemon=True)
        self._pump.start()
        print(f"[rooms:{self.id}] 采集已启动 → {url}")
        return True

    def _run_pump(self, url: str) -> None:
        stop = self._pump_stop or threading.Event()
        src = WsSource(url, on_status=self._on_status, stop_event=stop)
        self._src = src
        try:
            for dm in src.events():
                if stop.is_set():
                    break
                self.enqueue(dm)
        finally:
            self._src = None
            src.close()

    def _on_status(self, st: dict) -> None:
        """采集器状态：开播/下播是场次与 AI 声明的唯一可靠信号。"""
        self.last_status = st
        self.status_at = time.time()
        live = st.get("live")
        if live is True and self.live is not True:
            self.live = True
            # 新一场：切留痕 + 重置统计，然后念一次 AI 声明
            self.pipe.rotate_session()
            if self.cfg.get("disclosure", {}).get("enabled", True):
                text = self.cfg["disclosure"]["text"]
                self.submit(lambda t=text: self.pipe.announce(t))
            print(f"[rooms:{self.id}] 检测到开播，已开启新场次")
        elif live is False and self.live is not False:
            self.live = False
            print(f"[rooms:{self.id}] 检测到下播")
        self.sink.emit_status(st)

    def stop_pump(self) -> None:
        """停止采集并等旧线程退出，避免换房间号时两条流同时进同一管线。"""
        if self._pump_stop:
            self._pump_stop.set()
        if self._src:
            self._src.close()
        if self._pump and self._pump.is_alive():
            self._pump.join(timeout=6.0)
        self._pump = None
        self._src = None

    def stop(self) -> None:
        self.stop_pump()
        self._stop_worker.set()
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass
        if self._worker.is_alive():
            self._worker.join(timeout=3.0)
        self.pipe.close()

    # ---- 知识库热更新 ----
    def apply_kb(self, profile: dict) -> dict:
        self.profile = profile
        faqs = compile_faqs(profile)
        assistant = profile.get("assistant_name") or "小陶"
        if hasattr(self.brain, "set_kb"):
            self.brain.set_kb(faqs, compile_tone(profile))
            self.brain.assistant_name = assistant
        else:
            self.brain.set_faqs(faqs)
        if profile.get("studio_name"):
            self.name = profile["studio_name"]
            self.cfg["room"]["name"] = self.name
        # 预合成 FAQ 答案：首播不卡顿，且后续命中缓存零成本。
        # 这段可能耗时几十秒，必须跑在 worker 线程里，不能阻塞 HTTP。
        prewarmed = 0
        if hasattr(self.tts, "prewarm"):
            prewarmed = self.tts.prewarm([f.get("answer") for f in faqs if f.get("answer")])
        print(f"[rooms:{self.id}] 知识库已更新：{len(faqs)} 条 FAQ，预合成 {prewarmed} 条语音")
        return {"faqs": len(faqs), "prewarmed": prewarmed, "digest": kb_digest(profile)}

    # ---- 状态 ----
    def state(self) -> dict:
        src_state = self._src.state() if self._src else {"connected": False, "last_event_at": 0}
        return {
            "id": self.id,
            "name": self.name,
            "platform": self.platform,
            "room_id": self.room_id,
            "room_id_set": bool(self.room_id),
            "live": self.live,
            "connected": src_state.get("connected", False),
            "last_event_at": src_state.get("last_event_at", 0),
            "last_error": src_state.get("last_error", ""),
            "queue": self.queue_depth(),
            "dropped": self.pipe.usage.get("dropped", 0),
            "muted": self.pipe.muted,
            "assist_voice": self.pipe.assist_voice,
            "brain": self.brain_kind,
            "stats": dict(self.pipe.stats),
            "usage": dict(self.pipe.usage),
            "session": self.pipe.log_path.name,
        }

    def set_muted(self, muted: bool) -> None:
        self.pipe.set_muted(muted)


class RoomRegistry:
    def __init__(self, cfg: dict, tenants: dict, kb_store, cache_root: Path | None = None):
        self.cfg = cfg
        self.kb_store = kb_store
        self.cache_root = cache_root or (ROOT / "cache")
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self._tts_cache: dict = {}
        self.rooms: dict[str, Room] = {}
        self.base_ws_url = cfg.get("ws_url", "")
        self._loop = None
        # 知识库保存时自动热更新对应房间（入队执行，不阻塞保存请求）
        kb_store.subscribe(self._on_kb_saved)
        for token, meta in tenants.items():
            self.add(meta)

    def add(self, meta: dict) -> Room:
        room = Room(meta, self.cfg, self.kb_store, self._tts_cache, self.cache_root)
        self.rooms[room.id] = room
        return room

    def register_live(self, meta: dict) -> Room:
        """运行时新增租户（管理 API 用）：建房间、绑事件循环、按需拉起采集。"""
        room = self.add(meta)
        if self._loop:
            room.sink.bind_loop(self._loop)
        if self.cfg.get("source") == "ws" and self.base_ws_url:
            room.start_pump(self.base_ws_url)
        return room

    def remove(self, tenant_id: str) -> bool:
        room = self.rooms.pop(tenant_id, None)
        if not room:
            return False
        room.stop()
        return True

    def get(self, tenant_id: str) -> Room | None:
        return self.rooms.get(tenant_id)

    def _on_kb_saved(self, tenant_id: str, profile: dict, faqs: list[dict]) -> None:
        room = self.rooms.get(tenant_id)
        if room:
            # 走房间队列：预合成 TTS 可能要几十秒，绝不能在 HTTP 请求里同步做
            room.submit(lambda: room.apply_kb(profile))

    def start_all_pumps(self) -> dict:
        """启动所有已填房间号的采集；返回 {tenant_id: 是否启动}。"""
        out = {}
        for tid, room in self.rooms.items():
            out[tid] = room.start_pump(self.base_ws_url)
        return out

    def stop_all(self) -> None:
        for room in list(self.rooms.values()):
            room.stop()

    def bind_loop(self, loop) -> None:
        self._loop = loop
        for room in self.rooms.values():
            room.sink.bind_loop(loop)

    def stats(self) -> dict:
        return {tid: r.pipe.stats for tid, r in self.rooms.items()}