#!/usr/bin/env python3
"""抖音弹幕采集器模拟器。

用途：在没有真实直播间、也不便跑逆向采集器时，**按 douyinLive / BarrageGrab 的真实
输出格式**造一条弹幕流，用来验证 adapters + ws_source + pipeline + H5 的整条链路。

格式来源（已核实）：
- douyinLive  ws://127.0.0.1:1088/ws/{room}
  系统消息 {"type":"system","event":"live_status","code":"ROOM_ONLINE","live":true,...}
  业务消息 {"method":"WebcastChatMessage","user":{"nickname":".."},"content":".."}
  客户端发文本 "ping"，服务回 "pong"
- BarrageGrab ws://127.0.0.1:8888
  弹幕 {"Type":3,"Data":{"MsgId":"..","User":{"NickName":".."},"Content":".."}}

用法：
  python tools/douyin_sim.py                       # douyinLive 格式，:1088
  python tools/douyin_sim.py --format barragegrab  # BarrageGrab 格式，:8888
  python tools/douyin_sim.py --interval 0.4 --loop # 加快节奏、循环播放
"""
from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from websockets.asyncio.server import serve
except ImportError:  # websockets < 13
    from websockets import serve  # type: ignore

GIFTS = ["小心心", "玫瑰花", "棒棒糖", "抖币", "紫陶壶（虚拟）"]
VIEWERS = ["陶友阿明", "茶客老李", "小美", "阿凯", "收藏家张", "朵朵", "小李", "王姐", "路过的风", "建水小杨"]


def load_chat_lines(path: Path) -> list[str]:
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        lines.append(raw.split("|", 1)[1].strip() if "|" in raw else raw)
    return [l for l in lines if l]


def dy_chat(user: str, text: str) -> dict:
    return {"method": "WebcastChatMessage", "user": {"nickname": user}, "content": text}


def dy_gift(user: str, gift: str) -> dict:
    return {
        "method": "WebcastGiftMessage",
        "user": {"nickname": user},
        "gift": {"name": gift},
        "count": random.randint(1, 5),
    }


def dy_simple(method: str, user: str) -> dict:
    return {"method": method, "user": {"nickname": user}}


def bg_chat(user: str, text: str) -> dict:
    return {
        "Type": 3,
        "Data": {"MsgId": str(random.randint(10**9, 10**10)), "User": {"NickName": user}, "Content": text},
    }


def build_script(fmt: str, chat_lines: list[str]) -> list[dict]:
    """按真实直播间的节奏混排：弹幕为主，穿插进场/点赞/礼物/关注。"""
    script: list[dict] = []
    cyc = itertools.cycle(chat_lines)
    for i in range(len(chat_lines) + 6):
        user = random.choice(VIEWERS)
        roll = random.random()
        if roll < 0.12:
            script.append(
                dy_simple("WebcastMemberMessage", user) if fmt == "douyinlive" else {"Type": 4, "Data": {"User": {"NickName": user}}}
            )
        elif roll < 0.24:
            script.append(
                dy_simple("WebcastLikeMessage", user) if fmt == "douyinlive" else {"Type": 2, "Data": {"User": {"NickName": user}}}
            )
        elif roll < 0.30:
            g = random.choice(GIFTS)
            script.append(
                dy_gift(user, g)
                if fmt == "douyinlive"
                else {"Type": 1, "Data": {"User": {"NickName": user}, "Gift": {"Name": g}}}
            )
        elif roll < 0.34:
            script.append(
                dy_simple("WebcastSocialMessage", user) if fmt == "douyinlive" else {"Type": 5, "Data": {"User": {"NickName": user}}}
            )
        else:
            text = next(cyc)
            script.append(dy_chat(user, text) if fmt == "douyinlive" else bg_chat(user, text))
    return script


async def handler(ws, fmt: str, script: list[dict], interval: float, loop: bool, room: str):
    peer = getattr(ws, "remote_address", None)
    print(f"[sim] 客户端接入 {peer} path={getattr(ws, 'path', '?')}")
    if fmt == "douyinlive":
        await ws.send(
            json.dumps(
                {
                    "type": "system",
                    "event": "live_status",
                    "code": "ROOM_ONLINE",
                    "valid": True,
                    "live": True,
                    "status": "online",
                    "status_text": "直播间已开播",
                    "room_id": room,
                    "live_name": "手工紫陶工作室",
                    "title": "建水紫陶·手工拉坯",
                    "message": "直播间已开播",
                    "retry_interval_seconds": 30,
                },
                ensure_ascii=False,
            )
        )

    async def pump():
        rounds = itertools.cycle(script) if loop else iter(script)
        for msg in rounds:
            await ws.send(json.dumps(msg, ensure_ascii=False))
            label = msg.get("method") or f"Type={msg.get('Type')}"
            detail = msg.get("content") or (msg.get("Data") or {}).get("Content") or ""
            print(f"[sim] → {label} {detail}")
            await asyncio.sleep(interval)
        print("[sim] 脚本播完")

    pump_task = asyncio.create_task(pump())
    try:
        async for raw in ws:  # 处理客户端 ping 保活
            if isinstance(raw, str) and raw.strip() == "ping":
                await ws.send("pong")
    finally:
        pump_task.cancel()


async def main_async(args) -> None:
    chat_lines = load_chat_lines(ROOT / args.script)
    script = build_script(args.format, chat_lines)
    print(f"[sim] 格式={args.format} 端口={args.port} 脚本 {len(script)} 条，间隔 {args.interval}s")
    print(f"[sim] 让服务端连：ws://127.0.0.1:{args.port}/ws/{args.room}")
    async with serve(
        lambda ws: handler(ws, args.format, script, args.interval, args.loop, args.room),
        "127.0.0.1",
        args.port,
    ):
        await asyncio.Future()


def main() -> int:
    ap = argparse.ArgumentParser(description="抖音弹幕采集器模拟器（douyinLive / BarrageGrab 格式）")
    ap.add_argument("--format", choices=["douyinlive", "barragegrab"], default="douyinlive")
    ap.add_argument("--port", type=int, default=None, help="默认 douyinlive=1088, barragegrab=8888")
    ap.add_argument("--room", default="516466932480")
    ap.add_argument("--interval", type=float, default=1.5, help="每条消息间隔秒数")
    ap.add_argument("--loop", action="store_true", help="循环播放脚本")
    ap.add_argument("--script", default="sample_danmaku.txt")
    args = ap.parse_args()
    if args.port is None:
        args.port = 1088 if args.format == "douyinlive" else 8888
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\n[sim] 已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
