#!/bin/sh
# 裸机启动（不用 Docker）：直接跑两个 Python 进程
#   sh run-local.sh
#
# 想用 Docker + 网页面板，用 sh start.sh
# 同时起两个服务：
#   1) HTTP 正向代理（CCSPROXY）  —— curl/git/浏览器下载用，只有 TCP
#   2) SOCKS5 + UDP ASSOCIATE     —— UU 远程这类 UDP 应用用，配 sing-box TUN 接管整机流量
# 全部用环境变量驱动；收到 SIGINT/SIGTERM 时一并收掉两个子进程。
set -eu

PORT="${PORT:-18081}"                 # HTTP 代理端口
SOCKS_PORT="${SOCKS_PORT:-18082}"     # SOCKS5 端口
ENABLE_SOCKS="${ENABLE_SOCKS:-1}"     # 0 = 只起 HTTP 代理
BIND="${BIND:-0.0.0.0}"
ALLOW="${ALLOW:-}"
MAX_CONN="${MAX_CONN:-200}"
TIMEOUT="${TIMEOUT:-30}"
SRCIP="${SRCIP:-}"
SOCKS_USER="${SOCKS_USER:-ccs}"       # SOCKS5 认证用户名
SOCKS_ADVERTISE="${SOCKS_ADVERTISE:-}" # 回给客户端的 UDP relay 地址，空=自动探测
SOCKS_RELAX_AUTH="${SOCKS_RELAX_AUTH:-1}"  # 1=客户端不支持用户名密码认证时按无认证放行（UU 远程这类）
MASK_IP="${MASK_IP:-}"                # 日志里展示的客户端 IP（真实 IP 记到 /tmp/ccsproxy.real.log）

# 令牌：优先 TOKEN；否则从 TOKEN_FILE（docker secret / 挂载文件）首行读取
TOKEN="${TOKEN:-}"
if [ -z "${TOKEN}" ] && [ -n "${TOKEN_FILE:-}" ] && [ -r "${TOKEN_FILE}" ]; then
  TOKEN="$(head -n 1 "${TOKEN_FILE}" | tr -d '\r\n')"
fi
if [ -z "${TOKEN}" ] && [ "${REQUIRE_TOKEN:-0}" = "1" ]; then
  echo "[ERR] REQUIRE_TOKEN=1 但未提供 TOKEN / TOKEN_FILE" >&2
  exit 1
fi

# 按脚本自身所在目录定位程序：容器内是 /app，裸机是仓库目录
DIR=$(cd "$(dirname "$0")" && pwd -P)
if ! command -v python3 >/dev/null 2>&1; then
  echo "[ERR] 未找到 python3，请先安装 Python 3.8+" >&2
  exit 1
fi

# 启动前清理：只杀命令行里含本程序名的旧进程，不动别人的服务
if [ "${KILL_OLD:-1}" = "1" ] && command -v pkill >/dev/null 2>&1; then
  # 方括号写法让 pkill 不会匹配到执行它的 shell 自身（命令行里的字面方括号不匹配该正则）
  pkill -f '[c]csproxy[.]py' 2>/dev/null || true
  pkill -f '[s]ocks5udp[.]py' 2>/dev/null || true
  sleep 1
  # 老版本进程可能不响应 SIGTERM（阻塞在 accept 上），强杀兜底
  pkill -9 -f '[c]csproxy[.]py' 2>/dev/null || true
  pkill -9 -f '[s]ocks5udp[.]py' 2>/dev/null || true
  sleep 0.3
fi

PIDS=""
cleanup() {
  trap - INT TERM
  for p in $PIDS; do kill "$p" 2>/dev/null || true; done
  wait 2>/dev/null || true
  exit 0
}
trap cleanup INT TERM

start_http() {
  set -- "$PORT" "$BIND" --max-conn "$MAX_CONN" --timeout "$TIMEOUT"
  if [ -n "$ALLOW" ]; then set -- "$@" --allow "$ALLOW"; fi
  if [ -n "$TOKEN" ]; then set -- "$@" --token "$TOKEN"; fi
  if [ -n "$SRCIP" ]; then set -- "$@" --srcip "$SRCIP"; fi
  if [ -n "$MASK_IP" ]; then set -- "$@" --mask-ip "$MASK_IP"; fi
  exec python3 "$DIR/ccsproxy.py" "$@"   # exec：后台子 shell 被 python 取代，kill 得到的就是它
}

start_socks() {
  set -- "$SOCKS_PORT" "$BIND" --max-conn "$MAX_CONN" --timeout "$TIMEOUT"
  if [ -n "$ALLOW" ]; then set -- "$@" --allow "$ALLOW"; fi
  if [ -n "$TOKEN" ]; then set -- "$@" --token "$TOKEN" --user "$SOCKS_USER"; fi
  if [ "$SOCKS_RELAX_AUTH" = 1 ]; then set -- "$@" --relax-auth; fi
  if [ -n "$SOCKS_ADVERTISE" ]; then set -- "$@" --advertise "$SOCKS_ADVERTISE"; fi
  if [ -n "$MASK_IP" ]; then set -- "$@" --mask-ip "$MASK_IP"; fi
  exec python3 "$DIR/socks5udp.py" "$@"
}

if [ -n "${TOKEN}" ]; then TOKEN_STATE=on; else TOKEN_STATE=off; fi
echo "[INFO] start.sh http=${PORT} socks=${SOCKS_PORT} bind=${BIND} allow=${ALLOW:-ALL} token=${TOKEN_STATE} max-conn=${MAX_CONN} timeout=${TIMEOUT} mask=${MASK_IP:-off}" >&2

start_http &
PIDS="$PIDS $!"

if [ "$ENABLE_SOCKS" = "1" ]; then
  start_socks &
  PIDS="$PIDS $!"
fi

# 启动自检：任一服务没起来（端口占用等）就收掉另一个并报错，避免半死不活
sleep 1
for p in $PIDS; do
  if ! kill -0 "$p" 2>/dev/null; then
    echo "[ERR] 有服务启动失败，检查端口占用：ss -ltnp | grep -E ':${PORT}|:${SOCKS_PORT}'" >&2
    for q in $PIDS; do kill "$q" 2>/dev/null || true; done
    exit 1
  fi
done

wait
