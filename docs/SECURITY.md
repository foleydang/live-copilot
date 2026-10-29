# 安全部署清单

## 当前访问方式

```
https://live.yanten.top/          ← 主入口（Basic Auth + 免 URL token）
http://47.111.181.116:8010/?t=…   ← 仅内网/临时调试（公网建议关掉）
```

- **Basic Auth**：用户名 `foley`，密码见部署时生成（浏览器记住一次即可）
- **免 URL token**：nginx 注入 `X-Proxy-Token`，服务端据此放行默认租户
- **客户链接**：仍然用 `?t=<token>`，与上面的测试通道并存

## 现状（已实现）

- ✅ 多租户 token 鉴权（`secrets.token_urlsafe(24)` ≈ 144 bit 熵）
- ✅ 常量时间 `hmac.compare_digest` 防时序侧信道
- ✅ 鉴权失败限流（20 次/分钟/封 2 分钟）
- ✅ admin API 只接受 Header，禁止 URL 传参
- ✅ 留痕路径防穿越（`[^A-Za-z0-9_.-]` 过滤 + `.`/`..` 拒绝）
- ✅ HTML/API 响应 `Cache-Control: no-store`
- ✅ 前端 `history.replaceState` 抹掉 URL token
- ✅ HTTPS（Let's Encrypt，`live.yanten.top`，自动续期）
- ✅ 旧域名 `zhubo.yanten.top` 已下线（`00-retired-hosts.conf` 返回 444）
- ✅ nginx Basic Auth + nginx 注入内部令牌（`X-Proxy-Token`，客户端同名头会被覆盖）
- ✅ systemd 加固：`NoNewPrivileges / PrivateTmp / ProtectSystem / ReadWritePaths`
- ✅ 日志轮转（logrotate 每天 + 50MB 上限）
- ✅ TTS 缓存 / 留痕 6h 自动清理

## 相关文件

| 文件 | 权限 | 说明 |
|---|---|---|
| `/etc/aa_nginx/conf.d/live-copilot.conf` | 644 | 站点配置（含内部令牌） |
| `/etc/aa_nginx/.htpasswd_live` | 640 root:nginx | Basic Auth 密码 |
| `/etc/systemd/system/live-copilot.service.d/proxy.conf` | 600 | 内部令牌 + 默认租户 |
| `/etc/systemd/system/live-copilot.service.d/dashscope.conf` | 600 | DashScope API Key |
| `/root/github/live-copilot/tenants.yaml` | 600 | 客户 token |
| `/root/github/live-copilot/admin.yaml` | 600 | 管理 token |

## 上线前必做

### 1. 阿里云安全组

| 端口 | 来源 | 用途 |
|---|---|---|
| 22 | 仅你的固定 IP | SSH |
| 80 | 任意 | HTTP → 301 到 HTTPS |
| 443 | 任意 | HTTPS |
| 8010 | **删除** | 已由 nginx 接管，不要直连公网 |

改完 systemd：`Environment=HOST=127.0.0.1`，`systemctl restart live-copilot`。

### 2. 改 Basic Auth 密码

```bash
# 生成新密码并写入
openssl passwd -apr1 '你的新密码' | sed 's/^/foley:/' > /etc/aa_nginx/.htpasswd_live
chown root:nginx /etc/aa_nginx/.htpasswd_live && chmod 640 /etc/aa_nginx/.htpasswd_live
nginx -s reload
```

### 3. 客户 token 管理 SOP

- 每个客户发一条 `https://live.yanten.top/?t=<token>`
- 每月调 `/api/admin/rotate/{id}` 换一次
- 合约结束 → `DELETE /api/admin/tenants/{id}`
- 绝不在群里贴完整 URL

### 4. 非 root 运行（商业化前）

```bash
sudo useradd -r -s /sbin/nologin copilot
sudo chown -R copilot:copilot /root/github/live-copilot   # 或迁到 /opt
sudo systemctl edit live-copilot   # User=copilot / Group=copilot
```

> 迁到 `/opt/live-copilot` 更规范，但 `ProtectHome` 需要恢复为 `true`。

### 5. 租户配额

所有租户共用一把 DashScope key，**一个客户能烧光所有额度**。需要：

- 每租户月度 token 上限（如 100 万）
- 80% 推告警、100% 降级到本地规则
- 计量点已埋好：`/api/usage` 的 `llm.prompt_tokens` / `completion_tokens` / `tts.chars`

### 6. 监控

外部探针定时打 `https://live.yanten.top/api/health`（带 Basic Auth），`ok:false` 报警。

## 应急 SOP

| 现象 | 处置 |
|---|---|
| AI 说了违规话 | H5 点「🔇 静音」，立刻生效 |
| token 泄露 | `/api/admin/rotate/{id}`，H5 自动失效，重发链接 |
| Basic Auth 泄露 | 改 `.htpasswd_live` + `nginx -s reload` |
| 内部令牌泄露 | 改 `proxy.conf` 的 `LIVE_COPILOT_PROXY_TOKEN` + 重跑 `gen_nginx` + 重启服务 |
| 服务挂了 | `systemctl status live-copilot` / `journalctl -u live-copilot -n 100` |
| 磁盘满 | `du -sh cache sessions`；维护线程每 6h 清理 |