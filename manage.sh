#!/usr/bin/env bash
# StockHawk for OVH 管理命令（install.sh 会链接为 /usr/local/bin/ovh-sniper）
#   ovh-sniper status|logs|restart|stop|start|update|backup|restore <文件>|create-admin|reset-password|list-users|uninstall
set -Eeuo pipefail
SELF="$(readlink -f "${BASH_SOURCE[0]}")"
DIR="$(dirname "$SELF")"
SERVICE="ovh-sniper"
cd "$DIR"

dc() { if docker compose version >/dev/null 2>&1; then docker compose "$@"; else docker-compose "$@"; fi; }
die() { printf '[x] %s\n' "$*" >&2; exit 1; }
running() { [ -n "$(dc ps -q --status running "$SERVICE" 2>/dev/null)" ]; }
# messages: English by default; Chinese with OVH_SNIPER_LANG=zh (or DEFAULT_LANG=zh in .env)
_lang() { local l="${OVH_SNIPER_LANG:-}"; [ -n "$l" ] || l=$(sed -n 's/^DEFAULT_LANG=//p' "${DIR:-.}/.env" 2>/dev/null | tail -n1); printf '%s' "${l:-en}"; }
t() { if [ "$(_lang)" = zh ]; then printf '%s' "$2"; else printf '%s' "$1"; fi; }
# Decide once at startup: inside $(...) stdout is a pipe, so testing -t 1 there always failed and forced -T
if [ -t 0 ] && [ -t 1 ]; then TTY_OPT=""; else TTY_OPT="-T"; fi

backup() {
  local ts out tmpdb
  ts=$(date +%Y%m%d-%H%M%S)
  mkdir -p backups && chmod 700 backups
  out="backups/ovh-sniper-$ts.tgz"
  tmpdb="data/backup-$ts.db"
  if running; then
    # SQLite 在线备份（不停服务、数据一致）
    dc exec -T -u app "$SERVICE" python -c \
      "import sqlite3,sys;s=sqlite3.connect('/data/app.db');d=sqlite3.connect(sys.argv[1]);s.backup(d);d.close()" \
      "/data/backup-$ts.db"
  else
    [ -f data/app.db ] || die "$(t "data/app.db not found" "没有找到 data/app.db")"
    cp data/app.db "$tmpdb"
  fi
  # .env 里的 SECRET_KEY 是解密已保存 OVH 密钥所必需的，必须一起备份
  (umask 077; tar -czf "$out" .env -C data "backup-$ts.db")
  rm -f "$tmpdb"
  chmod 600 "$out"
  # 只保留最近 10 份
  find backups -maxdepth 1 -name 'ovh-sniper-*.tgz' -printf '%T@ %p\n' 2>/dev/null | sort -rn | awk 'NR>10{print $2}' | xargs -r rm -f
  echo "$DIR/$out"
}

restore() {
  local f="${1:-}" ts tmp db
  [ -n "$f" ] && [ -f "$f" ] || die "$(t "Usage: ovh-sniper restore backups/ovh-sniper-XXXX.tgz" "用法：ovh-sniper restore backups/ovh-sniper-XXXX.tgz")"
  f="$(readlink -f "$f")"
  tmp=$(mktemp -d); trap 'rm -rf "$tmp"' RETURN
  tar -xzf "$f" -C "$tmp"
  db=$(find "$tmp" -maxdepth 1 -name 'backup-*.db' | head -n1)
  [ -n "$db" ] || die "$(t "No database in the backup archive" "备份包里没有数据库")"
  if [ -t 0 ]; then
    read -r -p "$(t "Overwrite the current data with $f (a copy of the current data is kept first)? [y/N] " "将用 $f 覆盖当前数据（当前数据会先另存一份），继续？[y/N] ")" a
    [[ "$a" =~ ^[Yy]$ ]] || exit 1
  fi
  ts=$(date +%Y%m%d-%H%M%S)
  dc stop "$SERVICE" >/dev/null 2>&1 || true
  [ -f data/app.db ] && cp data/app.db "data/app.db.before-restore-$ts"
  rm -f data/app.db-wal data/app.db-shm
  cp "$db" data/app.db
  if [ -f "$tmp/.env" ] && ! cmp -s "$tmp/.env" .env; then
    cp .env ".env.before-restore-$ts"
    cp "$tmp/.env" .env && chmod 600 .env
    echo "$(t "Restored .env from the backup (previous .env saved as .env.before-restore-$ts)" "已恢复备份里的 .env（原 .env 另存为 .env.before-restore-$ts）")"
  fi
  dc up -d
  echo "$(t "Restored. Previous data saved as data/app.db.before-restore-$ts" "已恢复。原数据另存为 data/app.db.before-restore-$ts")"
}

cmd="${1:-help}"; shift || true
case "$cmd" in
  status)   dc ps; echo; dc exec -T "$SERVICE" python -m app.cli list-users 2>/dev/null || true ;;
  logs)     dc logs --tail "${1:-200}" -f "$SERVICE" ;;
  restart)  dc up -d --force-recreate "$SERVICE" ;;
  start)    dc up -d ;;
  stop)     dc stop ;;
  update)   exec "$DIR/update.sh" "$@" ;;
  backup)   backup ;;
  restore)  restore "$@" ;;
  create-admin|reset-password|list-users)
    running || die "$(t "The app is not running; run first: ovh-sniper start" "应用没有运行，先执行：ovh-sniper start")"
    # shellcheck disable=SC2046
    dc exec $TTY_OPT "$SERVICE" python -m app.cli "$cmd" "$@" ;;
  uninstall) exec "$DIR/uninstall.sh" "$@" ;;
  *) if [ "$(_lang)" = zh ]; then cat <<'USAGE'
用法：ovh-sniper <命令>
  status           查看容器状态和账号
  logs [行数]      查看日志（Ctrl+C 退出）
  restart|start|stop
  update           拉取最新代码并重建（保留 .env 和 data/）
  backup           备份数据库和 .env 到 backups/
  restore <文件>   从备份恢复
  create-admin     创建所有者账号（仅在还没有账号时）
  reset-password   重置密码（所有已登录设备会被踢下线）
  list-users       列出账号
  uninstall        卸载
USAGE
  else cat <<'USAGE'
Usage: ovh-sniper <command>
  status           container status and accounts
  logs [lines]     follow the logs (Ctrl+C to quit)
  restart|start|stop
  update           pull the latest code and rebuild (keeps .env and data/)
  backup           back up the database and .env to backups/
  restore <file>   restore from a backup
  create-admin     create the owner account (only when none exists)
  reset-password   reset a password (signs out every device)
  list-users       list accounts
  uninstall        uninstall
USAGE
  fi
  ;;
esac
