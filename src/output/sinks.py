"""输出通道抽象。

两条通道（对应 README 的硬件接线）：
- assist  AI 助播语音：云端合成 → H5 页面播放 → 闲置手机 → 直播声卡 AUX → 混入直播流（观众听到）
- prompt  提词提醒：H5 页面大字显示（主播抬头一扫），不出声、不进流
"""
from __future__ import annotations

import asyncio
import base64
import queue
import threading
from typing import Any


class Sink:
    def emit(self, channel: str, text: str, wav: bytes | None) -> None:  # pragma: no cover
        raise NotImplementedError

    def emit_status(self, status: dict) -> None:
        """默认忽略状态消息；只有 WebSink 需要推给 H5。"""
        return None


class ConsoleSink(Sink):
    """本地调试：只打印，不发声。"""

    def emit(self, channel: str, text: str, wav: bytes | None) -> None:
        tag = "助播🔊" if channel == "assist" else "提词📋"
        extra = f"（{len(wav)}B 音频）" if wav else ""
        print(f"[{tag}] {text} {extra}")


class LocalAudioSink(Sink):
    """本地调试：sounddevice 直接播放（电脑上跑通全链路用）。"""

    def __init__(self, assist_device: str = "", prompt_device: str = ""):
        self.assist_device = assist_device
        self.prompt_device = prompt_device

    def emit(self, channel: str, text: str, wav: bytes | None) -> None:
        tag = "助播🔊" if channel == "assist" else "提词📋"
        if channel == "prompt" or not wav:
            print(f"[{tag}] {text}")
            return
        try:
            import io
            import wave

            import numpy as np
            import sounddevice as sd

            with wave.open(io.BytesIO(wav)) as w:
                sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
                frames = w.readframes(w.getnframes())
            if sw != 2:
                raise ValueError(f"仅支持 16bit WAV，实际 {sw * 8}bit")
            data = np.frombuffer(frames, dtype=np.int16)
            if ch > 1:
                data = data.reshape(-1, ch)
            sd.play(data, sr, device=self._resolve(self.assist_device))
            sd.wait()
        except Exception as e:  # noqa: BLE001
            print(f"[{tag}] 播放失败（{e}），降级打印：{text}")

    def _resolve(self, keyword: str):
        if not keyword:
            return None
        import sounddevice as sd

        for i, dev in enumerate(sd.query_devices()):
            if dev["max_output_channels"] > 0 and keyword.lower() in dev["name"].lower():
                return i
        return None


class WebSink(Sink):
    """云端模式：把消息广播给所有已连接的 H5 助播端（WebSocket）。

    音频以 base64 WAV 内嵌在消息里，H5 用 Audio 播放；
    没有音频时（mock 引擎）H5 用浏览器 Web Speech 兜底发声。
    """

    # 断线重连/换手机时回放最近多少条消息（含音频），让新设备立刻进入状态
    REPLAY = 8

    def __init__(self):
        self._clients: list[asyncio.Queue] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()
        self.history: list[dict[str, Any]] = []  # 供 H5 首次连接时回放最近状态

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def register(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        with self._lock:
            # 回放最近消息：手机锁屏重连后不会白屏，也能补听到刚落下的助播语音
            for msg in self.history[-self.REPLAY:]:
                try:
                    q.put_nowait(msg)
                except asyncio.QueueFull:
                    break
            self._clients.append(q)
        return q

    def unregister(self, q: asyncio.Queue) -> None:
        with self._lock:
            if q in self._clients:
                self._clients.remove(q)

    def emit(self, channel: str, text: str, wav: bytes | None) -> None:
        msg = {
            "type": "audio" if channel == "assist" else "prompt",
            "channel": channel,
            "text": text,
            "audio": base64.b64encode(wav).decode() if wav else None,
        }
        with self._lock:
            self.history.append(msg)
            self.history = self.history[-20:]
        self._broadcast(msg)

    def emit_status(self, status: dict) -> None:
        """直播间状态（未开播/已开播/房间号无效）。让店主在手机上就能看到采集是否正常，
        不用去翻服务器日志——这是"傻瓜化"的关键一环。"""
        self._broadcast({"type": "status", **status})

    def _broadcast(self, msg: dict) -> None:
        if not self._loop:
            print(f"[websink] 未绑定事件循环，丢弃：{str(msg)[:60]}")
            return
        with self._lock:
            clients = list(self._clients)
        for q in clients:
            self._loop.call_soon_threadsafe(_safe_put, q, msg)


def _safe_put(q: "asyncio.Queue", msg: dict) -> None:
    try:
        q.put_nowait(msg)
    except asyncio.QueueFull:
        pass


class QueueSink(Sink):
    """测试用：收集到内存队列。"""

    def __init__(self):
        self.q: "queue.Queue[dict]" = queue.Queue()

    def emit(self, channel: str, text: str, wav: bytes | None) -> None:
        self.q.put({"channel": channel, "text": text, "has_audio": bool(wav)})
