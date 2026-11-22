#!/usr/bin/env bash
# 更新到最新版本：备份 → 拉取代码 → 重建镜像 → 健康检查。.env 和 data/ 不会被改动。
#   cd /opt/ovh-sniper && sudo ./update.sh        （或：ovh-sniper update）
set -Eeuo pipefail
DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
SERVICE="ovh-sniper"
BRANCH="${OVH_SNIPER_BRANCH:-main}"
cd "$DIR"
dc() { if docker compose version >/dev/null 2>&1; then docker compose "$@"; else docker-compose "$@"; fi; }
info() { printf '==> %s\n' "$*"; }
die() { printf '[x] %s\n' "$*" >&2; exit 1; }
# messages: English by default; Chinese with OVH_SNIPER_LANG=zh (or DEFAULT_LANG=zh in .env)
_lang() { local l="${OVH_SNIPER_LANG:-}"; [ -n "$l" ] || l=$(sed -n 's/^DEFAULT_LANG=//p' "${DIR:-.}/.env" 2>/dev/null | tail -n1); printf '%s' "${l:-en}"; }
t() { if [ "$(_lang)" = zh ]; then printf '%s' "$2"; else printf '%s' "$1"; fi; }

[ "$(id -u)" = 0 ] || [ -w "$DIR" ] || die "$(t "Please run as root (sudo ./update.sh)" "请用 root 运行（sudo ./update.sh）")"
[ -f .env ] || die "$(t ".env not found; run install.sh first" "没有找到 .env，请先运行 install.sh")"

info "$(t "Backing up data" "备份数据")"
"$DIR/manage.sh" backup || die "$(t "Backup failed, update aborted" "备份失败，已停止更新")"

if [ -d .git ]; then
  info "$(t "Pulling code ($BRANCH)" "拉取代码（$BRANCH）")"
  before=$(git rev-parse --short HEAD)
  if ! git pull --ff-only origin "$BRANCH"; then
    die "$(t "git pull failed. If you changed files in the repo (e.g. docker-compose.yml), move the changes to docker-compose.override.yml, then git stash or git checkout -- <file> and retry" "git pull 失败。如果你改过仓库里的文件（例如 docker-compose.yml），请把改动移到 docker-compose.override.yml，然后 git stash 或 git checkout -- <文件> 后重试")"
  fi
  after=$(git rev-parse --short HEAD)
  [ "$before" = "$after" ] && info "$(t "Already up to date ($after); rebuilding the image anyway" "已经是最新版本（$after），仍然重建一次镜像")"
else
  die "$(t "$DIR is not a git repo, so it can't update itself. Re-run install.sh or download the new version over it (keep .env and data/)" "$DIR 不是 git 仓库，无法自动更新。请重新运行 install.sh，或手动下载新版本覆盖（保留 .env 和 data/）")"
fi

info "$(t "Rebuilding and restarting" "重建并重启")"
dc up -d --build --remove-orphans
for _ in $(seq 1 60); do
  if dc exec -T "$SERVICE" python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=3)" >/dev/null 2>&1; then
    docker image prune -f >/dev/null 2>&1 || true
    info "$(t "Update complete: $(git rev-parse --short HEAD)" "更新完成：$(git rev-parse --short HEAD)")"
    exit 0
  fi
  sleep 2
done
dc logs --tail 80 "$SERVICE" >&2 || true
die "$(t "The new version did not start. Roll back: git checkout $before && docker compose up -d --build; data backups are in backups/" "新版本没有正常启动。可回滚：git checkout $before && docker compose up -d --build；数据备份在 backups/")"
