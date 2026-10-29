"""LLM 决策层：一次调用完成 意图分类 + 回复生成，失败自动降级到本地规则。

性能要点（直播场景对延迟极敏感）：
- 复用 httpx.Client 连接，避免每条弹幕都重新做 TLS 握手（实测省 50~150ms）
- 关闭思考模式；429/5xx 只重试一次，避免把一条弹幕拖到 2 倍超时
- 缓存 key 必须带观众昵称：回复里可能带称呼，只按文本缓存会串人名
- 弹幕文本截断后再进 prompt，防止超长文本/提示注入烧 token
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time

import httpx

from ..config import load_faqs, load_tone
from .local_rules import ANSWERABLE, Decision, RuleBrain

_MAX_INPUT_CHARS = 200
_RETRY_STATUS = {429, 500, 502, 503, 504}

_SYSTEM_TMPL = """你是「{room}」直播间的 AI 助播「{assistant}」。主播是手艺人，直播时正专心做手工，看不了弹幕。
你的职责：用不超过{max_len}字的口语短句回答观众关于价格、发货、售后、常见问题的问题；欢迎新观众；绝不代替主播讲解复杂工艺、处理定制细节和商务合作。

下面是店主自己填写的人设与规矩，必须严格遵守：

{tone}

严格只输出一个 JSON 对象（不要 markdown 代码块）：
{{"intent":"price|shipping|product|craft|custom|cooperation|greeting|interaction|noise|other","need_host":true/false,"reply":"对观众说的话，不需要回答时为空字符串","priority":1}}

规则：
- 只有 intent 属于 {answerable} 且 need_host=false 时才给 reply，其余情况 reply 为空字符串
- custom/cooperation/other/不确定 → need_host=true（转给主播）
- noise（纯表情、无意义灌水）→ intent=noise
- 资料里没有的信息绝对不要编造，用兜底话术并 need_host=true
- 禁止任何绝对化用语和功效承诺
- 措辞自然多样，避免机械重复
- reply 里不要带观众昵称（观众昵称由前端展示，语音里念错名字很尴尬）

店主资料（关键词→要点）：
{faq_digest}"""


def _faq_digest(faqs: list[dict]) -> str:
    lines = []
    for f in faqs:
        lines.append(f"- {'/'.join(f.get('keywords', []))} → {f.get('answer', '')}")
    return "\n".join(lines) or "（空）"


class LlmBrain:
    def __init__(
        self,
        room_name: str,
        cfg: dict,
        max_len: int = 60,
        faqs: list[dict] | None = None,
        tone: str | None = None,
        assistant_name: str = "小陶",
    ):
        self.room_name = room_name
        self.max_len = max_len
        self.assistant_name = assistant_name
        self.base_url = cfg.get("base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1")
        self.model = cfg.get("model", "qwen-flash")
        self.timeout = float(cfg.get("timeout", 8.0))
        self.max_tokens = int(cfg.get("max_tokens", 150))
        # 关思考模式是延迟的关键：实测 Token Plan 上 qwen3.8-flash
        # 默认 2209ms → 关思考 484ms；qwen3.6-flash 关思考 284ms（短prompt）/468ms（生产prompt）。
        # 直播场景要的是快，不是推理深度。
        self.enable_thinking = bool(cfg.get("enable_thinking", False))
        self.api_key = os.environ.get(cfg.get("api_key_env", "DASHSCOPE_API_KEY"), "")
        # 连接池复用：每个房间一个 client，keep-alive 长连接
        self._client = httpx.Client(
            timeout=httpx.Timeout(self.timeout, connect=min(3.0, self.timeout)),
            limits=httpx.Limits(max_keepalive_connections=8, max_connections=16),
            headers={"Connection": "keep-alive"},
        )
        self.fallback = RuleBrain(room_name, faqs)
        self._cache: dict[str, tuple[float, Decision]] = {}
        self._cache_ttl = 300.0
        self._cache_max = 2000
        self._usage = {"llm_calls": 0, "llm_errors": 0, "prompt_tokens": 0, "completion_tokens": 0}
        self._usage_lock = threading.Lock()
        self.set_kb(faqs if faqs is not None else load_faqs(), tone if tone is not None else load_tone())

    def set_kb(self, faqs: list[dict], tone: str) -> None:
        """知识库热更新：重建系统提示词并清空决策缓存（旧答案可能已失效）。"""
        self._system = _SYSTEM_TMPL.format(
            room=self.room_name,
            assistant=self.assistant_name,
            max_len=self.max_len,
            tone=tone,
            answerable="/".join(sorted(ANSWERABLE)),
            faq_digest=_faq_digest(faqs),
        )
        self._cache.clear()
        self.fallback.set_faqs(faqs)

    def pop_usage(self) -> dict:
        """取走本次累计用量（线程安全），供按租户计量。"""
        with self._usage_lock:
            out = dict(self._usage)
            for k in self._usage:
                self._usage[k] = 0
        return out

    def _bump(self, **kw) -> None:
        with self._usage_lock:
            for k, v in kw.items():
                self._usage[k] = self._usage.get(k, 0) + v

    def decide(self, user: str, text: str) -> Decision:
        # key 带上昵称：prompt 里会带观众名字，回复可能包含称呼，不能跨观众复用
        key = hashlib.md5(f"{user}|{text.strip()}".encode()).hexdigest()
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < self._cache_ttl:
            return hit[1]
        try:
            d = self._ask(user, text)
        except Exception as e:  # noqa: BLE001
            self._bump(llm_errors=1)
            print(f"[brain] LLM 失败，降级本地规则：{e}")
            d = self.fallback.decide(user, text)
        if len(self._cache) > self._cache_max:
            # 简单清一半，避免长时间运行内存无限增长
            for k in list(self._cache)[: self._cache_max // 2]:
                self._cache.pop(k, None)
        self._cache[key] = (time.time(), d)
        return d

    def _ask(self, user: str, text: str) -> Decision:
        text = (text or "").strip()[:_MAX_INPUT_CHARS]
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self._system},
                {"role": "user", "content": f"观众「{user[:24]}」发弹幕：{text}"},
            ],
            "temperature": 0.7,
            "max_tokens": self.max_tokens,
        }
        # DashScope OpenAI 兼容模式支持该参数；非 DashScope 端点会忽略或报错，故按需注入
        if not self.enable_thinking:
            body["enable_thinking"] = False

        resp = self._post(body)
        if resp.status_code >= 400 and "enable_thinking" in body:
            # 端点不认这个参数就去掉重试一次，避免整条链路降级
            body.pop("enable_thinking")
            resp = self._post(body)
        resp.raise_for_status()
        payload = resp.json()
        usage = payload.get("usage") or {}
        self._bump(
            llm_calls=1,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
        )
        content = payload["choices"][0]["message"]["content"]
        m = re.search(r"\{.*\}", content, re.S)
        if not m:
            raise ValueError(f"LLM 输出无 JSON：{content[:80]}")
        data = json.loads(m.group())
        intent = str(data.get("intent", "other"))
        need_host = bool(data.get("need_host", True))
        reply = str(data.get("reply") or "")
        priority = int(data.get("priority") or (1 if need_host else 2))
        if intent in ANSWERABLE and not need_host and reply:
            return Decision(intent, False, reply, priority, "llm")
        return Decision(intent, need_host, "", priority, "llm")

    def _post(self, body: dict) -> httpx.Response:
        """带一次快速重试的 POST。只对 429/5xx 重试，且退避很短。"""
        resp = self._client.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json=body,
        )
        if resp.status_code in _RETRY_STATUS:
            time.sleep(0.2)
            resp = self._client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=body,
            )
        return resp

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass