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

DIR = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("DATA_DIR") or os.path.join(DIR, "data")
USERS_FILE = os.path.join(DATA, "users.json")
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
    socks_cmd += ["--users", USERS_FILE]
    if TOKEN:
        socks_cmd += ["--token", TOKEN]
    if RELAX_AUTH == "1":
        socks_cmd += ["--relax-auth"]
    http_cmd += ["--users", USERS_FILE]
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
        if not check_basic(self.headers.get("Authorization")):
            return self._deny()

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
