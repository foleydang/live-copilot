"""Mock 弹幕源：从文本文件或 stdin 读取，格式 `用户|弹幕` 或纯弹幕。

用途：离线调试整条管线、演示。正式接入见 ws_source.py。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Iterator

from .base import Danmaku, DanmakuSource


class MockSource(DanmakuSource):
    def __init__(self, file: str | Path | None = None, delay: float = 0.0):
        self.file = Path(file) if file else None
        self.delay = delay

    def events(self) -> Iterator[Danmaku]:
        if self.file:
            lines = self.file.read_text(encoding="utf-8").splitlines()
        else:
            lines = sys.stdin.read().splitlines()
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "|" in line:
                user, text = line.split("|", 1)
            else:
                user, text = "观众", line
            yield Danmaku(user=user.strip(), text=text.strip())
            if self.delay:
                time.sleep(self.delay)
