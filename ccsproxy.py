#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ccsproxy.py — 单文件零依赖 HTTP 正向代理（CONNECT 隧道 + 普通 HTTP 转发），内网跳板出网用。
    python3 ccsproxy.py [port] [bind_host] [--allow a,b] [--token T] [--max-conn N] [--timeout S] [--srcip IP]"""
import argparse
import signal
import socket
import sys
import threading

BUF, HDR_LIMIT = 65536, 65536  # 单次 recv 大小 / 头部字节上限
LS = None                      # 监听 socket，供信号处理器关闭
SHUTDOWN = False
LOCK = threading.Lock()
NOW = 0                        # 当前活动连接数

def log(tag, msg):
    print("[%s] %s" % (tag, msg), flush=True)  # 只打元信息，绝不打印头部/body/Cookie/Token

def fail(conn, code, reason, ip, msg):
    """回一个无 body 的错误状态行 + 打一行 ERR 日志（不回显任何敏感内容）。"""
    try:
        conn.sendall(("HTTP/1.1 %d %s\r\nContent-Length: 0\r\nConnection: close\r\n\r\n" % (code, reason)).encode("latin-1"))
    except OSError:
        pass
    log("ERR", "%s <- %s" % (msg, ip))
    try: conn.close()          # 错误响应发完即关，避免句柄泄漏
    except OSError: pass

def read_header(sock):
    """读到 \\r\\n\\r\\n 为止（容忍分包），返回 (头部, 头部之后多读到的字节)。
    extra 在 CONNECT 时是 TLS ClientHello、在 HTTP 时是 body 前奏，必须原样透传。"""
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
    if len(p) != 3 or not p[2].upper().startswith("HTTP/"):
        return None
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
    if not allow:
        return True
    host = host.lower().rstrip(".")
    return any(host == d or host.endswith("." + d)
               for d in (x.lower().strip().rstrip(".") for x in allow) if d)

def split_hostport(target, default_port):
    """拆 host:port / [IPv6]:port；失败返回 (None, None)。"""
    if target.startswith("["):
        if "]" not in target:
            return None, None
        host, _, rest = target[1:].partition("]")
        return (host, int(rest[1:])) if rest.startswith(":") and rest[1:].isdigit() else (host, default_port)
    if target.count(":") == 1:
        h, p = target.split(":")
        return (h, int(p)) if p.isdigit() else (None, None)
    return (None, None) if ":" in target else (target, default_port)

def connect_upstream(host, port, cfg):
    """建立上游 TCP；--srcip 时先 bind，把出站源 IP 钉在指定出口地址（上游只看到该 IP）。"""
    last = None
    for fam, stype, proto, _, sa in socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM):
        s = socket.socket(fam, stype, proto)
        s.settimeout(cfg.timeout)  # 工作 socket 必须带超时
        try:
            if cfg.srcip:
                s.bind((cfg.srcip, 0))
            s.connect(sa)
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
        try:
            dst.shutdown(socket.SHUT_WR)  # 半关闭：我不写了，但还能读
        except OSError:
            pass

def pump(a, b):
    """双向透传，两个方向都收尾才返回（尾部读完再关，避免大文件被截断）。"""
    ts = [threading.Thread(target=pipe, args=(a, b), daemon=True),
          threading.Thread(target=pipe, args=(b, a), daemon=True)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

def handle(conn, addr, cfg):
    """单个客户端连接全流程；任何异常都只影响本连接。"""
    global NOW
    ip = addr[0] if addr else "-"
    up = None
    try:
        conn.settimeout(cfg.timeout)
        head, extra = read_header(conn)
        if head is None:            # 空连接/超长头：静默丢弃，不刷 400（含容器健康探活）
            return
        parsed = parse_head(head)
        if parsed is None:
            fail(conn, 400, "Bad Request", ip, "400 bad request")
            return
        method, target, version, headers = parsed
        if cfg.token:  # 令牌校验，不比对明文回显
            got = (get_header(headers, "Proxy-Authorization") or "").strip().lower()
            if got != "bearer " + cfg.token.lower():
                fail(conn, 407, "Proxy Authentication Required", ip, "407 token missing/bad")
                return
        path = None
        if method.upper() == "CONNECT":
            host, port = split_hostport(target, 443)
        else:  # 普通 HTTP：绝对 URI → origin-form
            if not target.lower().startswith("http://"):
                fail(conn, 400, "Bad Request", ip, "400 bad uri")
                return
            rest = target[7:]
            idx = rest.find("/")
            path = "/" if idx < 0 else rest[idx:]
            host, port = split_hostport(rest if idx < 0 else rest[:idx], 80)
        if not host:
            fail(conn, 400, "Bad Request", ip, "400 bad target")
            return
        if not host_allowed(host, cfg.allow):
            fail(conn, 403, "Forbidden", ip, "403 %s not allowed" % host)
            return
        try:
            up = connect_upstream(host, port, cfg)
        except OSError as e:
            fail(conn, 502, "Bad Gateway", ip, "502 %s:%d %s" % (host, port, e))
            return
        if method.upper() == "CONNECT":
            log("CONNECT", "%s:%d <- %s" % (host, port, ip))
            conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            if extra:  # 客户端抢跑的 TLS 字节，先补给上游
                up.sendall(extra)
        else:
            log("HTTP", "%s%s <- %s" % (host, path, ip))
            out = ["%s %s %s" % (method, path, version)]
            for k, v in headers:
                if k.lower() in ("proxy-connection", "proxy-authorization", "connection", "keep-alive"):
                    continue  # 代理专用头与连接管理头不下传
                out.append("%s: %s" % (k, v))
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
            if s is not None:
                try: s.close()
                except OSError: pass
        with LOCK:
            NOW -= 1

def serve(cfg):
    """监听循环：accept → 并发上限判断 → 起线程处理。"""
    global LS, NOW
    ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # 退出后端口立即可复用
    ls.bind((cfg.bind, cfg.port))
    ls.listen(256)
    LS = ls
    log("INFO", "listening %s:%d allow=%s token=%s max-conn=%d timeout=%ss srcip=%s"
        % (cfg.bind, cfg.port, ",".join(cfg.allow) or "ALL", "on" if cfg.token else "off",
           cfg.max_conn, cfg.timeout, cfg.srcip or "auto"))
    while not SHUTDOWN:
        try:
            conn, addr = ls.accept()
        except OSError:
            break  # 监听 socket 被信号处理器关闭
        with LOCK:
            over = NOW >= cfg.max_conn
            if not over:
                NOW += 1
        if over:
            fail(conn, 503, "Service Unavailable", addr[0] if addr else "-", "503 max-conn")
            continue
        threading.Thread(target=handle, args=(conn, addr, cfg), daemon=True).start()

def on_signal(signum, frame):
    """SIGINT/SIGTERM：关监听 socket，accept 立刻抛错退出，端口立即释放。"""
    global SHUTDOWN
    SHUTDOWN = True
    if LS is not None:
        try: LS.close()
        except OSError: pass

def parse_args(argv):
    ap = argparse.ArgumentParser(prog="ccsproxy.py", description="零依赖 HTTP 正向代理（CONNECT + 普通 HTTP）")
    ap.add_argument("port", nargs="?", type=int, default=18081, help="监听端口，默认 18081")
    ap.add_argument("bind", nargs="?", default="0.0.0.0", help="监听地址，默认 0.0.0.0")
    ap.add_argument("--allow", default="", help="目标白名单，逗号分隔（后缀匹配），空=不限")
    ap.add_argument("--token", default="", help="要求 Proxy-Authorization: Bearer <token>")
    ap.add_argument("--max-conn", type=int, default=200, dest="max_conn", help="并发上限，默认 200")
    ap.add_argument("--timeout", type=float, default=30.0, help="连接/读写超时秒，默认 30")
    ap.add_argument("--srcip", default="", help="出站源地址绑定（多网卡出口 IP），默认系统选路")
    cfg = ap.parse_args(argv)
    cfg.allow = [x for x in cfg.allow.split(",") if x.strip()]
    return cfg

def main(argv=None):
    cfg = parse_args(sys.argv[1:] if argv is None else argv)
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    try:
        serve(cfg)
    except OSError as e:
        log("ERR", "listen failed: %s" % e)
        return 2
    print("bye", flush=True)

if __name__ == "__main__":
    sys.exit(main())
