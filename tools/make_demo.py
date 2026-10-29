#!/usr/bin/env python3
"""从真实留痕 JSONL 生成 H5 助播台的可交互回放片段（用于在对话里展示实际效果）。

用法：python tools/make_demo.py <sessions/xxx.jsonl> <输出.html>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def load_events(path: Path, limit: int = 46) -> list[dict]:
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    # 保留有决策价值的事件：助播/提词全留，忽略类抽样几条以体现过滤
    picked, ignored_seen = [], 0
    for r in rows:
        if r["action"] == "ignore":
            ignored_seen += 1
            if ignored_seen % 4 != 0:
                continue
        picked.append(
            {
                "u": r["user"],
                "d": r["danmaku"],
                "i": r["intent"],
                "a": r["action"],
                "s": r.get("spoken") or "",
                "v": r.get("via", ""),
            }
        )
        if len(picked) >= limit:
            break
    return picked


HTML = """<style>
.zb-wrap{display:flex;gap:20px;flex-wrap:wrap;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
  background:#0b0d12;padding:20px;border-radius:16px;color:#e5e7eb;align-items:flex-start}
.zb-phone{width:340px;flex:0 0 auto;background:#0f1115;border:8px solid #23262e;border-radius:32px;overflow:hidden;
  box-shadow:0 12px 40px rgba(0,0,0,.6)}
.zb-screen{max-height:600px;overflow-y:auto}
.zb-hd{padding:11px 14px;display:flex;align-items:center;gap:9px;border-bottom:1px solid #262a33;
  position:sticky;top:0;background:#0f1115;z-index:3}
.zb-hd h3{font-size:14px;margin:0;font-weight:600}
.zb-dot{width:8px;height:8px;border-radius:50%;background:#ef4444;flex:0 0 auto;transition:.3s}
.zb-dot.on{background:#4ade80;box-shadow:0 0 8px #4ade80}
.zb-stats{margin-left:auto;font-size:10.5px;color:#9ca3af;text-align:right;line-height:1.5}
.zb-body{padding:11px 13px}
.zb-sec{background:#1a1d24;border-radius:12px;padding:12px;margin-bottom:12px}
.zb-sec h4{font-size:11px;margin:0 0 9px;color:#9ca3af;font-weight:600;letter-spacing:.4px}
.zb-empty{color:#4b5563;font-size:12.5px;padding:6px 0}
.zb-p{border-left:4px solid #fbbf24;padding:9px 11px;margin-bottom:9px;background:#20242c;border-radius:0 9px 9px 0;
  animation:zbIn .35s ease}
.zb-p .who{font-size:10.5px;color:#fbbf24;margin-bottom:3px}
.zb-p .what{font-size:19px;line-height:1.32;font-weight:600}
.zb-a{font-size:13px;line-height:1.45;padding:6px 0;border-bottom:1px solid #23272f;color:#d1d5db;animation:zbIn .35s ease}
.zb-a:last-child{border-bottom:none}
.zb-a .tag{color:#4ade80;font-size:10.5px;margin-right:5px}
@keyframes zbIn{from{opacity:0;transform:translateY(-6px)}to{opacity:1;transform:none}}
.zb-bar{display:flex;gap:8px;padding:10px 13px;border-top:1px solid #262a33}
.zb-bar button{flex:1;padding:11px;border:none;border-radius:10px;font-size:13.5px;font-weight:600;background:#2b303b;
  color:#e5e7eb;cursor:pointer}
.zb-bar button.pri{background:#4ade80;color:#06281a}
.zb-side{flex:1 1 340px;min-width:300px}
.zb-ctl{display:flex;gap:8px;align-items:center;margin-bottom:14px;flex-wrap:wrap}
.zb-ctl button{padding:8px 14px;border:1px solid #2b303b;border-radius:9px;background:#171a21;color:#e5e7eb;
  font-size:12.5px;cursor:pointer}
.zb-ctl button:hover{border-color:#4ade80}
.zb-flow{background:#12151b;border:1px solid #23272f;border-radius:12px;padding:12px;max-height:420px;overflow-y:auto}
.zb-row{display:grid;grid-template-columns:1fr 74px 62px;gap:8px;align-items:center;padding:7px 0;
  border-bottom:1px solid #1c2028;font-size:12px;animation:zbIn .3s ease}
.zb-row:last-child{border-bottom:none}
.zb-row .dm{color:#cbd5e1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.zb-row .dm b{color:#6b7280;font-weight:400;margin-right:5px;font-size:11px}
.zb-badge{font-size:10.5px;padding:2.5px 7px;border-radius:20px;text-align:center;font-weight:600}
.b-assist{background:rgba(74,222,128,.16);color:#4ade80}
.b-prompt{background:rgba(251,191,36,.16);color:#fbbf24}
.b-ignore{background:rgba(107,114,128,.16);color:#9ca3af}
.zb-intent{font-size:10.5px;color:#818cf8;text-align:center}
.zb-hdrow{display:grid;grid-template-columns:1fr 74px 62px;gap:8px;font-size:10.5px;color:#6b7280;
  padding-bottom:7px;border-bottom:1px solid #23272f;margin-bottom:4px}
.zb-note{font-size:11.5px;color:#9ca3af;line-height:1.7;margin-top:12px;background:#12151b;
  border-left:3px solid #818cf8;padding:10px 12px;border-radius:0 9px 9px 0}
.zb-tt{font-size:15px;font-weight:600;margin:0 0 4px;color:#f1f5f9}
.zb-sub{font-size:11.5px;color:#9ca3af;margin:0 0 14px}
</style>

<div class="zb-wrap">
  <div class="zb-phone">
    <div class="zb-screen">
      <div class="zb-hd">
        <span class="zb-dot" id="zbDot"></span>
        <h3 id="zbRoom">手工紫陶工作室 · AI 助播台</h3>
        <div class="zb-stats" id="zbStats">弹幕 0 · 助播 0<br>转主播 0 · 拦截 0</div>
      </div>
      <div class="zb-body">
        <div class="zb-sec">
          <h4>📋 需要主播回应（提词）</h4>
          <div id="zbPrompts"><div class="zb-empty">暂无</div></div>
        </div>
        <div class="zb-sec">
          <h4>🔊 AI 助播已发声（观众听到）</h4>
          <div id="zbAssists"><div class="zb-empty">暂无</div></div>
        </div>
      </div>
      <div class="zb-bar">
        <button class="pri" id="zbBtn">开始助播</button>
        <button style="flex:0 0 92px">AI 声明</button>
      </div>
    </div>
  </div>

  <div class="zb-side">
    <p class="zb-tt">决策流回放 · 数据来自真实运行留痕</p>
    <p class="zb-sub">左边是主播实际看到的 H5 界面（1:1 还原 static/assist.html），右边是每条弹幕的意图判定与路由结果。</p>
    <div class="zb-ctl">
      <button id="zbPlay">▶ 播放</button>
      <button id="zbReset">↺ 重置</button>
      <button id="zbSpeed">速度 1×</button>
      <span style="font-size:11.5px;color:#9ca3af" id="zbProg"></span>
    </div>
    <div class="zb-flow">
      <div class="zb-hdrow"><span>弹幕原文</span><span style="text-align:center">路由</span><span style="text-align:center">意图</span></div>
      <div id="zbRows"></div>
    </div>
    <div class="zb-note">
      <b>看点：</b>价格/发货/工艺/欢迎 → <span style="color:#4ade80">AI 助播直接出声</span>（经声卡混入直播流）；
      定制/批发/砍价/复杂工艺 → <span style="color:#fbbf24">只上提词屏，不出声</span>，留给主播本人；
      灌水/空弹幕 → 忽略。25 秒内同类问题重复出现会自动转提词，避免机械重复感。
      礼物与关注由 AI 即时感谢——这是双手沾泥的主播最没空做的事。
    </div>
  </div>
</div>

<script>
(function(){
  var EVENTS = __DATA__;
  var idx = 0, timer = null, speed = 1, stats = {danmaku:0, assist:0, prompt:0, ignore:0, guard:0};
  var prompts = [], assists = [];
  var $ = function(id){ return document.getElementById(id); };
  var esc = function(t){ return String(t==null?'':t).replace(/[&<>"]/g, function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); };

  function render(){
    $('zbStats').innerHTML = '弹幕 '+stats.danmaku+' · 助播 '+stats.assist+'<br>转主播 '+stats.prompt+' · 拦截 '+stats.guard;
    $('zbPrompts').innerHTML = prompts.length ? prompts.slice(0,6).map(function(p){
      return '<div class="zb-p"><div class="who">'+esc(p.u)+' · '+esc(p.i)+'</div><div class="what">'+esc(p.d)+'</div></div>';
    }).join('') : '<div class="zb-empty">暂无</div>';
    $('zbAssists').innerHTML = assists.length ? assists.slice(0,7).map(function(a){
      return '<div class="zb-a"><span class="tag">AI</span>'+esc(a)+'</div>';
    }).join('') : '<div class="zb-empty">暂无</div>';
    $('zbProg').textContent = idx + ' / ' + EVENTS.length;
  }

  function step(){
    if (idx >= EVENTS.length){ stop(); return; }
    var e = EVENTS[idx++];
    stats.danmaku++;
    stats[e.a === 'assist' ? 'assist' : (e.a === 'prompt' ? 'prompt' : 'ignore')]++;
    if (e.a === 'assist' && e.s) assists.unshift(e.s);
    if (e.a === 'prompt') prompts.unshift(e);
    assists = assists.slice(0,7); prompts = prompts.slice(0,6);
    var cls = e.a === 'assist' ? 'b-assist' : (e.a === 'prompt' ? 'b-prompt' : 'b-ignore');
    var label = e.a === 'assist' ? 'AI出声' : (e.a === 'prompt' ? '转主播' : '忽略');
    var row = document.createElement('div');
    row.className = 'zb-row';
    row.innerHTML = '<span class="dm"><b>'+esc(e.u)+'</b>'+esc(e.d || '(礼物/点赞/进场)')+'</span>'
      + '<span class="zb-badge '+cls+'">'+label+'</span>'
      + '<span class="zb-intent">'+esc(e.i)+'</span>';
    var box = $('zbRows');
    box.insertBefore(row, box.firstChild);
    while (box.children.length > 40) box.removeChild(box.lastChild);
    render();
    timer = setTimeout(step, (e.a === 'ignore' ? 420 : 900) / speed);
  }

  function play(){
    if (timer) return;
    $('zbDot').classList.add('on'); $('zbBtn').textContent = '助播中'; $('zbPlay').textContent = '⏸ 暂停';
    step();
  }
  function stop(){
    clearTimeout(timer); timer = null; $('zbPlay').textContent = '▶ 播放';
  }
  function reset(){
    stop(); idx = 0; prompts = []; assists = [];
    stats = {danmaku:0, assist:0, prompt:0, ignore:0, guard:0};
    $('zbRows').innerHTML = ''; $('zbDot').classList.remove('on'); $('zbBtn').textContent = '开始助播'; render();
  }

  $('zbPlay').onclick = function(){ timer ? stop() : play(); };
  $('zbReset').onclick = reset;
  $('zbSpeed').onclick = function(){
    speed = speed === 1 ? 2 : (speed === 2 ? 4 : 1);
    $('zbSpeed').textContent = '速度 ' + speed + '×';
  };
  render();
  setTimeout(play, 500);
})();
</script>"""


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    src, out = Path(sys.argv[1]), Path(sys.argv[2])
    events = load_events(src)
    if not events:
        print("留痕文件为空")
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(HTML.replace("__DATA__", json.dumps(events, ensure_ascii=False)), encoding="utf-8")
    n_a = sum(1 for e in events if e["a"] == "assist")
    n_p = sum(1 for e in events if e["a"] == "prompt")
    print(f"已生成 {out}（{len(events)} 条真实事件：AI出声 {n_a} · 转主播 {n_p} · 忽略 {len(events)-n_a-n_p}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
