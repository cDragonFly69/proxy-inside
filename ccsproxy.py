#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ccsproxy.py — 零依赖 HTTP 正向代理（CONNECT + 普通 HTTP）。python3 ccsproxy.py [port] [bind_host] [--allow a,b] [--token T] [--mask-ip IP]"""
import argparse
import base64

import users as usertable
import os
import signal
import socket
import sys
import threading
import time

BUF, HDR_LIMIT = 65536, 65536  # 单次 recv 大小 / 头部字节上限
LS = None                      # 监听 socket，供信号处理器关闭
SHUTDOWN = False
LOCK = threading.Lock()
NOW = 0                        # 当前活动连接数

REAL_LOG = "/tmp/ccsproxy.real.log"  # 伪装模式下真实 IP 的私有记录，0600 仅本用户可读


def log(tag, msg):
    print("[%s] %s" % (tag, msg), flush=True)  # 只打元信息，绝不打印头部/body/Cookie/Token


def note_real(real, shown):
    """--mask-ip 生效时把真实 IP 追加进 0600 私有文件，只有本用户能排查。"""
    try:
        fd = os.open(REAL_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.write(fd, ("%s\t%s <- real %s\n" % (time.strftime("%F %T"), shown, real)).encode()); os.close(fd)
    except OSError:
        pass

def fail(conn, code, reason, ip, msg):
    """回一个无 body 的错误状态行 + 打一行 ERR 日志（不回显任何敏感内容）。"""
    hdr = "Proxy-Authenticate: Basic realm=\"ccsproxy\"\r\n" if code == 407 else ""  # 缺它 curl/git 不会重试
    try:
        conn.sendall(("HTTP/1.1 %d %s\r\n%sContent-Length: 0\r\nConnection: close\r\n\r\n" % (code, reason, hdr)).encode("latin-1"))
        conn.close()  # 错误响应发完即关
    except OSError:
        pass
    log("ERR", "%s <- %s" % (msg, ip))

def read_header(sock):
    """读到 \\r\\n\\r\\n 为止（容忍分包）；extra 是 TLS ClientHello 或 body 前奏，必须原样透传。"""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(BUF)
        if not chunk or len(buf) + len(chunk) > HDR_LIMIT:
            return None, b""
        buf += chunk
    head, _, extra = buf.partition(b"\r\n\r\n")
    return head, extra

def parse_head(head):
    """解析头部 → (method, target, version, headers)；非法返回 None。"""
    lines = head.decode("latin-1", "replace").split("\r\n")
    p = lines[0].split(" ")
    if len(p) != 3 or not p[2].upper().startswith("HTTP/"): return None
    hs = []
    for line in (x for x in lines[1:] if x):
        if ":" not in line:
            return None
        k, v = line.split(":", 1)
        hs.append((k.strip(), v.strip()))
    return p[0], p[1], p[2], hs

def get_header(hs, name):
    name = name.lower()
    return next((v for k, v in hs if k.lower() == name), None)

def host_allowed(host, allow):
    """白名单：精确或子域后缀匹配（--allow 163.com 覆盖 *.163.com）；空表放行全部。"""
    if not allow: return True
    host = host.lower().rstrip(".")
    return any(host == d or host.endswith("." + d)
               for d in (x.lower().strip().rstrip(".") for x in allow) if d)

def split_hostport(target, default_port):
    """拆 host:port / [IPv6]:port；失败返回 (None, None)。"""
    if target.startswith("["):
        if "]" not in target: return None, None
        host, _, rest = target[1:].partition("]")
        return (host, int(rest[1:])) if rest.startswith(":") and rest[1:].isdigit() else (host, default_port)
    if target.count(":") == 1:
        h, p = target.split(":")
        return (h, int(p)) if p.isdigit() else (None, None)
    return (None, None) if ":" in target else (target, default_port)

def connect_upstream(host, port, cfg):
    """建立上游 TCP；IPv4 优先，容器里常有不可达的 IPv6，会让 connect 白等到超时。"""
    infos = sorted(socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM),
                   key=lambda x: 0 if x[0] == socket.AF_INET else 1)
    last = None
    for fam, stype, proto, _, sa in infos:
        s = socket.socket(fam, stype, proto)
        s.settimeout(min(float(cfg.timeout), 8.0))  # 单地址连接上限，坏地址不拖死整个请求
        try:
            if cfg.srcip:
                s.bind((cfg.srcip, 0))
            s.connect(sa)
            s.settimeout(cfg.timeout)               # 连上后恢复业务读写超时
            return s
        except OSError as e:
            last = e
            s.close()
    raise last or OSError("connect failed")

def pipe(src, dst):
    """单向泵：EOF 后对 dst 做 SHUT_WR 半关闭，反向线程继续把剩余数据读完。"""
    try:
        while True:
            chunk = src.recv(BUF)
            if not chunk:
                break
            dst.sendall(chunk)  # sendall 内部循环，大块不截断
    except (BrokenPipeError, ConnectionResetError, socket.timeout, OSError):
        pass  # 单连接异常静默收尾
    finally:
        try: dst.shutdown(socket.SHUT_WR)  # 半关闭：我不写了，但还能读
        except OSError: pass

def pump(a, b):
    """双向透传，两个方向都收尾才返回（尾部读完再关，避免大文件被截断）。"""
    ts = [threading.Thread(target=pipe, args=(a, b), daemon=True),
          threading.Thread(target=pipe, args=(b, a), daemon=True)]
    for t in ts: t.start()
    for t in ts: t.join()

def handle(conn, addr, cfg):
    """单个客户端连接全流程；任何异常都只影响本连接。"""
    global NOW
    real = addr[0] if addr else "-"
    ip = cfg.mask_ip or real      # 日志对外展示的地址
    if cfg.mask_ip:
        note_real(real, ip)
    up = None
    try:
        conn.settimeout(cfg.timeout)
        head, extra = read_header(conn)
        if head is None:            # 空连接/超长头：静默丢弃，不刷 400（含容器健康探活）
            return
        parsed = parse_head(head)
        if parsed is None:
            return fail(conn, 400, "Bad Request", ip, "400 bad request")
        method, target, version, headers = parsed
        who = ""
        if usertable.ip_trusted(real, cfg.trust):
            who = "trusted"          # 免认证 IP 名单命中，直接放行
        elif cfg.token or cfg.users:  # 单令牌或 users.json 多用户；Bearer 与 Basic(user:token) 都认
            auth = next((v for k, v in headers if k.lower() == "proxy-authorization"), "").strip()
            who = usertable.pick(auth, cfg.token, cfg.users) or ""
            if not who:
                return fail(conn, 407, "Proxy Authentication Required", ip, "407 token missing/bad")
        tag = (" [%s]" % who) if who else ""
        if method.upper() == "CONNECT":
            host, port = split_hostport(target, 443)
        else:  # 普通 HTTP：绝对 URI → origin-form
            if not target.lower().startswith("http://"):
                return fail(conn, 400, "Bad Request", ip, "400 bad uri")
            rest = target[7:]
            idx = rest.find("/")
            path = "/" if idx < 0 else rest[idx:]
            host, port = split_hostport(rest if idx < 0 else rest[:idx], 80)
        if not host: return fail(conn, 400, "Bad Request", ip, "400 bad target")
        if not host_allowed(host, cfg.allow):
            return fail(conn, 403, "Forbidden", ip, "403 %s not allowed" % host)
        if method.upper() == "CONNECT":
            log("CONNECT", "%s:%d <- %s%s" % (host, port, ip, tag))
        else:
            log("HTTP", "%s%s <- %s" % (host, path, ip))
        try:
            up = connect_upstream(host, port, cfg)
        except OSError as e:
            return fail(conn, 502, "Bad Gateway", ip, "502 %s:%d %s" % (host, port, e))
        if method.upper() == "CONNECT":
            conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            if extra:  # 客户端抢跑的 TLS 字节，先补给上游
                up.sendall(extra)
        else:
            out = ["%s %s %s" % (method, path, version)]
            out += ["%s: %s" % (k, v) for k, v in headers  # 代理专用头与连接管理头不下传
                    if k.lower() not in ("proxy-connection", "proxy-authorization", "connection", "keep-alive")]
            out.append("Connection: close")
            up.sendall(("\r\n".join(out) + "\r\n\r\n").encode("latin-1"))
            if extra:  # 已读出的 body 前奏补回
                up.sendall(extra)
        pump(conn, up)
    except (BrokenPipeError, ConnectionResetError, socket.timeout, OSError):
        pass  # 连接级异常静默收尾，主进程不受影响
    except Exception as e:  # 兜底：绝不让单连接打挂服务
        log("ERR", "unexpected %s <- %s" % (e.__class__.__name__, ip))
    finally:
        for s in (conn, up):
            try: s and s.close()
            except OSError: pass
        with LOCK: NOW -= 1

def serve(cfg):
    """监听循环：accept → 并发上限判断 → 起线程处理。"""
    global LS, NOW
    ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # 退出后端口立即可复用
    ls.bind((cfg.bind, cfg.port)); ls.listen(256)
    ls.settimeout(0.5)  # accept 带超时：信号到达后最多 0.5s 退出，端口立即释放
    LS = ls
    log("INFO", "listening %s:%d allow=%s token=%s max-conn=%d timeout=%ss srcip=%s mask=%s" % (
        cfg.bind, cfg.port, ",".join(cfg.allow) or "ALL", "on(%d)" % (1 + len(usertable.load(cfg.users))) if (cfg.token or cfg.users) else "off",
        cfg.max_conn, cfg.timeout, cfg.srcip or "auto", cfg.mask_ip or "off"))
    while not SHUTDOWN:
        try: conn, addr = ls.accept()
        except socket.timeout: continue
        except OSError: break
        with LOCK:
            over = NOW >= cfg.max_conn
            if not over: NOW += 1
        if over:
            fail(conn, 503, "Service Unavailable", cfg.mask_ip or (addr[0] if addr else "-"), "503 max-conn")
            continue
        threading.Thread(target=handle, args=(conn, addr, cfg), daemon=True).start()

def on_signal(signum, frame):
    """SIGINT/SIGTERM：只置退出标志。Linux 上 close 不会唤醒阻塞中的 accept，靠轮询退出。"""
    global SHUTDOWN
    SHUTDOWN = True

def parse_args(argv):
    ap = argparse.ArgumentParser(prog="ccsproxy.py", description="零依赖 HTTP 正向代理（CONNECT + 普通 HTTP）")
    ap.add_argument("port", nargs="?", type=int, default=18081, help="监听端口，默认 18081")
    ap.add_argument("bind", nargs="?", default="0.0.0.0", help="监听地址，默认 0.0.0.0")
    ap.add_argument("--allow", default="", help="目标白名单，逗号分隔（后缀匹配），空=不限")
    ap.add_argument("--token", default="", help="要求 Proxy-Authorization: Bearer <token>")
    ap.add_argument("--users", default="", help="多用户令牌表 users.json；与 --token 可并存")
    ap.add_argument("--trust", default="", help="免认证客户端 IP 名单 trusted.json（支持 CIDR）")
    ap.add_argument("--max-conn", type=int, default=200, dest="max_conn", help="并发上限，默认 200")
    ap.add_argument("--timeout", type=float, default=30.0, help="连接/读写超时秒，默认 30")
    ap.add_argument("--srcip", default="", help="出站源地址绑定（多网卡出口 IP），默认系统选路")
    ap.add_argument("--mask-ip", default="", dest="mask_ip", help="日志显示用这个 IP，真实 IP 记到 " + REAL_LOG)
    cfg = ap.parse_args(argv)
    cfg.allow = [x for x in cfg.allow.split(",") if x.strip()]
    return cfg

def main(argv=None):
    cfg = parse_args(sys.argv[1:] if argv is None else argv)
    for sig in (signal.SIGINT, signal.SIGTERM): signal.signal(sig, on_signal)
    try: serve(cfg)
    except OSError as e:
        log("ERR", "listen failed: %s" % e); return 2
    print("bye", flush=True)

if __name__ == "__main__":
    sys.exit(main())
