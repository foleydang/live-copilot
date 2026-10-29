"""通用 WebSocket 弹幕源：对接 douyinLive / BarrageGrab 等上游采集器。

- 消息格式解析见 adapters.py（已按上游源码/文档核实）
- douyinLive 要求客户端每 30s 发文本 "ping"，这里用后台线程保活
- 上游"未开播"等状态消息不会变成弹幕，只打印并回调 on_status
- 暴露 connected / last_event_at / last_error：健康检查不能再拿"线程还活着"当"已连上"
"""
from __future__ import annotations

import json
import threading
import time
from typing import Callable, Iterator

from .adapters import KIND_STATUS, parse
from .base import Danmaku, DanmakuSource


class WsSource(DanmakuSource):
    def __init__(
        self,
        url: str,
        reconnect_delay: float = 5.0,
        max_reconnect_delay: float = 60.0,
        ping_interval: float = 25.0,
        on_status: Callable[[dict], None] | None = None,
        stop_event: threading.Event | None = None,
    ):
        self.url = url
        self.reconnect_delay = reconnect_delay
        self.max_reconnect_delay = max_reconnect_delay
        self.ping_interval = ping_interval
        self.on_status = on_status or (lambda s: print(f"[直播间状态] {s.get('text')}"))
        self._stop = stop_event or threading.Event()
        self._state_lock = threading.Lock()
        self.connected = False
        self.last_event_at = 0.0
        self.last_error = ""

    def state(self) -> dict:
        with self._state_lock:
            return {
                "connected": self.connected,
                "last_event_at": int(self.last_event_at),
                "last_error": self.last_error,
            }

    def _set(self, **kw) -> None:
        with self._state_lock:
            for k, v in kw.items():
                setattr(self, k, v)

    def events(self) -> Iterator[Danmaku]:
        from websockets.sync.client import connect  # 延迟导入：mock 模式不需要

        delay = self.reconnect_delay
        while not self._stop.is_set():
            try:
                with connect(self.url, max_size=2**22, open_timeout=5) as ws:
                    print(f"[danmaku] 已连接上游采集器 {self.url}")
                    self._set(connected=True, last_error="")
                    delay = self.reconnect_delay  # 连上就重置退避
                    stop = threading.Event()
                    pinger = threading.Thread(target=self._keepalive, args=(ws, stop), daemon=True)
                    pinger.start()
                    try:
                        for raw in ws:
                            if self._stop.is_set():
                                break
                            if isinstance(raw, bytes):
                                continue
                            if raw.strip() in ("pong", ""):
                                continue
                            try:
                                payload = json.loads(raw)
                            except (json.JSONDecodeError, TypeError):
                                continue
                            if not isinstance(payload, dict):
                                continue
                            ev = parse(payload)
                            if ev is None:
                                continue
                            if ev.kind == KIND_STATUS:
                                self.on_status({"text": ev.text, **ev.extra})
                                continue
                            self._set(last_event_at=time.time())
                            yield Danmaku(user=ev.user, text=ev.text, kind=ev.kind, extra=ev.extra)
                    finally:
                        stop.set()
                        self._set(connected=False)
            except Exception as e:  # noqa: BLE001
                self._set(connected=False, last_error=str(e)[:160])
                # 指数退避：采集器没起来时不要每 5 秒刷一条日志（小磁盘机器会被写满）
                print(f"[danmaku] 连接断开：{e}，{delay:.0f}s 后重连")
                if self._stop.wait(delay):
                    break
                delay = min(delay * 2, self.max_reconnect_delay)

    def _keepalive(self, ws, stop: threading.Event) -> None:
        """douyinLive 需要文本 ping 保活；发送失败说明连接已断，交给主循环重连。"""
        while not stop.wait(self.ping_interval):
            try:
                ws.send("ping")
            except Exception:  # noqa: BLE001
                return

    def close(self) -> None:
        self._stop.set()