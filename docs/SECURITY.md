# 安全部署清单

## 现状（已实现）

- ✅ 多租户 token 鉴权（`secrets.token_urlsafe(24)` ≈ 144 bit 熵）
- ✅ 常量时间 `hmac.compare_digest` 防时序侧信道
- ✅ 鉴权失败限流（20 次/分钟/封 2 分钟）
- ✅ admin API 只接受 Header，禁止 URL 传参
- ✅ 留痕路径防穿越（`[^A-Za-z0-9_.-]` 过滤 + `.`/`..` 拒绝）
- ✅ HTML/API 响应 `Cache-Control: no-store`
- ✅ 前端 `history.replaceState` 抹掉 URL token
- ✅ systemd 加固：`NoNewPrivileges / PrivateTmp / ProtectSystem / ProtectHome / ReadWritePaths`
- ✅ 日志轮转（logrotate 每天 + 50MB 上限）
- ✅ TTS 缓存 / 留痕 6h 自动清理

## 上线前必做（客户上线前必须完成）

### 1. HTTPS + 反向代理（当前最大风险）

**当前 8010 直连公网 HTTP**，token 在网络上明文传输，扫描器已经在探了。

最小修复（用现有 `yanten.top` 域名）：

```nginx
# /etc/nginx/conf.d/zhubo.conf
server {
    listen 443 ssl;
    server_name zhubo.yanten.top;
    ssl_certificate     /etc/letsencrypt/live/yanten.top/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/yanten.top/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8010;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # WebSocket
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600s;
    }
}
```

然后：

```bash
# 加 DNS A 记录 zhubo.yanten.top → 服务器 IP
certbot --nginx -d zhubo.yanten.top
# 改 systemd
systemctl edit live-copilot
# 在 [Service] 段加：
Environment=HOST=127.0.0.1
systemctl restart live-copilot
```

### 2. 阿里云安全组

入站规则只保留必要端口：

| 端口 | 来源 | 用途 |
|---|---|---|
| 22 | 仅你的固定 IP | SSH |
| 80 | 任意 | HTTP → 301 到 HTTPS |
| 443 | 任意 | HTTPS |
| 8010 | **删除** | 已被 nginx 接管 |

### 3. 客户 token 管理 SOP

- 每个客户发一个**短链接**（`zhubo.yanten.top/?t=<token>`），客户扫码即开
- 定期（每月）调 `/api/admin/rotate/{id}` 换一次 token
- 客户离职 / 合约结束 → `/api/admin/tenants DELETE`
- **绝不在群里贴完整 URL**，截图会泄露 token

### 4. 敏感文件权限

```bash
# 已经是 600，复查：
ls -l /root/github/live-copilot/tenants.yaml /root/github/live-copilot/admin.yaml /etc/systemd/system/live-copilot.service.d/dashscope.conf
```

## 商业化前必做

### 5. 非 root 运行

```bash
sudo useradd -r -s /sbin/nologin aizhubo
sudo chown -R aizhubo:aizhubo /root/github/live-copilot
sudo systemctl edit live-copilot
# [Service]
User=aizhubo
Group=aizhubo
sudo systemctl restart live-copilot
```

### 6. 租户配额

当前所有租户共用一把 DashScope key，**一个客户能烧光所有额度**。需要：

- 每租户 LLM token 月度上限（如 100 万 tokens）
- 到达 80% 时给店主手机推告警
- 到达 100% 时降级到本地规则 + 通知运营

`src/usage.py` 已经埋好计量点（`llm.prompt_tokens` / `completion_tokens` / `tts_chars`），接一个配额模块即可。

### 7. 监控告警

接一个外部健康检查（UptimeRobot / 阿里云云监控）定时打 `/api/health`，`ok:false` 时报警。

## 应急 SOP

| 现象 | 处置 |
|---|---|
| AI 说了违规话 | H5 上点「🔇 静音」，立刻生效；客户群里道歉 |
| token 泄露 | 调 `/api/admin/rotate/{id}`，所有 H5 自动失效，重发新链接 |
| 服务挂了 | `systemctl status live-copilot` + `journalctl -u live-copilot -n 100` |
| 日志刷屏 | 检查 `/var/log/live-copilot.log`；限流已经能挡扫描器 |
| 磁盘满 | 维护线程每 6h 清理一次；手动：`du -sh /root/github/live-copilot/cache /root/github/live-copilot/sessions` |
