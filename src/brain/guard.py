"""输出护栏：违禁词过滤 + 长度限制。

必须支持否定语境：店主自己填的规矩常是"不私下交易""不加微信"，
若一律拦截，AI 就没法替店主声明规矩了——拒绝私下交易和主动招揽是两回事。

长度超限不再整条丢弃：直播里 AI 不发声比说短一点更糟。
按标点截到 60 字以内，实在没有标点才硬截 + 省略号。
"""
from __future__ import annotations

from ..config import load_banned_words

_FALLBACK = "这个问题我请主播来回答哈，稍一下～"

# 命中词前 4 字内出现这些标记，视为"拒绝/禁止"语境，放行
_NEGATIONS = ("不", "别", "禁", "勿", "拒", "无法", "不能", "不要", "谢绝", "请勿", "严禁")
_WINDOW = 4
_BREAKS = "。！？!?；;，,、…"


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    head = text[: max_len - 1]
    cut = max(head.rfind(ch) for ch in _BREAKS)
    if cut >= max(6, max_len // 3):
        return head[: cut + 1]
    return head + "…"


class Guard:
    def __init__(self, max_len: int = 60):
        self.max_len = max_len
        self.banned = [w for w in load_banned_words() if w]

    def _is_negated(self, reply: str, idx: int) -> bool:
        prefix = reply[max(0, idx - _WINDOW):idx]
        return any(n in prefix for n in _NEGATIONS)

    def find_hits(self, reply: str) -> list[str]:
        hits = []
        for w in self.banned:
            idx = reply.find(w)
            while idx != -1:
                if not self._is_negated(reply, idx):
                    hits.append(w)
                    break
                idx = reply.find(w, idx + len(w))
        return hits

    def check(self, reply: str) -> tuple[str, list[str]]:
        """返回 (可用回复, 命中的违禁词列表)。违禁词命中时返回兜底话术。"""
        hits = self.find_hits(reply)
        if hits:
            return _FALLBACK, hits
        return _truncate(reply.strip(), self.max_len), []