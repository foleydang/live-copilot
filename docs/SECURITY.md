# 安全设计

## 鉴权架构

### 用户登录（v0.3+）
- **注册**：用户名 + 密码 → 自动创建租户（一个账号一个直播间）
- **登录**：验证密码后下发签名 cookie（HMAC-SHA256，30 天有效）
- **密码存储**：pbkdf2-hmac-sha256，12 万次迭代 + 每用户随机盐
- **会话**：无状态签名 cookie，服务重启不掉线
- **账号文件**：`data/accounts.json`（600 权限，已 gitignore）

### 三种鉴权渠道（按优先级）
1. **登录 cookie**（`lc_sid`）— 主渠道，用户登录后自动生效
2. **nginx 内部令牌**（`X-Proxy-Token` header）— 已停用，保留代码兼容
3. **URL token**（`?t=<token>`）— 兼容旧客户端链接

### 访问控制
- 未登录访问 `/` → 302 跳转 `/login`
- 未登录访问 `/api/*` → 401 JSON
- 公开路径：`/login`、`/api/login`、`/api/register`、`/api/health`、`/static/`

### nginx
- HTTP → HTTPS 301 跳转
- TLS 1.2/1.3，Let's Encrypt 证书自动续期
- 不再做 Basic Auth（由应用内登录替代）
- WebSocket 透传（Upgrade/Connection header）

## 已下线
- ✅ 旧域名 `zhubo.yanten.top` 已下线（`00-retired-hosts.conf` 返回 444）
- ✅ nginx Basic Auth 已移除（改为应用内登录）
- ✅ nginx X-Proxy-Token 注入已移除（改为应用内 cookie session）

## 数据安全
- 密码不明文存储（pbkdf2 哈希）
- 账号文件 `data/accounts.json` 权限 600
- 会话密钥 `data/session_secret` 权限 600
- `data/`、`tenants.yaml`、`admin.yaml` 均在 `.gitignore`
