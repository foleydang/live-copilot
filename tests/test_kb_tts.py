"""知识库编译与 TTS 缓存测试。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.kb_store import compile_faqs, compile_tone, kb_digest  # noqa: E402
from src.voice.tts import CachedTTS, TTSBase  # noqa: E402

PROFILE = {
    "studio_name": "建水紫陶·老王工作室",
    "assistant_name": "小陶",
    "greeting": "欢迎新朋友，主播正在手工拉坯～",
    "shipping": "48小时内发货，韵达或顺丰",
    "custom": "支持定制刻字，周期15天",
    "fallback": "这个我请主播来讲哈～",
    "rules": ["不议价", "不私下交易"],
    "products": [
        {"name": "紫陶壶", "price": "380元"},
        {"name": "主人杯", "price": "120元"},
    ],
    "craft_qa": [{"q": "这个要烧几天", "a": "进窑要烧好几天"}],
}


def test_compile_faqs_covers_all_sections():
    faqs = compile_faqs(PROFILE)
    intents = {f["intent"] for f in faqs}
    assert {"greeting", "shipping", "custom", "price", "craft"} <= intents, f"缺少意图：{intents}"
    all_kws = [k for f in faqs if f["intent"] == "price" for k in f["keywords"]]
    assert "紫陶壶" in all_kws and "主人杯" in all_kws, "两个商品名都应成为关键词"
    assert "多少钱" in all_kws, "泛问价格的关键词要保留"
    price_answers = " ".join(f["answer"] for f in faqs if f["intent"] == "price")
    assert "380" in price_answers and "120" in price_answers, "价格必须进答案"
    craft = next(f for f in faqs if f["intent"] == "craft")
    assert craft["answer"] == "进窑要烧好几天"
    assert any(len(k) >= 2 for k in craft["keywords"]), "问句应切出实词当关键词"


def test_compile_faqs_per_product_answers():
    """每个商品单独一条 FAQ：问某个商品只答那个商品，不念整张价格表。

    合并成一条会导致：① 答案冗长；② 不同商品的问题因答案相同被防重复机制误判。
    """
    faqs = compile_faqs(PROFILE)
    price_faqs = [f for f in faqs if f["intent"] == "price"]
    assert len(price_faqs) >= 3, f"两个商品 + 一个泛问兜底，至少 3 条，实际 {len(price_faqs)}"

    from src.brain.local_rules import RuleBrain

    b = RuleBrain()
    b.set_faqs(faqs)
    a1 = b.decide("u", "主人杯多少钱").reply
    a2 = b.decide("u", "紫陶壶多少钱").reply
    assert "120" in a1 and "380" not in a1, f"问主人杯应只答主人杯价格，实际：{a1}"
    assert "380" in a2 and "120" not in a2, f"问紫陶壶应只答紫陶壶价格，实际：{a2}"
    # 泛问价格（没提商品名）→ 兜底那条，可以列多个
    a3 = b.decide("u", "这个怎么卖").reply
    assert a3 and a3 != a1 and a3 != a2, f"泛问应走兜底答案，实际：{a3}"


def test_compile_faqs_price_without_numbers():
    """店主只填商品名不填价格时，AI 应引导看小黄车而不是编价格。"""
    faqs = compile_faqs({**PROFILE, "products": [{"name": "紫陶壶", "price": ""}]})
    price = next(f for f in faqs if f["intent"] == "price")
    assert "小黄车" in price["answer"]
    assert "380" not in price["answer"]


def test_store_rules_take_effect_without_llm():
    """店主填的规矩必须在本地规则层也生效。

    缺陷：rules 原本只进 LLM 提示词，没配 API Key 时（走 RuleBrain）
    店主写的"不议价""不私下交易"完全不起作用，AI 会用通用话术回答砍价。
    """
    faqs = compile_faqs(PROFILE)
    from src.brain.local_rules import RuleBrain

    b = RuleBrain()
    b.set_faqs(faqs)
    bargain = b.decide("u", "能便宜点吗").reply
    assert "不议价" in bargain, f"应使用店主的不议价规矩，实际：{bargain}"
    assert "实价" in bargain or "不议价" in bargain

    faqs2 = compile_faqs({**PROFILE, "rules": ["不议价", "不私下交易，只走小黄车"]})
    b2 = RuleBrain()
    b2.set_faqs(faqs2)
    # 必须覆盖真实弹幕的变体写法："加我微信"不含子串"加微信"
    for variant in ("加我微信看货", "加微信", "能私下交易吗", "走薇信行不行"):
        r = b2.decide("u", variant)
        assert r.reply and ("私下" in r.reply or "小黄车" in r.reply), \
            f"「{variant}」应被拦下并回复，实际 intent={r.intent} reply={r.reply!r}"


def test_compile_faqs_empty_profile_is_safe():
    faqs = compile_faqs({})
    assert faqs == [], "空资料不应生成任何 FAQ（避免 AI 瞎答）"


def test_compile_tone_includes_rules_and_identity():
    tone = compile_tone(PROFILE)
    assert "小陶" in tone and "老王工作室" in tone
    assert "不议价" in tone and "不私下交易" in tone
    assert "60 字" in tone, "必须保留长度约束"
    assert "绝对化用语" in tone, "必须保留违禁约束"


def test_kb_digest_and_empty_hint():
    d = kb_digest(PROFILE)
    assert "紫陶壶=380元" in d and "发货" in d
    empty = kb_digest({})
    assert "转主播" in empty, "空资料时必须提示 AI 一律转主播"


def test_tts_cache_hits_and_saves_cost():
    class CountingTTS(TTSBase):
        voice = "test-voice"

        def __init__(self):
            self.calls = 0

        def synth(self, text):
            self.calls += 1
            return b"RIFF" + text.encode()

    inner = CountingTTS()
    with tempfile.TemporaryDirectory() as d:
        c = CachedTTS(inner, d)
        a1 = c.synth("价格在小黄车里")
        a2 = c.synth("价格在小黄车里")
        assert a1 == a2, "缓存必须返回同样的音频"
        assert inner.calls == 1, f"第二次应命中缓存，实际调了 {inner.calls} 次"
        c.synth("另一句话")
        assert inner.calls == 2
        st = c.stats()
        assert st["hits"] == 1 and st["miss"] == 2 and st["cached_files"] == 2, st
        # 新实例（模拟重启）仍应命中磁盘缓存
        inner2 = CountingTTS()
        c2 = CachedTTS(inner2, d)
        c2.synth("价格在小黄车里")
        assert inner2.calls == 0, "重启后应命中磁盘缓存，不再花钱合成"


def test_tts_cache_isolated_by_voice():
    """不同租户用不同音色时不能串缓存。"""
    class V(TTSBase):
        def __init__(self, voice):
            self.voice = voice
            self.calls = 0

        def synth(self, text):
            self.calls += 1
            return f"{self.voice}:{text}".encode()

    with tempfile.TemporaryDirectory() as d:
        a, b = V("voice-a"), V("voice-b")
        ca, cb = CachedTTS(a, d), CachedTTS(b, d)
        ra = ca.synth("同一句话")
        rb = cb.synth("同一句话")
        assert ra != rb, "不同音色必须各自合成"
        assert a.calls == 1 and b.calls == 1


def test_tts_prewarm_skips_existing():
    class Counting(TTSBase):
        voice = "pw"

        def __init__(self):
            self.calls = 0

        def synth(self, text):
            self.calls += 1
            return b"x" + text.encode()

    inner = Counting()
    with tempfile.TemporaryDirectory() as d:
        c = CachedTTS(inner, d)
        n = c.prewarm(["答案A", "答案B", "答案A", "  ", ""])
        assert n == 2, f"应只合成 2 条（去重+跳空），实际 {n}"
        assert inner.calls == 2
        assert c.prewarm(["答案A"]) == 0, "已缓存的不应重复合成"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
