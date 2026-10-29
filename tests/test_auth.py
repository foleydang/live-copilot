"""鉴权测试。公网部署必须有 token，否则任何人都能看提词、灌假弹幕、触发 AI 声明。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.auth import auth_enabled, extract_token, load_tenants, new_token, verify  # noqa: E402

CFG = {"auth": {"tenants": [{"id": "a", "name": "紫陶工作室", "room": "123", "token": "tok-aaa"}]}}


class H(dict):
    def get(self, k, d=None):  # 模拟 header 大小写不敏感
        return super().get(k.lower(), d)


def test_load_and_verify():
    t = load_tenants(CFG)
    assert auth_enabled(t)
    assert verify(t, "tok-aaa")["name"] == "紫陶工作室"
    assert verify(t, "wrong") is None
    assert verify(t, "") is None
    assert verify(t, None) is None


def test_auth_disabled_when_no_tenants():
    # 显式指向不存在的文件，避免被真实的 tenants.yaml 污染
    missing = str(Path(__file__).resolve().parent / "__no_such_tenants__.yaml")
    assert not auth_enabled(load_tenants({}, tenants_file=missing))
    assert not auth_enabled(load_tenants({"auth": {"tenants": []}}, tenants_file=missing))


def test_extract_token_from_all_positions():
    assert extract_token(H({"x-token": "abc"}), {}) == "abc"
    assert extract_token(H({"authorization": "Bearer xyz"}), {}) == "xyz"
    assert extract_token(H(), {"t": "q1"}) == "q1"
    assert extract_token(H(), {"token": "q2"}) == "q2"
    assert extract_token(H(), {}) is None
    # header 优先于 query
    assert extract_token(H({"x-token": "head"}), {"t": "query"}) == "head"


def test_load_preserves_platform_and_voice():
    """回归：platform 曾被 load_tenants 丢弃，导致视频号店静默退回 douyin 配置、
    AI 出声的合规默认值失效。voice 同理（音色会串）。"""
    t = load_tenants(
        {
            "auth": {
                "tenants": [
                    {"id": "vp", "name": "视频号店", "token": "t1", "platform": "channels", "voice": "longwan"},
                    {"id": "dy", "name": "抖音店", "token": "t2"},
                ]
            }
        }
    )
    vp = verify(t, "t1")
    assert vp["platform"] == "channels", f"platform 必须透传，实际 {vp}"
    assert vp["voice"] == "longwan"
    assert "assist_voice" not in vp, "未显式配置时不应有该键（交给平台默认值决定）"
    dy = verify(t, "t2")
    assert dy["platform"] == "", "未指定平台应为空串，由 rooms 层回退到全局配置"

    # 显式配置 assist_voice 时必须保留（租户可自主开/关）
    t2 = load_tenants({"auth": {"tenants": [{"id": "x", "token": "t3", "platform": "channels", "assist_voice": True}]}})
    assert verify(t2, "t3")["assist_voice"] is True


def test_token_is_url_safe_and_unique():
    toks = {new_token() for _ in range(50)}
    assert len(toks) == 50, "token 必须唯一"
    assert all(len(t) >= 24 for t in toks)
    assert all(all(c.isalnum() or c in "-_" for c in t) for t in toks), "必须 URL 安全"


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
