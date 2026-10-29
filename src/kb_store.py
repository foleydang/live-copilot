"""租户知识库：让主播在手机上填表单，后端编译成 FAQ + 人设提示词。

设计目标是"傻瓜化"：非技术用户不接触 YAML，只填他本来就清楚的东西
（商品叫什么多少钱、几天发货、能不能定制、常见工艺问题）。

存储：kb/tenants/<tenant_id>/profile.json
编译产物：faqs（供 RuleBrain 关键词匹配）+ tone（供 LlmBrain 系统提示词）
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

from .config import ROOT

DEFAULT_ASSISTANT = "小陶"

EMPTY_PROFILE: dict = {
    "studio_name": "",
    "assistant_name": DEFAULT_ASSISTANT,
    "greeting": "",
    "shipping": "",
    "custom": "",
    "rules": [],
    "products": [],   # [{"name": "...", "price": "..."}]
    "craft_qa": [],   # [{"q": "...", "a": "..."}]
    "fallback": "",   # 答不上来时的兜底话术
}


def _tenant_dir(tenant_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", tenant_id or "default")
    return ROOT / "kb" / "tenants" / safe


def load_profile(tenant_id: str) -> dict:
    f = _tenant_dir(tenant_id) / "profile.json"
    if not f.exists():
        return dict(EMPTY_PROFILE)
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return dict(EMPTY_PROFILE)
    out = dict(EMPTY_PROFILE)
    out.update({k: v for k, v in data.items() if k in EMPTY_PROFILE})
    return out


def save_profile(tenant_id: str, profile: dict) -> dict:
    clean = {}
    for k in EMPTY_PROFILE:
        v = profile.get(k, EMPTY_PROFILE[k])
        if isinstance(v, list):
            v = [x for x in v if x]
        elif isinstance(v, str):
            v = v.strip()[:500]
        clean[k] = v
    d = _tenant_dir(tenant_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "profile.json").write_text(
        json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return clean


def compile_faqs(p: dict) -> list[dict]:
    """把表单编译成关键词 FAQ 表（RuleBrain 直接用，LlmBrain 当参考摘要）。"""
    faqs: list[dict] = []
    name = p.get("assistant_name") or DEFAULT_ASSISTANT

    if p.get("greeting"):
        faqs.append(
            {
                "keywords": ["你好", "来了", "晚上好", "大家好", "下午好", "早上好", "主播好", "欢迎"],
                "intent": "greeting",
                "answer": p["greeting"],
            }
        )
    if p.get("shipping"):
        faqs.append(
            {
                "keywords": ["发货", "几天发", "多久发", "什么时候发", "快递", "邮费", "包邮", "物流", "到货"],
                "intent": "shipping",
                "answer": p["shipping"],
            }
        )
    if p.get("custom"):
        faqs.append(
            {
                "keywords": ["定制", "订制", "刻字", "来图", "定做", "私人"],
                "intent": "custom",
                "answer": p["custom"],
            }
        )
    # 商品与价格：**每个商品单独一条 FAQ**，这样"主人杯多少钱"只答主人杯的价格，
    # 而不是把整个价格表念一遍。合并成一条会导致答案冗长，且不同商品的问题
    # 因答案相同被防重复机制误判为机械重复。
    if p.get("products"):
        named = [(str(i.get("name") or "").strip(), str(i.get("price") or "").strip())
                 for i in p["products"][:40]]
        named = [(n, pr) for n, pr in named if n]
        # 具体商品条目放前面：关键词打分相同时先命中者胜，商品名比"多少钱"更精确
        for nm, pr in named:
            if pr:
                ans = f"{nm}是 {pr}，在小黄车里可以直接拍～"
            else:
                ans = f"{nm}的价格在小黄车里标着呢，点开就能看到～"
            faqs.append({"keywords": [nm], "intent": "price", "answer": ans})
        if named:
            with_price = [f"{n} {pr}" for n, pr in named if pr]
            if len(with_price) > 1:
                general = "、".join(with_price[:5])
                if len(with_price) > 5:
                    general += f" 等 {len(with_price)} 款"
                general += "，完整价格在小黄车里哦～"
            else:
                general = "价格在小黄车里都标着呢，点开看看，喜欢哪件可以扣个编号～"
            # 泛问价格（没提商品名）时的兜底
            faqs.append({"keywords": ["多少钱", "价格", "怎么卖", "贵不贵", "什么价"],
                         "intent": "price", "answer": general})
    for qa in p.get("craft_qa", [])[:40]:
        q, a = str(qa.get("q") or "").strip(), str(qa.get("a") or "").strip()
        if not q or not a:
            continue
        # 问句里挑 2 字以上的实词当关键词
        kws = [w for w in re.split(r"[，,。？?\s]+", q) if len(w) >= 2][:6] or [q[:4]]
        faqs.append({"keywords": kws, "intent": "craft", "answer": a})
    # 店主规矩里凡是提到议价/私下交易的，编译成可被本地规则层命中的 FAQ。
    # 否则没配 LLM API Key 时，店主填的规矩完全不生效（只有 LLM 提示词会读 rules）。
    bargain_kw = ("议价", "讲价", "便宜", "砍价", "打折", "优惠", "少点", "实价")
    private_kw = ("私下", "微信", "转账", "加微", "走链接", "只走")
    for r in p.get("rules", [])[:12]:
        rt = str(r).strip()
        if not rt:
            continue
        if any(k in rt for k in bargain_kw):
            faqs.append({
                "keywords": ["便宜", "少点", "优惠", "议价", "讲价", "砍价", "打折", "能少"],
                "intent": "price",
                "answer": f"{rt}哦～喜欢的话可以直接在小黄车拍下来。",
            })
        elif any(k in rt for k in private_kw):
            # intent 必须在 ASSIST_INTENTS 内才会出声；公开拒绝私下交易本身就有威慑作用。
            # 关键词要覆盖真实弹幕的变体："加我微信"不包含子串"加微信"，
            # 早先用精确短语导致这类弹幕被当噪音忽略。
            faqs.append({
                "keywords": ["微信", "加微", "威信", "薇信", "私下", "转账", "加好友", "qq", "QQ", "闲鱼", "私下交易"],
                "intent": "product",
                "answer": f"{rt}哦，走小黄车下单更有保障～",
            })
    # 兜底
    if p.get("fallback"):
        faqs.append({"keywords": [], "intent": "other", "answer": p["fallback"]})
    return faqs


def compile_tone(p: dict) -> str:
    name = p.get("assistant_name") or DEFAULT_ASSISTANT
    studio = p.get("studio_name") or "直播间"
    lines = [
        f"# AI 助播人设（由 {studio} 自己在手机上填写，可随时更新）",
        "",
        f"## 角色：{name}，{studio}的 AI 助播。",
        "主播是手艺人，直播时双手和眼睛都占用，看不了弹幕。",
        f"{name} 负责承接重复性问题，语气亲切简短口语化，不装懂、不抢戏。",
        "",
        "## 硬性规则",
        "1. 单条回复不超过 60 字。",
        "2. 不使用绝对化用语（最、第一、顶级、国家级等）和任何功效承诺。",
        "3. 不虚构价格、库存、发货时间；资料里没有的，用兜底话术或转主播。",
        "4. 不模仿主播口头禅，保持明显独立的合成音色。",
        "5. 措辞多样化，同样问题两次回答用词要有变化。",
    ]
    rules = [r for r in (p.get("rules") or []) if str(r).strip()]
    if rules:
        lines += ["", "## 本店规矩（必须遵守）"]
        lines += [f"- {r}" for r in rules[:12]]
    if p.get("fallback"):
        lines += ["", f"## 答不上来时的兜底话术", f"「{p['fallback']}」"]
    lines += [
        "",
        "## 定位",
        "AI 只答：价格、发货、售后、常见工艺、欢迎与感谢。",
        "转主播（need_host=true）：复杂工艺讲解、定制细节、议价、批发合作、投诉、任何拿不准的。",
    ]
    return "\n".join(lines)


def kb_digest(p: dict) -> str:
    """给 LLM 系统提示词用的紧凑资料摘要。"""
    parts = []
    if p.get("products"):
        items = [f"{i.get('name')}={i.get('price') or '见小黄车'}" for i in p["products"][:30] if i.get("name")]
        if items:
            parts.append("商品与价格：" + "；".join(items))
    for key, label in (("shipping", "发货"), ("custom", "定制"), ("greeting", "欢迎语"), ("fallback", "兜底")):
        if p.get(key):
            parts.append(f"{label}：{p[key]}")
    for qa in p.get("craft_qa", [])[:20]:
        if qa.get("q") and qa.get("a"):
            parts.append(f"{qa['q']} → {qa['a']}")
    return "\n".join(f"- {x}" for x in parts) or "（店主还没填资料，遇到具体问题一律转主播）"


class KBStore:
    """带热更新的租户知识库。保存后自动通知所有订阅者重建提示词/FAQ。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._subs: list = []
        self._updated: dict[str, float] = {}

    def get(self, tenant_id: str) -> dict:
        return load_profile(tenant_id)

    def save(self, tenant_id: str, profile: dict) -> dict:
        with self._lock:
            clean = save_profile(tenant_id, profile)
            self._updated[tenant_id] = time.time()
        faqs = compile_faqs(clean)
        for cb in list(self._subs):
            try:
                cb(tenant_id, clean, faqs)
            except Exception as e:  # noqa: BLE001
                print(f"[kb] 订阅者更新失败：{e}")
        return clean

    def subscribe(self, cb) -> None:
        self._subs.append(cb)

    def last_updated(self, tenant_id: str) -> float:
        return self._updated.get(tenant_id, 0.0)
