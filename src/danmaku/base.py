"""弹幕事件与数据源抽象。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Iterator

KIND_CHAT = "chat"


@dataclass
class Danmaku:
    user: str
    text: str
    ts: float = field(default_factory=time.time)
    kind: str = KIND_CHAT  # chat|gift|like|enter|follow
    extra: dict = field(default_factory=dict)


class DanmakuSource:
    """所有数据源实现 events() 生成器。"""

    def events(self) -> Iterator[Danmaku]:  # pragma: no cover - 接口
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - 接口
        pass
