# 代理全家桶：HTTP + SOCKS5(UDP) + GitHub SSH 中转 + Web 管理面板
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    PANEL_PORT=8080 \
    HTTP_PORT=18081 \
    SOCKS_PORT=18082 \
    GH_RELAY_PORT=2222 \
    BIND=0.0.0.0 \
    PANEL_USER=admin \
    PANEL_PASS=changeme

WORKDIR /app

# 纯标准库，无 requirements
COPY ccsproxy.py socks5udp.py users.py panel.py gh_relay_worker.py start.sh /app/
COPY web /app/web

RUN mkdir -p /data/logs && chown -R nobody:nogroup /app /data

USER nobody

VOLUME ["/data"]
EXPOSE 8080 18081 18082 2222

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import socket,sys; s=socket.create_connection(('127.0.0.1',8080),3); s.close()" || exit 1

ENTRYPOINT ["python3", "/app/panel.py"]
