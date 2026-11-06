#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""socks5udp.py — 零依赖 SOCKS5 服务（CONNECT + UDP ASSOCIATE），给 UU 远程这类 UDP 应用做跳板出口。
    python3 socks5udp.py [port] [bind_host] [--token T] [--user ccs] [--allow a,b] [--timeout S] [--advertise IP]
客户端：sing-box / v2ray 的 SOCKS5 出站（TCP + UDP），TUN 模式接管整机流量。"""
import argparse
import os
import select
import signal
import socket
import struct
import sys
import threading
import time

BUF = 65536
LS = None
SHUTDOWN = False
LOCK = threading.Lock()
NOW = 0


REAL_LOG = "/tmp/ccsproxy.real.log"  # 与 HTTP 代理共用一份私有记录，0600 仅本用户可读


import users as usertable


def log(tag, msg):
    print("[%s] %s" % (tag, msg), flush=True)  # 只打主机/端口/客户端 IP，不打凭据


def note_real(real, shown, kind):
    """--mask-ip 生效时把真实 IP 追加进 0600 私有文件，只有本用户能排查。"""
    try:
        fd = os.open(REAL_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.write(fd, ("%s\t%s [%s] <- real %s\n" % (time.strftime("%F %T"), shown, kind, real)).encode())
        os.close(fd)
    except OSError:
        pass


def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        d = sock.recv(n - len(buf))
        if not d:
            return None
        buf += d
    return buf


def local_ip():
    """取本机对外通信用的源地址，作为回给客户端的 UDP relay 地址。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def host_allowed(host, allow):
    """白名单：精确或子域后缀匹配；空表放行全部。"""
    if not allow:
        return True
    host = host.lower().rstrip(".")
    return any(host == d or host.endswith("." + d)
               for d in (x.lower().strip().rstrip(".") for x in allow) if d)


def handshake(conn, cfg):
    """SOCKS5 握手。成功返回用户名（无认证时为 "anon"），失败返回 None。"""
    head = recv_exact(conn, 2)
    if not head or head[0] != 5:
        return None
    methods = recv_exact(conn, head[1])
    if methods is None:
        return None
    auth_on = bool(cfg.token or cfg.users)
    if not auth_on:  # 完全不认证
        if 0 not in methods:
            conn.sendall(b"\x05\xff")
            return None
        conn.sendall(b"\x05\x00")
        return "anon"
    if 2 not in methods:
        # 有些客户端不实现 RFC1929，只 offer 0x00。
        # 开 --relax-auth 时按无认证放行，目标仍受 --allow 白名单约束。
        if cfg.relax_auth and 0 in methods:
            log("WARN", "client 不支持 RFC1929，按无认证放行（--relax-auth）")
            conn.sendall(b"\x05\x00")
            return "relax"
        conn.sendall(b"\x05\xff")
        return None
    conn.sendall(b"\x05\x02")
    if recv_exact(conn, 1) != b"\x01":
        return None
    n = recv_exact(conn, 1)
    user = recv_exact(conn, n[0]) if n else None
    n = recv_exact(conn, 1)
    pwd = recv_exact(conn, n[0]) if n else None
    if user is None or pwd is None:
        return None
    who = usertable.check_pair(user.decode("utf-8", "replace"),
                               pwd.decode("utf-8", "replace"),
                               cfg.token, cfg.users)
    if who:
        conn.sendall(b"\x01\x00")
        return who
    conn.sendall(b"\x01\x01")
    return None


def read_request(conn):
    """读 SOCKS5 请求：返回 (cmd, host, port)。"""
    hdr = recv_exact(conn, 4)
    if not hdr or hdr[0] != 5:
        return None
    cmd, atyp = hdr[1], hdr[3]
    if atyp == 1:
        d = recv_exact(conn, 4)
        if d is None:
            return None
        host = socket.inet_ntoa(d)
    elif atyp == 3:
        n = recv_exact(conn, 1)
        if n is None:
            return None
        d = recv_exact(conn, n[0])
        if d is None:
            return None
        host = d.decode("utf-8", "replace")
    elif atyp == 4:
        d = recv_exact(conn, 16)
        if d is None:
            return None
        host = socket.inet_ntop(socket.AF_INET6, d)
    else:
        return None
    p = recv_exact(conn, 2)
    if p is None:
        return None
    return cmd, host, struct.unpack("!H", p)[0]


def reply(conn, code, addr="0.0.0.0", port=0):
    try:
        conn.sendall(b"\x05" + bytes([code]) + b"\x00\x01"
                     + socket.inet_aton(addr) + struct.pack("!H", port))
    except OSError:
        pass


def connect_upstream(host, port, cfg):
    """建上游 TCP；IPv4 优先，容器里不可达的 IPv6 不拖死请求。"""
    infos = sorted(socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM),
                   key=lambda x: 0 if x[0] == socket.AF_INET else 1)
    last = None
    for fam, stype, proto, _, sa in infos:
        s = socket.socket(fam, stype, proto)
        s.settimeout(min(float(cfg.timeout), 8.0))
        try:
            s.connect(sa)
            s.settimeout(cfg.timeout)
            return s
        except OSError as e:
            last = e
            s.close()
    raise last or OSError("connect failed")


def pipe(src, dst):
    try:
        while True:
            chunk = src.recv(BUF)
            if not chunk:
                break
            dst.sendall(chunk)
    except (BrokenPipeError, ConnectionResetError, socket.timeout, OSError):
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def pump(a, b):
    t1 = threading.Thread(target=pipe, args=(a, b), daemon=True)
    t2 = threading.Thread(target=pipe, args=(b, a), daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()


def parse_udp(data):
    """SOCKS5 UDP 头：RSV(2) FRAG(1) ATYP(1) ADDR PORT DATA；只支持 FRAG=0。"""
    if len(data) < 4 or data[0] or data[1] or data[2]:
        return None
    atyp = data[3]
    i = 4
    if atyp == 1:
        if len(data) < i + 6:
            return None
        host = socket.inet_ntoa(data[i:i + 4])
        i += 4
    elif atyp == 3:
        n = data[i]
        i += 1
        if len(data) < i + n + 2:
            return None
        host = data[i:i + n].decode("utf-8", "replace")
        i += n
    elif atyp == 4:
        if len(data) < i + 18:
            return None
        host = socket.inet_ntop(socket.AF_INET6, data[i:i + 16])
        i += 16
    else:
        return None
    port = struct.unpack("!H", data[i:i + 2])[0]
    return host, port, data[i + 2:]


def build_udp(host, port, payload):
    try:
        return b"\x00\x00\x00\x01" + socket.inet_aton(host) + struct.pack("!H", port) + payload
    except OSError:
        return None


def resolve(host, port):
    try:
        infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_DGRAM)
        return infos[0][4] if infos else None
    except OSError:
        return None


def udp_loop(udp, conn, cfg):
    """UDP ASSOCIATE：客户端包 → 解封装发目标；目标回包 → 封装发回客户端。"""
    client = None
    peers = set()
    while not SHUTDOWN:
        try:
            r, _, _ = select.select([udp, conn], [], [], None)
        except OSError:
            break
        if conn in r:  # 控制连接关闭 = 客户端结束 UDP 关联
            try:
                if not conn.recv(1):
                    break
            except OSError:
                break
        if udp in r:
            try:
                data, addr = udp.recvfrom(BUF)
            except OSError:
                break
            if client is None:
                client = addr  # 第一个包的来源就是客户端
            if addr == client:
                p = parse_udp(data)
                if not p:
                    continue
                host, port, payload = p
                if not host_allowed(host, cfg.allow):
                    continue
                sa = resolve(host, port)
                if not sa:
                    continue
                peers.add(sa)
                try:
                    udp.sendto(payload, sa)
                except OSError:
                    continue
            elif addr in peers:  # 目标回包
                out = build_udp(addr[0], addr[1], data)
                if out:
                    try:
                        udp.sendto(out, client)
                    except OSError:
                        break


def do_udp(conn, ip, cfg):
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        udp.bind((cfg.udp_bind, 0))
    except OSError as e:
        reply(conn, 1)
        log("ERR", "udp bind failed: %s" % e)
        udp.close()
        return
    adv = cfg.advertise or local_ip()
    port = udp.getsockname()[1]
    reply(conn, 0, adv, port)
    log("UDP", "associate %s:%d <- %s" % (adv, port, ip))
    try:
        udp_loop(udp, conn, cfg)
    finally:
        udp.close()


def handle(conn, addr, cfg):
    global NOW
    real = addr[0] if addr else "-"
    ip = cfg.mask_ip or real      # 日志对外展示的地址
    if cfg.mask_ip:
        note_real(real, ip, "socks5")
    try:
        conn.settimeout(cfg.timeout)
        who = handshake(conn, cfg)
        if who is None:
            log("ERR", "handshake failed <- %s" % ip)
            return
        tag = "" if who in ("anon", "relax") else " [%s]" % who
        req = read_request(conn)
        if not req:
            reply(conn, 1)
            log("ERR", "bad request <- %s" % ip)
            return
        cmd, host, port = req
        if cmd == 1:
            if not host_allowed(host, cfg.allow):
                reply(conn, 2)
                log("ERR", "denied %s <- %s" % (host, ip))
                return
            try:
                up = connect_upstream(host, port, cfg)
            except OSError as e:
                reply(conn, 5)
                log("ERR", "502 %s:%d %s <- %s" % (host, port, e, ip))
                return
            log("CONNECT", "%s:%d <- %s%s" % (host, port, ip, tag))
            reply(conn, 0, up.getsockname()[0], up.getsockname()[1])
            try:
                pump(conn, up)
            finally:
                up.close()
        elif cmd == 3:
            do_udp(conn, ip, cfg)   # DST 通常是 0.0.0.0，白名单在 udp_loop 里按每个包的真实目标校验
        else:
            reply(conn, 7)
            log("ERR", "unsupported cmd %d <- %s" % (cmd, ip))
    except (BrokenPipeError, ConnectionResetError, socket.timeout, OSError):
        pass
    except Exception as e:
        log("ERR", "unexpected %s <- %s" % (e.__class__.__name__, ip))
    finally:
        try:
            conn.close()
        except OSError:
            pass
        with LOCK:
            NOW -= 1


def serve(cfg):
    global LS, NOW
    ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ls.bind((cfg.bind, cfg.port))
    ls.listen(128)
    ls.settimeout(0.5)  # accept 带超时，信号到达后及时退出
    LS = ls
    log("INFO", "socks5 listening %s:%d token=%s udp-advertise=%s mask=%s"
        % (cfg.bind, cfg.port, "on(%d)" % (1 + len(usertable.load(cfg.users))) if (cfg.token or cfg.users) else "off", cfg.advertise or "auto",
           cfg.mask_ip or "off"))
    while not SHUTDOWN:
        try:
            conn, addr = ls.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        with LOCK:
            over = NOW >= cfg.max_conn
            if not over:
                NOW += 1
        if over:
            reply(conn, 5)
            try:
                conn.close()
            except OSError:
                pass
            log("ERR", "max-conn reached <- %s" % (addr[0] if addr else "-"))
            continue
        threading.Thread(target=handle, args=(conn, addr, cfg), daemon=True).start()


def on_signal(signum, frame):
    """只置退出标志；Linux 上 close 唤不醒阻塞的 accept，靠 accept 超时轮询。"""
    global SHUTDOWN
    SHUTDOWN = True


def parse_args(argv):
    ap = argparse.ArgumentParser(prog="socks5udp.py",
                                 description="零依赖 SOCKS5（CONNECT + UDP ASSOCIATE），给 UDP 应用做出网出口。")
    ap.add_argument("port", nargs="?", type=int, default=18082, help="监听端口，默认 18082")
    ap.add_argument("bind", nargs="?", default="0.0.0.0", help="监听地址，默认 0.0.0.0")
    ap.add_argument("--token", default="", help="用户名/密码认证的密码部分（空=不认证）")
    ap.add_argument("--users", default="", help="多用户令牌表 users.json；与 --token 可并存")
    ap.add_argument("--user", default="ccs", help="认证用户名，默认 ccs")
    ap.add_argument("--allow", default="", help="目标白名单，逗号分隔（后缀匹配），空=不限")
    ap.add_argument("--max-conn", type=int, default=200, dest="max_conn", help="并发上限，默认 200")
    ap.add_argument("--timeout", type=float, default=30.0, help="TCP 超时秒，默认 30")
    ap.add_argument("--udp-bind", default="0.0.0.0", dest="udp_bind", help="UDP relay 绑定地址")
    ap.add_argument("--advertise", default="", help="回给客户端的 UDP relay 地址，默认自动探测")
    ap.add_argument("--mask-ip", default="", dest="mask_ip", help="日志显示用这个 IP，真实 IP 记到 " + REAL_LOG)
    ap.add_argument("--relax-auth", action="store_true", dest="relax_auth",
                    help="客户端不支持用户名/密码认证时按无认证放行（目标仍受 --allow 约束）")
    cfg = ap.parse_args(argv)
    cfg.allow = [x for x in cfg.allow.split(",") if x.strip()]
    if not 0 < cfg.port < 65536:
        ap.error("port 必须在 1-65535")
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
    print("socks5 bye", flush=True)


if __name__ == "__main__":
    sys.exit(main())
