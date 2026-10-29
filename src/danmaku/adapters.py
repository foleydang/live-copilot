"""上游弹幕采集器的消息格式适配。

已按源码/官方文档核实两种真实格式：

1. douyinLive (https://github.com/jwwsjlm/douyinLive)
   连接：ws://127.0.0.1:1088/ws/{直播间ID}
   系统消息：{"type":"system","event":"live_status","code":"ROOM_OFFLINE","live":false,...}
   业务消息：{"method":"WebcastChatMessage","user":{"nickname":"..."},"content":"..."}
             {"method":"WebcastGiftMessage","user":{...},"gift":{"name":"..."}}
             {"method":"WebcastLikeMessage","user":{...}}
             {"method":"WebcastMemberMessage","user":{...}}   # 进场
             {"method":"WebcastSocialMessage","user":{...}}   # 关注/分享
   客户端需每 30s 发文本 "ping"，服务回 "pong"。

2. BarrageGrab (https://gitee.com/DonkeySmall/BarrageGrab)
   连接：ws://127.0.0.1:8888
   弹幕：{"Type":3,"Data":{"MsgId":"...","User":{"NickName":"..."},"Content":"..."}}
"""
from __future__ import annotations

from dataclasses import dataclass, field

KIND_CHAT = "chat"
KIND_GIFT = "gift"
KIND_LIKE = "like"
KIND_ENTER = "enter"
KIND_FOLLOW = "follow"
KIND_STATUS = "status"

# douyinLive method → 内部事件类型
_DY_METHOD = {
    "WebcastChatMessage": KIND_CHAT,
    "WebcastEmojiChatMessage": KIND_CHAT,
    "WebcastGiftMessage": KIND_GIFT,
    "WebcastLikeMessage": KIND_LIKE,
    "WebcastMemberMessage": KIND_ENTER,
    "WebcastSocialMessage": KIND_FOLLOW,
    "WebcastFansclubMessage": KIND_FOLLOW,
}

# BarrageGrab Type → 内部事件类型（3=弹幕，其余常见类型按礼物/点赞/进场处理）
_BG_TYPE = {1: KIND_GIFT, 2: KIND_LIKE, 3: KIND_CHAT, 4: KIND_ENTER, 5: KIND_FOLLOW}

_NICK_KEYS = ("nickname", "nickName", "nick_name", "name", "NickName", "nicknameStr")


@dataclass
class RawEvent:
    kind: str
    user: str = "观众"
    text: str = ""
    extra: dict = field(default_factory=dict)


def _nick(user_obj) -> str:
    if isinstance(user_obj, dict):
        for k in _NICK_KEYS:
            v = user_obj.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return "观众"


def detect_format(payload: dict) -> str:
    if payload.get("type") == "system" or "method" in payload:
        return "douyinlive"
    if "Type" in payload or "Data" in payload:
        return "barragegrab"
    return "generic"


def parse(payload: dict) -> RawEvent | None:
    """把上游消息解析成统一事件；无法识别时返回 None。"""
    fmt = detect_format(payload)

    if fmt == "douyinlive":
        if payload.get("type") == "system":
            return RawEvent(
                kind=KIND_STATUS,
                text=str(payload.get("message") or payload.get("status_text") or ""),
                extra={
                    "code": payload.get("code"),
                    "live": payload.get("live"),
                    "room": payload.get("live_name") or "",
                    "retry": payload.get("retry_interval_seconds"),
                },
            )
        method = payload.get("method")
        kind = _DY_METHOD.get(method)
        if not kind:
            return None  # WebcastControlMessage / RoomStats 等无需处理
        user = _nick(payload.get("user"))
        if kind == KIND_CHAT:
            text = str(payload.get("content") or "").strip()
            return RawEvent(KIND_CHAT, user, text) if text else None
        if kind == KIND_GIFT:
            gift = payload.get("gift") or {}
            name = str(gift.get("name") or "礼物").strip()
            count = payload.get("count") or gift.get("count") or 1
            return RawEvent(KIND_GIFT, user, "", {"gift": name, "count": count})
        return RawEvent(kind, user)

    if fmt == "barragegrab":
        data = payload.get("Data") or payload.get("data") or {}
        t = payload.get("Type", payload.get("type"))
        kind = _BG_TYPE.get(t, KIND_CHAT)
        user = _nick(data.get("User") or data.get("user"))
        if kind == KIND_CHAT:
            text = str(data.get("Content") or data.get("content") or "").strip()
            return RawEvent(KIND_CHAT, user, text) if text else None
        if kind == KIND_GIFT:
            g = data.get("Gift") or data.get("gift") or {}
            return RawEvent(KIND_GIFT, user, "", {"gift": str(g.get("Name") or g.get("name") or "礼物")})
        return RawEvent(kind, user)

    # generic：字段名兼容抽取（对接未知采集器时的兜底）
    text = ""
    for k in ("text", "content", "msg", "message", "danmu", "chat"):
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            text = v.strip()
            break
    if not text:
        return None
    user = "观众"
    for k in ("user", "username", "user_name", "nickname", "uname", "nick", "name"):
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            user = v.strip()
            break
        if isinstance(v, dict):
            user = _nick(v)
            break
    return RawEvent(KIND_CHAT, user, text)
