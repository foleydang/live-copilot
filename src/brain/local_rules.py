"""本地关键词规则层：无 LLM key 时的兜底决策，也用于 LLM 失败时降级。"""
from __future__ import annotations

from dataclasses import dataclass

from ..config import load_faqs

ANSWERABLE = {"price", "shipping", "product", "craft", "greeting", "interaction"}

_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("cooperation", ("合作", "批发", "代理", "供货", "大量", "采购", "订货")),
    ("custom", ("定制", "订制", "刻字", "logo", "私人", "来图")),
    ("price", ("多少钱", "价格", "怎么卖", "贵不贵", "优惠", "便宜", "几块")),
    ("shipping", ("发货", "几天", "快递", "邮费", "包邮", "物流", "什么时候到")),
    ("greeting", ("你好", "来了", "晚上好", "大家好", "下午好", "早上好", "主播好")),
    ("interaction", ("好看", "漂亮", "厉害", "支持", "加油", "666", "点赞", "解压")),
    ("craft", ("紫陶", "拉坯", "阴刻", "阳填", "窑", "烧", "泥料", "建水", "工艺", "手工")),
]

_NOISE_REPLIES = {"hhh", "哈哈", "哈哈哈哈", "666666", "。。。"}


@dataclass
class Decision:
    intent: str
    need_host: bool
    reply: str
    priority: int  # 1=高（转主播），2=中，3=低
    via: str  # llm | rules


class RuleBrain:
    """FAQ 关键词匹配 + 意图规则。永远可用，速度零延迟。"""

    def __init__(self, room_name: str = "直播间", faqs: list[dict] | None = None):
        self.room_name = room_name
        self.faqs = list(faqs) if faqs is not None else load_faqs()

    def set_faqs(self, faqs: list[dict]) -> None:
        """知识库热更新：店主在手机上改完立即生效，无需重启服务。"""
        self.faqs = [f for f in (faqs or []) if f.get("keywords")]

    def decide(self, user: str, text: str) -> Decision:
        text = (text or "").strip()
        if not text or len(text) < 2 or text.lower() in _NOISE_REPLIES:
            return Decision("noise", False, "", 3, "rules")

        # FAQ 匹配按"命中关键词总长度"打分，取最高分。
        # 不能用首个命中：宽泛词（如"几天"）会抢在更具体的条目（如"烧窑"）前面。
        best, best_score = None, 0
        for faq in self.faqs:
            score = sum(len(kw) for kw in faq.get("keywords", []) if kw and kw in text)
            if score > best_score:
                best, best_score = faq, score
        if best:
            return Decision(best.get("intent", "product"), False, best["answer"], 2, "rules")

        for intent, kws in _RULES:
            if any(kw in text for kw in kws):
                if intent in ("cooperation", "custom"):
                    return Decision(intent, True, "", 1, "rules")
                if intent in ("price", "shipping"):
                    return Decision(intent, False, "价格和发货信息在小黄车里都有标哦，可以点开看看～", 2, "rules")
                if intent == "greeting":
                    return Decision(intent, False, "欢迎新朋友，主播正在手工制陶，有问题尽管打在公屏上～", 3, "rules")
                if intent == "interaction":
                    return Decision(intent, False, "谢谢支持！喜欢的话点个小红心～", 3, "rules")
                return Decision(intent, True, "", 2, "rules")  # craft 细节转主播

        # 问句但没命中 → 转主播
        if any(m in text for m in ("?", "？", "吗", "呢", "怎么", "能不能", "有没有")):
            return Decision("other", True, "", 2, "rules")
        return Decision("noise", False, "", 3, "rules")
