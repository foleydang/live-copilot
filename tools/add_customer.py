#!/usr/bin/env python3
"""一键加客户。

两种模式：
  远程模式（推荐，不用 SSH）：在本地直接对线上服务操作，客户立即生效
    export LIVE_COPILOT_SERVER=http://47.242.158.242:8010
    export LIVE_COPILOT_ADMIN_TOKEN=<管理令牌>
    python tools/add_customer.py --id laowang --name "建水紫陶·老王工作室"

  本地模式：改本地 tenants.yaml，之后需要自己上传并重启服务
    python tools/add_customer.py --id laowang --name "老王工作室"

其他：
  --list                 列出所有客户与链接
  --remove <id>          删除客户
  --rotate <id>          重置某客户的访问链接（链接泄露时用）
  --room <直播间号>       可留空，让客户自己在手机设置页填
  --platform channels    视频号（会自动关闭 AI 出声）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.auth import new_token  # noqa: E402

TENANTS_FILE = ROOT / "tenants.yaml"
EXAMPLE_FILE = ROOT / "tenants.example.yaml"
SERVER = os.environ.get("LIVE_COPILOT_SERVER", "").rstrip("/")
ADMIN_TOKEN = os.environ.get("LIVE_COPILOT_ADMIN_TOKEN", "")


def api(method: str, path: str, body: dict | None = None):
    req = urllib.request.Request(
        f"{SERVER}{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", "X-Token": ADMIN_TOKEN},
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:  # noqa: BLE001
            return e.code, {"error": str(e)}
    except Exception as e:  # noqa: BLE001
        return 0, {"error": f"{type(e).__name__}: {e}"}


def remote_mode() -> bool:
    return bool(SERVER)


def load() -> dict:
    if not TENANTS_FILE.exists():
        if EXAMPLE_FILE.exists():
            data = yaml.safe_load(EXAMPLE_FILE.read_text(encoding="utf-8")) or {}
            data["tenants"] = [t for t in data.get("tenants", []) if "REPLACE_WITH" not in str(t.get("token", ""))]
        else:
            data = {"tenants": []}
        return data
    return yaml.safe_load(TENANTS_FILE.read_text(encoding="utf-8")) or {"tenants": []}


def save(data: dict) -> None:
    TENANTS_FILE.write_text(
        "# 租户表（含密钥，勿提交公开仓库）\n"
        "# token 重新生成：python -c \"from src.auth import new_token; print(new_token())\"\n"
        + yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    os.chmod(TENANTS_FILE, 0o600)


def public_base() -> str:
    return (SERVER or os.environ.get("PUBLIC_BASE", "http://127.0.0.1:8010")).rstrip("/")


def print_customer_help(name: str, url: str, platform: str) -> None:
    print(f"✅ 已添加客户 {name}\n")
    print(f"发给客户的链接（手机打开即可，不用装任何东西）：\n  {url}\n")
    print("客户要做的三件事：")
    print("  1. 打开链接 → 点「开始助播」")
    print("  2. 进「设置」页填直播间号 + 常见问题 + 商品价格 → 点「保存并生效」")
    print("  3. 手机/平板接直播声卡，声卡接开播的手机")
    if platform == "channels":
        print("\n⚠️ 视频号平台：AI 出声已自动关闭（平台把虚拟人直播归为违规），只用提词功能")


def cmd_list(data: dict) -> int:
    if remote_mode():
        code, d = api("GET", "/api/admin/tenants")
        if code != 200 or not d.get("ok"):
            print(f"查询失败 HTTP {code}: {d.get('error')}")
            print("检查 LIVE_COPILOT_SERVER / LIVE_COPILOT_ADMIN_TOKEN 是否设置正确")
            return 1
        ts = d["tenants"]
        if not ts:
            print("（线上还没有客户）")
            return 0
        print(f"线上 {len(ts)} 个客户：\n")
        print(f"{'ID':<16} {'名称':<22} {'平台':<9} {'资料':<5} {'房间号':<13} 链接")
        for t in ts:
            print(f"{t['id']:<16} {t['name']:<22} {t['platform']:<9} "
                  f"{'已填' if t['kb_filled'] else '未填':<5} {str(t['room'] or '未填'):<13} "
                  f"{public_base()}/{t['url']}")
        return 0
    ts = data.get("tenants", [])
    if not ts:
        print("（还没有客户，用 --id/--name 加一个）")
        return 0
    print(f"{'ID':<16} {'名称':<22} {'平台':<10} {'房间号':<14} 链接")
    for t in ts:
        print(f"{t.get('id',''):<16} {t.get('name',''):<22} {t.get('platform','douyin'):<10} "
              f"{str(t.get('room') or '未填'):<14} {public_base()}/?t={t.get('token','')}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="一键加客户")
    ap.add_argument("--id", help="租户 ID（英文/数字，用于目录名）")
    ap.add_argument("--name", help="客户名称（显示在 H5 标题上）")
    ap.add_argument("--room", default="", help="抖音直播间号（可留空，让客户自己在手机上填）")
    ap.add_argument("--platform", default="douyin", choices=["douyin", "channels"],
                    help="channels=视频号，会自动关闭 AI 出声（平台合规）")
    ap.add_argument("--voice", default="", help="CosyVoice 音色名，留空用全局默认")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--remove", help="删除某个租户 ID")
    ap.add_argument("--rotate", help="重置某个租户的访问链接")
    args = ap.parse_args()

    if remote_mode() and not ADMIN_TOKEN:
        print("已设置 LIVE_COPILOT_SERVER 但缺 LIVE_COPILOT_ADMIN_TOKEN，无法远程操作")
        return 1

    data = load()

    if args.list:
        return cmd_list(data)

    if args.remove:
        if remote_mode():
            code, d = api("DELETE", f"/api/admin/tenants/{args.remove}")
            print(f"{'✅ 已删除' if d.get('ok') else '❌ 失败'} {args.remove}（HTTP {code}）{'' if d.get('ok') else d.get('error')}")
            return 0 if d.get("ok") else 1
        tenants = data.setdefault("tenants", [])
        before = len(tenants)
        tenants[:] = [t for t in tenants if t.get("id") != args.remove]
        if len(tenants) == before:
            print(f"未找到租户 {args.remove}")
            return 1
        save(data)
        print(f"已删除 {args.remove}（记得 systemctl restart live-copilot）")
        return 0

    if args.rotate:
        if remote_mode():
            code, d = api("POST", f"/api/admin/rotate/{args.rotate}")
            if d.get("ok"):
                print(f"✅ 已重置 {args.rotate} 的链接（旧链接立即失效）：\n  {public_base()}/?t={d['token']}")
                return 0
            print(f"❌ 失败 HTTP {code}: {d.get('error')}")
            return 1
        for t in data.get("tenants", []):
            if t.get("id") == args.rotate:
                t["token"] = new_token()
                save(data)
                print(f"已重置 {args.rotate} 的 token：\n  {public_base()}/?t={t['token']}")
                print("记得 systemctl restart live-copilot")
                return 0
        print(f"未找到租户 {args.rotate}")
        return 1

    if not args.id or not args.name:
        ap.error("需要 --id 和 --name（或用 --list / --remove / --rotate）")

    if remote_mode():
        body = {"id": args.id, "name": args.name, "room": args.room, "platform": args.platform}
        if args.voice:
            body["voice"] = args.voice
        code, d = api("POST", "/api/admin/tenants", body)
        if code == 200 and d.get("ok"):
            print_customer_help(args.name, f"{public_base()}/?t={d['token']}", args.platform)
            print("\n客户已立即生效，不需要重启服务。")
            if d.get("pump_started"):
                print("采集已自动启动。")
            return 0
        print(f"❌ 添加失败 HTTP {code}: {d.get('error')}")
        return 1

    tenants = data.setdefault("tenants", [])
    if any(t.get("id") == args.id for t in tenants):
        print(f"租户 {args.id} 已存在，用 --rotate 重置 token 或换个 --id")
        return 1
    token = new_token()
    entry = {"id": args.id, "name": args.name, "room": args.room, "platform": args.platform, "token": token}
    if args.voice:
        entry["voice"] = args.voice
    tenants.append(entry)
    save(data)
    (ROOT / "kb" / "tenants" / args.id).mkdir(parents=True, exist_ok=True)
    print_customer_help(args.name, f"{public_base()}/?t={token}", args.platform)
    print("\n你要做的一件事：")
    print("  systemctl restart live-copilot   # 让新租户生效（远程模式不需要这步）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
