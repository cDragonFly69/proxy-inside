<#
  run-tun.ps1 —— Windows 端一键把整机流量（含 UDP）导到跳板机。

  管理员 PowerShell 里运行：
    .\run-tun.ps1 -Password s3cret        # 诊断 -> 拉客户端包 -> 启动 TUN
    .\run-tun.ps1 -Check -Password s3cret # 只诊断

  缺 sing-box.exe / wintun.dll 时，会从跳板机的分发服务（默认 18090）自动拉包，
  不再访问 github.com。跳板机上先跑过 make-bundle.sh 即可。
#>
param(
  [string]$Server     = "10.200.1.26",
  [int]   $HttpPort   = 18081,
  [int]   $SocksPort  = 18082,
  [int]   $BundlePort = 18090,
  [string]$User       = "ccs",
  [string]$Password   = "",
  [switch]$Check
)

$ErrorActionPreference = "Stop"
$dir     = Split-Path -Parent $MyInvocation.MyCommand.Path
$exe     = Join-Path $dir "sing-box.exe"
$wintun  = Join-Path $dir "wintun.dll"
$cfgPath = Join-Path $dir "config.json"
$tplPath = Join-Path $dir "sing-box.json"
$proxy   = "http://${Server}:${HttpPort}"
$bundle  = "http://${Server}:${BundlePort}/client-bundle.tgz"

function Line($t, $c = "Gray") { Write-Host $t -ForegroundColor $c }

# curl / tar 的 stderr 会被 PowerShell 当致命异常，这里统一吃掉
function Invoke-Native([string]$exe, [string[]]$ArgList) {
  $old = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    $out = & $exe @ArgList 2>&1
    return [pscustomobject]@{ Code = $LASTEXITCODE; Out = ($out -join " ") }
  } finally { $ErrorActionPreference = $old }
}

function Test-Tcp([string]$h, [int]$p) {
  $c = New-Object Net.Sockets.TcpClient
  try {
    $iar = $c.BeginConnect($h, $p, $null, $null)
    if (-not $iar.AsyncWaitHandle.WaitOne(3000)) { return $false }
    $c.EndConnect($iar); return $true
  } catch { return $false } finally { $c.Close() }
}

# 从跳板机拉客户端包并解到本目录
function Fetch-Bundle {
  Line "`n== 拉取客户端包 =="
  Line "  $bundle"
  $tgz = Join-Path $dir "client-bundle.tgz"
  Remove-Item $tgz -ErrorAction SilentlyContinue
  $r = Invoke-Native "curl.exe" @("--ssl-no-revoke", "-L", "--retry", "3", "-o", $tgz, $bundle)
  if ($r.Code -ne 0 -or -not (Test-Path $tgz)) {
    throw "下载失败，确认跳板机上 make-bundle.sh 还在跑（端口 $BundlePort）"
  }
  $size = (Get-Item $tgz).Length
  if ($size -lt 100000) { throw "下载的包只有 $size 字节，肯定不完整" }
  Line ("  已下载 {0:N0} 字节" -f $size)

  $r = Invoke-Native "tar.exe" @("-xzf", $tgz, "-C", $dir)
  if ($r.Code -ne 0) { throw "解压失败：$($r.Out)" }
  Remove-Item $tgz -ErrorAction SilentlyContinue
  if (-not (Test-Path $exe)) { throw "解压后仍没有 sing-box.exe" }
  Line "  [OK] 客户端文件就绪" "Green"
}

# ---------------- 诊断 ----------------
Line "== 1. 权限 =="
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
  [Security.Principal.WindowsBuiltInRole]::Administrator)
if ($isAdmin) { Line "  [OK] 管理员" "Green" }
else { Line "  [X] 不是管理员，TUN 建不起来：右键 PowerShell -> 以管理员身份运行" "Red" }

Line "== 2. 跳板机端口 =="
foreach ($pair in @(@($HttpPort, "HTTP 代理"), @($SocksPort, "SOCKS5/UDP"), @($BundlePort, "分发服务"))) {
  if (Test-Tcp $Server $pair[0]) { Line ("  [OK] {0}:{1} ({2})" -f $Server, $pair[0], $pair[1]) "Green" }
  else { Line ("  [X]  {0}:{1} ({2}) 连不上" -f $Server, $pair[0], $pair[1]) "Red" }
}

Line "== 3. 代理出网 =="
if (-not $Password) {
  Line "  [--] 没给 -Password，跳过" "Yellow"
} else {
  $r = Invoke-Native "curl.exe" @("--ssl-no-revoke", "-sS", "-m", "20", "-x", $proxy,
                                  "--proxy-header", "Proxy-Authorization: Bearer $Password",
                                  "-o", "NUL", "-w", "%{http_code}", "https://api.github.com/zen")
  $code = ($r.Out -split "`n" | Select-Object -Last 1).Trim()
  if ($code -eq "200") { Line "  [OK] api.github.com 200" "Green" }
  elseif ($code -eq "407") { Line "  [X] 407 令牌不对：-Password 要和跳板机 TOKEN 一致" "Red" }
  elseif ($code -eq "403") { Line "  [X] 403 目标不在 --allow 白名单" "Red" }
  else { Line ("  [X] 代理没出网（返回 '{0}'）" -f $code) "Red" }
  Line "  [注] github.com 不通是链路问题，与代理无关" "DarkGray"
}

Line "== 4. 客户端组件 =="
Line ("  sing-box.exe : " + $(if (Test-Path $exe) { "已存在" } else { "缺失（启动时自动拉）" }))
Line ("  wintun.dll   : " + $(if (Test-Path $wintun) { "已存在" } else { "缺失（启动时自动拉）" }))
$tun = Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object { $_.InterfaceDescription -match "sing|wintun" }
if ($tun) { Line "  [--] 已有 TUN 网卡：$($tun.Name -join ', ')" "Yellow" }
else { Line "  [--] 当前没有 TUN 网卡（未运行）" }

if ($Check) { Line "`n诊断结束（-Check，未做任何改动）。" "Cyan"; exit 0 }

if (-not $isAdmin) { exit 1 }
if ([string]::IsNullOrWhiteSpace($Password)) {
  Line "`n[!] 缺少 -Password（跳板机启动时 TOKEN 的值）" "Red"; exit 1
}
if (-not (Test-Path $tplPath)) { Line "`n[!] 找不到 sing-box.json" "Red"; exit 1 }

# ---------------- 补齐客户端文件 ----------------
if (-not (Test-Path $exe) -or -not (Test-Path $wintun)) { Fetch-Bundle }

# ---------------- 生成配置并启动 ----------------
$tpl  = Get-Content $tplPath -Raw -Encoding UTF8
$conf = $tpl.Replace("__SERVER__", $Server).Replace("__PORT__", "$SocksPort").
             Replace("__USER__", $User).Replace("__PASS__", $Password)
# 去掉可能残留的 BOM：sing-box 不认带 BOM 的 JSON
$conf = $conf.TrimStart([char]0xFEFF)
# 必须写成无 BOM UTF-8，Set-Content -Encoding UTF8 在 PS 5.1 会加 BOM
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($cfgPath, $conf, $utf8NoBom)
$head = [System.IO.File]::ReadAllBytes($cfgPath)
if ($head.Length -ge 3 -and $head[0] -eq 0xEF -and $head[1] -eq 0xBB -and $head[2] -eq 0xBF) {
  throw "config.json 仍带 BOM，写入失败"
}

Line "`n== 启动 TUN =="
Line "  服务器 $Server`:$SocksPort  user=$User" "Green"
Line "  验证（另开管理员窗口）：" "Cyan"
Line "    curl.exe --ssl-no-revoke https://ifconfig.me     # 应是跳板机出口 IP" "DarkGray"
Line "    nslookup uu.163.com 8.8.8.8                      # UDP 是否通" "DarkGray"
Write-Host ""
& $exe run -c $cfgPath
