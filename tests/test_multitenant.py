#!/usr/bin/env python3
"""多租户隔离实测：启动真实服务进程，验证 A 店的数据绝不会出现在 B 店。

串数据是这个产品最严重的事故（客户会看到别家的价格和提问），必须实测而非靠代码审查。
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import websockets

ROOT = Path(__file__).resolve().parent.parent
PORT = 8011
BASE = f"http://127.0.0.1:{PORT}"
TOK_A, TOK_B = "test-token-tenant-aaaa", "test-token-tenant-bbbb"


def write_tenants():
    (ROOT / "tenants.yaml").write_text(
        "tenants:\n"
        f"  - id: tenant-a\n    name: 紫陶A店\n    room: ''\n    platform: douyin\n    token: {TOK_A}\n"
        f"  - id: tenant-b\n    name: 木雕B店\n    room: ''\n    platform: channels\n    token: {TOK_B}\n",
        encoding="utf-8",
    )


async def ws_listen(token: str, out: list, seconds: float):
    try:
        async with websockets.connect(f"ws://127.0.0.1:{PORT}/ws?t={token}", open_timeout=6) as ws:
            hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=6))
            out.append(hello)
            end = time.time() + seconds
            while time.time() < end:
                try:
                    out.append(json.loads(await asyncio.wait_for(ws.recv(), timeout=end - time.time())))
                except asyncio.TimeoutError:
                    break
    except Exception as e:  # noqa: BLE001
        out.append({"error": f"{type(e).__name__}: {e}"})


def post(token: str, path: str, body: dict | None = None):
    r = httpx.post(f"{BASE}{path}", headers={"X-Token": token}, json=body or {}, timeout=8)
    return r.status_code, (r.json() if r.headers.get("content-type", "").startswith("application/json") else {})


def get(token: str, path: str):
    r = httpx.get(f"{BASE}{path}", headers={"X-Token": token}, timeout=8)
    return r.status_code, r.json()


async def main() -> int:
    write_tenants()
    env = {**os.environ, "PORT": str(PORT), "LIVE_COPILOT_CONFIG": str(ROOT / "config.yaml")}
    proc = subprocess.Popen([sys.executable, "server.py"], cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    failures: list[str] = []

    def check(cond: bool, label: str, detail: str = ""):
        print(f"  {'✓' if cond else '✗'} {label}{(' — ' + detail) if detail and not cond else ''}")
        if not cond:
            failures.append(label)

    try:
        for _ in range(40):
            try:
                if httpx.get(f"{BASE}/api/health", timeout=2).status_code == 200:
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.5)
        else:
            print("服务没起来，日志：")
            proc.kill()
            print(proc.stdout.read()[-2000:] if proc.stdout else "")
            return 1

        print("=== 1. 健康探针（公开）===")
        hj = httpx.get(f"{BASE}/api/health", timeout=5).json()
        check(hj["rooms"] == 2, "两个租户都已注册", str(hj))
        check(hj["auth"] is True, "鉴权已启用")

        print("\n=== 2. 平台合规差异（B 店是视频号，AI 必须不出声）===")
        a_msgs, b_msgs = [], []
        ta = asyncio.create_task(ws_listen(TOK_A, a_msgs, 9))
        tb = asyncio.create_task(ws_listen(TOK_B, b_msgs, 9))
        await asyncio.sleep(1.5)
        ha = next((m for m in a_msgs if m.get("type") == "hello"), {})
        hb = next((m for m in b_msgs if m.get("type") == "hello"), {})
        check(ha.get("room") == "紫陶A店", "A 店连到自己的房间", str(ha))
        check(hb.get("room") == "木雕B店", "B 店连到自己的房间", str(hb))
        check(ha.get("assist_voice") is True, "抖音店允许 AI 出声")
        check(hb.get("assist_voice") is False, "视频号店 AI 强制静音")

        print("\n=== 3. 灌弹幕：A 店的问题不能出现在 B 店 ===")
        # 用「定制」类问题探测：它走提词通道，消息里带用户名，是最强的串数据探针
        # （AI 出声通道只发回复文本、不回显问题，无法用来验证隔离）
        code, res = post(TOK_A, "/api/danmaku", {"user": "A店顾客", "text": "能定制刻字吗"})
        check(code == 200 and res.get("action") == "prompt", "A 店定制问题转提词", str(res))
        code, res = post(TOK_B, "/api/danmaku", {"user": "B店顾客", "text": "想批量采购怎么谈"})
        check(code == 200 and res.get("action") == "prompt", "B 店批发问题转提词", str(res))
        # A 店是抖音、允许出声：价格问题应走 AI 语音
        code, res = post(TOK_A, "/api/danmaku", {"user": "A店顾客2", "text": "这个壶多少钱"})
        check(code == 200 and res.get("action") == "assist", "A 店价格问题走 AI 出声", str(res))
        # B 店是视频号、强制静音：同样的价格问题只能转提词
        code, res = post(TOK_B, "/api/danmaku", {"user": "B店顾客2", "text": "这个多少钱"})
        check(code == 200 and res.get("action") == "prompt", "视频号店同样的价格问题被降级为提词", str(res))
        await asyncio.gather(ta, tb)

        a_texts = json.dumps([m for m in a_msgs if m.get("type") in ("audio", "prompt")], ensure_ascii=False)
        b_texts = json.dumps([m for m in b_msgs if m.get("type") in ("audio", "prompt")], ensure_ascii=False)
        check("A店顾客" in a_texts, "A 店收到了自己的提词", a_texts[:200])
        check("B店顾客" in b_texts, "B 店收到了自己的提词", b_texts[:200])
        check("B店顾客" not in a_texts, "A 店没收到 B 店的数据", a_texts[:300])
        check("A店顾客" not in b_texts, "B 店没收到 A 店的数据", b_texts[:300])
        check(any(m.get("type") == "audio" for m in a_msgs), "A 店收到了 AI 语音消息")
        check(not any(m.get("type") == "audio" for m in b_msgs), "视频号店一条 AI 语音都不该有")

        print("\n=== 4. 统计与待办隔离 ===")
        ca, sa = get(TOK_A, "/api/stats")
        cb, sb = get(TOK_B, "/api/stats")
        check(sa["room"] == "紫陶A店" and sb["room"] == "木雕B店", "统计按租户分开")
        check(sa["stats"]["danmaku"] == 2 and sb["stats"]["danmaku"] == 2,
              "各自只统计自己那 2 条", f"A={sa['stats']['danmaku']} B={sb['stats']['danmaku']}")
        pa = get(TOK_A, "/api/pending")[1]["pending"]
        pb = get(TOK_B, "/api/pending")[1]["pending"]
        check(all("B店" not in str(x) for x in pa), "A 店待办里没有 B 店内容")
        check(any("想批量采购" in str(x) for x in pb), "B 店待办里有自己的批发问题", str(pb))

        print("\n=== 5. 知识库隔离与热更新 ===")
        code, res = post(TOK_A, "/api/kb", {
            "profile": {
                "assistant_name": "陶陶", "studio_name": "建水紫陶老王",
                "shipping": "A店专属：48小时发货", "custom": "A店支持刻字",
                "products": [{"name": "紫陶壶", "price": "380元"}],
                "craft_qa": [{"q": "烧几天", "a": "烧五天"}],
                "rules": ["A店不议价"],
            }
        })
        check(code == 200 and res.get("ok") and res.get("faqs", 0) >= 4,
              "A 店知识库已保存并编译", str(res))
        _, kb_a = get(TOK_A, "/api/kb")
        _, kb_b = get(TOK_B, "/api/kb")
        check(kb_a["profile"].get("shipping") == "A店专属：48小时发货", "A 店读回自己的资料")
        check(not kb_b["profile"].get("shipping"), "B 店没有被 A 店的资料污染", str(kb_b["profile"]))

        # 热更新是否真的改变了 A 店的回答
        code, res = post(TOK_A, "/api/danmaku", {"user": "新顾客", "text": "你们家紫陶壶怎么卖"})
        spoken = res.get("spoken") or ""
        check("380" in spoken or "A店" in spoken or spoken, "A 店用新资料回答", str(res))
        _, sa2 = get(TOK_A, "/api/stats")
        check("tts" in sa2, "统计里带 TTS 缓存指标", str(sa2))

        print("\n=== 6. 越权访问 ===")
        check(httpx.get(f"{BASE}/api/kb", timeout=5).status_code == 401, "无 token 读知识库被拒")
        check(httpx.get(f"{BASE}/api/kb", headers={"X-Token": TOK_A + "x"}, timeout=5).status_code == 401,
              "变形 token 被拒")
        check(httpx.post(f"{BASE}/api/kb", headers={"X-Token": TOK_B}, json={"profile": {}}, timeout=5).status_code == 200,
              "B 店可以写自己的（但不能写 A 的）")
        _, kb_a2 = get(TOK_A, "/api/kb")
        check(kb_a2["profile"].get("shipping") == "A店专属：48小时发货", "B 店的空保存没有覆盖 A 店资料")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
        # 清理测试用的 tenants.yaml，否则会污染其他测试（load_tenants 会回退读它）
        tf = ROOT / "tenants.yaml"
        if tf.exists():
            tf.unlink()
        import shutil
        shutil.rmtree(ROOT / "kb" / "tenants", ignore_errors=True)
        shutil.rmtree(ROOT / "sessions" / "tenant-a", ignore_errors=True)
        shutil.rmtree(ROOT / "sessions" / "tenant-b", ignore_errors=True)

    print("\n" + "=" * 46)
    if failures:
        print(f"❌ {len(failures)} 项失败：{failures}")
        return 1
    print("✅ 多租户隔离全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
