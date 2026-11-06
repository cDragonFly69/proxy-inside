#!/usr/bin/env python3
"""gh_relay_worker.py — 通用 TCP 中转（面板里的 "GitHub SSH 中转" 用它）。

    python3 gh_relay_worker.py [listen_port] [bind_host]

上游由环境变量指定，默认 github.com:22：
    GH_RELAY_UPSTREAM=github.com:22

为什么需要它：受管控的出口对 443 做 SNI 阻断（TLS 被 RST），对 22 也会
掐掉 SSH 握手。但跳板机到 github.com:22 是通的，于是把跳板机当 SSH 跳板。
"""

import os
import socket
import sys
import threading

UPSTREAM_RAW = os.environ.get("GH_RELAY_UPSTREAM", "github.com:22")


def split_hostport(text, default_port=22):
    text = text.strip()
    if text.startswith("["):            # [::1]:22
        host, _, rest = text[1].partition("]")
        port = rest.lstrip(":") or default_port
        return host, int(port)
    if text.count(":") == 1:
        host, port = text.rsplit(":", 1)
        return host, int(port)
    return text, default_port


def pipe(a, b):
    try:
        while True:
            chunk = a.recv(65536)
            if not chunk:
                break
            b.sendall(chunk)
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


def handle(client, upstream):
    try:
        remote = socket.create_connection(upstream, 15)
    except Exception as e:
        print("upstream fail %s:%d %s" % (upstream[0], upstream[1], e), flush=True)
        try:
            client.close()
        except Exception:
            pass
        return
    threading.Thread(target=pipe, args=(client, remote), daemon=True).start()
    threading.Thread(target=pipe, args=(remote, client), daemon=True).start()


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 2222
    bind = sys.argv[2] if len(sys.argv) > 2 else "0.0.0.0"
    upstream = split_hostport(UPSTREAM_RAW)

    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((bind, port))
    srv.listen(100)
    print("relay %s:%d -> %s:%d" % (bind, port, upstream[0], upstream[1]), flush=True)

    while True:
        try:
            client, _ = srv.accept()
        except OSError:
            break
        threading.Thread(target=handle, args=(client, upstream), daemon=True).start()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
