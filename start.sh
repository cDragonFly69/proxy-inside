#!/usr/bin/env bash
# ccsproxy 启动脚本：全部用环境变量驱动，最后 exec 到 python，
# 让容器的 PID 1 就是代理进程本身 —— docker stop 的 SIGTERM 会直达它，打印 bye 并立即释放端口。
set -euo pipefail

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

ARGS=("${PORT}" "${BIND}" "--max-conn" "${MAX_CONN}" "--timeout" "${TIMEOUT}")
[ -n "${ALLOW}" ] && ARGS+=("--allow" "${ALLOW}")
[ -n "${TOKEN}" ] && ARGS+=("--token" "${TOKEN}")
[ -n "${SRCIP}" ] && ARGS+=("--srcip" "${SRCIP}")

# 只汇报令牌开/关，绝不打印令牌值
echo "[INFO] start.sh port=${PORT} bind=${BIND} allow=${ALLOW:-ALL} token=$([ -n "${TOKEN}" ] && echo on || echo off) max-conn=${MAX_CONN} timeout=${TIMEOUT}" >&2
exec python3 /app/ccsproxy.py "${ARGS[@]}" "$@"
