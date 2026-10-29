"""配置与知识库加载。"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def load_config(path: str | Path | None = None) -> dict:
    p = Path(path) if path else ROOT / "config.yaml"
    with open(p, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_banned_words() -> list[str]:
    p = ROOT / "kb" / "banned_words.txt"
    return [
        w.strip()
        for w in p.read_text(encoding="utf-8").splitlines()
        if w.strip() and not w.strip().startswith("#")
    ]


def load_faqs() -> list[dict]:
    p = ROOT / "kb" / "faq.yaml"
    with open(p, "r", encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("faqs", [])


def load_tone() -> str:
    p = ROOT / "kb" / "tone.md"
    return p.read_text(encoding="utf-8")
