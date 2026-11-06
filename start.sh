#!/bin/sh
# ccsproxy 启动脚本 —— POSIX sh 兼容，dash / bash / busybox sh 都能直接跑：
#   sh start.sh          （dash 也 OK，不依赖 bash 数组与 pipefail）
#   ./start.sh
# 全部用环境变量驱动；最后 exec 到 python，让代理进程直接接收 SIGTERM，
# 收到 docker stop / kill 时打印 bye 并立即释放端口。
set -eu

PORT="${PORT:-18081}"
BIND="${BIND:-0.0.0.0}"
ALLOW="${ALLOW:-}"
MAX_CONN="${MAX_CONN:-200}"
TIMEOUT="${TIMEOUT:-30}"
SRCIP="${SRCIP:-}"

# 令牌：优先 TOKEN；否则从 TOKEN_FILE（docker secret / 挂载文件）首行读取
TOKEN="${TOKEN:-}"
if [ -z "${TOKEN}" ] && [ -n "${TOKEN_FILE:-}" ] && [ -r "${TOKEN_FILE}" ]; then
  TOKEN="$(head -n 1 "${TOKEN_FILE}" | tr -d '\r\n')"
fi
# REQUIRE_TOKEN=1 时拒绝裸奔启动
if [ -z "${TOKEN}" ] && [ "${REQUIRE_TOKEN:-0}" = "1" ]; then
  echo "[ERR] REQUIRE_TOKEN=1 但未提供 TOKEN / TOKEN_FILE" >&2
  exit 1
fi

# 按脚本自身所在目录定位代理文件：容器内是 /app，裸机是仓库目录
DIR=$(cd "$(dirname "$0")" && pwd -P)
if ! command -v python3 >/dev/null 2>&1; then
  echo "[ERR] 未找到 python3，请先安装 Python 3.8+" >&2
  exit 1
fi

# POSIX 方式拼参数，避开 bash 数组；末尾把调用方额外传入的参数透传（如 --help）
set -- "${PORT}" "${BIND}" --max-conn "${MAX_CONN}" --timeout "${TIMEOUT}" "$@"
if [ -n "${ALLOW}" ]; then set -- "$@" --allow "${ALLOW}"; fi
if [ -n "${TOKEN}" ]; then set -- "$@" --token "${TOKEN}"; fi
if [ -n "${SRCIP}" ]; then set -- "$@" --srcip "${SRCIP}"; fi

# 只汇报令牌开/关，绝不打印令牌值
if [ -n "${TOKEN}" ]; then TOKEN_STATE=on; else TOKEN_STATE=off; fi
echo "[INFO] start.sh port=${PORT} bind=${BIND} allow=${ALLOW:-ALL} token=${TOKEN_STATE} max-conn=${MAX_CONN} timeout=${TIMEOUT}" >&2
exec python3 "${DIR}/ccsproxy.py" "$@"
