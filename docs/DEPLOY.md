# 部署说明

## 生产部署（当前机器）

### 服务文件

- systemd unit：`/etc/systemd/system/live-copilot.service`
- dashscope key drop-in：`/etc/systemd/system/live-copilot.service.d/dashscope.conf`
- logrotate：`/etc/logrotate.d/live-copilot`

### 常用命令

```bash
# 状态
systemctl status live-copilot
journalctl -u live-copilot -f        # 实时日志
journalctl -u live-copilot --since "1 hour ago"

# 重启
sudo systemctl restart live-copilot

# 健康检查
curl http://127.0.0.1:8010/api/health | jq

# 查看/换 token
cat /root/github/live-copilot/tenants.yaml
# 改完不需要重启（_persist_room_id 已经热更新内存）

# 加客户（命令行）
curl -X POST http://127.0.0.1:8010/api/admin/tenants \
  -H "X-Admin-Token: <admin token>" \
  -H "Content-Type: application/json" \
  -d '{"id":"new-shop","name":"新店","platform":"douyin"}'
```

### 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `PORT` | 8010 | 监听端口 |
| `HOST` | 127.0.0.1 | 绑地址；公网访问设 `0.0.0.0`，**nginx 反代后改回 127.0.0.1** |
| `DASHSCOPE_API_KEY` | （drop-in 提供） | LLM + TTS 共用 |
| `LIVE_COPILOT_CONFIG` | config.yaml | 主配置路径 |
| `LIVE_COPILOT_ADMIN_TOKEN` | （admin.yaml 提供） | 管理令牌，环境变量优先于文件 |
| `MAINT_INTERVAL_HOURS` | 6 | 维护线程间隔 |
| `SESSIONS_KEEP_DAYS` | 30 | 留痕保留天数 |
| `TTS_CACHE_MAX_MB` | 512 | TTS 缓存上限 MB |
| `TTS_CACHE_MAX_FILES` | 2000 | TTS 缓存文件数上限 |

### 安装到另一台机器

```bash
git clone <repo> /root/github/live-copilot
cd /root/github/live-copilot
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp tenants.example.yaml tenants.yaml
chmod 600 tenants.yaml
# 编辑 config.yaml / tenants.yaml / admin.yaml
sudo cp deploy/live-copilot.service /etc/systemd/system/
sudo mkdir -p /etc/systemd/system/live-copilot.service.d
sudo cp deploy/dashscope.conf /etc/systemd/system/live-copilot.service.d/
sudo chmod 600 /etc/systemd/system/live-copilot.service.d/dashscope.conf
sudo cp deploy/logrotate.d/live-copilot /etc/logrotate.d/
sudo systemctl daemon-reload
sudo systemctl enable --now live-copilot
```

## 开发

```bash
cd /root/github/live-copilot
.venv/bin/python server.py         # 本地跑（默认 127.0.0.1:8010）
.venv/bin/python tests/smoke.py    # smoke test
```
