"""TTS：DashScope CosyVoice（返回 WAV bytes）+ 磁盘缓存 + mock 引擎。

缓存是成本与延迟的关键优化：FAQ 答案高度重复（就那十几条），
命中缓存后 TTS 费用为 0、延迟接近 0。实测某真实直播间 335 条事件里
价格/发货/工艺类问题反复出现，缓存命中率可达 80%+。

合规约束：固定使用平台预置音色，禁止声纹克隆。
"""
from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path


class TTSBase:
    voice = "default"

    def synth(self, text: str) -> bytes | None:  # pragma: no cover - 接口
        raise NotImplementedError

    def close(self) -> None:
        return None

    def cleanup(self, max_bytes: int = 512 * 1024 * 1024, max_files: int = 2000) -> int:
        """默认实现：什么也不做。CachedTTS 会按 mtime 清理最旧的缓存文件。"""
        return 0


class MockTTS(TTSBase):
    """不发声，打印文本。用于无 API key 的离线调试。"""

    voice = "mock"

    def synth(self, text: str) -> bytes | None:
        print(f"[tts:mock] {text}")
        return None


class CosyVoiceTTS(TTSBase):
    def __init__(self, model: str, voice: str, api_key: str):
        import dashscope  # 延迟导入
        from dashscope.audio.tts_v2 import AudioFormat, SpeechSynthesizer

        self._AudioFormat = AudioFormat
        dashscope.api_key = api_key
        self._cls = SpeechSynthesizer
        self.model = model
        self.voice = voice

    def synth(self, text: str) -> bytes | None:
        # WAV 16bit 单声道，便于标准库 wave + sounddevice 播放，不引入 ffmpeg
        fmt = getattr(
            self._AudioFormat,
            "WAV_24000HZ_MONO_16BIT",
            self._AudioFormat.WAV_22050HZ_MONO_16BIT,
        )
        s = self._cls(model=self.model, voice=self.voice, format=fmt)
        audio = s.call(text)
        if not audio:
            raise RuntimeError("TTS 返回空音频")
        return audio


class CachedTTS(TTSBase):
    """包一层磁盘缓存。key = voice + 文本，所以不同租户/音色不会串。"""

    def __init__(self, inner: TTSBase, cache_dir: str | Path, enabled: bool = True):
        self.inner = inner
        self.voice = inner.voice
        self.dir = Path(cache_dir)
        self.enabled = enabled
        self._lock = threading.Lock()
        self.hits = 0
        self.miss = 0
        if enabled:
            self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, text: str) -> Path:
        h = hashlib.sha256(f"{self.voice}|{text}".encode("utf-8")).hexdigest()[:32]
        return self.dir / f"{h}.wav"

    def synth(self, text: str) -> bytes | None:
        if not self.enabled:
            return self.inner.synth(text)
        p = self._path(text)
        if p.exists():
            try:
                data = p.read_bytes()
                if data:
                    self.hits += 1
                    return data
            except OSError:
                pass
        wav = self.inner.synth(text)
        if wav:
            self.miss += 1
            with self._lock:
                try:
                    p.write_bytes(wav)
                except OSError as e:
                    print(f"[tts] 缓存写入失败（不影响本次合成）：{e}")
        return wav

    def prewarm(self, texts) -> int:
        """开播前预合成 FAQ 答案，避免首次回答时卡顿。返回成功条数。"""
        n = 0
        for t in texts:
            t = (t or "").strip()
            if not t or self._path(t).exists():
                continue
            try:
                if self.synth(t):
                    n += 1
            except Exception as e:  # noqa: BLE001
                print(f"[tts] 预热失败：{e}")
                break
        return n

    def stats(self) -> dict:
        total = self.hits + self.miss
        return {
            "hits": self.hits,
            "miss": self.miss,
            "hit_rate": round(self.hits / total, 3) if total else 0.0,
            "cached_files": len(list(self.dir.glob("*.wav"))) if self.enabled else 0,
        }

    def cleanup(self, max_bytes: int = 512 * 1024 * 1024, max_files: int = 2000) -> int:
        """按 mtime 清理最旧的缓存文件，直到总大小/总条数低于上限。返回删除数。"""
        if not self.enabled:
            return 0
        files = sorted(self.dir.glob("*.wav"), key=lambda p: p.stat().st_mtime)
        if len(files) <= max_files:
            total = sum(f.stat().st_size for f in files)
            if total <= max_bytes:
                return 0
        removed = 0
        while files and (len(files) > max_files or sum(f.stat().st_size for f in files) > max_bytes):
            f = files.pop(0)
            try:
                f.unlink()
                removed += 1
            except OSError:
                continue
        return removed


def make_tts(cfg: dict, cache_dir: str | Path | None = None) -> TTSBase:
    engine = cfg.get("engine", "mock")
    inner: TTSBase
    if engine == "mock":
        inner = MockTTS()
    else:
        key = os.environ.get(cfg.get("api_key_env", "DASHSCOPE_API_KEY"), "")
        if not key:
            print("[tts] 未配置 API key，自动降级为 mock 引擎")
            inner = MockTTS()
        else:
            try:
                inner = CosyVoiceTTS(
                    cfg.get("model", "cosyvoice-v2"), cfg.get("voice", "longxiaochun"), key
                )
            except Exception as e:  # noqa: BLE001
                print(f"[tts] CosyVoice 初始化失败（{e}），降级 mock")
                inner = MockTTS()
    if cache_dir and cfg.get("cache", True) and not isinstance(inner, MockTTS):
        return CachedTTS(inner, cache_dir)
    return inner
