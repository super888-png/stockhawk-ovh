#!/usr/bin/env bash
# 卸载 StockHawk for OVH：停止并删除容器和镜像，可选保留数据。不会卸载 Docker，也不会删除 swap。
#   sudo /opt/ovh-sniper/uninstall.sh            交互确认
#   sudo /opt/ovh-sniper/uninstall.sh --yes --keep-data
set -Eeuo pipefail
DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
# messages: English by default; Chinese with OVH_SNIPER_LANG=zh (or DEFAULT_LANG=zh in .env)
_lang() { local l="${OVH_SNIPER_LANG:-}"; [ -n "$l" ] || l=$(sed -n 's/^DEFAULT_LANG=//p' "${DIR:-.}/.env" 2>/dev/null | tail -n1); printf '%s' "${l:-en}"; }
t() { if [ "$(_lang)" = zh ]; then printf '%s' "$2"; else printf '%s' "$1"; fi; }
YES=0; KEEP=""
for a in "$@"; do
  case "$a" in
    --yes|-y) YES=1 ;;
    --keep-data) KEEP=1 ;;
    --delete-data) KEEP=0 ;;
    *) echo "$(t "Unknown argument: $a" "未知参数：$a")" >&2; exit 2 ;;
  esac
done
dc() { if docker compose version >/dev/null 2>&1; then docker compose "$@"; else docker-compose "$@"; fi; }
[ "$(id -u)" = 0 ] || { echo "$(t "Please run as root (sudo)" "请用 root 运行（sudo）")" >&2; exit 1; }
cd "$DIR"

if [ "$YES" != 1 ]; then
  read -r -p "$(t "Uninstall StockHawk for OVH in $DIR? Type yes to continue: " "确定卸载 $DIR 里的 StockHawk for OVH？输入 yes 继续：")" a </dev/tty
  [ "$a" = "yes" ] || { echo "$(t "Cancelled" "已取消")"; exit 1; }
fi
if [ -z "$KEEP" ]; then
  if [ "$YES" = 1 ]; then KEEP=1
  else
    read -r -p "$(t "Keep the data (database, .env, backups)? [Y/n] " "保留数据（数据库、.env、备份）吗？[Y/n] ")" a </dev/tty
    case "$a" in [Nn]*) KEEP=0 ;; *) KEEP=1 ;; esac
  fi
fi

ts=$(date +%Y%m%d-%H%M%S)
final=""
if [ -f .env ] && [ -d data ]; then
  final="/root/ovh-sniper-final-$ts.tgz"
  echo "$(t "==> Final backup: $final" "==> 最后备份一次：$final")"
  (umask 077; tar -czf "$final" .env data backups 2>/dev/null || tar -czf "$final" .env data)
fi

echo "$(t "==> Stopping and removing the container and image" "==> 停止并删除容器和镜像")"
dc down --rmi local --remove-orphans || true
BIN="${OVH_SNIPER_BIN_DIR:-/usr/local/bin}/ovh-sniper"
if [ -L "$BIN" ] && [ "$(readlink -f "$BIN")" = "$DIR/manage.sh" ]; then rm -f "$BIN"; fi

if [ "$KEEP" = 1 ]; then
  echo "$(t "==> Container removed. Code and data kept in $DIR (re-run install.sh to bring it back)" "==> 已删除容器。代码和数据保留在 $DIR（重新运行 install.sh 可直接恢复）")"
else
  cd /
  rm -rf "$DIR"
  echo "$(t "==> Deleted $DIR" "==> 已删除 $DIR")"
fi
[ -n "$final" ] && echo "$(t "    Final backup: $final (contains SECRET_KEY — keep it safe or delete it)" "    最终备份：$final（含 SECRET_KEY，请妥善保管或删除）")"
echo "$(t "Done. Docker and swap were left untouched." "完成。Docker 和 swap 未改动。")"
