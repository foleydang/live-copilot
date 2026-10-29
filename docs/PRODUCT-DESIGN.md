# AI 助播 POC · 真人在场 · 双手忙碌型直播间

给手工紫陶工作室（以及一切"主播必须专心动手、没法看弹幕"的直播间）做的 AI 助播系统。
**真人出镜做真实劳动，AI 只承接重复性问答与互动**——不做数字人形象、不做虚拟主播。

设计依据见同目录上级的《AI主播技术服务创业方向调研报告.md》：形象/驱动层已被商品化，
"直播大脑"是全链条最弱一环，而平台政策把"纯 AI 无人直播"逼进了死路。本 POC 走的正是
"只卖大脑、不碰脸和场地"的路线。

---

## 一、架构与数据流

```
 ┌─────────────┐   弹幕    ┌──────────────────── 阿里云服务器 ────────────────────┐
 │ 抖音直播间   │ ────────> │ 采集 → 意图分类(LLM) → FAQ RAG → 违禁词护栏 → 防重复  │
 │ 视频号直播间 │           │                        ↓                             │
 └─────────────┘           │              CosyVoice TTS 合成                      │
                           └──────────────────────┬───────────────────────────────┘
                                                  │ WebSocket
                                                  ▼
                                     ┌────────────────────────┐
                                     │ H5 助播台（闲置手机/平板）│
                                     │  · 提词区：大字待答问题   │
                                     │  · 音频：AI 助播语音      │
                                     └───────────┬────────────┘
                                                 │ 3.5mm/Type-C 音频线
                                                 ▼
                                     ┌────────────────────────┐
                                     │  直播声卡（AUX/伴奏输入）│──► 直播手机（正常开播）
                                     └────────────────────────┘
```

**两条输出通道**

| 通道 | 内容 | 观众能听到 | 实现 |
|---|---|---|---|
| `assist` AI 助播 | 价格、发货、售后、常见工艺、欢迎、感谢 | ✅ 经声卡混入直播流 | 云端 TTS → H5 播放 → 声卡 |
| `prompt` 提词 | 定制、合作、批发、复杂工艺、违禁风险问题 | ❌ 只在屏幕上 | H5 大字显示，主播抬头一扫 |

**为什么提词用视觉而不是语音**：双手忙碌的主播戴耳机听语音提醒，会被现场噪音干扰、
且打断专注；大字提词扫一眼即可，还不占用直播音频通道。

---

## 二、硬件清单（手机开播方案，总成本约 400-800 元）

手机直播 App 只认麦克风输入，所以 AI 声音**必须经外置直播声卡混入**——这是不依赖电脑的唯一路径。

| 物品 | 说明 | 参考价 |
|---|---|---|
| 直播声卡 | 森然播吧2代 / 十盏灯 等，需有"AUX/伴奏输入"和"耳机监听" | 200-500 元 |
| 3.5mm 对录线 | 助播设备 → 声卡 AUX 输入 | 10-20 元 |
| 闲置手机或平板 | 打开 H5 助播台，兼作提词屏 | 已有则 0 元 |
| 阿里云服务器 | 2核4G 轻量应用服务器即可 | 约 60-100 元/月 |

**接线**

```
闲置手机(耳机孔/Type-C) ──对录线──► 声卡「AUX/伴奏输入」
主播麦克风 ─────────────────────► 声卡「话筒输入」
声卡「输出/直播口」 ─────────────► 直播手机（被识别为外接麦克风）
声卡「耳机监听」 ────────────────► 主播耳机（可选，监听混音效果）
闲置手机屏幕 ────────────────────► 立在主播视线内，当提词屏
```

> 声卡的"伴奏"通道会和话筒混音后一起送进直播手机，这正是我们要的效果。
> 记得把伴奏音量调到低于人声，AI 是助手不是主角。

**如果用电脑开播**（可选，更简单）：装 BlackHole(mac) / VB-CABLE(win) 虚拟声卡，
把 `config.yaml` 的 `tts.assist_device` 填成虚拟声卡名，OBS 里加一个音频输入源即可，
不需要买声卡。

---

## 三、5 分钟本地验证（不花钱、不需要 key）

```bash
cd live-copilot-poc
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 1) 单元测试（护栏 + 路由 + 平台合规开关）
.venv/bin/python tests/test_core.py

# 2) 端到端跑一遍样例弹幕（mock TTS，只打印不发声）
.venv/bin/python run.py --dry-run --mock-file sample_danmaku.txt

# 3) 场次复盘
.venv/bin/python -m src.replay sessions/<刚才生成的文件>.jsonl
```

预期：价格/发货/工艺/欢迎 → `助播🔊`；定制/批发/砍价 → `提词📋`；灌水/空弹幕 → 忽略。
25 秒内同类问题第二次出现会自动转提词（防机械重复）。

---

## 四、云端部署（阿里云）

### 4.1 裸机 + systemd

```bash
# 服务器上
git clone <你的仓库> /root/github/live-copilot-poc && cd /root/github/live-copilot-poc
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

export DASHSCOPE_API_KEY=sk-xxxx        # 百炼 API Key（LLM + CosyVoice 共用）
.venv/bin/python server.py              # 先手动跑一次确认无误

cp deploy/live-copilot.service /etc/systemd/system/   # 记得先把 Key 填进 unit 文件
systemctl daemon-reload && systemctl enable --now live-copilot
```

### 4.2 Docker

```bash
docker build -t live-copilot .
docker run -d --name live-copilot -p 8000:8000 \
  -e DASHSCOPE_API_KEY=sk-xxxx -v $(pwd)/sessions:/app/sessions live-copilot
```

### 4.3 手机端访问

浏览器打开 `http://<服务器IP>:8000/` → 点「开始助播」。

> **建议配 HTTPS + 域名**（阿里云免费证书 + Nginx 反代）。原因：iOS Safari 和微信内置
> 浏览器对非安全源的音频自动播放、`speechSynthesis`、Wake Lock 都有限制，
> 走 `https://` + `wss://` 最稳。局域网内用 http 调试没问题。

**H5 页面使用说明**
- 「开始助播」按钮必须点一次——iOS/Android 都要求用户手势才能解锁音频，同时会申请屏幕常亮
- 提词区超过 3 分钟的条目自动变灰，避免主播被过期问题干扰
- 「AI 声明」按钮可随时重播合规声明（开播后 1 分钟内必须播一次）
- 联调区可手动灌弹幕，正式直播时不用

---

## 四点五、hkserver 部署记录（已退役，保留作参照）

已在 `hkserver`（阿里云 HK ECS，47.242.158.242，x86_64，1870MB 内存）部署验证通过。

**服务地址：`http://47.242.158.242:8010/`**（手机浏览器打开，点「开始助播」）

| 项 | 值 |
|---|---|
| 应用目录 | `/root/github/live-copilot`（venv 在 `.venv`，Python 3.13） |
| 采集器 | `/root/github/live-copilot/bin/douyinLive`（linux/amd64，本地交叉编译后上传） |
| 采集器工作目录 | `/root/github/live-copilot/collector`（**必须是独立空目录，见下方坑**） |
| systemd 单元 | `live-copilot.service`（:8010）、`douyinlive.service`（:1088），均已 enable |
| 日志 | `/var/log/live-copilot.log`、`/var/log/douyinlive.log` |
| 端口 | 8010（8000 被 stock-api 占用）；firewalld 与阿里云安全组均已放行 |

### 内存护栏（这台机器有 OOM 历史，必须遵守）

机器上已有 `openclaw-gateway`（MemoryMax=1300M, OOMScoreAdjust=200）与
`stock-feishu-bot`（MemoryMax=1500M, OOMScoreAdjust=-500）两个关键服务，可用内存仅约 1GB。
**本机是 cgroup v1，只有 `MemoryMax` 和 `OOMScoreAdjust` 真正生效**（`MemoryHigh/Low/Min`
被静默忽略；`systemctl show -p MemoryLimit` 报 infinity 不反映真实值，必须读
`/sys/fs/cgroup/memory/<...>/memory.limit_in_bytes` 验证）。

新服务按"最先被杀"配置，绝不牵连那两个 bot：

```
live-copilot    MemoryMax=400M  OOMScoreAdjust=300   实测 RSS 58MB
douyinlive  MemoryMax=300M  OOMScoreAdjust=300   实测 RSS 47MB
```

杀序 300（本服务）> 200（gateway）> -500（stock bot），已读 `/proc/PID/oom_score_adj` 验证。
两服务合计约 105MB，部署后整机可用内存 986MB，原有四个服务全部 active。

### ★ 坑一：douyinLive 会误读应用的 config.yaml

douyinLive 启动时**自动从工作目录加载 `config.yaml`**。若把它的工作目录设成 `/root/github/live-copilot`，
它会读到本应用的配置并因字段不匹配拒绝启动，服务卡在 `activating` 反复重启：

```
line 48: field routing not found in type server.configFileSchema
line 53: field disclosure not found in type server.configFileSchema
解决方法: 在同目录下创建 config.yaml，或使用命令行参数 douyinLive --port 1088
```

**解法：给它独立空工作目录**（`WorkingDirectory=/root/github/live-copilot/collector`），
找不到 config.yaml 就用默认值监听 1088。

### ★ 坑二：办公电脑跑不了采集器

douyinLive 二进制在办公电脑上被系统防护策略（云壳）直接 SIGKILL（exit 137），需报备。
**所以采集器只能跑在云服务器上**——这也正是生产架构本来的样子。
本地开发用 `tools/douyin_sim.py`（纯 Python，不被拦）。

交叉编译（本地只编译不执行，不触发防护）：

```bash
git clone https://github.com/jwwsjlm/douyinLive.git && cd douyinLive
GOOS=linux GOARCH=amd64 CGO_ENABLED=0 go build -o douyinLive-linux-amd64 ./cmd/main
scp douyinLive-linux-amd64 hkserver:/root/github/live-copilot/bin/douyinLive
```

### 真实链路验证结果

用 douyinLive 官方文档里的示例房间号做了一次真实联调：**HK 出口 IP 可以正常握手抖音弹幕
WebSocket**（`status_code=101`、`host=webcast100-ws-web-lf.douyin.com`、`sign_implementation=native`），
之前担心的地域限制不成立。真实处理 335 条事件：

```
意图分布: noise 175 | enter 78 | follow 31 | like 18 | price 14 | other 9 | interaction 9 | greeting 1
路由分布: ignore 292 | prompt 28 | assist 15        决策来源: rules 208 | social 127
```

真实数据暴露并已修复的问题：高人气直播间里寒暄类弹幕（"支持一下"）反复撞 25 秒防重复窗口，
原本全部升级成提词，把提词屏刷满垃圾、淹没真正的定制/批发问题。现改为**只有
price/shipping/product/craft 这类高价值意图重复时才升级提词，寒暄类重复直接丢弃**
（测试 `test_low_value_repeat_is_dropped_not_escalated` 锁定）。

另给 ws 重连加了指数退避（5→10→20→40→60s 封顶）：采集器没起时不会每 5 秒刷一条日志，
小磁盘机器上很关键。服务器日志实测确认退避生效（3 条记录，间隔递增）。

### 换成自己直播间

```bash
ssh hkserver
# 1. 改房间号：末尾换成你直播间的 web_rid（开播时从直播间链接取）
vi /root/github/live-copilot/config.yaml      # ws_url: ws://127.0.0.1:1088/ws/你的房间号
# 2.（可选）配百炼 Key，启用 LLM 意图分类 + CosyVoice 真实语音
systemctl edit live-copilot           # 填 Environment=DASHSCOPE_API_KEY=sk-xxx
# 3. 起采集器 + 重启主服务
systemctl start douyinlive && systemctl restart live-copilot
# 4. 看真实弹幕有没有进来
tail -f /var/log/douyinlive.log && curl -s http://127.0.0.1:8010/api/stats
```

> 联调完记得 `systemctl stop douyinlive`，不要长期挂在别人的直播间上。

### 还没解决的：手机端音频与 HTTPS

H5 目前走 `http://`。**iOS Safari 对非安全源的音频自动播放、`speechSynthesis`、Wake Lock
均有限制**，可能在 iPhone 上不出声。三条路，按推荐排序：

1. 助播台用**安卓**设备（限制少，且视频号方案本来也需要一台安卓做无障碍读屏）
2. 买个便宜域名 + 用机器上已有的 nginx 反代 + Let's Encrypt 证书，走 `https://`（最彻底）
3. 先真机实测 http 是否够用——点过「开始助播」后 iOS 通常允许播放

**这一条必须在真机上验证，我无法远程确认。**

---

## 四点五-B、hzserver 生产部署（当前主部署，HTTPS 已就绪）

**服务地址：`https://zhubo.yanten.top/`**（手机浏览器打开，点「开始助播」）

已从 hkserver 迁移过来，HK 那套已 `stop` + `disable`（文件保留在 `/root/github/live-copilot`，188MB，可随时重启）。

### 为什么迁到国内

| 维度 | hkserver（HK） | hzserver（杭州） |
|---|---|---|
| 跨境延迟 | 实测 HK→杭州 **122-157ms** | 境内同区 **9ms**（本机直连 0.57ms） |
| HTTPS | 无（http://IP:8010） | **已有**，Let's Encrypt 证书 + 自动续期 |
| iOS 音频 | 非安全源，`speechSynthesis`/Wake Lock 受限，可能不出声 | HTTPS 下正常 |
| 抖音风控 | 境外 IP，长期是未知数 | 境内 IP，更自然 |
| 公网端口 | 需放行 8010 | 走 443，无需额外放行 |

对一个核心指标是"1 秒内出声"的实时产品，跨境抖动正好打在要害上。

> 测量方法说明：以上数字都是**同一观测点**测的（从 hzserver 发起）。早先"8ms vs 152ms"
> 的说法是无效对比——8ms 来自服务器本机、152ms 来自办公电脑跨境，观测点不同。
> 真实手机端（云南）的延迟我没有观测点，未测量。

### 部署事实

| 项 | 值 |
|---|---|
| 机器 | Alibaba Cloud Linux 3，x86_64，2 核，1904MB 内存，40G 磁盘（可用 21G） |
| Python | **3.11.13**（`dnf install python3.11`，系统自带的 3.6/3.8 未动） |
| 应用目录 | `/root/github/live-copilot`（venv `.venv`） |
| 采集器 | `/root/github/live-copilot/bin/douyinLive`（本地交叉编译 linux/amd64 后上传） |
| 采集器工作目录 | `/root/github/live-copilot/collector`（**必须独立空目录**，见四点五坑一） |
| systemd | `live-copilot.service`（:8010，enabled）、`douyinlive.service`（:1088，disabled，需填房间号后手动起） |
| nginx | `aa_nginx`，配置 `/etc/aa_nginx/aa_nginx.conf`；新增 `include /etc/aa_nginx/conf.d/*.conf;` |
| 站点配置 | `/etc/aa_nginx/conf.d/zhubo.conf` + `00-websocket-map.conf` |
| 证书 | `/etc/letsencrypt/live/zhubo.yanten.top/`，有效期至 **2027-01-24**，`certbot-renew.timer` 每日两次自动续期 |
| DNS | `*.yanten.top` 泛解析到 47.111.181.116，**新增子域名无需任何 DNS 操作** |
| 内存护栏 | `MemoryMax=500M` + `OOMScoreAdjust=300`（cgroup v1 实读 limit=500MB / usage=40MB） |

### nginx 改动的安全边界

现有生产站点（`yanten.top` 静态站、`api.yanten.top` 反代到 :3000 和 :5000）**一个都没改**。做法：

1. 先 `cp -a` 备份主配置（`aa_nginx.conf.bak.<时间戳>`）
2. 只在 `http{}` 末尾加一行 `include /etc/aa_nginx/conf.d/*.conf;`
3. 新站点全部写在独立的 `conf.d/zhubo.conf`
4. **`nginx -t` 通过才 reload**，失败即退出不动线上

改完复查：`https://yanten.top/` 200、`https://api.yanten.top/` 200、`http://yanten.top/` 仍 301。

踩到的两个 nginx 版本坑（1.22.1）：
- 不支持独立指令 `http2 on;`（1.25.1+ 才有），要用 `listen 443 ssl http2;`
- 这个编译版本**没有 `ngx_http_v2_module`**，`http2` 参数直接报错 → 最终去掉 http2。
  WebSocket 升级本来就走 HTTP/1.1，不影响功能。

另一个坑：`systemctl reload nginx | tail -2 || nginx -s reload` **不会执行兜底命令**——
管道退出码来自 `tail`（永远成功）。这台机器 nginx 不在 systemd 里（`nginx.service` 不存在），
必须直接 `nginx -s reload` 并单独判退出码。

### WebSocket 反代要点（漏一个提词就会成批延迟到达）

```nginx
proxy_http_version 1.1;
proxy_set_header Upgrade    $http_upgrade;
proxy_set_header Connection $connection_upgrade;   # 需要 http 级的 map，见 00-websocket-map.conf
proxy_read_timeout  3600s;
proxy_send_timeout  3600s;
proxy_buffering     off;
```

已实测 WSS 双向打通：带 token 连上收到 hello，从另一路 `POST /api/danmaku` 灌弹幕，
H5 侧通过 WSS 收到 `type=audio` 推送。无 token 的 WS 被拒（客户端看到 403）。

### ★ 办公电脑访问不了这个域名

`zhubo.yanten.top` 会被公司终端防护（云壳）拦截，返回 403 拦截页：
"您要访问的域名不在安全策略默认允许的范围内"。**这是云壳拦的，不是服务的问题**
（从 hzserver 本机和 hkserver 访问都是 200）。

要在办公电脑上验证，需按拦截页提示走"云壳-防护记录-域名拦截"免批加白或报备。
**家人用普通手机流量/家用 WiFi 访问不受影响**——真机测试请用手机，别用办公电脑。


## 四点六、租户鉴权（公网部署必配，已在 hkserver 启用）

服务跑在公网 IP 上，**无鉴权时任何人都能看提词内容、灌假弹幕、触发 AI 声明**（实测确认过）。
接第二个客户之前必须先配上。

```bash
# 1. 生成 token
python -c "from src.auth import new_token; print(new_token())"
# 2. 建租户表（参考 tenants.example.yaml，该文件已 gitignore）
cat > tenants.yaml <<'EOF'
tenants:
  - id: ztao-studio
    name: 手工紫陶工作室
    room: ""
    token: <上一步生成的>
EOF
chmod 600 tenants.yaml
# 3. 重启，日志应出现「租户鉴权 已启用（N 个租户）」
systemctl restart live-copilot
```

**访问方式**：手机打开 `http://<服务器IP>:8010/?t=<token>`（token 会存 localStorage，下次可省）。
API 用 `X-Token` 头或 `?t=` 参数；WS 用 `/ws?t=<token>`。

**已验证的拦截行为**

| 场景 | 结果 |
|---|---|
| 无 token 打 4 个 API | 全部 401，且日志记录来源 IP |
| ASCII 错 token / 正确 token 加前后缀变形 | 全部 401（用 `hmac.compare_digest` 常量时间比较，无子串匹配漏洞） |
| WS 无 token / 错 token | 拒绝（Starlette 在 accept 前关闭，客户端看到 403） |
| WS 正确 token | 连上并收到 hello（含 tenant 与 room） |
| 公网带 token 打开 H5 | 200 |

> 已知无害行为：HTTP 头里塞非 ASCII 字符（如中文）会返回 500 而非 401——h11 在进入业务代码前
> 就拒绝了该请求，日志里没有 `[auth] 拒绝` 记录可证，不泄露任何数据。

**加一个客户 = 往 `tenants.yaml` 加一条记录**，不用改代码。但注意当前是单房间架构：
一个进程服务一个直播间，鉴权只解决"谁能看/谁能操作"。真正的多租户隔离见下节。

## 四点七、多租户与傻瓜化操作（已实现并验证）

**一个进程服务 N 个直播间**，每个租户独立的 Pipeline / 知识库 / 提词队列 / 留痕目录 / WebSink，
A 店的数据绝不会出现在 B 店手机上（`tests/test_multitenant.py` 29 项断言实测覆盖）。

采集器不是瓶颈：**一个 douyinLive 实例可服务多个房间**（房间号在 WS 路径里，每租户一条连接）。
实测每租户内存增量约 8-10MB，主进程 57MB。

### 运营者：加一个客户（本地一条命令，不用 SSH）

```bash
export LIVE_COPILOT_SERVER=http://47.242.158.242:8010
export LIVE_COPILOT_ADMIN_TOKEN=<管理令牌，在服务器 /root/github/live-copilot/admin.yaml>

python tools/add_customer.py --id laowang --name "建水紫陶·老王"
python tools/add_customer.py --id chenshi --name "木雕·陈师傅" --platform channels  # 视频号店，AI 自动静音
python tools/add_customer.py --list             # 列出客户、链接、资料是否已填
python tools/add_customer.py --rotate laowang   # 链接泄露时重置，旧链接立即失效
python tools/add_customer.py --remove chenshi
```

新增客户**立即生效，不需要重启服务**，并自动写回 `tenants.yaml`（重启后仍在）。
脚本直接打印可转发给客户的链接。

管理 API（`X-Token: <admin token>`）：`GET/POST /api/admin/tenants`、
`DELETE /api/admin/tenants/{id}`、`POST /api/admin/rotate/{id}`。
未配置 admin token 时管理 API 全部 403（默认关闭，更安全）。

### 客户：手机上三步自助配置（不碰命令行、不碰 YAML）

打开链接 → 点「开始助播」→ 进「设置」页填表 → 点「保存并生效」。

设置页只问店主本来就知道的事：直播间号、AI 助手叫什么、欢迎语、发货、定制、
答不上来怎么说、商品和价格（一行一个）、常见手艺问答（一行一个）、本店规矩（一行一条）。

保存后**热更新立即生效**（不重启）：后端把表单编译成关键词 FAQ + LLM 人设提示词，
并预合成 FAQ 语音（配了百炼 Key 时）。直播间号也在这页填，填完自动拉起采集。

### 编译规则里几个刻意的设计（都是实测踩出来的）

| 设计 | 原因 |
|---|---|
| **每个商品单独一条价格 FAQ** | 问"主人杯多少钱"只答主人杯，不念整张价格表；也避免不同商品因答案相同被防重复误判 |
| 商品没填价格 → 引导看小黄车 | 绝不编造价格 |
| 店主规矩编译成可命中的 FAQ | 否则没配 LLM Key 时（走本地规则层）店主写的"不议价""不私下交易"完全不生效 |
| 护栏支持否定语境 | "不私下交易"里的"私下"曾命中违禁词表，导致 AI 说不出店主自己的规矩。拒绝 ≠ 招揽 |
| 防重复按「意图+答案」计时 | 只按意图会让 10 秒内两个不同商品的价格问题被误判为机械重复 |
| 私下交易类关键词覆盖变体 | "加我微信"不含子串"加微信"，精确短语匹配会漏 |
| 空资料不生成任何 FAQ | 宁可转主播，也不让 AI 瞎答 |

### 成本模型（按已核实的百炼单价）

单价：cosyvoice-v2 **2 元/万字符**；qwen-plus 输入 0.8 元/百万 token、输出 2 元/百万 token
（qwen-flash 官方页该行被截断未取到，用 qwen-plus 价作保守上限）。

| 项 | 不优化 | 优化后 | 说明 |
|---|---|---|---|
| TTS | 36 元/月 | **6 元/月** | 小工作室 4h/天、150 条回复/天 × 40 字；优化=FAQ 答案预合成缓存（已实现） |
| LLM | 3.5 元/月 | **0.6 元/月** | 每次约 820 输入+60 输出 token ≈ 0.00078 元；优化=本地预过滤（真实数据 85% 是灌水/进场/点赞） |
| 算力 | 6 元/月 | **1.5 元/月** | 多租户后每房间约 8-10MB |
| **合计** | ~46 元/月 | **~12 元/月** | 大流量直播间不缓存 TTS 会到 480 元/月，**缓存是必做项** |

对照调研定价：白牌 SaaS 1980-4980 元/年（165-415 元/月），服务商收商家 4000 元/月。
定价 299 元/月、成本 12 元 → **毛利率约 96%**。
一次性硬件（客户自购）：声卡 200-500 元 + 对录线 + 闲置手机。

> 真正的成本瓶颈是 onboarding 人力，不是服务器。手机自助配置页就是为了把它从
> 1-2 小时压到 10-20 分钟。

## 五、弹幕接入（两个平台差别很大，务必看）

### 抖音

**部署拓扑要点：采集器跑在阿里云服务器上，不要装在办公电脑上。**
办公电脑的系统防护策略（如云壳）会拦截这类未报备的逆向采集二进制——实测被 SIGKILL。
生产链路本来就全在云端，办公电脑只需要浏览器打开 H5 助播台。

采集器推荐 [jwwsjlm/douyinLive](https://github.com/jwwsjlm/douyinLive)（Go，独立服务模式），
已核实其接口契约：

```bash
# 阿里云服务器上
git clone https://github.com/jwwsjlm/douyinLive.git && cd douyinLive
go build -o douyinLive ./cmd/main        # 实测 go1.24 构建通过，产物约 59MB
./douyinLive                             # 默认监听 127.0.0.1:1088
# 也可用官方镜像：docker run --rm -p 1088:1088 ghcr.io/jwwsjlm/douyinlive:latest
```

直播间标识**通过 WebSocket 路径传入**，不是启动参数：

```yaml
# config.yaml
source: ws
ws_url: ws://127.0.0.1:1088/ws/516466932480   # 末尾换成你直播间的 web_rid / room_id
```

消息格式（`src/danmaku/adapters.py` 已按此实现，并有单测覆盖）：

| 消息 | 结构 | 内部事件 |
|---|---|---|
| 系统状态 | `{"type":"system","event":"live_status","code":"ROOM_OFFLINE","live":false,...}` | `status`（**不会当弹幕处理**，只打印/回调） |
| 弹幕 | `{"method":"WebcastChatMessage","user":{"nickname":".."},"content":".."}` | `chat` |
| 表情弹幕 | `{"method":"WebcastEmojiChatMessage",...}` | `chat` |
| 礼物 | `{"method":"WebcastGiftMessage","user":{..},"gift":{"name":".."},"count":3}` | `gift` |
| 点赞 | `{"method":"WebcastLikeMessage","user":{..}}` | `like` |
| 进场 | `{"method":"WebcastMemberMessage","user":{..}}` | `enter` |
| 关注/分享 | `{"method":"WebcastSocialMessage","user":{..}}` | `follow` |

douyinLive 要求客户端每 30s 发文本 `ping`（服务回 `pong`），`ws_source.py` 已内置保活线程。

**社交消息是双手忙碌型主播的刚需**：`config.yaml` 的 `social` 段控制礼物/关注即时感谢、
点赞聚合感谢（默认 120s 一次）、进场欢迎（默认关，人多会刷屏）。

其他可选采集器：[BarrageGrab](https://gitee.com/DonkeySmall/BarrageGrab)
（`ws://127.0.0.1:8888`，格式 `{"Type":3,"Data":{"User":{"NickName":..},"Content":..}}`，
适配器已支持，且它同时覆盖快手/视频号/TikTok）、
[DySpider](https://github.com/zhonghangAlex/DySpider)（Python）。
也可以完全不用 ws，由任意外部脚本 `POST /api/danmaku` 推给服务。

**本地无采集器时用模拟器**（纯 Python，不会被防护策略拦）：

```bash
python tools/douyin_sim.py                        # douyinLive 格式，:1088
python tools/douyin_sim.py --format barragegrab   # BarrageGrab 格式，:8888
python tools/douyin_sim.py --interval 0.9 --loop  # 加快节奏、循环
```

> ⚠️ 这些都是逆向 WebSocket 协议的手段，抖音改版就会失效，**只适合自用 POC**，
> 且在公司设备上运行可能需要报备。商业化必须走抖音开放平台官方能力
> （弹幕相关权限需后台申请，部分是定向邀约，且通常要求服务商资质）——
> 这正是调研报告里"B2B2B 借牌照"结论的由来。

### 视频号

**微信没有开放任何第三方直播弹幕接口**（腾讯云开发者社区有官方问答确认）。三条现实路径：

| 方案 | 做法 | 评价 |
|---|---|---|
| A. Android 无障碍读屏（推荐） | 一台闲置安卓手机打开视频号直播间，用无障碍服务读评论区文本，`POST /api/danmaku` 上报 | 不碰协议逆向，最稳；仅限 Android |
| B. OCR | 电脑端微信打开直播间，截取评论区做 OCR（或多模态模型识别） | 通用但延迟 1-2s、需一台常开设备 |
| C. 社区工具 | wxlivespy 等 | 稳定性与合规性都差，不建议 |

> ⚠️ **视频号合规红线**：平台把"数字人直播、虚拟人直播"整体归为**非真实直播违规**，
> 且明确反对买卖 AI 代播工具。虽然本方案是真人在场做真实劳动、AI 只是辅助答疑，
> 但 AI 语音出声仍有被判定风险。因此 `platform: channels` 时 **`output.assist_voice`
> 默认为 false**（只用提词通道，AI 不出声）。要强行开启需自己在 config 里显式打开，
> 程序会打印风险告警。建议视频号先用提词模式跑一段，确认平台不判违规再考虑开声。

---

## 六、合规清单（依据 2025-09 与 2026-02 两部新规）

- [x] **显式标识**：服务启动自动口播 AI 声明；`kb/disclosure.md` 提供常驻字幕文案
      （《人工智能生成合成内容标识办法》2025-09-01 施行）
- [x] **留痕**：每条弹幕的意图、决策、AI 原话写入 `sessions/*.jsonl`，
      文件名含 pid+微秒避免同秒覆盖（《直播电商监督管理办法》第 37 条持续提示义务的证据）
- [x] **违禁词护栏**：`kb/banned_words.txt` 命中即丢弃 AI 回复并转提词，AI 绝不发声
- [x] **不克隆声纹**：固定使用平台预置音色（`tts.voice`），代码层不提供音色克隆入口
- [x] **不自动发评论**：只走音频通道，不触碰平台"自动发评论"权限（抖音该项属定向邀约）
- [x] **平台差异化**：视频号默认静音
- [ ] **待你补**：按抖音/视频号最新违规清单扩充违禁词；每场保存推流录像 ≥60 天

---

## 七、验证记录（我实际跑过的）

| 项 | 结果 |
|---|---|
| 单元测试 **17 项** | 全部通过（核心 8 项：护栏/超长/意图/FAQ打分/路由留痕/违禁降级/视频号静音/抖音出声；适配器 9 项：douyinLive 与 BarrageGrab 全消息类型、未开播状态不当弹幕、控制消息忽略、camelCase 昵称兼容、空/垃圾载荷） |
| **真实链路**：模拟器（douyinLive 格式）→ 云端服务 → 管线 | 累计处理 **350+ 条**事件，含 chat/gift/like/enter/follow 五类；统计 `assist 74 · prompt 153 · ignore 109 · social 64` |
| H5 浏览器实测（真实 DOM 校验） | WebSocket 连接指示灯亮起、房间名由 `hello` 消息注入、统计实时刷新、提词区 12 条 / 助播区 6 条正常渲染、关注感谢正确触发 |
| `GET /` H5 页面 | HTTP 200 |
| `POST /api/danmaku` 路由 | 价格→assist、定制→prompt，均正确 |
| `GET /api/stats` `/api/pending` | 统计与待办清单正确 |
| WebSocket `/ws` 推送 | hello + audio + prompt 三类消息均正常送达 |
| douyinLive 真实构建 | `go build` 通过（go1.24，产物 59MB）；**但办公电脑被云壳拦截（SIGKILL）**，需在云服务器运行或先报备 |
| 全部 Python 文件语法编译 | 通过 |

**尚未验证（需要你接上真实环境）**
- **真实直播间的弹幕抓取**：douyinLive 二进制在本机被防护策略拦截，我只验证到"构建成功 + 接口契约与适配器单测一致"。真实房间联调需在云服务器上做（那里没有终端防护）。
- CosyVoice 真实合成与延迟（需百炼 API Key；当前用 mock 引擎 + 浏览器 Web Speech 兜底）
- 声卡硬件链路的实际音质与延迟
- 端到端互动延迟（目标 <1.5s：弹幕→LLM≈0.5-1s，TTS 首包≈150ms）
- 视频号弹幕（微信无官方接口，需自建安卓无障碍读屏或 OCR）

**调试中修掉的真实缺陷**（都已加测试锁定）
1. 留痕文件名只精确到秒 → 同秒重启/多房间会串场，已加 pid+微秒
2. LLM 依赖缺失时直接崩溃 → 改为 try 包裹并降级本地规则（直播不能因为缺依赖就断）
3. FAQ"首个关键词命中"→"烧窑要几天"被宽泛词"几天"抢判为发货问题，已改为按命中关键词总长度打分
4. 未开播状态消息若被当弹幕喂给大脑会产生胡言乱语 → 适配器单独识别为 `status` 并拦下

---

## 八、已知限制与商业化切换点

1. **弹幕采集是最大不确定性**。逆向方案随时可能失效；商业化必须换官方接口，
   而官方接口需要服务商资质（抖音）或根本不存在（视频号）。
2. **本地关键词规则只是兜底**。真实直播间的弹幕噪音、错别字、方言、反讽，
   需要 LLM 层才扛得住——务必配 `DASHSCOPE_API_KEY`。
3. **FAQ 知识库决定体验上限**。`kb/faq.yaml` 现在是示例内容，
   必须换成工作室真实的价格表、发货规则、定制流程、工艺术语。
4. **单房间架构**。当前一个进程服务一个直播间；多房间需要把 room 作为参数化维度
   （商业化时再做）。
5. **成本**：LLM 用 qwen-flash，单场几百条弹幕的推理成本在几元量级；
   CosyVoice 按字符计费，一场 4 小时约几元到几十元。整体月成本远低于一个真人场控。

---

## 九、目录结构

```
live-copilot-poc/
├── run.py                  本地调试入口（终端跑通全链路）
├── server.py               云端服务（FastAPI + WebSocket + H5）
├── config.yaml             全部配置（平台/LLM/TTS/路由/社交消息/合规开关）
├── sample_danmaku.txt      16 条真实感弹幕样本，用于端到端验证
├── kb/
│   ├── faq.yaml            FAQ 知识库（★ 必须替换成真实信息）
│   ├── tone.md             AI 助播人设与硬性话术约束
│   ├── banned_words.txt    违禁词清单（★ 需按平台最新规则扩充）
│   └── disclosure.md       AI 标识合规文案与红线
├── src/
│   ├── pipeline.py         核心管线：决策→护栏→防重复→路由→留痕（含社交消息感谢）
│   ├── replay.py           场次复盘
│   ├── config.py           配置与知识库加载
│   ├── danmaku/
│   │   ├── adapters.py     ★ douyinLive / BarrageGrab 消息格式适配（按源码文档核实）
│   │   ├── ws_source.py    通用 ws 源（含 30s ping 保活、断线重连、状态消息回调）
│   │   ├── mock_source.py  文件/stdin 弹幕源
│   │   └── base.py         Danmaku 事件模型（含 kind: chat/gift/like/enter/follow）
│   ├── brain/              决策层：LLM 意图分类 + 本地规则兜底 + 违禁词护栏
│   ├── voice/tts.py        CosyVoice 合成（含 mock 引擎，浏览器端可兜底发声）
│   └── output/sinks.py     输出通道：Console / 本机音频 / WebSocket 广播
├── static/assist.html      H5 助播台（手机/平板浏览器打开）
├── tools/
│   ├── douyin_sim.py       ★ 采集器模拟器（按真实格式造流，本地联调不被防护策略拦）
│   └── make_demo.py        从真实留痕生成可交互回放页面
├── tests/
│   ├── test_core.py        核心测试 8 项
│   └── test_adapters.py    格式适配测试 9 项
├── deploy/live-copilot.service systemd unit
└── Dockerfile
```
