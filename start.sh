#!/bin/sh
# start.sh —— 一条命令启动全部服务（Docker）
#
#   sh start.sh            启动（自动清旧容器/占端口的残留进程，构建并后台运行）
#   sh start.sh down       停止并删除容器
#   sh start.sh restart    重启
#   sh start.sh logs       跟踪日志
#   sh start.sh status     查看状态
#   sh start.sh local      不用 Docker，直接起裸机进程
#
# 启动后打开 http://<服务器IP>:9999 ，账号密码见 .env
set -eu

DIR=$(cd "$(dirname "$0")" && pwd -P)
cd "$DIR"

PORT_PANEL="${PANEL_PORT:-9999}"
PORT_HTTP="${HTTP_PORT:-18081}"
PORT_SOCKS="${SOCKS_PORT:-18082}"
PORT_RELAY="${GH_RELAY_PORT:-2222}"
IMAGE="ccs-proxy:latest"

log()  { printf '\033[36m[start]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[warn]\033[0m %s\n' "$*"; }
die()  { printf '\033[31m[ERR]\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- 环境检测
need_docker() {
  command -v docker >/dev/null 2>&1 || die "没装 docker，先装：curl -fsSL https://get.docker.com | sh"
  if docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
  elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE="docker-compose"
  else
    die "没装 docker compose 插件"
  fi
  docker info >/dev/null 2>&1 || die "docker 守护进程没跑，或当前用户没权限（试试 sudo 或加入 docker 组）"
}

port_busy() {
  if command -v ss >/dev/null 2>&1; then
    ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]$1\$"
  elif command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
  else
    return 1
  fi
}

show_owner() {
  if command -v ss >/dev/null 2>&1; then
    ss -ltnp 2>/dev/null | grep -E "[:.]$1\b" || true
  fi
}

# 找出监听某端口的进程 pid（ss 优先，macOS 退 lsof）
port_pids() {
  if command -v ss >/dev/null 2>&1; then
    ss -ltnp 2>/dev/null | grep -E "[:.]$1\b" | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u
  elif command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null | sort -u
  fi
}

# 按端口强杀：命令行认不出来的旧进程（比如内联 python 脚本）也能清掉
kill_port_owners() {
  for p in "$PORT_PANEL" "$PORT_HTTP" "$PORT_SOCKS" "$PORT_RELAY"; do
    pids=$(port_pids "$p")
    [ -n "$pids" ] || continue
    for pid in $pids; do
      warn "端口 ${p} 被 pid=${pid} 占用，强制结束"
      kill -9 "$pid" 2>/dev/null || true
    done
  done
  sleep 0.5
}

# ./data 可能被上一版以 root 跑起来的容器改成了 root 属主，
# 之后宿主用户就写不进去了。这里自动修，不劳用户敲命令。
fix_data_owner() {
  mkdir -p "$DIR/data" || die "无法创建 $DIR/data"
  [ -w "$DIR/data" ] && return 0

  _uid=$(id -u); _gid=$(id -g)
  warn "$DIR/data 不可写，尝试自动修正为 ${_uid}:${_gid}"

  # 先试最轻的：只是权限位丢了（属主本来就是你）
  if chmod -R u+rwX "$DIR/data" 2>/dev/null && [ -w "$DIR/data" ]; then
    log "已补回写权限"
    return 0
  fi

  # 1) 用本地已构建的镜像以 root 身份改（不联网、不需要 sudo）
  if docker image inspect "$IMAGE" >/dev/null 2>&1; then
    if docker run --rm --user 0:0 -v "$DIR:/w" --entrypoint sh "$IMAGE" \
         -c "chown -R ${_uid}:${_gid} /w/data" >/dev/null 2>&1; then
      if [ -w "$DIR/data" ]; then
        log "已用 $IMAGE 修正属主"
        return 0
      fi
    fi
  fi

  # 2) 宿主机有免密 sudo
  if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
    if sudo chown -R "${_uid}:${_gid}" "$DIR/data" 2>/dev/null && [ -w "$DIR/data" ]; then
      log "已用 sudo 修正属主"
      return 0
    fi
  fi

  # 3) 都不行，给出可直接复制的手动命令
  warn "自动修正失败，手动执行其中一条后重试："
  warn "  sudo chown -R ${_uid}:${_gid} $DIR/data"
  warn "  docker run --rm --user 0:0 -v \"$DIR:/w\" --entrypoint sh $IMAGE -c 'chown -R ${_uid}:${_gid} /w/data'"
  die "权限未修复，停止启动"
}

# 干掉以前手动起的裸机进程（它们会占住容器要用的端口）
kill_strays() {
  pkill -f '[c]csproxy[.]py'      2>/dev/null || true
  pkill -f '[s]ocks5udp[.]py'     2>/dev/null || true
  pkill -f '[g]h_relay_worker[.]py' 2>/dev/null || true
  pkill -f '[g]h-relay[.]sh'      2>/dev/null || true
  pkill -f '[p]anel[.]py'         2>/dev/null || true
  sleep 1
}

# ---------------------------------------------------------------- .env
ensure_env() {
  if [ -f .env ]; then
    # shellcheck disable=SC1091
    . ./.env
    if ! grep -q '^HOST_UID=' .env 2>/dev/null; then
      printf 'HOST_UID=%s\nHOST_GID=%s\n' "$(id -u)" "$(id -g)" >> .env
      HOST_UID=$(id -u); HOST_GID=$(id -g)
    fi
    return
  fi
  PASS=$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 16)
  [ -n "$PASS" ] || PASS="changeme$RANDOM"
  cat > .env <<EOF
PANEL_USER=admin
PANEL_PASS=$PASS
HOST_UID=$(id -u)
HOST_GID=$(id -g)
EOF
  chmod 600 .env
  log "首次运行，已生成 .env（面板密码：${PASS}）"
  PANEL_USER=admin
  PANEL_PASS=$PASS
}

# ---------------------------------------------------------------- 动作
do_up() {
  need_docker
  ensure_env

  log "清理旧容器"
  $COMPOSE down --remove-orphans >/dev/null 2>&1 || true
  docker rm -f ccs-proxy >/dev/null 2>&1 || true

  log "清理占端口的残留进程"
  kill_strays
  kill_port_owners

  for p in "$PORT_PANEL" "$PORT_HTTP" "$PORT_SOCKS" "$PORT_RELAY"; do
    if port_busy "$p"; then
      warn "端口 $p 仍被占用："
      show_owner "$p"
      die "先停掉上面的程序，或改 .env 里的端口再启动"
    fi
  done

  fix_data_owner


  log "构建镜像"
  $COMPOSE build

  log "启动容器"
  $COMPOSE up -d

  log "等待服务就绪"
  i=0
  while [ "$i" -lt 30 ]; do
    if curl -fsS -o /dev/null -m 2 "http://127.0.0.1:$PORT_PANEL/" 2>/dev/null; then
      break
    fi
    i=$((i + 1))
    sleep 1
  done

  if [ "$i" -ge 30 ]; then
    warn "面板 30 秒内没响应，看日志："
    $COMPOSE logs --tail 40
    die "启动失败"
  fi

  # 首次启动的 admin 令牌在容器日志里
  TOK=$(docker logs ccs-proxy 2>&1 | grep -o 'token=[A-Za-z0-9_-]*' | head -1 | cut -d= -f2 || true)
  IP=$(hostname -I 2>/dev/null | awk '{print $1}')
  [ -n "$IP" ] || IP="<服务器IP>"

  printf '\n'
  printf '══════════════════════════════════════════════\n'
  printf ' 面板    http://%s:%s\n' "$IP" "$PORT_PANEL"
  printf ' 账号    %s / %s\n' "${PANEL_USER:-admin}" "${PANEL_PASS:-见 .env}"
  if [ -n "$TOK" ]; then
    printf ' 代理令牌 %s\n' "$TOK"
  fi
  printf ' 端口    HTTP %s · SOCKS5 %s · SSH 中转 %s\n' "$PORT_HTTP" "$PORT_SOCKS" "$PORT_RELAY"
  printf '══════════════════════════════════════════════\n'
  printf ' 停止：sh start.sh down    日志：sh start.sh logs\n\n'
}

do_down() {
  need_docker
  log "停止容器"
  $COMPOSE down --remove-orphans
  log "已停止"
}

do_logs() {
  need_docker
  exec $COMPOSE logs -f --tail 100
}

do_status() {
  need_docker
  $COMPOSE ps
}

case "${1:-up}" in
  up|"")    do_up ;;
  down|stop) do_down ;;
  restart)  do_down; do_up ;;
  logs)     do_logs ;;
  status|ps) do_status ;;
  local)
    log "裸机模式（不用 Docker）"
    exec sh "$DIR/run-local.sh"
    ;;
  -h|--help|help)
    sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
    ;;
  *)
    die "未知参数 $1；可用：up / down / restart / logs / status / local"
    ;;
esac
