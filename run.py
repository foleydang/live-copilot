#!/usr/bin/env python3
"""本地调试入口（不启服务，直接在终端跑通管线）。

  python run.py --dry-run                       # 用 sample_danmaku.txt 跑通全链路
  python run.py --source ws                     # 连 douyinLive / BarrageGrab
  python run.py --audio                         # 本机发声（需 sounddevice + 音频设备）
  python -m src.replay sessions/xxx.jsonl       # 场次复盘

正式直播请用 server.py（云端 + H5 助播端）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.config import load_config  # noqa: E402
from src.danmaku.mock_source import MockSource  # noqa: E402
from src.danmaku.ws_source import WsSource  # noqa: E402
from src.output.sinks import ConsoleSink, LocalAudioSink  # noqa: E402
from src.pipeline import Pipeline  # noqa: E402
from src.voice.tts import make_tts  # noqa: E402


def build_brain(cfg: dict):
    room = cfg["room"]["name"]
    llm_cfg = cfg.get("llm", {})
    from src.brain.local_rules import RuleBrain

    if llm_cfg.get("enabled", True):
        try:
            from src.brain.llm_client import LlmBrain

            brain = LlmBrain(room, llm_cfg, max_len=int(cfg.get("routing", {}).get("max_reply_len", 60)))
            if brain.api_key:
                print(f"[brain] LLM 已启用：{llm_cfg.get('model')}")
                return brain
            print("[brain] 未配置 LLM API key，使用本地关键词规则")
        except Exception as e:  # noqa: BLE001  依赖缺失/初始化失败都不应阻断直播
            print(f"[brain] LLM 不可用（{e}），降级为本地关键词规则")
    return RuleBrain(room)


def main() -> int:
    ap = argparse.ArgumentParser(description="AI 助播 POC · 本地调试")
    ap.add_argument("--config", default=None)
    ap.add_argument("--source", choices=["mock", "ws"])
    ap.add_argument("--mock-file", default="sample_danmaku.txt")
    ap.add_argument("--ws-url", default=None)
    ap.add_argument("--dry-run", action="store_true", help="TTS 用 mock 引擎，不发声")
    ap.add_argument("--audio", action="store_true", help="本机真实发声（调试音频链路）")
    args = ap.parse_args()

    cfg = load_config(args.config)
    tts_cfg = dict(cfg.get("tts", {}))
    if args.dry_run or not args.audio:
        tts_cfg["engine"] = "mock"

    sink = LocalAudioSink(tts_cfg.get("assist_device", ""), tts_cfg.get("prompt_device", "")) if args.audio else ConsoleSink()
    pipe = Pipeline(cfg, build_brain(cfg), make_tts(tts_cfg), sink, log_dir=cfg.get("log", {}).get("dir", "sessions"))

    source_kind = args.source or cfg.get("source", "mock")
    print(f"=== AI 助播 POC ｜ {cfg['room']['name']} ｜ 源：{source_kind} ===")
    if cfg.get("disclosure", {}).get("enabled", True):
        pipe.announce(cfg["disclosure"]["text"])

    src = WsSource(args.ws_url or cfg["ws_url"]) if source_kind == "ws" else MockSource(args.mock_file)
    try:
        for dm in src.events():
            print(f"[弹幕] {dm.user}：{dm.text}")
            pipe.handle(dm)
    except KeyboardInterrupt:
        pass
    finally:
        print("\n=== 场次结束 ===")
        print(pipe.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
