#!/bin/bash
# 本地自测：在不上 NAS 真机的前提下，尽量早地暴露「能否跑起来」的问题。
# 用法: ./local_smoke.sh
set -u
cd "$(dirname "$0")"

# 1) 找一个能 import flask 的 python（优先当前 python3，其次 .venv）
PY=python3
if [ -x .venv/bin/python ] && .venv/bin/python -c "import flask" 2>/dev/null; then
  PY=.venv/bin/python
fi

# 2) 语法检查（最高频、最该在本地拦住的类别）
echo "[smoke] 1) 语法检查 app.py ..."
if $PY -m py_compile app.py 2>/tmp/pycompile.err; then
  echo "  OK: app.py 语法通过"
else
  echo "  FAIL: app.py 语法错误 ——"
  cat /tmp/pycompile.err
  exit 1
fi

# 3) 若没有 flask，尝试建 .venv 安装（需网络）；装不上就跳过 Web 自测
if ! $PY -c "import flask" 2>/dev/null; then
  echo "[smoke] 未检测到 flask，尝试创建 .venv 并 pip install flask（需网络）..."
  if [ ! -x .venv/bin/python ]; then $PY -m venv .venv 2>/dev/null; fi
  if [ -x .venv/bin/python ]; then
    .venv/bin/pip install -q flask 2>/dev/null && PY=.venv/bin/python
  fi
fi

if ! $PY -c "import flask" 2>/dev/null; then
  echo "[smoke] 跳过 Web 自测（无 flask 且无法安装）。可手动：pip install flask 后重跑。"
  exit 0
fi

# 4) 本地起服务，验证关键接口可响应
PORT=9801
echo "[smoke] 2) 本地起服务 (port $PORT) ..."
TRIM_SERVICE_PORT=$PORT $PY app.py >/tmp/nasdash_smoke.log 2>&1 &
PID=$!
sleep 3
trap "kill $PID 2>/dev/null" EXIT

code_ver=$(curl -s -o /tmp/ver.json -w "%{http_code}" "http://127.0.0.1:$PORT/api/version")
code_all=$(curl -s -o /tmp/all.json -w "%{http_code}" "http://127.0.0.1:$PORT/api/all")
echo "  /api/version -> HTTP $code_ver"
echo "  /api/all     -> HTTP $code_all  (硬件接口在 Mac 上可能 500，属正常，只要服务没崩即可)"

if [ "$code_ver" = "200" ]; then
  echo "  OK: 服务正常启动并可响应 /api/version"
else
  echo "  WARN: /api/version 非 200，详见 /tmp/nasdash_smoke.log"
fi
echo "  /api/version 返回："
head -c 400 /tmp/ver.json 2>/dev/null; echo
echo "[smoke] 完成（日志: /tmp/nasdash_smoke.log）"
