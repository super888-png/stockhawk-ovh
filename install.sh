#!/usr/bin/env bash
# StockHawk for OVH 一键安装脚本（Debian 11+/Ubuntu 20.04+，需要 root）
#
#   curl -fsSL https://www.7919.net/install.sh | sudo bash
#   (mirror: curl -fsSL https://raw.githubusercontent.com/super888-png/stockhawk-ovh/main/install.sh | sudo bash)
#
# 可以重复运行（幂等）：已有的 .env（含 SECRET_KEY）和 data/ 永远不会被覆盖，已有账号不会重建。
#
# 可选环境变量（用于无人值守安装）：
#   OVH_SNIPER_REPO            git 仓库地址（默认见 DEFAULT_REPO）
#   OVH_SNIPER_BRANCH          分支（默认 main）
#   OVH_SNIPER_DIR             安装目录（默认 /opt/ovh-sniper）
#   OVH_SNIPER_PORT            监听端口（默认 8000；交互模式下会询问）
#   OVH_SNIPER_BIND            监听地址（默认 0.0.0.0；放在反向代理后面时用 127.0.0.1）
#   OVH_SNIPER_ADMIN_USER      无人值守时创建的所有者用户名
#   OVH_SNIPER_ADMIN_PASSWORD  无人值守时创建的所有者密码（会通过 stdin 传给容器，不出现在进程列表）
#   OVH_SNIPER_NONINTERACTIVE  设为 1 时不询问任何问题
#   OVH_SNIPER_SKIP_SWAP       设为 1 时不自动创建 swap
#   OVH_SNIPER_BIN_DIR         管理命令 ovh-sniper 的链接位置（默认 /usr/local/bin）
#   OVH_SNIPER_LANG            en（默认）| zh：安装脚本、命令行和面板默认语言 / script, CLI and default panel language
#   OVH_SNIPER_TZ              时区（默认 Asia/Shanghai）/ time zone
set -Eeuo pipefail

# Fork 后可改成你自己的仓库，或运行时设置 OVH_SNIPER_REPO / forks: change this or set OVH_SNIPER_REPO
DEFAULT_REPO="https://github.com/super888-png/stockhawk-ovh.git"

REPO="${OVH_SNIPER_REPO:-$DEFAULT_REPO}"
BRANCH="${OVH_SNIPER_BRANCH:-main}"
# 从已克隆的仓库里运行（sudo ./install.sh）时直接使用该目录；curl | bash 时安装到 /opt/ovh-sniper
SRC_DIR=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  [ -f "$SRC_DIR/docker-compose.yml" ] && [ -d "$SRC_DIR/app" ] || SRC_DIR=""
fi
DIR="${OVH_SNIPER_DIR:-${SRC_DIR:-/opt/ovh-sniper}}"
SERVICE="ovh-sniper"

if [ -t 1 ]; then C_G=$'\033[32m'; C_Y=$'\033[33m'; C_R=$'\033[31m'; C_B=$'\033[1m'; C_0=$'\033[0m'
else C_G=""; C_Y=""; C_R=""; C_B=""; C_0=""; fi
info() { printf '%s==>%s %s\n' "$C_G" "$C_0" "$*"; }
warn() { printf '%s[!]%s %s\n' "$C_Y" "$C_0" "$*" >&2; }
die()  { printf '%s[x]%s %s\n' "$C_R" "$C_0" "$*" >&2; exit 1; }
# messages: English by default; Chinese with OVH_SNIPER_LANG=zh (or DEFAULT_LANG=zh in .env)
_lang() { local l="${OVH_SNIPER_LANG:-}"; [ -n "$l" ] || l=$(sed -n 's/^DEFAULT_LANG=//p' "${DIR:-.}/.env" 2>/dev/null | tail -n1); printf '%s' "${l:-en}"; }
t() { if [ "$(_lang)" = zh ]; then printf '%s' "$2"; else printf '%s' "$1"; fi; }

# 通过 curl | bash 运行时 stdin 是脚本本身，交互输入要从 /dev/tty 读
HAVE_TTY=0
if [ "${OVH_SNIPER_NONINTERACTIVE:-0}" != "1" ] && (exec </dev/tty) 2>/dev/null; then HAVE_TTY=1; fi

ask() { # ask "提示" 默认值 -> 输出答案
  local prompt="$1" def="${2:-}" ans=""
  if [ "$HAVE_TTY" = 1 ]; then
    read -r -p "$prompt${def:+ [$def]}: " ans </dev/tty || true
  fi
  printf '%s' "${ans:-$def}"
}

dc() { if docker compose version >/dev/null 2>&1; then docker compose "$@"; else docker-compose "$@"; fi; }

need_root() {
  [ "$(id -u)" = 0 ] || die "$(t "Please run as root (prefix the command with sudo)" "请用 root 运行（在命令前加 sudo）")"
}

check_os() {
  local id="" like=""
  if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release; id="${ID:-}"; like="${ID_LIKE:-}"
  fi
  case "$id $like" in
    *debian*|*ubuntu*) ;;
    *) warn "$(t "Not detected as Debian/Ubuntu ($id); trying anyway, but only Debian/Ubuntu are tested" "未识别为 Debian/Ubuntu（$id），继续尝试，但只在 Debian/Ubuntu 上测试过")" ;;
  esac
  case "$(uname -m)" in
    x86_64|amd64|aarch64|arm64) ;;
    *) warn "$(t "Architecture $(uname -m) is untested" "架构 $(uname -m) 未测试")" ;;
  esac
}

apt_install() {
  local missing=() p
  for p in "$@"; do dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p"); done
  [ "${#missing[@]}" -eq 0 ] && return 0
  info "$(t "Installing dependencies: ${missing[*]}" "安装依赖：${missing[*]}")"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq "${missing[@]}" >/dev/null
}

install_docker() {
  if command -v docker >/dev/null 2>&1; then
    info "$(t "Docker already installed: $(docker --version 2>/dev/null | head -n1)" "Docker 已安装：$(docker --version 2>/dev/null | head -n1)")"
  else
    info "$(t "Installing Docker (official script get.docker.com)" "安装 Docker（官方脚本 get.docker.com）")"
    curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
    sh /tmp/get-docker.sh >/dev/null
    rm -f /tmp/get-docker.sh
  fi
  if command -v systemctl >/dev/null 2>&1; then systemctl enable --now docker >/dev/null 2>&1 || true; fi
  if ! docker compose version >/dev/null 2>&1 && ! command -v docker-compose >/dev/null 2>&1; then
    info "$(t "Installing the docker compose plugin" "安装 docker compose 插件")"
    apt_install docker-compose-plugin || die "$(t "Could not install docker compose; install it manually and retry" "无法安装 docker compose，请手动安装后重试")"
  fi
  docker info >/dev/null 2>&1 || die "$(t "Docker is not running (see systemctl status docker)" "Docker 没有运行（systemctl status docker 查看原因）")"
}

ensure_swap() {
  [ "${OVH_SNIPER_SKIP_SWAP:-0}" = 1 ] && return 0
  local mem_kb swap_kb size_mb free_mb
  mem_kb=$(awk '/^MemTotal:/{print $2}' /proc/meminfo)
  swap_kb=$(awk '/^SwapTotal:/{print $2}' /proc/meminfo)
  if [ "$mem_kb" -ge 1048576 ] || [ "$swap_kb" -ge 524288 ]; then return 0; fi
  info "$(t "Memory $((mem_kb / 1024)) MB < 1 GB and no swap: creating swap (needed to build the image)" "内存 $((mem_kb / 1024)) MB < 1 GB 且没有 swap，创建 swap（构建镜像需要）")"
  free_mb=$(df -Pm / | awk 'NR==2{print $4}')
  size_mb=2048; [ "$free_mb" -lt 6144 ] && size_mb=1024
  if [ "$free_mb" -lt 3072 ]; then warn "$(t "Only ${free_mb} MB disk free, skipping swap" "磁盘剩余 ${free_mb} MB，跳过 swap")"; return 0; fi
  if [ ! -f /swapfile ]; then
    fallocate -l "${size_mb}M" /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count="$size_mb" status=none
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
  fi
  if swapon /swapfile 2>/dev/null; then
    grep -qE '^/swapfile\s' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
    info "$(t "Enabled ${size_mb} MB swap (/swapfile)" "已启用 ${size_mb} MB swap（/swapfile）")"
  else
    warn "$(t "Could not enable swap (common on OpenVZ/LXC); continuing — the build may fail if memory runs out" "无法启用 swap（OpenVZ/LXC 容器常见），继续安装；内存不足时构建可能失败")"
  fi
}

fetch_code() {
  if [ -d "$DIR/.git" ]; then
    info "$(t "Updating code: $DIR" "更新代码：$DIR")"
    git -C "$DIR" pull --ff-only origin "$BRANCH" || warn "$(t "git pull failed (local changes?); continuing with the existing code" "git pull 失败（本地有改动？），继续使用现有代码")"
    return 0
  fi
  if [ -f "$DIR/docker-compose.yml" ] && [ -d "$DIR/app" ]; then
    info "$(t "Using the existing code in $DIR (not a git repo, skipping update)" "使用 $DIR 里已有的代码（不是 git 仓库，跳过更新）")"
    return 0
  fi
  mkdir -p "$DIR"
  if [ -n "$(ls -A "$DIR" 2>/dev/null)" ]; then
    die "$(t "$DIR exists, is not empty and is not this project; choose another OVH_SNIPER_DIR or clean it up first" "$DIR 已存在且不是空目录，也不是本项目，请换一个 OVH_SNIPER_DIR 或先清理")"
  fi
  info "$(t "Downloading code: $REPO ($BRANCH) → $DIR" "下载代码：$REPO（$BRANCH）→ $DIR")"
  if ! git clone -q --depth 1 -b "$BRANCH" "$REPO" "$DIR"; then
    case "$REPO" in
      https://github.com/*)
        local slug="${REPO#https://github.com/}"; slug="${slug%.git}"
        warn "$(t "git clone failed, falling back to a tarball" "git clone 失败，改用 tar 包下载")"
        curl -fsSL "https://codeload.github.com/$slug/tar.gz/refs/heads/$BRANCH" | tar -xz -C "$DIR" --strip-components=1 \
          || die "$(t "Download failed; check the network or the repository URL" "下载失败，请检查网络或仓库地址")"
        ;;
      *) die "$(t "git clone failed: $REPO" "git clone 失败：$REPO")" ;;
    esac
  fi
}

gen_secret() { head -c 48 /dev/urandom | base64 | tr -d '\n/+='; }

port_in_use() {
  command -v ss >/dev/null 2>&1 || return 1
  [ -n "$(ss -ltnH "sport = :$1" 2>/dev/null)" ]
}

write_env() {
  cd "$DIR"
  if [ -f .env ]; then
    info "$(t "Keeping the existing .env (SECRET_KEY unchanged)" "保留已有 .env（SECRET_KEY 不变）")"
    grep -q '^SECRET_KEY=..*' .env || die "$(t "No SECRET_KEY in .env; check $DIR/.env" ".env 里没有 SECRET_KEY，请检查 $DIR/.env")"
    chmod 600 .env
    return 0
  fi
  local port bind
  port="${OVH_SNIPER_PORT:-}"
  while :; do
    [ -n "$port" ] || port=$(ask "$(t "Web port" "网页端口")" 8000)
    if ! [[ "$port" =~ ^[0-9]+$ ]] || [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then
      warn "$(t "Invalid port: $port" "端口无效：$port")"; port=""; [ "$HAVE_TTY" = 1 ] || die "$(t "OVH_SNIPER_PORT is invalid" "OVH_SNIPER_PORT 无效")"; continue
    fi
    if port_in_use "$port"; then
      warn "$(t "Port $port is already in use" "端口 $port 已被占用")"; port=""; [ "$HAVE_TTY" = 1 ] || die "$(t "Port in use; set OVH_SNIPER_PORT" "端口已被占用，请设置 OVH_SNIPER_PORT")"; continue
    fi
    break
  done
  bind="${OVH_SNIPER_BIND:-0.0.0.0}"
  info "$(t "Generating .env (random SECRET_KEY, port $port)" "生成 .env（随机 SECRET_KEY，端口 $port）")"
  umask 077
  cat > .env <<ENV
# Generated by install.sh. ⚠ SECRET_KEY encrypts the saved OVH keys and signs login sessions:
#   never share or change it; back it up together with data/ when moving servers.
SECRET_KEY=$(gen_secret)
# Web port and bind address (used by docker-compose.yml). Behind Caddy/Nginx use BIND_ADDR=127.0.0.1
PORT=$port
BIND_ADDR=$bind
# Set to 1 once served over HTTPS (cookies only over HTTPS)
COOKIE_SECURE=0
# Minimum poll interval per task (seconds)
MIN_POLL_INTERVAL=5
# Default UI / notification / CLI language for users who haven't picked one: en | zh
DEFAULT_LANG=${OVH_SNIPER_LANG:-en}
# Time zone for pages and logs
APP_TZ=${OVH_SNIPER_TZ:-Asia/Shanghai}
TZ=${OVH_SNIPER_TZ:-Asia/Shanghai}
ENV
  umask 022
  chmod 600 .env
}

start_app() {
  cd "$DIR"
  mkdir -p data
  info "$(t "Building and starting (the first time takes a few minutes)" "构建并启动（第一次需要几分钟）")"
  dc up -d --build
  info "$(t "Waiting for the app to start" "等待应用启动")"
  for _ in $(seq 1 60); do
    if dc exec -T "$SERVICE" python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=3)" >/dev/null 2>&1; then
      info "$(t "App is up" "应用已启动")"; return 0
    fi
    sleep 2
  done
  dc logs --tail 50 "$SERVICE" >&2 || true
  die "$(t "The app did not start within 120 s; see the logs above (cd $DIR && docker compose logs)" "应用 120 秒内没有启动成功，日志见上方（cd $DIR && docker compose logs）")"
}

create_owner() {
  cd "$DIR"
  if dc exec -T "$SERVICE" python -m app.cli has-owner >/dev/null 2>&1; then
    info "$(t "An owner account already exists, skipping (forgot the password? ovh-sniper reset-password)" "已有所有者账号，跳过创建（忘记密码：ovh-sniper reset-password）")"
    return 0
  fi
  if [ -n "${OVH_SNIPER_ADMIN_USER:-}" ] && [ -n "${OVH_SNIPER_ADMIN_PASSWORD:-}" ]; then
    info "$(t "Creating owner account $OVH_SNIPER_ADMIN_USER" "创建所有者账号 $OVH_SNIPER_ADMIN_USER")"
    printf '%s\n' "$OVH_SNIPER_ADMIN_PASSWORD" | dc exec -T "$SERVICE" python -m app.cli create-admin \
      -u "$OVH_SNIPER_ADMIN_USER" --password-stdin || die "$(t "Creating the account failed (password needs ≥10 characters of at least two kinds)" "创建账号失败（密码需 ≥10 位且至少包含两类字符）")"
    return 0
  fi
  if [ "$HAVE_TTY" = 1 ]; then
    printf "\n%s$(t 'Create the owner account' '创建所有者账号')%s$(t ' (single-account app, no sign-up page; password ≥10 characters of two kinds)' '（本应用只有一个账号，没有注册页面；密码至少 10 位，包含两类字符）')\n" "$C_B" "$C_0"
    local tries
    for tries in 1 2 3; do
      if dc exec "$SERVICE" python -m app.cli create-admin </dev/tty >/dev/tty 2>&1; then return 0; fi
      warn "$(t "Creation failed, please try again ($tries/3)" "创建失败，请重试（$tries/3）")"
    done
  fi
  warn "$(t "No account created yet. Run later: ovh-sniper create-admin" "还没有创建账号。稍后运行：ovh-sniper create-admin")"
}

install_cli() {
  chmod +x "$DIR"/install.sh "$DIR"/update.sh "$DIR"/uninstall.sh "$DIR"/manage.sh 2>/dev/null || true
  local bin="${OVH_SNIPER_BIN_DIR:-/usr/local/bin}"
  mkdir -p "$bin"
  ln -sf "$DIR/manage.sh" "$bin/ovh-sniper"
}

public_ip() {
  local ip=""
  ip=$(curl -fsS4 --max-time 5 https://api.ipify.org 2>/dev/null || true)
  [[ "$ip" =~ ^[0-9.]+$ ]] || ip=$(hostname -I 2>/dev/null | awk '{print $1}')
  printf '%s' "$(t "${ip:-your-server-ip}" "${ip:-你的服务器IP}")"
}

finish() {
  cd "$DIR"
  local port bind host
  port=$(sed -n 's/^PORT=//p' .env | tail -n1); port="${port:-8000}"
  bind=$(sed -n 's/^BIND_ADDR=//p' .env | tail -n1); bind="${bind:-0.0.0.0}"
  if [ "$bind" = "0.0.0.0" ]; then host=$(public_ip); else host="$bind"; fi
  printf "\n%s$(t 'Installation complete' '安装完成')%s\n" "$C_B" "$C_0"
  printf "  $(t 'URL:' '访问地址：') %shttp://%s:%s%s\n" "$C_G" "$host" "$port" "$C_0"
  printf "  $(t 'Directory:' '安装目录：') %s $(t '(config .env, data data/)' '（配置 .env，数据 data/）')\n" "$DIR"
  printf "  $(t 'Commands:' '常用命令：') ovh-sniper status | logs | update | backup | reset-password | uninstall\n"
  if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
    warn "$(t "ufw firewall is active; if you can't connect run: ufw allow $port/tcp" "检测到 ufw 防火墙已启用，如无法访问请运行：ufw allow $port/tcp")"
  fi
  printf "\n  %s$(t 'Tip' '建议')%s$(t ': this is plain HTTP. Before real use set up HTTPS (Caddy or Cloudflare Tunnel, see README),' '：现在是 HTTP 明文访问。正式使用前请配置 HTTPS（Caddy 或 Cloudflare Tunnel，见 README），')\n" "$C_Y" "$C_0"
  printf "  $(t 'then set COOKIE_SECURE=1 and BIND_ADDR=127.0.0.1 in .env and run ovh-sniper restart.' '然后在 .env 设置 COOKIE_SECURE=1、BIND_ADDR=127.0.0.1，再运行 ovh-sniper restart。')\n\n"
}

main() {
  cd / # 当前目录可能已被删除（例如刚卸载过），git/docker 需要有效的工作目录
  need_root
  check_os
  apt_install ca-certificates curl git
  install_docker
  ensure_swap
  fetch_code
  write_env
  start_app
  create_owner
  install_cli
  finish
}

main "$@"
