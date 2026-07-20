#!/bin/bash
set -euo pipefail
umask 077

APP_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_ROOT"
mkdir -p .local/runtime

PYTHON_BIN=""
for candidate in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v "$candidate")"
    break
  fi
done

if [ -z "$PYTHON_BIN" ]; then
  osascript -e 'display dialog "未找到 Python 3。请先从 python.org 安装 Python 3.9 或更高版本，然后重新双击本应用。" buttons {"打开下载页面", "退出"} default button "打开下载页面"' >/dev/null 2>&1 || exit 1
  open "https://www.python.org/downloads/macos/"
  exit 1
fi

PYTHON_OK="$($PYTHON_BIN -c 'import sys; print(int(sys.version_info >= (3, 9)))')"
if [ "$PYTHON_OK" != "1" ]; then
  osascript -e 'display alert "Python 版本过低" message "ShotGrid Backup 需要 Python 3.9 或更高版本。"' >/dev/null 2>&1 || true
  exit 1
fi

VENV_OK=0
if [ -x ".venv/bin/python" ] && .venv/bin/python -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
  VENV_OK=1
fi

if [ "$VENV_OK" != "1" ] && [ -e ".venv" ]; then
  FOREIGN_VENV=".local/runtime/foreign_venv_$(date -u +%Y%m%dT%H%M%SZ)"
  mv .venv "$FOREIGN_VENV"
fi

if [ "$VENV_OK" != "1" ]; then
  "$PYTHON_BIN" -m venv .venv
fi

REQ_HASH="$(.venv/bin/python -c 'import hashlib; print(hashlib.sha256(open("requirements.txt", "rb").read()).hexdigest())')"
STAMP_PATH=".local/runtime/requirements.sha256"
INSTALLED_HASH=""
if [ -f "$STAMP_PATH" ]; then
  INSTALLED_HASH="$(tr -d '\r\n' < "$STAMP_PATH")"
fi

if [ "$REQ_HASH" != "$INSTALLED_HASH" ] || ! .venv/bin/python -c 'import shotgun_api3' >/dev/null 2>&1; then
  if ! .venv/bin/pip install -r requirements.txt; then
    if [ -n "${SHOTGRID_BOOTSTRAP_PROXY:-}" ]; then
      HTTPS_PROXY="http://${SHOTGRID_BOOTSTRAP_PROXY}" HTTP_PROXY="http://${SHOTGRID_BOOTSTRAP_PROXY}" .venv/bin/pip install -r requirements.txt
    else
      osascript -e 'display alert "依赖安装失败" message "请检查网络。若安装依赖也必须走代理，请先设置 SHOTGRID_BOOTSTRAP_PROXY=host:port，再重新启动。"' >/dev/null 2>&1 || true
      exit 1
    fi
  fi
  printf '%s\n' "$REQ_HASH" > "$STAMP_PATH"
fi

exec .venv/bin/python tools/shotgrid_backup/app.py "$@"
