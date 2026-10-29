"""场次复盘：读取 jsonl 留痕，输出本场统计与需要主播跟进的问题清单。"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path


def summarize(path: str | Path) -> str:
    records = [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]
    if not records:
        return "（空场次）"
    intents = Counter(r["intent"] for r in records)
    actions = Counter(r["action"] for r in records)
    host_questions = [r for r in records if r["action"] == "ear"]
    lines = [
        f"场次：{Path(path).name}",
        f"弹幕总数：{len(records)}",
        f"意图分布：{dict(intents.most_common())}",
        f"处置分布：AI助播 {actions.get('assist', 0)} ｜ 转主播 {actions.get('ear', 0)} ｜ 忽略 {actions.get('ignore', 0)}",
        "",
        f"待主播跟进（{len(host_questions)} 条）：",
    ]
    for r in host_questions:
        lines.append(f"  [{r['user']}] {r['danmaku']}")
    return "\n".join(lines)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法：python -m src.replay <sessions/xxx.jsonl>")
        sys.exit(1)
    print(summarize(sys.argv[1]))
