# ccsproxy

单文件零依赖 HTTP 正向代理（CONNECT 隧道 + 普通 HTTP 转发），用于内网跳板机出网下载。仅需 Python 3.8+ 标准库。

## 快速开始

```bash
# 裸机 / Docker 均可
python3 ccsproxy.py 18081 0.0.0.0 --allow github.com,163.com --token s3cret

# 容器
docker build -t ccsproxy:1.0 .
docker run -d --name ccsproxy -p 18081:18081 -e TOKEN=s3cret -e ALLOW=github.com,163.com ccsproxy:1.0
```

客户端（注意令牌必须走 `--proxy-header`，`-H` 不进 CONNECT 请求）：

```bash
curl -x http://JUMP:18081 --proxy-header 'Proxy-Authorization: Bearer s3cret' https://api.github.com/zen
curl -x http://JUMP:18081 --proxy-header 'Proxy-Authorization: Bearer s3cret' -L -o out.bin https://github.com/...
```

## 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `port` | 18081 | 监听端口 |
| `bind_host` | 0.0.0.0 | 监听地址 |
| `--allow a,b` | 不限 | 目标白名单，后缀匹配（`163.com` 覆盖 `*.163.com`） |
| `--token T` | 空 | 要求 `Proxy-Authorization: Bearer T`，缺失回 407 |
| `--max-conn N` | 200 | 并发上限，超限回 503 |
| `--timeout S` | 30 | 连接/读写超时秒 |
| `--srcip IP` | 系统选路 | 出站源地址绑定（多网卡选出口 IP） |

## 行为

- CONNECT：建上游 TCP → `200 Connection established` → 双向透传，任一端 EOF 后 `shutdown(SHUT_WR)` 半关闭，反向读干净再关。
- 普通 HTTP：绝对 URI 改写为 origin-form，剔除 `Proxy-Connection`/`Proxy-Authorization`/`Connection`/`Keep-Alive` 后转发。
- 错误码：请求行非法 400、白名单外 403、缺令牌 407、上游失败 502、超并发 503。
- 日志只打 `[CONNECT]`/`[HTTP]`/`[ERR]` 与主机端口，不打印头部、body、Cookie、令牌。
- SIGINT/SIGTERM 关监听 socket 并打印 `bye`，端口立即释放。

## 容器

`start.sh` 是 POSIX sh 脚本（`sh start.sh` / `./start.sh` 都行，不依赖 bash），用环境变量驱动：`PORT` `BIND` `ALLOW` `TOKEN` `TOKEN_FILE` `REQUIRE_TOKEN` `MAX_CONN` `TIMEOUT` `SRCIP`，最后 exec 到 python，PID 1 直接接收 `docker stop` 的 SIGTERM。

注意：容器内 `--srcip` 一般无意义（出口 IP 由 Docker NAT 决定），需要固定出口请用 `--network host`。

## 已知边界

不做 TLS 中间人、不解析 HTTP/2、不缓存、不落盘。UU 远程这类走 UDP 打洞/中继的客户端不受 HTTP 代理覆盖，那类需求请改用 SOCKS5。
