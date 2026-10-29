"""核心管线：弹幕 → 决策 → 护栏 → 防重复 → 路由（助播出声 / 提词 / 忽略） → 留痕。

设计原则：AI 只承接重复性问答与互动，工艺讲解、定制、合作一律转主播（提词）。

线程模型：每个房间由 Room 的单线程 worker 串行调用 handle()，
所以同一个房间内不会有并发（stats 不会丢数、提词不会乱序）；
仍保留内部锁，因为 /api/danmaku?wait=1 调试路径和 announce 线程可能并发进入。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

from .brain.guard import Guard
from .brain.local_rules import Decision
from .danmaku.base import Danmaku
from .output.sinks import Sink
from .voice.tts import TTSBase

ASSIST_INTENTS = {"price", "shipping", "product", "craft", "greeting", "interaction"}
# 重复出现时值得提醒主播的高价值意图。
# 寒暄类（greeting/interaction）重复出现只应丢弃：真实高人气直播间里这类弹幕极多，
# 全部升级成提词会把提词屏刷满垃圾，淹没真正需要主播回应的定制/批发问题。
ESCALATE_ON_REPEAT = {"price", "shipping", "product", "craft"}

_NORM_RE = re.compile(r"[，。？?！!~～\s、啊呀吧呢嘛哦哈]+")


def _norm(text: str) -> str:
    """归一化问题文本做去重键：去掉标点和语气词，'多少钱' 和 '多少钱？' 视为同一条。"""
    return _NORM_RE.sub("", (text or "").lower())[:32]


class Pipeline:
    def __init__(
        self,
        cfg: dict,
        brain,
        tts: TTSBase,
        sink: Sink,
        log_dir: str | Path = "sessions",
    ):
        self.cfg = cfg
        self.brain = brain
        self.tts = tts
        self.sink = sink
        r = cfg.get("routing", {})
        self.assist_debounce = float(r.get("assist_debounce_sec", 25))
        self.prompt_dedupe = float(r.get("prompt_dedupe_sec", 30))
        self.guard = Guard(max_len=int(r.get("max_reply_len", 60)))
        # 合规开关：视频号把"数字人/虚拟人直播"整体归为违规（非真实直播），
        # AI 出声存在被判定风险，因此该平台默认只走提词通道。
        platform = cfg.get("room", {}).get("platform", "douyin")
        self.assist_voice = bool(cfg.get("output", {}).get("assist_voice", platform != "channels"))
        if platform == "channels" and self.assist_voice:
            print(
                "[pipeline] ⚠️ 视频号已开启 AI 助播语音：该平台将虚拟人直播归为违规，"
                "AI 出声有被判『非真实直播』的风险，建议设 output.assist_voice: false"
            )
        self._assist_last: dict[str, float] = {}
        self._prompt_last: dict[str, float] = {}
        self._social_last: dict[str, float] = {}
        self._like_count = 0
        self._lock = threading.RLock()
        self._log_lock = threading.Lock()
        self._fh = None
        self.muted = False
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.usage = {
            "llm_calls": 0,
            "llm_errors": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "tts_chars": 0,
            "tts_synth": 0,
            "dropped": 0,
        }
        self.stats = {"danmaku": 0, "assist": 0, "prompt": 0, "ignore": 0, "guard_hit": 0}
        self.pending_host: list[dict] = []  # 待主播跟进，供 H5 提词区展示
        self.session_started_at = time.time()
        self.log_path = self._new_log_path()

    # ---- 留痕文件 ----
    def _new_log_path(self) -> Path:
        stamp = (
            time.strftime("%Y%m%d_%H%M%S", time.localtime(self.session_started_at))
            + f"_{os.getpid()}_{int(time.time() * 1e6) % 1000000:06d}"
        )
        return self.log_dir / f"{stamp}.jsonl"

    def rotate_session(self) -> None:
        """开播/下播切换新场次：重置本场统计与提词，留痕切到新文件。"""
        with self._lock:
            self.session_started_at = time.time()
            new_path = self._new_log_path()
            with self._log_lock:
                if self._fh:
                    try:
                        self._fh.close()
                    except OSError:
                        pass
                    self._fh = None
                self.log_path = new_path
            self.stats = {"danmaku": 0, "assist": 0, "prompt": 0, "ignore": 0, "guard_hit": 0}
            self.pending_host = []
            self._assist_last.clear()
            self._prompt_last.clear()
            self._social_last.clear()

    def set_muted(self, muted: bool) -> None:
        with self._lock:
            self.muted = bool(muted)

    # ---- 路由（纯逻辑，可单测） ----
    def route(self, d: Decision, text: str, now: float | None = None) -> tuple[str, str]:
        """返回 (action, payload)；action ∈ assist|prompt|prompt_dup|ignore"""
        now = now if now is not None else time.time()
        if d.intent == "noise" or (not d.reply and not d.need_host):
            return "ignore", ""
        if d.need_host or not d.reply:
            key = _norm(text) or text
            if now - self._prompt_last.get(key, 0) < self.prompt_dedupe:
                # 去重只抑制"再次打扰主播"这个动作，仍要照常计数入库。
                # 否则 10 个人问同一个 AI 答不上来的问题，复盘会显示"没有知识库缺口"。
                return "prompt_dup", text
            self._prompt_last[key] = now
            return "prompt", text
        if d.intent in ASSIST_INTENTS:
            # 防重复的键必须包含答案本身，不能只按意图。
            # 只按意图会导致：10 秒前答过"多少钱"，接着问"大号紫陶壶多少钱"
            # 就被误判为机械重复而转主播——不同商品的价格问题是不同的答案。
            key = f"{d.intent}:{hashlib.md5(d.reply.encode('utf-8')).hexdigest()[:10]}"
            if now - self._assist_last.get(key, 0) < self.assist_debounce:
                # 完全相同的答案刚播过：高价值意图转提词让主播知道有人再问，寒暄类直接丢弃
                if d.intent in ESCALATE_ON_REPEAT:
                    return "prompt", text
                return "ignore", ""
            self._assist_last[key] = now
            return "assist", d.reply
        return "prompt", text

    def handle(self, dm: Danmaku) -> tuple[str, str]:
        with self._lock:
            self.stats["danmaku"] += 1
        t_start = time.time()
        if dm.kind != "chat":
            return self._handle_social(dm)

        decision = self.brain.decide(dm.user, dm.text)
        self._track_llm_usage()

        with self._lock:
            action, payload = self.route(decision, dm.text)
            # 平台不允许 AI 出声 / 店主一键静音时，助播降级为提词（不合成语音，省成本）
            if action == "assist" and (not self.assist_voice or self.muted):
                action, payload = "prompt", dm.text

        guard_hit: list[str] = []
        wav: bytes | None = None

        if action == "assist":
            safe, guard_hit = self.guard.check(payload)
            if guard_hit:
                with self._lock:
                    self.stats["guard_hit"] += 1
                action, payload = "prompt", dm.text  # 违禁命中 → AI 不发声，转主播
            else:
                payload = safe
                wav = self._synth(payload)

        if action == "assist":
            with self._lock:
                self.stats["assist"] += 1
            self.sink.emit("assist", payload, wav)
        elif action == "prompt":
            with self._lock:
                self.stats["prompt"] += 1
                self.pending_host.append(
                    {"user": dm.user, "text": payload, "intent": decision.intent, "ts": round(time.time())}
                )
                self.pending_host = self.pending_host[-20:]
            self.sink.emit("prompt", f"{dm.user}：{payload}", None)
        elif action == "prompt_dup":
            # 同一问题短时间内重复：计数入库，但不再弹提词、不打扰主播
            with self._lock:
                self.stats["prompt"] += 1
                self.stats["suppressed"] = self.stats.get("suppressed", 0) + 1
        else:
            with self._lock:
                self.stats["ignore"] += 1

        self._log(dm, decision, "prompt" if action == "prompt_dup" else action,
                  payload if action == "assist" else "", guard_hit,
                  decide_ms=round((time.time() - t_start) * 1000),
                  suppressed=action == "prompt_dup")
        return action, payload

    # ---- 社交消息（礼物/关注/点赞/进场）：双手忙碌的主播最没空做的就是这些 ----
    def _handle_social(self, dm: Danmaku) -> tuple[str, str]:
        s = self.cfg.get("social", {})
        now = time.time()
        with self._lock:
            self.stats.setdefault("social", 0)
            self.stats["social"] += 1
        text: str | None = None

        with self._lock:
            if dm.kind == "gift" and s.get("thank_gift", True):
                if now - self._social_last.get("gift", 0) < float(s.get("gift_debounce_sec", 5)):
                    return self._finish(dm, "ignore", "")
                gift = dm.extra.get("gift") or "礼物"
                text = f"谢谢{dm.user}送的{gift}，太破费啦，主播待会儿专门谢你～"
                self._social_last["gift"] = now

            elif dm.kind == "follow" and s.get("thank_follow", True):
                if now - self._social_last.get("follow", 0) < float(s.get("follow_debounce_sec", 5)):
                    return self._finish(dm, "ignore", "")
                text = f"感谢{dm.user}的关注，欢迎常来工作室看做陶～"
                self._social_last["follow"] = now

            elif dm.kind == "like":
                interval = float(s.get("thank_like_sec", 120))
                self._like_count += 1
                if interval <= 0 or now - self._social_last.get("like", 0) < interval:
                    return self._finish(dm, "ignore", "")
                text = f"谢谢大家的支持，刚收到一波小爱心，主播做陶更有劲了～"
                self._social_last["like"] = now
                self._like_count = 0

            elif dm.kind == "enter" and s.get("welcome_enter", False):
                if now - self._social_last.get("enter", 0) < float(s.get("enter_debounce_sec", 20)):
                    return self._finish(dm, "ignore", "")
                text = f"欢迎{dm.user}来到直播间，主播正在手工做陶，有问题尽管打公屏～"
                self._social_last["enter"] = now

        if not text:
            return self._finish(dm, "ignore", "")

        # 感谢语一律走助播通道；平台禁止 AI 出声 / 已静音时降级为提词，让主播自己谢
        action = "assist" if (self.assist_voice and not self.muted) else "prompt"
        payload = text if action == "assist" else f"{dm.kind}：{dm.user}"
        wav = None
        if action == "assist":
            safe, hits = self.guard.check(text)
            if hits:  # 昵称里带违禁词就不念了，避免 AI 替观众说违规内容
                with self._lock:
                    self.stats["guard_hit"] += 1
                return self._finish(dm, "ignore", "")
            payload = safe
            wav = self._synth(payload)
            with self._lock:
                self.stats["assist"] += 1
            self.sink.emit("assist", payload, wav)
        else:
            with self._lock:
                self.stats["prompt"] += 1
                self.pending_host.append(
                    {"user": dm.user, "text": payload, "intent": dm.kind, "ts": round(now)}
                )
                self.pending_host = self.pending_host[-20:]
            self.sink.emit("prompt", payload, None)
        self._log(dm, Decision(dm.kind, False, "", 3, "social"), action,
                  payload if action == "assist" else "", [])
        return action, payload

    def _finish(self, dm: Danmaku, action: str, payload: str) -> tuple[str, str]:
        with self._lock:
            self.stats[action] = self.stats.get(action, 0) + 1
        self._log(dm, Decision(dm.kind, False, "", 3, "social"), action, "", [])
        return action, payload

    def announce(self, text: str) -> None:
        """开播 AI 声明（《标识办法》显式标识要求）。静音时只显示不发声。"""
        if self.muted:
            self.sink.emit("prompt", f"（已静音）AI 声明：{text}", None)
            return
        wav = self._synth(text)
        self.sink.emit("assist", text, wav)

    def _synth(self, text: str) -> bytes | None:
        """统一走这里，顺带按租户统计 TTS 字符数（成本计量的基础）。"""
        try:
            wav = self.tts.synth(text)
        except Exception as e:  # noqa: BLE001
            print(f"[pipeline] TTS 失败（{e}），H5 端将用浏览器语音兜底")
            return None
        with self._lock:
            self.usage["tts_synth"] += 1
            self.usage["tts_chars"] += len(text)
        return wav

    def _track_llm_usage(self) -> None:
        pop = getattr(self.brain, "pop_usage", None)
        if not pop:
            return
        try:
            u = pop()
        except Exception:  # noqa: BLE001
            return
        with self._lock:
            for k, v in u.items():
                self.usage[k] = self.usage.get(k, 0) + int(v)

    def _log(self, dm: Danmaku, decision: Decision, action: str, spoken: str,
             guard_hit: list[str], decide_ms: int = 0, suppressed: bool = False) -> None:
        record = {
            "ts": round(time.time(), 3),
            "user": dm.user,
            "danmaku": dm.text,
            "kind": dm.kind,
            "intent": decision.intent,
            "need_host": decision.need_host,
            "via": decision.via,
            "action": action,
            "spoken": spoken,
            "guard_hit": guard_hit,
            "decide_ms": decide_ms,   # 决策耗时，效果看板与延迟优化都靠它
            "suppressed": suppressed,  # 被去重压掉（没弹提词），但仍计入知识库缺口统计
        }
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with self._log_lock:
            try:
                if self._fh is None:
                    self._fh = open(self.log_path, "a", encoding="utf-8")
                self._fh.write(line)
                self._fh.flush()
            except OSError as e:  # noqa: BLE001  磁盘满不应打断直播
                print(f"[pipeline] 留痕写入失败：{e}")

    def close(self) -> None:
        with self._log_lock:
            if self._fh:
                try:
                    self._fh.close()
                except OSError:
                    pass
                self._fh = None
        close = getattr(self.brain, "close", None)
        if close:
            try:
                close()
            except Exception:  # noqa: BLE001
                pass

    def summary(self) -> str:
        return (
            f"弹幕 {self.stats['danmaku']} ｜ AI助播 {self.stats['assist']} ｜ "
            f"转主播 {self.stats['prompt']} ｜ 忽略 {self.stats['ignore']} ｜ "
            f"违禁拦截 {self.stats['guard_hit']} ｜ 留痕 {self.log_path}"
        )