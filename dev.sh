#!/usr/bin/env bash
# 本地一键起服务（H5 调试用）
#   ./dev.sh           # 127.0.0.1:8010，自动打印带 token 的链接
#   PORT=9000 ./dev.sh # 换端口
set -euo pipefail
cd "$(dirname "$0")"

PY=${PYTHON:-python3.11}
command -v "$PY" >/dev/null 2>&1 || PY=python3

if [ ! -d .venv ]; then
  echo "→ 创建 venv"
  "$PY" -m venv .venv
fi
echo "→ 安装依赖（首次较慢）"
.venv/bin/pip install -q -r requirements.txt

if [ ! -f tenants.yaml ]; then
  echo "→ 生成 tenants.yaml（本地调试用）"
  TOKEN=$(.venv/bin/python -c "from src.auth import new_token; print(new_token())")
  sed "s/REPLACE_WITH_GENERATED_TOKEN/$TOKEN/" tenants.example.yaml > tenants.yaml
  chmod 600 tenants.yaml
fi

if [ ! -f admin.yaml ]; then
  ATOKEN=$(.venv/bin/python -c "import secrets; print(secrets.token_urlsafe(24))")
  printf 'admin_token: %s\n' "$ATOKEN" > admin.yaml
  chmod 600 admin.yaml
fi

TOKEN=$(.venv/bin/python - <<'PY'
import yaml
d = yaml.safe_load(open("tenants.yaml", encoding="utf-8"))
print(d["tenants"][0]["token"])
PY
)
PORT=${PORT:-8010}

echo
echo "======================================================"
echo "  打开（手机/电脑同局域网时把 127.0.0.1 换成内网 IP）"
echo "  http://127.0.0.1:${PORT}/?t=${TOKEN}"
echo "======================================================"
echo "  Ctrl+C 停止"
echo

HOST=${HOST:-127.0.0.1} PORT="$PORT" exec .venv/bin/python server.py