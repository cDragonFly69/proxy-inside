#!/bin/sh
# gh-relay.sh —— GitHub SSH 中转
#
# 场景：内网的机器连不上 github.com:22（TCP 能建、SSH 握手被掐），
#       但跳板机能通。把跳板机当成 SSH 跳板：
#
#         git push ssh://git@JUMP_HOST:2222/OWNER/REPO.git
#
# 用法（在**跳板机**上跑）：
#   sh gh-relay.sh                # 前台运行，Ctrl+C 停止
#   sh gh-relay.sh -d            # 后台常驻（setsid + nohup）
#   sh gh-relay.sh -k            # 停掉后台实例
#
# 环境变量：
#   GH_RELAY_PORT=2222           监听端口
#   GH_RELAY_BIND=0.0.0.0        监听地址
#   GH_RELAY_UPSTREAM=github.com:22   上游目标
set -eu

PORT="${GH_RELAY_PORT:-2222}"
BIND="${GH_RELAY_BIND:-0.0.0.0}"
UPSTREAM="${GH_RELAY_UPSTREAM:-github.com:22}"
PIDFILE="/tmp/ccs-gh-relay.pid"
LOGFILE="/tmp/ccs-gh-relay.log"
SELF=$(cd "$(dirname "$0")" && pwd -P)/$(basename "$0")

log() { printf '[gh-relay] %s\n' "$*"; }
die() { printf '[gh-relay][ERR] %s\n' "$*" >&2; exit 1; }

command -v python3 >/dev/null 2>&1 || die "需要 python3"

stop_running() {
  if [ -f "$PIDFILE" ]; then
    OLD=$(cat "$PIDFILE" 2>/dev/null || true)
    if [ -n "${OLD:-}" ] && kill -0 "$OLD" 2>/dev/null; then
      kill "$OLD" 2>/dev/null || true
      sleep 0.5
      kill -9 "$OLD" 2>/dev/null || true
      log "已停止旧实例 pid=$OLD"
    fi
    rm -f "$PIDFILE"
  fi
  pkill -f "[g]h-relay[.]sh --worker" 2>/dev/null || true
}

run_worker() {
  exec python3 - "$PORT" "$BIND" "$UPSTREAM" <<'PY'
import socket, sys, threading

port = int(sys.argv[1])
bind = sys.argv[2]
host, _, p = sys.argv[3].rpartition(":")
upstream = (host, int(p))

def pipe(a, b):
    try:
        while True:
            d = a.recv(65536)
            if not d:
                break
            b.sendall(d)
    except Exception:
        pass
    finally:
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
        for s in (a, b):
            try:
                s.close()
            except Exception:
                pass

def handle(c):
    try:
        u = socket.create_connection(upstream, 15)
    except Exception as e:
        print("upstream fail %s:%d %s" % (upstream[0], upstream[1], e), flush=True)
        c.close()
        return
    threading.Thread(target=pipe, args=(c, u), daemon=True).start()
    threading.Thread(target=pipe, args=(u, c), daemon=True).start()

srv = socket.socket()
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind((bind, port))
srv.listen(100)
print("listening %s:%d -> %s:%d" % (bind, port, upstream[0], upstream[1]), flush=True)
while True:
    try:
        c, _ = srv.accept()
    except OSError:
        break
    threading.Thread(target=handle, args=(c,), daemon=True).start()
PY
}

case "${1:-}" in
  -k|--kill)
    stop_running
    log "已停止"
    exit 0
    ;;
  -d|--daemon)
    stop_running
    rm -f "$PIDFILE"
    if command -v setsid >/dev/null 2>&1; then
      setsid nohup sh "$SELF" --worker >"$LOGFILE" 2>&1 </dev/null &
    else
      nohup sh "$SELF" --worker >"$LOGFILE" 2>&1 </dev/null &
    fi
    sleep 1.5
    if kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      log "后台运行中 pid=$(cat "$PIDFILE")"
      log "日志 $LOGFILE"
      log ""
      log "客户端配置（把 JUMP 换成跳板机 IP，把 2222 换成 ${PORT}）："
      log "  git remote set-url origin ssh://git@JUMP:$PORT/OWNER/REPO.git"
      log ""
      log "验证：ssh -o StrictHostKeyChecking=accept-new -p $PORT git@JUMP"
      log "停止：sh $(basename "$0") -k"
    else
      die "启动失败，看 $LOGFILE"
    fi
    exit 0
    ;;
  --worker)
    echo $$ > "$PIDFILE"
    run_worker
    ;;
  ""|--foreground|-f)
    stop_running
    log "前台运行：$BIND:$PORT -> $UPSTREAM （Ctrl+C 停止）"
    run_worker
    ;;
  *)
    die "未知参数 $1；可用：-d / -k / -f"
    ;;
esac
