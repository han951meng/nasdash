#!/bin/bash
# nasdash 本地回归测试：建/复用 .venv（flask+pytest），跑纯函数 pytest。
# 不依赖硬件，可在 Mac 本地跑，守护历史 bug（金士顿误判三星 / NVMe 通电时长逗号截断）不复发。
set -e
cd "$(dirname "$0")"

PY=python3
VENV=.venv
if [ ! -x "$VENV/bin/python" ]; then
  echo "[test] 创建虚拟环境 $VENV ..."
  "$PY" -m venv "$VENV"
fi
echo "[test] 安装依赖(flask, pytest) ..."
"$VENV/bin/pip" install -q --disable-pip-version-check flask pytest 2>&1 | tail -2 || \
  "$VENV/bin/pip" install -q flask pytest 2>&1 | tail -2

echo "[test] 运行 pytest ..."
"$VENV/bin/python" -m pytest test_app_parsers.py -v
