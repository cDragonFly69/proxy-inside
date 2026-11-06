# ccsproxy

内网跳板机出网工具，两个零依赖 Python 服务，一条 `sh start.sh` 全起：

| 文件 | 作用 | 协议 | 端口 |
|---|---|---|---|
| `ccsproxy.py` | HTTP 正向代理 | TCP（CONNECT 隧道 + 普通 HTTP） | 18081 |
| `socks5udp.py` | SOCKS5 服务 | TCP + **UDP ASSOCIATE** | 18082 |

只依赖 Python 3.8+ 标准库（socket / threading / select / argparse / signal），不需要 pip、不需要 root（端口 > 1024）。

- 下文件、`curl`/`git`/浏览器 → 用 18081 的 HTTP 代理。
- 要跑 **UU 远程**这类 UDP 应用 → 用 18082 的 SOCKS5，Windows 端配 sing-box TUN（见下文）。

## 快速开始

```bash
# 裸机（推荐：UDP 场景只有这种方式最省心）
sh start.sh
# 带令牌和白名单
TOKEN=s3cret ALLOW=github.com,163.com sh start.sh

# 容器
docker build -t ccsproxy:1.1 .
docker run -d --name ccsproxy -p 18081:18081 -p 18082:18082 -e TOKEN=s3cret -e ALLOW=github.com,163.com ccsproxy:1.1
```

日志：

```
[INFO] start.sh http=18081 socks=18082 bind=0.0.0.0 allow=ALL token=on ...
[INFO] listening 0.0.0.0:18081 allow=ALL token=on max-conn=200 timeout=30.0s srcip=auto
[INFO] socks5 listening 0.0.0.0:18082 token=on udp-advertise=auto
[CONNECT] api.github.com:443 <- 10.8.35.107
[UDP] associate 10.200.1.26:52341 <- 10.8.35.107
```

收工：`Ctrl+C`，或 `kill $(cat /tmp/ccsproxy.pid)`，两个端口一起释放。

## HTTP 代理（18081）

```bash
curl -x http://JUMP:18081 https://api.github.com/zen
curl -x http://JUMP:18081 -L -o out.bin https://github.com/...
# 开令牌时：curl 的 -H 不会进 CONNECT 请求，必须用 --proxy-header
curl -x http://JUMP:18081 --proxy-header 'Proxy-Authorization: Bearer s3cret' https://api.github.com/zen
```

Windows 上还要加 `--ssl-no-revoke`：本机无外网时 schannel 查不到证书吊销列表，会报
`curl: (35) schannel: CRYPT_E_REVOCATION_OFFLINE`。

```powershell
curl.exe --ssl-no-revoke -L -x http://10.200.1.26:18081 -o out.msi https://github.com/...
```

## SOCKS5 + UDP（18082）

服务端支持标准 SOCKS5 的 `CONNECT` 与 `UDP ASSOCIATE`，`--token` 时走 RFC1929 用户名/密码认证（用户名默认 `ccs`）。

**Windows 客户端：sing-box TUN**

> 服务器只是出口。Windows 上不装 TUN 客户端，UU 远程不会自动走这个代理——它不读系统代理，也没有代理设置项。

整个部署只有两个脚本，各跑一次：

### ① 跳板机上跑 `make-bundle.sh`

```bash
cd ~/proxy-inside
sh make-bundle.sh
```

它自动做完：查 sing-box 最新版 → 多镜像下载 + 完整性校验 → 下载 `wintun.dll` →
按 `SERVER/SOCKS_PORT/SOCKS_USER/TOKEN` 生成 `config.json` → 打包成
`client/client-bundle.tgz` → 在 **18090** 起分发服务。

输出最后会告诉你 Windows 上该怎么跑。常用环境变量（都有默认值）：

```bash
SERVER=10.200.1.26 SOCKS_PORT=18082 TOKEN=s3cret HTTP_PORT=18090 sh make-bundle.sh
```

想强制重下客户端二进制：`sh make-bundle.sh --refresh`。

### ② Windows 上双击 `start-client.bat`

`client/start-client.bat` 自动请求管理员权限并启动，不需要敲任何命令。
令牌和跳板机地址写在 bat 开头两行，改那里即可。

或者手动跑（等价）：

```powershell
.\run-tun.ps1 -Check -Password s3cret   # 只诊断
.\run-tun.ps1 -Password s3cret          # 诊断 + 启动
```

`run-tun.ps1` 发现缺 `sing-box.exe` / `wintun.dll` 时，会从跳板机 18090 拉
`client-bundle.tgz` 解包（不再访问 github.com），然后起 TUN。
起来后整机流量（含 UDP）走跳板机，UU 远程、浏览器、git 全都通。

**TUN 客户端的 DNS 必须走系统内网 DNS**：配置里用 `{"type": "local"}`。
实测经 SOCKS5 转发到 `8.8.8.8:53` 的查询在被管控的出口上全部 `context deadline
exceeded`，表现是所有域名解析失败、`sing-box` 日志刷满 `dns: exchange failed`。

**UDP relay 地址必须对**：跳板机有多网卡（docker0 之类）时自动探测可能选错，导致 TCP 通、UDP 不通。
启动跳板机时显式指定：

```bash
SOCKS_ADVERTISE=10.200.1.26 MASK_IP=10.8.34.61 TOKEN=s3cret sh start.sh
```

验证出口：

```powershell
curl.exe --ssl-no-revoke https://ifconfig.me
```

应该回显跳板机的出口 IP。

## GitHub SSH 中转（出口被管控时）

内网出口常按域名白名单放行：`api.github.com:443` 通，但 `github.com:443` 的 TLS 会被
RST（`SSL routines::unexpected eof`），`github.com:22` 的 SSH 握手也会被掐
（`Connection closed by 20.205.243.166 port 22`）。网页上传、`git push https://`、
`git clone` 全都走不通。

跳板机到 `github.com:22` 是通的，所以在**跳板机**上起一个 TCP 中转：

```bash
sh gh-relay.sh -d      # 后台常驻，监听 0.0.0.0:2222
sh gh-relay.sh -k      # 停止
sh gh-relay.sh         # 前台调试，Ctrl+C 停
```

内网机器上改用中转地址：

```bash
git remote set-url origin ssh://git@JUMP_HOST:2222/OWNER/REPO.git
ssh -o StrictHostKeyChecking=accept-new -p 2222 git@JUMP_HOST   # 应回 Hi <用户名>!
git push -u origin HEAD
```

环境变量：`GH_RELAY_PORT`（默认 2222）、`GH_RELAY_BIND`（默认 `0.0.0.0`）、
`GH_RELAY_UPSTREAM`（默认 `github.com:22`）。

已验证输出：`Hi cDragonFly69! You've successfully authenticated, but GitHub does not
provide shell access.`

> SSH 不是 TLS，没有 SNI 可拦，所以这条路能过；HTTPS 那条不行，不用再试。

## 参数

### ccsproxy.py（HTTP）

| 参数 | 默认 | 说明 |
|---|---|---|
| `port` | 18081 | 监听端口 |
| `bind_host` | 0.0.0.0 | 监听地址 |
| `--allow a,b` | 不限 | 目标白名单，后缀匹配（`163.com` 覆盖 `*.163.com`） |
| `--token T` | 空 | 要求 `Proxy-Authorization: Bearer T`，缺失回 407 |
| `--max-conn N` | 200 | 并发上限，超限回 503 |
| `--timeout S` | 30 | 连接/读写超时秒 |
| `--srcip IP` | 系统选路 | 出站源地址绑定（多网卡选出口 IP） |
| `--mask-ip IP` | 空 | 日志与对外记录里把客户端 IP 显示成这个地址，真实 IP 写进私有文件 |

### socks5udp.py（SOCKS5）

| 参数 | 默认 | 说明 |
|---|---|---|
| `port` | 18082 | 监听端口 |
| `bind_host` | 0.0.0.0 | 监听地址 |
| `--token T` | 空 | 用户名/密码认证的密码，空=不认证 |
| `--user U` | ccs | 认证用户名 |
| `--allow a,b` | 不限 | 目标白名单，UDP 按每个包的真实目标校验 |
| `--max-conn N` | 200 | TCP 并发上限 |
| `--timeout S` | 30 | TCP 超时秒 |
| `--udp-bind IP` | 0.0.0.0 | UDP relay 绑定地址 |
| `--advertise IP` | 自动探测 | 回给客户端的 UDP relay 地址 |
| `--mask-ip IP` | 空 | 同 HTTP 代理，日志显示用伪装地址 |

### start.sh 环境变量

`PORT` `SOCKS_PORT` `ENABLE_SOCKS` `BIND` `ALLOW` `TOKEN` `TOKEN_FILE` `REQUIRE_TOKEN` `MAX_CONN` `TIMEOUT` `SRCIP` `SOCKS_USER` `SOCKS_ADVERTISE` `MASK_IP`

## 容器里的 UDP 限制

SOCKS5 的 UDP relay 用**随机端口**，bridge 网络下没有对应的 `-p` 映射，容器外的客户端连不上。所以：

- 只要 HTTP 下载 → 用上面那条 `docker run` 就行；
- 要用 UU 远程 / UDP → 裸机 `sh start.sh`，或者容器改成 host 网络：

```yaml
services:
  ccsproxy:
    network_mode: host      # Linux 宿主；UDP relay 直接绑定宿主地址
```

## 客户端 IP 伪装（MASK_IP / --mask-ip）

```bash
MASK_IP=10.8.34.61 TOKEN=s3cret sh start.sh
```

效果：两个服务的所有日志、`[CONNECT]`/`[HTTP]`/`[UDP]` 记录里的客户端地址都显示 `10.8.34.61`，
任何看 stdout、`docker logs`、日志文件的人都只会看到这个地址。

真实 IP 同时被追加到 `/tmp/ccsproxy.real.log`，权限 `0600`（只有启动代理的那个用户能读）：

```
2026-11-06 15:05:52	10.8.34.61 <- real 10.8.35.107
2026-11-06 15:05:52	10.8.34.61 [socks5] <- real 10.8.35.107
```

自己排查：

```bash
tail -f /tmp/ccsproxy.real.log       # 唯一起点
stat -c '%a %U' /tmp/ccsproxy.real.log   # 600 ccspt
```

**边界（必须知道）：**

- 只影响**代理自己写出来的记录**。`ss -tnp`、`netstat`、交换机 MAC 表、防火墙连接表看到的仍然是真实 IP，
  那一层由内核和网络设备决定，任何应用层代码都改不了。
- 伪装地址必须是你确认过的、不会引起冲突的地址；如果 `10.8.34.61` 真机在线，网络层仍以真实 IP 为准，
  只有日志层面被替换。
- 日志文件默认路径固定为 `/tmp/ccsproxy.real.log`，容器里也在 `/tmp`，容器重建即丢。

## 行为说明

- CONNECT / SOCKS5 CONNECT：建上游 TCP → 应答 → 双向透传，任一端 EOF 后 `shutdown(SHUT_WR)` 半关闭，反向读干净再关。
- 普通 HTTP：绝对 URI 改写为 origin-form，剔除 `Proxy-Connection`/`Proxy-Authorization`/`Connection`/`Keep-Alive` 后转发。
- 上游连接 IPv4 优先，单地址 8s 上限，连上后恢复 `--timeout` 作为读写超时。
- 错误码：400 请求非法、403 白名单外、407 缺令牌、502 上游失败、503 超并发。
- 日志只打 `[CONNECT]`/`[HTTP]`/`[UDP]`/`[ERR]` 与主机端口，不打印头部、body、Cookie、令牌。
- SIGINT/SIGTERM 关闭监听并打印 `bye` / `socks5 bye`，端口立即释放。

## 已知边界

- HTTP 代理只能搬 TCP；UDP 必须走 SOCKS5 + TUN 客户端。
- 不做 TLS 中间人、不解析 HTTP/2、不缓存、不落盘。
- sing-box TUN 需要 Windows 管理员权限；没有管理员权限就无法建立 TUN 网卡，UDP 无解。
- UU 远程对 VPN 环境可能有检测，连上后画质/稳定性取决于它的策略，不是代理能控制的。
