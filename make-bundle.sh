#!/bin/sh
# make-bundle.sh —— 跳板机上跑这一个脚本就够了。
#
#   sh make-bundle.sh            # 准备客户端包并启动分发服务
#   sh make-bundle.sh --refresh  # 强制重新下载 sing-box / wintun
#
# 做的事：
#   1) 下载 sing-box.exe（GitHub API 查最新版，多镜像 + 断点续传 + 完整性校验）
#   2) 下载 wintun.dll
#   3) 按 SERVER/PORT/USER/TOKEN 生成 config.json
#   4) 打包成 client/client-bundle.tgz
#   5) 在 HTTP_PORT 起静态服务供 Windows 下载
#
# 环境变量（都有默认值，一般不用改）：
#   SERVER=10.200.1.26  SOCKS_PORT=18082  SOCKS_USER=ccs  TOKEN=s3cret  HTTP_PORT=18090
set -eu

DIR=$(cd "$(dirname "$0")" && pwd -P)
CLIENT="$DIR/client"
BUNDLE="$CLIENT/client-bundle.tgz"
PIDFILE=/tmp/ccs-bundle-http.pid

SERVER="${SERVER:-10.200.1.26}"
SOCKS_PORT="${SOCKS_PORT:-18082}"
SOCKS_USER="${SOCKS_USER:-ccs}"
TOKEN="${TOKEN:-s3cret}"
HTTP_PORT="${HTTP_PORT:-18090}"
REFRESH=0
[ "${1:-}" = "--refresh" ] && REFRESH=1

log() { printf '[bundle] %s\n' "$*"; }
die() { printf '[bundle][ERR] %s\n' "$*" >&2; exit 1; }

command -v python3 >/dev/null 2>&1 || die "需要 python3"
mkdir -p "$CLIENT"

# ---------- 1) sing-box.exe ----------
if [ "$REFRESH" = 1 ] || [ ! -s "$CLIENT/sing-box.exe" ]; then
  log "解析 sing-box 最新版下载地址 ..."
  URL=$(curl -fsSL --max-time 40 https://api.github.com/repos/SagerNet/sing-box/releases/latest \
        | grep -oE 'https://github.com/SagerNet/sing-box/releases/download/[^"]*windows[_-]amd64\.zip' \
        | head -1) || true
  [ -n "$URL" ] || die "GitHub API 没给出 windows-amd64 下载地址"
  log "目标 $URL"

  OK=0
  for P in "" "https://ghproxy.net/" "https://gh-proxy.com/" "https://ghfast.top/"; do
    log "尝试 ${P:-直连} ..."
    rm -f /tmp/sb.zip
    curl -L --http1.1 -C - --retry 6 --retry-delay 2 -o /tmp/sb.zip "${P}${URL}" 2>/dev/null || true
    if python3 -m zipfile -t /tmp/sb.zip >/dev/null 2>&1; then OK=1; break; fi
    log "  包不完整，换下一个源"
  done
  [ "$OK" = 1 ] || die "所有下载源都失败，稍后重试或手动放置 client/sing-box.exe"

  rm -rf /tmp/sb && python3 -m zipfile -e /tmp/sb.zip /tmp/sb
  EXE=$(find /tmp/sb -name 'sing-box.exe' | head -1)
  [ -n "$EXE" ] || die "压缩包里找不到 sing-box.exe"
  cp "$EXE" "$CLIENT/sing-box.exe"
  log "sing-box.exe 就绪（$(du -h "$CLIENT/sing-box.exe" | cut -f1)）"
else
  log "sing-box.exe 已存在，跳过（--refresh 可强制重下）"
fi

# ---------- 2) wintun.dll ----------
if [ "$REFRESH" = 1 ] || [ ! -s "$CLIENT/wintun.dll" ]; then
  log "下载 wintun.dll ..."
  rm -f /tmp/wt.zip
  if curl -L --retry 4 --retry-delay 2 -o /tmp/wt.zip \
       https://www.wintun.net/builds/wintun-0.14.1.zip 2>/dev/null \
     && python3 -m zipfile -t /tmp/wt.zip >/dev/null 2>&1; then
    rm -rf /tmp/wt && python3 -m zipfile -e /tmp/wt.zip /tmp/wt
    DLL=$(find /tmp/wt -name 'wintun.dll' | grep -i amd64 | head -1)
    [ -n "$DLL" ] || DLL=$(find /tmp/wt -name 'wintun.dll' | head -1)
    [ -n "$DLL" ] || die "wintun 包里没有 wintun.dll"
    cp "$DLL" "$CLIENT/wintun.dll"
    log "wintun.dll 就绪"
  else
    die "wintun 下载失败；可手动把 amd64 版 wintun.dll 放进 client/"
  fi
else
  log "wintun.dll 已存在，跳过"
fi

# ---------- 3) 生成 config.json ----------
python3 - "$CLIENT" "$SERVER" "$SOCKS_PORT" "$SOCKS_USER" "$TOKEN" <<'PY'
import json, sys
client, server, port, user, token = sys.argv[1:6]
tpl = open(client + "/sing-box.json", encoding="utf-8").read()
raw = (tpl.replace("__SERVER__", server)
          .replace("__PORT__", port)
          .replace("__USER__", user)
          .replace("__PASS__", token))
cfg = json.loads(raw)  # 顺带校验 JSON 合法性
with open(client + "/config.json", "w", encoding="utf-8") as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)
PY
log "config.json 已生成（server=${SERVER}:${SOCKS_PORT} user=${SOCKS_USER}）"

# ---------- 4) 打包 ----------
rm -f "$BUNDLE"
tar czf "$BUNDLE" -C "$CLIENT" \
  run-tun.ps1 sing-box.json config.json sing-box.exe wintun.dll start-client.bat
log "打包完成：${BUNDLE}（$(du -h "$BUNDLE" | cut -f1)）"

# ---------- 5) 起分发服务 ----------
if [ -f "$PIDFILE" ]; then
  OLD=$(cat "$PIDFILE" 2>/dev/null || true)
  [ -n "${OLD:-}" ] && kill "$OLD" 2>/dev/null || true
  sleep 0.3
fi
pkill -f "[h]ttp.server $HTTP_PORT" 2>/dev/null || true
sleep 0.3

cd "$CLIENT"
nohup python3 -m http.server "$HTTP_PORT" --bind 0.0.0.0 \
  >/tmp/ccs-bundle-http.log 2>&1 &
echo $! > "$PIDFILE"
sleep 1

if kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  log "分发服务已启动：http://${SERVER}:${HTTP_PORT}/client-bundle.tgz"
  log ""
  log "接下来去 Windows 上运行（管理员 PowerShell）："
  log "    cd D:\\code\\pi\\client"
  log "    .\\run-tun.ps1 -Password ${TOKEN}"
  log ""
  log "停止分发：kill \$(cat $PIDFILE)"
else
  die "分发服务启动失败，看 /tmp/ccs-bundle-http.log"
fi
