"""上游采集器消息格式适配的单元测试（样本均取自 douyinLive 文档与 BarrageGrab README）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.danmaku.adapters import (  # noqa: E402
    KIND_CHAT,
    KIND_ENTER,
    KIND_FOLLOW,
    KIND_GIFT,
    KIND_LIKE,
    KIND_STATUS,
    detect_format,
    parse,
)

# ---- douyinLive：docs/websocket-client.md 中的客户端契约 ----
DY_CHAT = {"method": "WebcastChatMessage", "user": {"nickname": "陶友阿明"}, "content": "这个壶多少钱"}
DY_GIFT = {"method": "WebcastGiftMessage", "user": {"nickname": "朵朵"}, "gift": {"name": "玫瑰花"}, "count": 3}
DY_LIKE = {"method": "WebcastLikeMessage", "user": {"nickname": "小美"}}
DY_MEMBER = {"method": "WebcastMemberMessage", "user": {"nickname": "路过的风"}}
DY_SOCIAL = {"method": "WebcastSocialMessage", "user": {"nickname": "阿凯"}}
DY_EMOJI = {"method": "WebcastEmojiChatMessage", "user": {"nickname": "小李"}, "content": "[赞]"}
DY_CONTROL = {"method": "WebcastControlMessage", "user": {"nickname": "x"}}
DY_STATUS_OFFLINE = {
    "type": "system",
    "event": "live_status",
    "code": "ROOM_OFFLINE",
    "valid": True,
    "live": False,
    "status": "offline",
    "status_text": "直播间未开播",
    "room_id": "32536162943",
    "live_name": "一只喵动漫",
    "message": "直播间当前未开播，服务端会保持连接并继续轮询",
    "retry_interval_seconds": 30,
}

# ---- BarrageGrab：README 中的示例结构 ----
BG_CHAT = {"Type": 3, "Data": {"MsgId": "7381234", "User": {"NickName": "茶客老李"}, "Content": "发货要几天"}}
BG_GIFT = {"Type": 1, "Data": {"User": {"NickName": "王姐"}, "Gift": {"Name": "小心心"}}}
BG_LIKE = {"Type": 2, "Data": {"User": {"NickName": "小周"}}}
BG_ENTER = {"Type": 4, "Data": {"User": {"NickName": "新朋友"}}}


def test_detect_formats():
    assert detect_format(DY_CHAT) == "douyinlive"
    assert detect_format(DY_STATUS_OFFLINE) == "douyinlive"
    assert detect_format(BG_CHAT) == "barragegrab"
    assert detect_format({"text": "随便一个字段", "user": "x"}) == "generic"


def test_douyinlive_chat():
    ev = parse(DY_CHAT)
    assert ev.kind == KIND_CHAT and ev.user == "陶友阿明" and ev.text == "这个壶多少钱"
    ev2 = parse(DY_EMOJI)
    assert ev2.kind == KIND_CHAT and ev2.text == "[赞]"


def test_douyinlive_social_kinds():
    assert parse(DY_GIFT).kind == KIND_GIFT
    assert parse(DY_GIFT).extra["gift"] == "玫瑰花"
    assert parse(DY_GIFT).extra["count"] == 3
    assert parse(DY_LIKE).kind == KIND_LIKE
    assert parse(DY_MEMBER).kind == KIND_ENTER
    assert parse(DY_SOCIAL).kind == KIND_FOLLOW


def test_douyinlive_status_is_not_danmaku():
    """未开播状态必须被识别为 status，绝不能当成弹幕喂给大脑。"""
    ev = parse(DY_STATUS_OFFLINE)
    assert ev.kind == KIND_STATUS
    assert ev.extra["live"] is False and ev.extra["code"] == "ROOM_OFFLINE"
    assert "未开播" in ev.text


def test_douyinlive_ignores_control_messages():
    assert parse(DY_CONTROL) is None, "控制消息无需处理"


def test_barragegrab_all_kinds():
    ev = parse(BG_CHAT)
    assert ev.kind == KIND_CHAT and ev.user == "茶客老李" and ev.text == "发货要几天"
    assert parse(BG_GIFT).kind == KIND_GIFT
    assert parse(BG_LIKE).kind == KIND_LIKE
    assert parse(BG_ENTER).kind == KIND_ENTER


def test_empty_and_garbage_payloads():
    assert parse({"method": "WebcastChatMessage", "user": {"nickname": "x"}, "content": "  "}) is None
    assert parse({"Type": 3, "Data": {"Content": ""}}) is None
    assert parse({}) is None
    assert parse({"foo": "bar"}) is None


def test_generic_fallback():
    ev = parse({"content": "这个多少钱", "nickname": "某观众"})
    assert ev.kind == KIND_CHAT and ev.text == "这个多少钱" and ev.user == "某观众"


def test_camelcase_nickname_tolerated():
    """protobuf JSON 可能输出 nickName 而非 nickname，必须都能取到。"""
    ev = parse({"method": "WebcastChatMessage", "user": {"nickName": "驼峰用户"}, "content": "你好"})
    assert ev.user == "驼峰用户"


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
