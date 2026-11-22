#!/usr/bin/env bash
# 不用 Docker 的运行方式：./run.sh  （首次会建 venv 并安装依赖）
set -e
# messages: English by default; Chinese with OVH_SNIPER_LANG=zh (or DEFAULT_LANG=zh in .env)
_lang() { local l="${OVH_SNIPER_LANG:-}"; [ -n "$l" ] || l=$(sed -n 's/^DEFAULT_LANG=//p' "${DIR:-.}/.env" 2>/dev/null | tail -n1); printf '%s' "${l:-en}"; }
t() { if [ "$(_lang)" = zh ]; then printf '%s' "$2"; else printf '%s' "$1"; fi; }
cd "$(dirname "$0")"
[ -f .env ] || { echo "$(t "First: cp .env.example .env and set SECRET_KEY" "请先: cp .env.example .env 并修改 SECRET_KEY")"; exit 1; }
set -a; . ./.env; set +a
export DATA_DIR="${DATA_DIR:-$PWD/data}"
if [ ! -d .venv ]; then python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt; fi
exec .venv/bin/uvicorn app.main:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}" --workers 1 \
  --proxy-headers --forwarded-allow-ips "127.0.0.1"
