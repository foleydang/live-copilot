"""护栏与路由的单元测试（无外部依赖，直接 python tests/test_core.py 运行）。"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.brain.guard import Guard  # noqa: E402
from src.brain.local_rules import RuleBrain  # noqa: E402
from src.danmaku.base import Danmaku  # noqa: E402
from src.output.sinks import QueueSink  # noqa: E402
from src.pipeline import Pipeline  # noqa: E402
from src.voice.tts import MockTTS  # noqa: E402

CFG = {
    "room": {"name": "测试直播间"},
    "routing": {"assist_debounce_sec": 25, "prompt_dedupe_sec": 30, "max_reply_len": 60},
    "log": {"dir": "/tmp/live_copilot_test_sessions"},
}


def test_guard_blocks_banned_words():
    g = Guard(max_len=60)
    ok, hits = g.check("这个壶是最好的，全网最低")
    assert hits, "违禁词应被命中"
    assert "最" not in ok and "全网最低" not in ok, "命中后必须替换为兜底话术"
    ok2, hits2 = g.check("价格在小黄车里都有标哦")
    assert not hits2 and ok2.startswith("价格")


def test_guard_allows_negated_context():
    """回归：店主自己的规矩"不私下交易"曾被违禁词表拦掉，AI 无法替店主声明规矩。

    拒绝私下交易 ≠ 主动招揽私下交易，护栏必须能区分否定语境。
    """
    g = Guard(max_len=60)
    # 否定语境：放行
    for ok in ("不私下交易，只走小黄车哦", "本店不加微信，走小黄车下单更有保障", "请勿转账，只走平台"):
        reply, hits = g.check(ok)
        assert not hits, f"「{ok}」是拒绝语境，不该被拦，命中={hits}"
        assert reply == ok
    # 招揽语境：必须拦（注意护栏只检查 AI 的输出，所以用例必须是 AI 可能说出的话）
    for bad in ("可以加微信详聊", "私下转账更便宜", "加微信有优惠", "转账给我就行"):
        reply, hits = g.check(bad)
        assert hits, f"「{bad}」是招揽语境，必须拦下"
        assert reply != bad, "命中后必须替换为兜底话术"


def test_guard_blocks_overlength():
    g = Guard(max_len=10)
    ok, hits = g.check("这是一句超过十个字的回复内容")
    assert hits and "超长" in hits[0]


def test_rules_intents():
    b = RuleBrain()
    assert b.decide("u", "这个多少钱").intent == "price"
    assert b.decide("u", "发货要几天").intent == "shipping"
    assert b.decide("u", "可以定制刻字吗").need_host is True
    assert b.decide("u", "我想批量采购50把").need_host is True
    assert b.decide("u", "hhh").intent == "noise"
    assert b.decide("u", "").intent == "noise"


def test_faq_scoring_prefers_specific_over_generic():
    """回归：'烧窑要几天' 曾被宽泛词'几天'抢判为发货问题。

    FAQ 匹配必须按命中关键词总长度打分，而不是首个命中。
    """
    b = RuleBrain()
    assert b.decide("u", "烧窑要几天呀").intent == "craft", "烧窑属工艺问题，不是发货"
    assert b.decide("u", "发货要几天呀").intent == "shipping", "发货仍需正确命中"
    assert b.decide("u", "这个壶多少钱").intent == "price"


def test_pipeline_routes_and_logs():
    sink = QueueSink()
    # 每个用例独立留痕目录：文件名按秒生成，共用目录会串场
    pipe = Pipeline(CFG, RuleBrain(), MockTTS(), sink, log_dir=tempfile.mkdtemp())
    t0 = time.time()

    # 价格问题 → AI 助播出声
    action, spoken = pipe.handle(Danmaku("张三", "这个壶多少钱", t0))
    assert action == "assist" and spoken, f"应走助播通道，实际 {action}"

    # 25 秒内同类问题 → 转提词（防机械重复）
    action2, _ = pipe.handle(Danmaku("李四", "价格多少", t0 + 5))
    assert action2 == "prompt", f"重复意图应转提词，实际 {action2}"

    # 定制 → 直接转主播
    action3, _ = pipe.handle(Danmaku("王五", "能不能定制刻字", t0 + 6))
    assert action3 == "prompt"

    # 噪音 → 忽略
    action4, _ = pipe.handle(Danmaku("赵六", "hhh", t0 + 7))
    assert action4 == "ignore"

    assert pipe.log_path.exists(), "必须留痕（合规要求）"
    lines = pipe.log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 4, f"留痕条数应为 4，实际 {len(lines)}"
    assert pipe.stats["danmaku"] == 4


def test_guard_hit_falls_back_to_prompt():
    """违禁词命中时 AI 不得发声，必须转提词。"""
    class BadBrain:
        def decide(self, user, text):
            from src.brain.local_rules import Decision

            return Decision("price", False, "全网最低价，第一名的品质", 2, "llm")

    sink = QueueSink()
    pipe = Pipeline(CFG, BadBrain(), MockTTS(), sink, log_dir=tempfile.mkdtemp())
    action, spoken = pipe.handle(Danmaku("张三", "便宜点呗", time.time()))
    assert action == "prompt", f"违禁命中应转提词，实际 {action}"
    assert pipe.stats["guard_hit"] == 1
    msgs = []
    while not sink.q.empty():
        msgs.append(sink.q.get())
    assert all(m["channel"] != "assist" for m in msgs), "违禁命中时不得有助播出声"


def test_channels_platform_mutes_assist_voice():
    """合规：视频号默认不允许 AI 出声，助播必须降级为提词。"""
    cfg = {**CFG, "room": {"name": "视频号直播间", "platform": "channels"}}
    sink = QueueSink()
    pipe = Pipeline(cfg, RuleBrain(), MockTTS(), sink, log_dir=tempfile.mkdtemp())
    action, _ = pipe.handle(Danmaku("张三", "这个壶多少钱", time.time()))
    assert action == "prompt", f"视频号应默认静音，实际 {action}"
    assert pipe.assist_voice is False

    # 显式打开开关后应恢复出声（并打印风险告警）
    cfg2 = {**cfg, "output": {"assist_voice": True}}
    pipe2 = Pipeline(cfg2, RuleBrain(), MockTTS(), QueueSink(), log_dir=tempfile.mkdtemp())
    action2, _ = pipe2.handle(Danmaku("张三", "这个壶多少钱", time.time()))
    assert action2 == "assist", f"显式开启后应出声，实际 {action2}"


def test_douyin_platform_allows_assist_voice():
    cfg = {**CFG, "room": {"name": "抖音直播间", "platform": "douyin"}}
    pipe = Pipeline(cfg, RuleBrain(), MockTTS(), QueueSink(), log_dir=tempfile.mkdtemp())
    assert pipe.assist_voice is True
    action, _ = pipe.handle(Danmaku("张三", "这个壶多少钱", time.time()))
    assert action == "assist"


def test_low_value_repeat_is_dropped_not_escalated():
    """真实高人气直播间暴露的问题：寒暄类弹幕重复出现不该升级成提词刷屏。

    实测某真实直播间 335 条事件里 interaction 反复撞防重复窗口，
    全部升级成提词会把提词屏塞满"支持一下"，淹没真正的定制/批发问题。
    """
    pipe = Pipeline(CFG, RuleBrain(), MockTTS(), QueueSink(), log_dir=tempfile.mkdtemp())
    t0 = time.time()

    a1, _ = pipe.handle(Danmaku("甲", "主播加油，做得真好看", t0))
    a2, _ = pipe.handle(Danmaku("乙", "支持一下", t0 + 1))
    assert a1 == "assist", f"首次寒暄应出声，实际 {a1}"
    assert a2 == "ignore", f"重复寒暄应丢弃而非升级提词，实际 {a2}"

    # 高价值意图重复出现仍须提醒主播
    p1, _ = pipe.handle(Danmaku("丙", "这个多少钱", t0 + 2))
    p2, _ = pipe.handle(Danmaku("丁", "价格多少", t0 + 3))
    assert p1 == "assist", f"首次价格问题应出声，实际 {p1}"
    assert p2 == "prompt", f"重复价格问题应升级提词，实际 {p2}"


def test_different_answers_same_intent_not_debounced():
    """回归：防重复必须按「意图+答案」而不是只按意图。

    实测发现的缺陷：店里填了多个商品价格后，10 秒内第二个不同商品的价格问题
    被误判为机械重复而转给主播，AI 明明能答。
    """
    faqs = [
        {"keywords": ["大号紫陶壶"], "intent": "price", "answer": "大号紫陶壶 680 元"},
        {"keywords": ["主人杯"], "intent": "price", "answer": "主人杯 120 元"},
    ]
    brain = RuleBrain()
    brain.set_faqs(faqs)
    pipe = Pipeline(CFG, brain, MockTTS(), QueueSink(), log_dir=tempfile.mkdtemp())
    t0 = time.time()

    a1, s1 = pipe.handle(Danmaku("甲", "大号紫陶壶多少钱", t0))
    a2, s2 = pipe.handle(Danmaku("乙", "主人杯多少钱", t0 + 3))
    assert a1 == "assist" and "680" in s1, f"第一个商品价格应出声，实际 {a1}/{s1}"
    assert a2 == "assist" and "120" in s2, f"不同商品的不同答案也该出声，实际 {a2}/{s2}"

    # 完全相同的答案在窗口内重复 → 仍应防重复
    a3, _ = pipe.handle(Danmaku("丙", "大号紫陶壶多少钱", t0 + 6))
    assert a3 == "prompt", f"相同答案重复应转提词，实际 {a3}"


def test_suppressed_duplicates_still_counted():
    """回归：提词去重曾把重复问题记成 ignore，导致复盘看不到知识库缺口。

    10 个人问同一个 AI 答不上来的问题，报告必须显示 10 次，而不是一次都没有。
    去重只应抑制"再次弹提词打扰主播"，不能抑制计数。
    """
    import json as _json

    sink = QueueSink()
    pipe = Pipeline(CFG, RuleBrain(), MockTTS(), sink, log_dir=tempfile.mkdtemp())
    t0 = time.time()
    actions = [pipe.handle(Danmaku(f"观众{i}", "能定制刻字吗", t0 + i))[0] for i in range(4)]

    assert actions[0] == "prompt", f"首次应弹提词，实际 {actions[0]}"
    assert all(a == "prompt_dup" for a in actions[1:]), f"重复应被抑制但标记为 prompt_dup，实际 {actions}"
    assert pipe.stats["prompt"] == 4, f"4 次都必须计入转主播，实际 {pipe.stats['prompt']}"
    assert pipe.stats["suppressed"] == 3
    assert len(pipe.pending_host) == 1, "提词清单只该有 1 条，不能刷屏"

    #  emitted 到 H5 的只有一条
    emitted = []
    while not sink.q.empty():
        emitted.append(sink.q.get())
    assert len(emitted) == 1, f"只应推 1 条到 H5，实际 {len(emitted)}"

    # 留痕里 4 条都在，且 suppressed 标记正确 → 复盘能算出缺口
    rows = [_json.loads(l) for l in pipe.log_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(rows) == 4
    assert sum(1 for r in rows if r["action"] == "prompt") == 4
    assert sum(1 for r in rows if r["suppressed"]) == 3


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
