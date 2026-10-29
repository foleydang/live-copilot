# 本地调试 H5

## 最快路径

```bash
git clone <你的仓库地址> live-copilot
cd live-copilot
./dev.sh
```

脚本会自动：
1. 建 `.venv`（Python 3.11+）
2. 装依赖
3. 生成 `tenants.yaml` / `admin.yaml`（本地随机 token）
4. 启动服务并打印**带 token 的访问链接**

打开打印出来的 `http://127.0.0.1:8010/?t=xxxx` 即可。

## 手动步骤（想自己控制时）

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp tenants.example.yaml tenants.yaml
TOKEN=$(.venv/bin/python -c "from src.auth import new_token; print(new_token())")
sed -i "s/REPLACE_WITH_GENERATED_TOKEN/$TOKEN/" tenants.yaml
chmod 600 tenants.yaml

.venv/bin/python server.py
# 打开 http://127.0.0.1:8010/?t=$TOKEN
```

## 本地要连真 LLM 时

```bash
export DASHSCOPE_API_KEY=sk-xxxx
.venv/bin/python server.py
```

没配 key 也能跑：自动降级成本地关键词规则（`brain: rules`），H5 功能全在。

## 手机连电脑调试

1. 电脑和手机同一 Wi-Fi
2. 电脑查内网 IP：`ip addr` / `ifconfig`（如 `192.168.1.20`）
3. 用 `HOST=0.0.0.0 ./dev.sh` 起服务
4. 手机打开 `http://192.168.1.20:8010/?t=<token>`

> 注意：HTTP + 局域网下浏览器**不会**给 `wakeLock`，屏幕可能自动锁。
> 真机联调音频链路时建议用 Android Chrome；iOS Safari 锁屏会断音频。

## 不启服务，只跑管线（更快验证逻辑）

```bash
# 用 sample_danmaku.txt 跑通全链路
.venv/bin/python run.py --dry-run

# 连本地采集器
.venv/bin/python run.py --source ws

# 本机发声（需 requirements-dev.txt 里的 sounddevice）
.venv/bin/python run.py --audio
```

## 常用接口自测

```bash
TOKEN=<tenants.yaml 里的 token>

curl -s localhost:8010/api/health | python3 -m json.tool
curl -s -H "X-Token: $TOKEN" localhost:8010/api/stats
curl -s -X POST -H "X-Token: $TOKEN" -H 'Content-Type: application/json' \
     -d '{"user":"测试","text":"多少钱"}' localhost:8010/api/danmaku
curl -s -H "X-Token: $TOKEN" localhost:8010/api/usage
```

## 本地 vs 线上差异

| 项 | 本地 | 线上 |
|---|---|---|
| TTS | `mock`（H5 用浏览器语音兜底） | `cosyvoice`（真音频） |
| 采集 | 手动灌弹幕 / `run.py` | douyinLive WS |
| 鉴权 | 随机 token | 每客户独立 token |
| 静音 | 可用 | 可用（应急） |
| 复盘 | 可用 | 可用 |