<#
  run-tun.ps1 —— Windows 端一键把整机流量（含 UDP）导到跳板机，UU 远程等应用即可用。

  用法（必须管理员 PowerShell）：
    .\run-tun.ps1 -Password s3cret              # 诊断 + 下载 + 配置 + 启动
    .\run-tun.ps1 -Check                        # 只诊断，不动系统
    .\run-tun.ps1 -Server 10.200.1.26 -Password s3cret

  前提：跳板机上 `MASK_IP=... TOKEN=s3cret sh start.sh` 已经在跑（18081 + 18082）。
#>
param(
  [string]$Server    = "10.200.1.26",
  [int]   $HttpPort  = 18081,
  [int]   $SocksPort = 18082,
  [string]$User      = "ccs",
  [string]$Password  = "",
  [switch]$Check
)

$ErrorActionPreference = "Stop"
$dir     = Split-Path -Parent $MyInvocation.MyCommand.Path
$exe     = Join-Path $dir "sing-box.exe"
$cfgPath = Join-Path $dir "config.json"
$tplPath = Join-Path $dir "sing-box.json"
$proxy   = "http://${Server}:${HttpPort}"

function Line($t, $c = "Gray") { Write-Host $t -ForegroundColor $c }

function Test-Tcp([string]$h, [int]$p) {
  $c = New-Object Net.Sockets.TcpClient
  try {
    $iar = $c.BeginConnect($h, $p, $null, $null)
    if (-not $iar.AsyncWaitHandle.WaitOne(3000)) { return $false }
    $c.EndConnect($iar); return $true
  } catch { return $false } finally { $c.Close() }
}

function Test-HttpProxy() {
  $a = @("--ssl-no-revoke", "-sS", "-m", "20", "-x", $proxy, "-o", "NUL", "-w", "%{http_code}",
         "https://api.github.com/zen")
  if ($Password) { $a = @("--ssl-no-revoke", "-sS", "-m", "20", "-x", $proxy,
                          "--proxy-header", "Proxy-Authorization: Bearer $Password",
                          "-o", "NUL", "-w", "%{http_code}", "https://api.github.com/zen") }
  $code = & curl.exe @a 2>$null
  return $code
}

# ---------------- 诊断 ----------------
Line "== 1. 管理员权限 =="
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
  [Security.Principal.WindowsBuiltInRole]::Administrator)
if ($isAdmin) { Line "  [OK] 管理员" "Green" } else { Line "  [X] 不是管理员，TUN 建不起来。右键 PowerShell -> 以管理员身份运行" "Red" }

Line "== 2. 跳板机端口 =="
foreach ($pair in @(@($HttpPort, "HTTP 代理"), @($SocksPort, "SOCKS5"))) {
  $ok = Test-Tcp $Server $pair[0]
  if ($ok) { Line ("  [OK] {0}:{1} ({2})" -f $Server, $pair[0], $pair[1]) "Green" }
  else     { Line ("  [X]  {0}:{1} ({2}) 连不上：跳板机没起服务、防火墙没放行" -f $Server, $pair[0], $pair[1]) "Red" }
}

Line "== 3. HTTP 代理能否出网 =="
$code = Test-HttpProxy
if ($code -match "^(200|403)") { Line "  [OK] 代理响应 $code" "Green" }
elseif ($code -eq "407") { Line "  [X] 407 令牌不对：-Password 要和跳板机 TOKEN 一致" "Red" }
else { Line "  [X] 代理没通（返回 '$code'）：先解决第 2 步" "Red" }

Line "== 4. 客户端组件 =="
Line ("  sing-box.exe : " + $(if (Test-Path $exe) { "已存在" } else { "缺失，启动时自动下载" }))
Line ("  wintun.dll   : " + $(if (Test-Path (Join-Path $dir "wintun.dll")) { "已存在" } else { "缺失，启动时自动下载" }))

Line "== 5. 当前是否已有 TUN =="
$tun = Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object { $_.InterfaceDescription -match "sing|wintun|tun" }
if ($tun) { $tun | Format-Table Name, InterfaceDescription, Status -AutoSize | Out-String | Write-Host }
else { Line "  [--] 没有 TUN 网卡（未运行）" }

if ($Check) {
  Line "`n诊断结束（-Check 模式，未做任何改动）。" "Cyan"
  exit 0
}

if (-not $isAdmin) { exit 1 }
if ([string]::IsNullOrWhiteSpace($Password)) {
  Line "`n[!] 缺少 -Password（跳板机启动时 TOKEN 的值）" "Red"; exit 1
}
if (-not (Test-Path $tplPath)) {
  Line "`n[!] 找不到 sing-box.json 模板，请与脚本放同一目录" "Red"; exit 1
}

function Fetch-File([string]$url, [string]$out) {
  curl.exe --ssl-no-revoke -L --retry 3 -x $proxy -o $out $url
  if ($LASTEXITCODE -ne 0 -or -not (Test-Path $out)) { throw "下载失败：$url" }
}

# ---------------- 准备 sing-box（锁 1.11.x，配置格式匹配）----------------
if (-not (Test-Path $exe)) {
  Line "`n== 下载 sing-box =="
  $rels = curl.exe --ssl-no-revoke -s -x $proxy "https://api.github.com/repos/SagerNet/sing-box/releases?per_page=100" | ConvertFrom-Json
  $rel = $rels | Where-Object { $_.tag_name -like "v1.11.*" } | Select-Object -First 1
  if (-not $rel) { $rel = $rels | Select-Object -First 1 }
  $asset = $rel.assets | Where-Object { $_.name -like "*windows-amd64.zip" } | Select-Object -First 1
  if (-not $asset) { throw "没有 windows-amd64 包" }
  Line ("  版本 " + $rel.tag_name)
  $zip = Join-Path $dir "sing-box.zip"
  Fetch-File $asset.browser_download_url $zip
  Expand-Archive -Path $zip -DestinationPath $dir -Force
  $found = Get-ChildItem $dir -Recurse -Filter "sing-box.exe" | Where-Object { $_.FullName -ne $exe } | Select-Object -First 1
  if ($found) { Copy-Item $found.FullName $exe -Force }
  Remove-Item $zip -Force -ErrorAction SilentlyContinue
  if (-not (Test-Path $exe)) { throw "解压后没找到 sing-box.exe" }
  Line "  [OK] sing-box 就绪" "Green"
}

# ---------------- 准备 wintun ----------------
$wintun = Join-Path $dir "wintun.dll"
if (-not (Test-Path $wintun)) {
  Line "`n== 下载 wintun =="
  try {
    $wz = Join-Path $dir "wintun.zip"
    Fetch-File "https://www.wintun.net/builds/wintun-0.14.1.zip" $wz
    Expand-Archive -Path $wz -DestinationPath (Join-Path $dir "wintun") -Force
    $dll = Get-ChildItem (Join-Path $dir "wintun") -Recurse -Filter "wintun.dll" |
           Where-Object { $_.FullName -match "amd64" } | Select-Object -First 1
    if ($dll) { Copy-Item $dll.FullName $wintun -Force }
    Remove-Item $wz -Force -ErrorAction SilentlyContinue
    Line "  [OK] wintun.dll 就绪" "Green"
  } catch {
    Line "  [!] wintun 自动下载失败，请手动把 amd64 版 wintun.dll 放到 $dir" "Yellow"
  }
}

# ---------------- 生成配置并启动 ----------------
$tpl  = Get-Content $tplPath -Raw -Encoding UTF8
$conf = $tpl.Replace("__SERVER__", $Server).Replace("__PORT__", "$SocksPort").
             Replace("__USER__", $User).Replace("__PASS__", $Password)
Set-Content -Path $cfgPath -Value $conf -Encoding UTF8
Line "`n== 启动 TUN =="
Line "  服务器 $Server`:$SocksPort  user=$User" "Green"
Line "  关闭窗口或 Ctrl+C 停止" "Cyan"
Line "  起来后另开窗口验证：" "Cyan"
Line "    curl.exe --ssl-no-revoke https://ifconfig.me     # 应显示跳板机出口 IP" "DarkGray"
Line "    nslookup github.com 8.8.8.8                      # UDP 是否通" "DarkGray"
Write-Host ""
& $exe run -c $cfgPath
