#!/usr/bin/env python3
"""panel.py — 代理服务管理面板（零第三方依赖）。

一个进程同时提供：
  * Web 管理界面 / API（默认 8080）
  * 子进程管理：HTTP 代理、SOCKS5、GitHub SSH 中转
  * 多用户令牌表读写（/data/users.json，代理热加载）
  * 按服务切分的日志文件（/data/logs/*.log）

环境变量：
  PANEL_PORT=8080  PANEL_USER=admin  PANEL_PASS=changeme
  HTTP_PORT=18081  SOCKS_PORT=18082  GH_RELAY_PORT=2222
  BIND=0.0.0.0  DATA_DIR=/data  TOKEN=  ALLOW=  MASK_IP=  RELAX_AUTH=1
"""

import base64
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import users as usertable

DIR = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("DATA_DIR") or os.path.join(DIR, "data")
USERS_FILE = os.path.join(DATA, "users.json")
TRUST_FILE = os.path.join(DATA, "trusted.json")
LOG_DIR = os.path.join(DATA, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

PANEL_PORT = int(os.environ.get("PANEL_PORT", "8080"))
PANEL_USER = os.environ.get("PANEL_USER", "admin")
PANEL_PASS = os.environ.get("PANEL_PASS", "changeme")
HTTP_PORT = int(os.environ.get("HTTP_PORT", "18081"))
SOCKS_PORT = int(os.environ.get("SOCKS_PORT", "18082"))
RELAY_PORT = int(os.environ.get("GH_RELAY_PORT", "2222"))
BIND = os.environ.get("BIND", "0.0.0.0")
TOKEN = os.environ.get("TOKEN", "")
ALLOW = os.environ.get("ALLOW", "")
MASK_IP = os.environ.get("MASK_IP", "")
MAX_CONN = os.environ.get("MAX_CONN", "200")
TIMEOUT = os.environ.get("TIMEOUT", "30")
RELAX_AUTH = os.environ.get("RELAX_AUTH", "1")

LOCK = threading.RLock()
SERVICES = {}


# ---------------------------------------------------------------- 用户表
def read_users():
    try:
        with open(USERS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    out = []
    for u in data.get("users") or []:
        if isinstance(u, dict) and u.get("token"):
            out.append({
                "name": str(u.get("name") or "user"),
                "token": str(u["token"]),
                "enabled": bool(u.get("enabled", True)),
            })
    return out


def write_users(users):
    tmp = USERS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"users": users}, f, indent=2, ensure_ascii=False)
    os.replace(tmp, USERS_FILE)
    try:
        os.chmod(USERS_FILE, 0o600)
    except OSError:
        pass


def ensure_users():
    if os.path.exists(USERS_FILE):
        return
    tok = TOKEN or secrets.token_urlsafe(18)
    write_users([{"name": "admin", "token": tok, "enabled": True}])
    print("[panel] 首次启动，已创建用户 admin  token=%s" % tok, flush=True)


# ---------------------------------------------------------------- 进程管理
def build_services():
    py = sys.executable
    common = ["--max-conn", MAX_CONN, "--timeout", TIMEOUT]
    if MASK_IP:
        common += ["--mask-ip", MASK_IP]
    allow = ["--allow", ALLOW] if ALLOW else []

    http_cmd = [py, os.path.join(DIR, "ccsproxy.py"), str(HTTP_PORT), BIND] + common + allow
    socks_cmd = [py, os.path.join(DIR, "socks5udp.py"), str(SOCKS_PORT), BIND] + common + allow
    socks_cmd += ["--users", USERS_FILE, "--trust", TRUST_FILE]
    if TOKEN:
        socks_cmd += ["--token", TOKEN]
    if RELAX_AUTH == "1":
        socks_cmd += ["--relax-auth"]
    http_cmd += ["--users", USERS_FILE, "--trust", TRUST_FILE]
    if TOKEN:
        http_cmd += ["--token", TOKEN]

    relay_cmd = [py, os.path.join(DIR, "gh_relay_worker.py"), str(RELAY_PORT), BIND]

    return {
        "http": {
            "label": "HTTP 代理", "port": HTTP_PORT, "cmd": http_cmd,
            "hint": "curl / git / 浏览器；env http_proxy=http://<host>:%d" % HTTP_PORT,
        },
        "socks": {
            "label": "SOCKS5 + UDP", "port": SOCKS_PORT, "cmd": socks_cmd,
            "hint": "UU 远程 / TUN 客户端；socks5h://<host>:%d" % SOCKS_PORT,
        },
        "relay": {
            "label": "GitHub SSH 中转", "port": RELAY_PORT, "cmd": relay_cmd,
            "hint": "git push ssh://git@<host>:%d/OWNER/REPO.git" % RELAY_PORT,
        },
    }


def start_service(name):
    svc = SERVICES[name]
    with LOCK:
        if svc.get("proc") and svc["proc"].poll() is None:
            return {"ok": True, "pid": svc["proc"].pid, "already": True}
        logf = open(os.path.join(LOG_DIR, name + ".log"), "ab", buffering=0)
        try:
            svc["proc"] = subprocess.Popen(
                svc["cmd"], stdout=logf, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, cwd=DIR,
            )
        except Exception as e:
            return {"ok": False, "error": str(e)}
        svc["started"] = time.time()
        return {"ok": True, "pid": svc["proc"].pid}


def stop_service(name):
    svc = SERVICES[name]
    with LOCK:
        p = svc.get("proc")
        if not p or p.poll() is not None:
            svc["proc"] = None
            return {"ok": True, "already": True}
        p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        svc["proc"] = None
        return {"ok": True}


def service_state(name):
    svc = SERVICES[name]
    p = svc.get("proc")
    running = bool(p and p.poll() is None)
    return {
        "name": name,
        "label": svc["label"],
        "port": svc["port"],
        "hint": svc["hint"],
        "running": running,
        "pid": p.pid if running else None,
        "uptime": int(time.time() - svc.get("started", 0)) if running else 0,
        "exit_code": p.poll() if (p and not running) else None,
    }


def tail_log(name, lines=200):
    path = os.path.join(LOG_DIR, name + ".log")
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 256 * 1024))
            blob = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    return blob.splitlines()[-lines:]


# ---------------------------------------------------------------- HTTP 层

def _client_ip_of(handler):
    try:
        return handler.client_address[0]
    except Exception:
        return ""


def _my_ip(handler):
    """面板看到的客户端地址（写进一键脚本，让脚本指向正确的服务器）。"""
    host = (handler.headers.get("Host") or "").split(":")[0]
    if host and host not in ("127.0.0.1", "localhost"):
        return host
    return handler.headers.get("X-Forwarded-Host", "").split(":")[0] or "127.0.0.1"


def _win_on(jump, hport):
    return r'''@echo off
chcp 65001 >nul
setlocal
set "JUMP=__JUMP__"
set "PPORT=__PPORT__"
set "KEY=HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings"

echo 代理地址 %JUMP%:%PPORT%
echo 添加到可信列表后无需密码；若提示认证，用面板里你的令牌。
echo.

reg add "%KEY%" /v ProxyEnable /t REG_DWORD /d 1 /f >nul
reg add "%KEY%" /v ProxyServer /t REG_SZ /d "%JUMP%:%PPORT%" /f >nul
reg add "%KEY%" /v ProxyOverride /t REG_SZ /d "10.*;172.16.*;172.17.*;172.18.*;172.19.*;172.2*;172.30.*;172.31.*;192.168.*;*.local;<local>" /f >nul

powershell -NoProfile -Command "$src='[DllImport(\"wininet.dll\")]public static extern bool InternetSetOption(IntPtr h,int o,IntPtr b,int l);';$t=Add-Type -MemberDefinition $src -Name W -Namespace W -PassThru;$t::InternetSetOption([IntPtr]::Zero,39,[IntPtr]::Zero,0)|Out-Null;$t::InternetSetOption([IntPtr]::Zero,37,[IntPtr]::Zero,0)|Out-Null" 2>nul

echo.
echo [完成] 系统代理已开启。浏览器 / curl / git(https) 现在都走 %JUMP%
echo 验证：curl.exe --ssl-no-revoke https://api.github.com/zen
echo 关闭：双击 proxy-off.bat
echo.
pause
'''.replace("__JUMP__", jump).replace("__PPORT__", str(hport))


def _win_off():
    return r'''@echo off
chcp 65001 >nul
setlocal
set "KEY=HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings"

reg add "%KEY%" /v ProxyEnable /t REG_DWORD /d 0 /f >nul

powershell -NoProfile -Command "$src='[DllImport(\"wininet.dll\")]public static extern bool InternetSetOption(IntPtr h,int o,IntPtr b,int l);';$t=Add-Type -MemberDefinition $src -Name W -Namespace W -PassThru;$t::InternetSetOption([IntPtr]::Zero,39,[IntPtr]::Zero,0)|Out-Null;$t::InternetSetOption([IntPtr]::Zero,37,[IntPtr]::Zero,0)|Out-Null" 2>nul

echo 系统代理已关闭。
pause
'''


def _sh_on(jump, hport):
    return '''#!/bin/sh
# 用法： . ./proxy-on.sh   （必须 source，否则只影响子进程）
export http_proxy="http://__JUMP__:__PPORT__"
export https_proxy="$http_proxy"
export HTTP_PROXY="$http_proxy"
export HTTPS_PROXY="$http_proxy"
export no_proxy="localhost,127.0.0.1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,*.local"
echo "代理已启用：$http_proxy"
echo "验证：curl -sS https://api.github.com/zen"
echo "关闭： . ./proxy-off.sh"
'''.replace("__JUMP__", jump).replace("__PPORT__", str(hport))


def _sh_off():
    return '''#!/bin/sh
# 用法： . ./proxy-off.sh
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY no_proxy
echo "代理已关闭"
'''


def _pac(jump, hport):
    return '''function FindProxyForURL(url, host) {
    if (isPlainHostName(host) ||
        dnsDomainIs(host, ".local") ||
        shExpMatch(host, "10.*") ||
        shExpMatch(host, "192.168.*") ||
        shExpMatch(host, "172.1[6-9].*") ||
        shExpMatch(host, "172.2[0-9].*") ||
        shExpMatch(host, "172.30.*") ||
        shExpMatch(host, "172.31.*")) {
        return "DIRECT";
    }
    return "PROXY __JUMP__:__PPORT__; DIRECT";
}
'''.replace("__JUMP__", jump).replace("__PPORT__", str(hport))


def check_basic(header):
    if not PANEL_PASS:
        return True
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        raw = base64.b64decode(header[6:].strip()).decode("utf-8", "replace")
    except Exception:
        return False
    user, _, pwd = raw.partition(":")
    return user == PANEL_USER and pwd == PANEL_PASS


class Handler(BaseHTTPRequestHandler):
    server_version = "ccs-panel"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    # ---- 工具
    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        raw = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(raw)
        except Exception:
            pass

    def _deny(self):
        self._send(401, {"error": "auth required"},
                   extra={"WWW-Authenticate": 'Basic realm="ccs-panel"'})

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if n <= 0 or n > 1 << 20:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception:
            return {}

    # ---- 路由
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            return self._page()
        if path == "/proxy.pac":
            return self._send(200, _pac(_my_ip(self), HTTP_PORT),
                              "application/x-ns-proxy-autoconfig; charset=utf-8")
        if not check_basic(self.headers.get("Authorization")):
            return self._deny()

        if path == "/api/trust":
            return self._send(200, {
                "ips": usertable.read_trusted(TRUST_FILE),
                "client_ip": _client_ip_of(self),
            })
        if path.startswith("/api/client/"):
            jump = _my_ip(self)
            name = path.rsplit("/", 1)[-1]
            if name == "on.bat":
                body, fname = _win_on(jump, HTTP_PORT), "proxy-on.bat"
            elif name == "off.bat":
                body, fname = _win_off(), "proxy-off.bat"
            elif name == "on.sh":
                body, fname = _sh_on(jump, HTTP_PORT), "proxy-on.sh"
            elif name == "off.sh":
                body, fname = _sh_off(), "proxy-off.sh"
            elif name == "pac":
                body, fname = _pac(jump, HTTP_PORT), "proxy.pac"
            else:
                return self._send(404, {"error": "not found"})
            return self._send(200, body, "text/plain; charset=utf-8",
                              {"Content-Disposition": 'attachment; filename="%s"' % fname})

        if path == "/api/status":
            return self._send(200, {
                "now": int(time.time()),
                "services": [service_state(n) for n in ("http", "socks", "relay")],
                "users": len([u for u in read_users() if u["enabled"]]),
                "host": self.headers.get("Host") or "",
            })
        if path == "/api/logs":
            q = parse_qs(parsed.query)
            name = (q.get("svc") or ["http"])[0]
            if name not in SERVICES:
                return self._send(400, {"error": "unknown service"})
            try:
                n = min(2000, max(10, int((q.get("n") or ["200"])[0])))
            except ValueError:
                n = 200
            return self._send(200, {"service": name, "lines": tail_log(name, n)})
        if path == "/api/users":
            return self._send(200, {"users": read_users()})
        if path == "/api/connect":
            host = (self.headers.get("Host") or "JUMP_HOST").split(":")[0]
            return self._send(200, {"markdown": self._connect_doc(host)})
        if path == "/api/env":
            return self._send(200, {
                "http_port": HTTP_PORT, "socks_port": SOCKS_PORT,
                "relay_port": RELAY_PORT, "allow": ALLOW or "ALL",
                "mask_ip": MASK_IP or "", "bind": BIND,
            })
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not check_basic(self.headers.get("Authorization")):
            return self._deny()
        path = urlparse(self.path).path
        body = self._body()

        if path == "/api/service":
            name = body.get("name")
            action = body.get("action")
            if name not in SERVICES:
                return self._send(400, {"error": "unknown service"})
            if action == "start":
                return self._send(200, start_service(name))
            if action == "stop":
                return self._send(200, stop_service(name))
            if action == "restart":
                stop_service(name)
                time.sleep(0.4)
                return self._send(200, start_service(name))
            return self._send(400, {"error": "unknown action"})

        if path == "/api/users":
            users = read_users()
            action = body.get("action")
            name = (body.get("name") or "").strip()
            if action == "add":
                if not name:
                    return self._send(400, {"error": "name required"})
                if any(u["name"] == name for u in users):
                    return self._send(409, {"error": "用户已存在"})
                tok = (body.get("token") or "").strip() or secrets.token_urlsafe(18)
                users.append({"name": name, "token": tok, "enabled": True})
                write_users(users)
                return self._send(200, {"ok": True, "users": users, "token": tok})
            if action == "delete":
                users = [u for u in users if u["name"] != name]
                write_users(users)
                return self._send(200, {"ok": True, "users": users})
            if action == "toggle":
                for u in users:
                    if u["name"] == name:
                        u["enabled"] = not u["enabled"]
                write_users(users)
                return self._send(200, {"ok": True, "users": users})
            if action == "rotate":
                tok = secrets.token_urlsafe(18)
                for u in users:
                    if u["name"] == name:
                        u["token"] = tok
                write_users(users)
                return self._send(200, {"ok": True, "token": tok, "users": users})
            return self._send(400, {"error": "unknown action"})

        if path == "/api/trust":
            ips = usertable.read_trusted(TRUST_FILE)
            action = body.get("action")
            val = (body.get("ip") or "").strip()
            if action == "add":
                if not val:
                    return self._send(400, {"error": "ip required"})
                if val not in ips:
                    ips.append(val)
                    usertable.write_trusted(TRUST_FILE, ips)
                return self._send(200, {"ok": True, "ips": ips})
            if action == "delete":
                ips = [x for x in ips if x != val]
                usertable.write_trusted(TRUST_FILE, ips)
                return self._send(200, {"ok": True, "ips": ips})
            if action == "clear":
                usertable.write_trusted(TRUST_FILE, [])
                return self._send(200, {"ok": True, "ips": []})
            return self._send(400, {"error": "unknown action"})

        if path == "/api/all":
            action = body.get("action")
            results = {}
            for n in ("http", "socks", "relay"):
                if action == "stop":
                    results[n] = stop_service(n)
                elif action == "start":
                    results[n] = start_service(n)
            return self._send(200, {"ok": True, "results": results})

        return self._send(404, {"error": "not found"})

    # ---- 静态页 / 文档
    def _page(self):
        path = os.path.join(DIR, "web", "index.html")
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except OSError:
            return self._send(500, "web/index.html 缺失", "text/plain; charset=utf-8")
        self._send(200, raw, "text/html; charset=utf-8")

    def _connect_doc(self, host):
        users = [u for u in read_users() if u["enabled"]]
        sample = users[0]["token"] if users else "YOUR_TOKEN"
        lines = [
            "# 代理接入说明",
            "",
            "服务器：`%s`" % host,
            "",
            "## 1. HTTP(S) 代理（curl / git https / 浏览器）",
            "",
            "```",
            "http://%s:%d" % (host, HTTP_PORT),
            "用户名：任意   密码：<你的 token>",
            "```",
            "",
            "```bash",
            "export http_proxy=http://%s:%d" % (host, HTTP_PORT),
            "export https_proxy=$http_proxy",
            "curl -x http://%s:%d --proxy-user any:%s https://api.github.com" % (host, HTTP_PORT, sample),
            "```",
            "",
            "git 走 HTTPS 代理：",
            "```bash",
            "git config --global http.proxy http://any:%s@%s:%d" % (sample, host, HTTP_PORT),
            "```",
            "",
            "## 2. SOCKS5（含 UDP，给 TUN 客户端）",
            "",
            "```",
            "socks5h://%s:%d" % (host, SOCKS_PORT),
            "用户名：任意   密码：<你的 token>",
            "```",
            "",
            "## 3. GitHub SSH 中转",
            "",
            "```bash",
            "git remote set-url origin ssh://git@%s:%d/OWNER/REPO.git" % (host, RELAY_PORT),
            "ssh -o StrictHostKeyChecking=accept-new -p %d git@%s" % (RELAY_PORT, host),
            "git push -u origin HEAD",
            "```",
            "",
            "## 当前用户",
            "",
        ]
        for u in users:
            lines.append("- `%s`  token: `%s`" % (u["name"], u["token"]))
        if not users:
            lines.append("- （还没有用户，去面板里加一个）")
        return "\n".join(lines)


def shutdown(signum=None, frame=None):
    for n in list(SERVICES):
        try:
            stop_service(n)
        except Exception:
            pass
    print("[panel] bye", flush=True)
    os._exit(0)


def main():
    ensure_users()
    SERVICES.update(build_services())
    for n in ("http", "socks", "relay"):
        r = start_service(n)
        print("[panel] start %-5s -> %s" % (n, r), flush=True)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    srv = ThreadingHTTPServer(("0.0.0.0", PANEL_PORT), Handler)
    srv.daemon_threads = True
    print("[panel] web on :%d  user=%s  http=%d socks=%d relay=%d"
          % (PANEL_PORT, PANEL_USER, HTTP_PORT, SOCKS_PORT, RELAY_PORT), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        shutdown()


if __name__ == "__main__":
    main()
