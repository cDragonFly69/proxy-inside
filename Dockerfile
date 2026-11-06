# ccsproxy —— 单文件零依赖 HTTP 正向代理，容器化运行
# 构建: docker build -t ccsproxy:1.0 .
# 运行: docker run -d --name ccsproxy -p 18081:18081 -e TOKEN=xxx ccsproxy:1.0
FROM python:3.12-slim

# 不缓冲 stdout，docker logs 才能实时看到 [CONNECT]/[HTTP]/[ERR]
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=18081 \
    BIND=0.0.0.0 \
    MAX_CONN=200 \
    TIMEOUT=30

WORKDIR /app
COPY ccsproxy.py start.sh /app/

# 非 root 运行：端口 18081 > 1024，无需特权
RUN useradd --system --uid 10001 --no-create-home ccsproxy \
 && chmod 0755 /app/start.sh /app/ccsproxy.py \
 && chown -R ccsproxy:ccsproxy /app
USER ccsproxy

EXPOSE 18081

# 健康检查只探本机端口是否在听（不探外网，避免出网抖动把容器判死）
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python3 -c "import os,socket;s=socket.socket();s.settimeout(3);s.connect(('127.0.0.1',int(os.environ.get('PORT','18081'))));s.close()"

ENTRYPOINT ["/app/start.sh"]
