# 架构说明（2026-10-29 优化后）

## 一句话

一个进程服务 N 个直播间的"AI 助播大脑"：接收抖音/视频号弹幕 → 意图分类 → 决策（LLM + 本地规则 + FAQ RAG） → 违禁词护栏 → 防重复 → 路由（AI 出声 / 转主播提词） → 留痕复盘。

## 数据流

```
┌──────────────┐  弹幕 WS   ┌──── live-copilot 服务（云端）────┐   WebSocket   ┌──────────────────┐
│ douyinLive   │ ─────────► │ WsSource → Queue → Pipeline │ ─────────────►│ H5 助播台（手机） │
│ 采集器       │            │  ↓         ↓                 │               │  · 提词区          │
│ 127.0.0.1:1088│           │  LLM       TTS              │               │  · 助播语音        │
└──────────────┘            │  ↓         ↓                 │               └─────────┬────────┘
                            │  Guard    Cached WAV         │                         │
                            │  ↓         ↓                 │                         ▼
                            │  决策     Sink.emit          │                  ┌──────────────────┐
                            └─────────────────────────────┘                  │ 直播声卡（AUX）  │
                                                                             └─────────┬────────┘
                                                                                       ▼
                                                                              ┌──────────────────┐
                                                                              │ 直播手机（正常开播）│
                                                                              └──────────────────┘
```

## 线程模型

每个租户（房间）独立三件套：

| 组件 | 职责 | 线程 |
|---|---|---|
| `WsSource` pump | 只读上游 WS → 入队 | 1 个 daemon |
| `Room` worker | 串行消费队列（弹幕 / KB 更新 / 声明口播） | 1 个 daemon |
| `WebSink` | asyncio.Queue 广播到所有 H5 客户端 | 共享 asyncio loop |

**为什么必须串行**：LLM / TTS 都是阻塞网络调用，串行化后既不会拖垮事件循环，也不会丢统计、乱序提词。HTTP 端点只负责"入队 → 立刻返回"。

## 关键模块

```
server.py           FastAPI 路由 + lifespan + 限流 + 维护线程
src/rooms.py        Room / RoomRegistry（每租户独立的 pipeline + 队列 + 采集）
src/pipeline.py     弹幕路由 + 防重复 + 护栏 + 留痕（带内部锁，可并发安全）
src/brain/
  llm_client.py     复用 httpx.Client + 重试 + 用量计量 + 缓存
  local_rules.py    无 key 时的降级大脑（关键词 + FAQ）
  guard.py          违禁词 + 长度截断（不再整条丢弃）
src/danmaku/
  ws_source.py      通用 WS 弹幕源 + 真实连接状态（connected/last_event_at）
  adapters.py       douyinLive / BarrageGrab 格式适配
src/output/sinks.py WebSink（带重连回放历史）
src/voice/tts.py    CosyVoice + 磁盘缓存 + LRU 清理
src/ratelimit.py    鉴权限流（20 次/分钟/封 2 分钟）
src/kb_store.py     租户知识库：手机表单 → FAQ + tone
src/analytics.py    效果度量（AI 承接率 / 延迟 / 知识库缺口）
```

## 生命周期

- **服务启动**：绑定端口、加载租户、启动维护线程；**不念 AI 声明**
- **开播（`live_status: live=true`）**：切新 session（留痕文件）→ 念 AI 声明
- **下播（`live_status: live=false`）**：标记 `live=false`，不切文件
- **服务停止**：`registry.stop_all()` → 关闭 pump → 关闭 pipeline 句柄

## 改动总览（对比 2026-10-26 版本）

| # | 问题 | 修复 |
|---|---|---|
| P0 | 同步 LLM/TTS 阻塞事件循环 | 每租户串行 worker 队列，HTTP 只入队 |
| P0 | 无背压，热门间 100 条/秒打爆 LLM | 队列满时丢最旧 + 计数；`httpx.Client` 复用连接 |
| P1 | AI 声明在启动时念，开播时不念 | `live_status` 驱动场次切分 + 声明 |
| P1 | `_persist_room_id` 不更新内存 → admin 操作覆盖 | 同步更新 TENANTS + `_write_tenants_file` 兜底取 registry |
| P1 | LLM 缓存串人名 | key 含 user |
| P1 | stop_pump 不 join → 换号瞬间双连接 | 加 join + stop_event 每实例化 |
| P1 | stats/pending 无锁 | 加 RLock |
| P1 | KB 保存跑两遍 apply_kb | 订阅走队列，server 只算 faqs 数量 |
| P2 | 每次 LLM 都新建 TLS | `httpx.Client` 长连接 |
| P2 | 日志每条弹幕开关文件 | 持久化句柄 + flush |
| P2 | TTS 缓存无限增长 | `cleanup()` + 6h 维护线程 |
| P2 | 生产依赖 numpy/sounddevice | 拆到 `requirements-dev.txt` |
| P3 | `/api/health` 假阳性 | 真实 connected/last_event_at 状态 |
| P3 | session 按进程切分 | `rotate_session()` 由 live_status 触发 |
| P3 | 无一键静音 | `/api/mute` + H5 红按钮 |
| P3 | 前端 token 留在地址栏 | `history.replaceState` 抹掉 |
| P3 | admin token 可从 query 传 | 只允许 Header |
| P3 | 无速率限制 | 内存滑动窗口限流 |
| P3 | 无租户级用量计量 | `/api/usage` + LLM tokens + TTS 字符数 |
| P3 | 长度超限整条丢弃 | 按标点截断 |
| P3 | 音频重叠打断 | H5 音频队列 + onended |
