"""效果度量测试。"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.analytics import analyze, render_text  # noqa: E402


def _write(rows: list[dict]) -> Path:
    p = Path(tempfile.mkdtemp()) / "s.jsonl"
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    return p


def _rec(action, intent="price", via="llm", text="这个多少钱", kind="chat", ms=450, user="观众", hit=None):
    return {
        "ts": time.time(), "user": user, "danmaku": text, "kind": kind, "intent": intent,
        "need_host": action == "prompt", "via": via, "action": action,
        "spoken": "答案" if action == "assist" else "", "guard_hit": hit or [], "decide_ms": ms,
    }


def test_empty_session():
    a = analyze(_write([]))
    assert a["empty"] is True
    assert "无数据" in render_text(a)


def test_coverage_rate():
    rows = [_rec("assist"), _rec("assist", text="多少钱啊"), _rec("prompt", intent="custom", text="能定制吗")]
    a = analyze(_write(rows))
    cov = a["coverage"]
    assert cov["ai_answered"] == 2 and cov["needs_reply"] == 3
    assert cov["ai_rate_pct"] == 66.7, f"承接率应为 66.7%，实际 {cov['ai_rate_pct']}"
    assert a["counts"]["total"] == 3


def test_latency_quantiles_only_count_llm():
    """本地规则是 0ms，混进来会把延迟指标拉得失真，必须只统计 llm。"""
    rows = [_rec("assist", via="llm", ms=400), _rec("assist", via="llm", ms=500, text="多少钱"),
            _rec("assist", via="llm", ms=900, text="价格"), _rec("assist", via="rules", ms=0, text="咋卖")]
    a = analyze(_write(rows))
    lat = a["latency_ms"]
    assert lat["samples"] == 3, f"只应统计 3 条 llm 样本，实际 {lat['samples']}"
    assert lat["llm_median"] == 500
    assert lat["llm_max"] == 900


def test_kb_gaps_only_repeated():
    """知识库缺口只列重复 2 次以上的，避免一次性问题刷屏。"""
    rows = [
        _rec("prompt", intent="craft", text="釉色怎么弄的"),
        _rec("prompt", intent="craft", text="釉色怎么弄的", user="乙"),
        _rec("prompt", intent="other", text="只出现一次的问题"),
    ]
    a = analyze(_write(rows))
    gaps = a["kb_gaps"]
    assert len(gaps) == 1, f"只应列出重复的那类，实际 {gaps}"
    assert gaps[0]["count"] == 2 and "釉色" in gaps[0]["text"]
    assert a["kb_gap_total"] == 2, "总数应含只出现一次的"


def test_social_and_guard_counted():
    rows = [
        _rec("assist", kind="gift", text="", intent="gift"),
        _rec("ignore", kind="like", text="", intent="like"),
        _rec("prompt", text="全网最低价", hit=["全网最低"]),
    ]
    a = analyze(_write(rows))
    assert a["counts"]["social"] == 2
    assert a["counts"]["guard_hit"] == 1
    assert a["social"].get("gift") == 1


def test_external_slot_reserved_for_compass():
    """罗盘数据槽位必须存在且为空，将来按 session_id 拼接。"""
    a = analyze(_write([_rec("assist")]))
    ext = a["external"]
    assert ext["gmv"] is None and ext["conversion_rate"] is None
    assert ext["session_id"] == a["session"]
    assert "服务商资质" in ext["note"]


def test_render_text_readable():
    # 需要重复问题才会出现"知识库缺口"段落（一次性问题不列，避免刷屏）
    rows = [_rec("assist"), _rec("prompt", intent="custom", text="能定制吗"),
            _rec("prompt", intent="custom", text="能定制吗", user="乙")]
    txt = render_text(analyze(_write(rows)))
    assert "AI 承接率" in txt and "决策延迟" in txt and "知识库缺口" in txt
    assert "能定制吗" in txt, "缺口清单应列出具体问题"


def test_render_text_omits_gap_section_when_none():
    """没有重复缺口时不该出现空段落，避免误导店主以为漏读了。"""
    txt = render_text(analyze(_write([_rec("assist"), _rec("prompt", text="只问过一次的问题")])))
    assert "知识库缺口" not in txt


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
