"""效果度量：从留痕 JSONL 算出这一场 AI 助播到底干了什么、值不值。

设计原则：
1. **只用自有数据**，不依赖平台授权——抖音电商罗盘的 GMV/转化需要服务商资质+商家授权，
   POC 阶段拿不到。所以这里算的是"AI 承接了多少工作、答得好不好、哪里还答不上来"。
2. 输出结构预留 `external` 字段，将来能把罗盘导出的人工数据（停留时长、转化率、GMV）
   按 session_id 拼进来做因果对照。
3. 知识库缺口是核心产出：转主播的问题就是店主下一批该填进设置页的内容。
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

# 归一化问题文本，用于聚类找重复问题
_STOP = re.compile(r"[，。？?！!~～\s、啊呀吧呢嘛哦哈]+")


def _norm(text: str) -> str:
    return _STOP.sub("", (text or "").lower())[:24]


def _pct(a: int, b: int) -> float:
    return round(a * 100.0 / b, 1) if b else 0.0


def _quantile(vals: list, q: float) -> int:
    if not vals:
        return 0
    s = sorted(vals)
    return int(s[min(len(s) - 1, int(len(s) * q))])


def load_records(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def analyze(path: str | Path, top_n: int = 12) -> dict:
    rows = load_records(path)
    if not rows:
        return {"empty": True, "session": Path(path).name}

    chat = [r for r in rows if r.get("kind", "chat") == "chat"]
    social = [r for r in rows if r.get("kind", "chat") != "chat"]
    assist = [r for r in chat if r["action"] == "assist"]
    prompt = [r for r in chat if r["action"] == "prompt"]
    ignore = [r for r in chat if r["action"] == "ignore"]

    # 决策延迟（只有走 LLM 的才有意义，本地规则是 0ms）
    llm_ms = [r["decide_ms"] for r in chat if r.get("decide_ms") and r.get("via") == "llm"]
    all_ms = [r["decide_ms"] for r in chat if r.get("decide_ms")]

    # 知识库缺口：转主播的问题按归一化文本聚类
    gaps = Counter(_norm(r["danmaku"]) for r in prompt if r.get("danmaku"))
    gap_samples: dict[str, str] = {}
    for r in prompt:
        k = _norm(r["danmaku"])
        if k and k not in gap_samples:
            gap_samples[k] = r["danmaku"]

    # 高频被 AI 回答的问题（说明这些是真正的热点，值得写进商品详情）
    hot = Counter(_norm(r["danmaku"]) for r in assist if r.get("danmaku"))
    hot_samples: dict[str, str] = {}
    for r in assist:
        k = _norm(r["danmaku"])
        if k and k not in hot_samples:
            hot_samples[k] = r["danmaku"]

    # 时段分布（按小时）
    hours = Counter()
    for r in rows:
        try:
            import datetime as _dt

            hours[_dt.datetime.fromtimestamp(r["ts"]).hour] += 1
        except Exception:  # noqa: BLE001
            pass

    session = Path(path).name
    ts0, ts1 = rows[0].get("ts", 0), rows[-1].get("ts", 0)
    minutes = round((ts1 - ts0) / 60.0, 1) if ts1 > ts0 else 0.0

    # 主播负担：AI 承接的条数 = 主播少说的话
    answered = len(assist) + len([r for r in social if r["action"] == "assist"])
    total_need_reply = len(assist) + len(prompt) + len([r for r in social if r["action"] != "ignore"])

    return {
        "empty": False,
        "session": session,
        "duration_min": minutes,
        "counts": {
            "total": len(rows),
            "chat": len(chat),
            "social": len(social),
            "assist": len(assist),
            "prompt": len(prompt),
            "ignore": len(ignore),
            "guard_hit": sum(1 for r in rows if r.get("guard_hit")),
        },
        # 核心指标：AI 承接率（对外销售时最有说服力的数字）
        "coverage": {
            "ai_answered": answered,
            "needs_reply": total_need_reply,
            "ai_rate_pct": _pct(answered, total_need_reply),
            "chat_assist_rate_pct": _pct(len(assist), len(chat)),
            "noise_rate_pct": _pct(len(ignore), len(chat)),
        },
        "latency_ms": {
            "llm_median": _quantile(llm_ms, 0.5),
            "llm_p90": _quantile(llm_ms, 0.9),
            "llm_max": max(llm_ms) if llm_ms else 0,
            "all_median": _quantile(all_ms, 0.5),
            "samples": len(llm_ms),
        },
        "via": dict(Counter(r.get("via", "?") for r in chat)),
        "intents": dict(Counter(r["intent"] for r in chat).most_common()),
        "social": dict(Counter(r.get("kind") for r in social if r.get("kind") != "chat")),
        "hours": {str(k): v for k, v in sorted(hours.items())},
        # 知识库缺口：出现 2 次以上、AI 答不了的问题 → 建议店主补进设置页
        "kb_gaps": [
            {"text": gap_samples[k], "count": c}
            for k, c in gaps.most_common(top_n)
            if c >= 2
        ],
        "kb_gap_total": len(gaps),
        "hot_questions": [
            {"text": hot_samples[k], "count": c} for k, c in hot.most_common(top_n)
        ],
        # 将来接抖音电商罗盘：按 session_id 拼接人工导出的转化数据
        "external": {
            "session_id": session,
            "note": "罗盘 GMV/停留/转化需服务商资质+商家授权，当前为空；导出后按 session_id 拼接",
            "gmv": None,
            "avg_watch_sec": None,
            "conversion_rate": None,
        },
    }


def render_text(a: dict) -> str:
    """给人看的文字版复盘。"""
    if a.get("empty"):
        return "（本场无数据）"
    c, cov, lat = a["counts"], a["coverage"], a["latency_ms"]
    lines = [
        f"场次 {a['session']}｜时长约 {a['duration_min']} 分钟",
        "",
        f"【AI 承接率】{cov['ai_rate_pct']}%  "
        f"（AI 答了 {cov['ai_answered']} 条 / 需要回应 {cov['needs_reply']} 条）",
        f"  弹幕总数 {c['total']}｜文字 {c['chat']}｜社交(礼物/关注/点赞) {c['social']}",
        f"  AI出声 {c['assist']}｜转主播 {c['prompt']}｜灌水忽略 {c['ignore']}（占文字弹幕 {cov['noise_rate_pct']}%）",
        f"  违禁拦截 {c['guard_hit']} 次",
        "",
        f"【决策延迟】中位 {lat['llm_median']}ms｜P90 {lat['llm_p90']}ms｜最慢 {lat['llm_max']}ms（样本 {lat['samples']}）",
        f"  决策来源：{a['via']}",
        "",
        f"【观众在问什么】{a['intents']}",
    ]
    if a["social"]:
        lines.append(f"【社交消息】{a['social']}")
    if a["kb_gaps"]:
        lines += ["", "【知识库缺口 · 建议补进手机设置页】"]
        lines += [f"  {g['count']}次  {g['text']}" for g in a["kb_gaps"]]
        lines.append(f"  （共 {a['kb_gap_total']} 类未覆盖问题，只列出重复 2 次以上的）")
    if a["hot_questions"]:
        lines += ["", "【高频问题 · 可考虑写进商品详情或话术】"]
        lines += [f"  {q['count']}次  {q['text']}" for q in a["hot_questions"][:8]]
    if a["hours"]:
        lines += ["", f"【时段分布】{a['hours']}"]
    return "\n".join(lines)
