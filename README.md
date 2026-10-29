# Live Copilot · AI 助播

> 直播间的 AI 副驾：替主播接住弹幕里的重复问答，让主播专注做手工、讲工艺。

```
┌──────────┐  弹幕 WS   ┌──── Live Copilot（云端）────┐   WebSocket   ┌─────────────┐
│ douyinLive│ ─────────► │ 弹幕 → 意图分类 → 护栏      │ ─────────────►│ H5 助播台    │
│ 采集器     │            │      → FAQ RAG → LLM        │               │ （闲置手机）  │
└──────────┘            │      → 防重复 → TTS          │               └──────┬──────┘
                        │      → 路由（出声/提词/忽略） │                      │
                        └────────────────────────────┘                       ▼
                                                                   ┌─────────────┐
                                                                   │ 直播声卡 AUX │
                                                                   └──────┬──────┘
                                                                          ▼
                                                                   ┌─────────────┐
                                                                   │ 直播手机     │
                                                                   └─────────────┘
```

## 它解决什么问题

手工艺直播间（紫陶、陶艺、木作…）主播**双手沾泥、眼睛盯活**，但弹幕里 80% 是重复问题：

> 多少钱？发货几天？能定制吗？这是什么泥？零基础能学吗？

主播没空回 → 观众流失 → 算法判定互动差 → 不推流。

Live Copilot 替主播**自动语音回答**这些重复问题（价格/发货/工艺/尺码），同时把需要主播亲自回应的（定制、批发、合作）**提词到备用屏**，主播扫一眼就知道该接什么话。

## 核心特性

- **多租户**：一个进程服务 N 个直播间，每户独立知识库 / 提词队列 / 留痕
- **双平台**：抖音 / 视频号（视频号自动关闭 AI 出声，只提词，合规）
- **大脑**：LLM（DashScope GLM/Qwen）+ 本地关键词规则降级 + FAQ RAG
- **护栏**：违禁词过滤 + 长度截断 + 防重复（同一人 60s 内不重复回答）
- **TTS**：CosyVoice 预置音色 + 磁盘缓存（命中率 80%+，命中后 0 成本 0 延迟）
- **一键静音**：出事故时 H5 红按钮立刻静音 AI 出声
- **效果度量**：AI 承接率、决策延迟、观众在问什么、知识库缺口
- **安全**：token 鉴权 + 限流 + admin API 仅 Header + systemd 加固

## 快速开始

### 直接打开（已部署）

```
https://live.yanten.top/
```

Basic Auth 登录一次，浏览器会记住；地址栏不需要带 token。
旧的 `https://zhubo.yanten.top/` 仍可用。

### 本地调试（3 步）

```bash
git clone git@github.com:foleydang/live-copilot.git
cd live-copilot
./dev.sh
```

`dev.sh` 自动建 venv、装依赖、生成 token、起服务，打印带 token 的链接。打开即可。

### 生产部署

详见 [docs/DEPLOY.md](docs/DEPLOY.md)，核心步骤：

```bash
git clone git@github.com:foleydang/live-copilot.git /root/github/live-copilot
cd /root/github/live-copilot
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp tenants.example.yaml tenants.yaml  # 编辑：填你的直播间号
chmod 600 tenants.yaml
sudo cp deploy/live-copilot.service /etc/systemd/system/
sudo mkdir -p /etc/systemd/system/live-copilot.service.d
sudo cp deploy/dashscope.conf /etc/systemd/system/live-copilot.service.d/
sudo systemctl daemon-reload
sudo systemctl enable --now live-copilot
```

## 配置

| 文件 | 用途 | 必填 |
|---|---|---|
| `config.yaml` | 主配置（平台、LLM、TTS、社交互动） | ✅ |
| `tenants.yaml` | 租户表（token → 直播间映射） | ✅ 多租户必填 |
| `admin.yaml` | 管理 API token | 管理操作必填 |
| `kb/tenants/<id>/profile.json` | 租户知识库（店主手机填写） | 自动生成 |

关键环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `DASHSCOPE_API_KEY` | — | LLM + TTS 共用；无 key 自动降级为本地规则 |
| `LIVE_COPILOT_CONFIG` | config.yaml | 主配置路径 |
| `LIVE_COPILOT_ADMIN_TOKEN` | — | 管理令牌 |
| `HOST` | 127.0.0.1 | 绑地址；公网设 0.0.0.0，nginx 反代后改回 |
| `PORT` | 8010 | 监听端口 |
| `LIVE_COPILOT_PROXY_TOKEN` | — | nginx 注入的免 token 内部令牌 |
| `LIVE_COPILOT_PROXY_TENANT` | — | 免 token 通道默认放行的租户 id |

## API 一览

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/` | — | H5 助播台 |
| WS | `/ws?t=<token>` | token | 实时通道：提词 + 语音 + 状态 |
| GET | `/api/health` | — | 公开健康探针（含真实连接状态） |
| POST | `/api/danmaku` | token | 弹幕入队（不阻塞） |
| POST | `/api/announce` | token | 触发 AI 声明口播 |
| GET | `/api/kb` | token | 读取知识库 |
| POST | `/api/kb` | token | 保存知识库（入队热更新 + 预合成） |
| POST | `/api/mute` | token | 一键静音 |
| GET | `/api/stats` | token | 本场统计 |
| GET | `/api/usage` | token | 本月用量（LLM tokens / TTS chars） |
| GET | `/api/report` | token | 效果看板（承接率/延迟/缺口） |
| GET | `/api/admin/tenants` | admin | 列出所有租户 |
| POST | `/api/admin/tenants` | admin | 新增租户（即时生效） |
| DELETE | `/api/admin/tenants/{id}` | admin | 删除租户 |
| POST | `/api/admin/rotate/{id}` | admin | 重置 token |
| POST | `/api/admin/mute/{id}` | admin | 远程静音某租户 |

## 项目结构

```
live-copilot/
├── server.py              FastAPI 路由 + lifespan + 限流 + 维护线程
├── dev.sh                 本地一键起服务
├── src/
│   ├── rooms.py           多租户房间管理（队列 + 采集 + 状态）
│   ├── pipeline.py        弹幕路由 + 防重复 + 护栏 + 留痕
│   ├── ratelimit.py       鉴权限流
│   ├── brain/
│   │   ├── llm_client.py  LLM 客户端（复用连接 + 重试 + 缓存 + 用量）
│   │   ├── local_rules.py 本地规则降级
│   │   └── guard.py       违禁词 + 截断
│   ├── danmaku/           弹幕源 + 适配器（douyinLive / BarrageGrab）
│   ├── output/sinks.py    WebSocket 广播（带重连回放）
│   ├── voice/tts.py       CosyVoice + 缓存 + LRU 清理
│   ├── kb_store.py        租户知识库
│   ├── analytics.py       效果度量
│   └── config.py          配置加载
├── static/assist.html     H5 助播台
├── docs/                  架构 / 安全 / 部署 / 路线图 / 客户端选型
├── deploy/                systemd unit + drop-in + logrotate
├── tests/                 smoke + 单元测试
└── tools/                 运营脚本
```

## 文档

- [架构说明](docs/ARCHITECTURE.md) — 数据流、线程模型、改动总览
- [安全部署](docs/SECURITY.md) — 上线清单 + 应急 SOP
- [部署指南](docs/DEPLOY.md) — 命令 + 环境变量
- [本地调试](docs/LOCAL-DEV.md) — dev.sh + 手机联调
- [客户端选型](docs/CLIENT.md) — H5 vs 微信小程序决策
- [产品路线图](docs/ROADMAP.md) — v0.2 → v1.0 → v2.0
- [产品设计原始文档](docs/PRODUCT-DESIGN.md) — POC 完整设计

## 技术栈

- **后端**：Python 3.11 / FastAPI / uvicorn / httpx
- **AI**：DashScope（GLM / Qwen LLM + CosyVoice TTS）
- **前端**：原生 HTML + WS（零构建、零依赖、手机秒开）
- **部署**：systemd + nginx（建议）+ Let's Encrypt

## License

Private. 仅供内部使用。

---

*AI 只接重复性问答与互动，工艺讲解、定制、合作一律转主播。*
