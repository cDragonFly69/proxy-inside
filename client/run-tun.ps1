<#
  run-tun.ps1 —— Windows 端把整机流量（含 UDP）导到跳板机，UU 远程等应用即可用。

  用法（管理员 PowerShell）：
    .\run-tun.ps1 -Password s3cret              # 诊断 + 下载 + 配置 + 启动
    .\run-tun.ps1 -Check                        # 只诊断，不动系统
    .\run-tun.ps1 -Check -Password s3cret       # 带令牌做完整诊断（含出网测试）
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

# curl 的错误输出会被 PowerShell 当成 NativeCommandError，这里统一吃掉
function Invoke-Curl([string[]]$ArgList) {
  $old = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    $out = & curl.exe @ArgList 2>&1
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

# 依次尝试：直连 → ghproxy → gh-proxy → ghfast（内网无外网，全靠 18081 代理出去）
function Fetch-File([string]$url, [string]$out) {
  $mirrors = @("", "https://ghproxy.net/", "https://gh-proxy.com/", "https://ghfast.top/")
  foreach ($m in $mirrors) {
    $u = if ($m) { $m + $url } else { $url }
    Line ("  尝试 " + $(if ($m) { $m.Split('/')[2] } else { "直连" })) "DarkGray"
    Remove-Item $out -ErrorAction SilentlyContinue
    $r = Invoke-Curl @("--ssl-no-revoke", "-L", "--retry", "2", "-m", "180", "-x", $proxy, "-o", $out, $u)
    if ($r.Code -eq 0 -and (Test-Path $out) -and (Get-Item $out).Length -gt 1000) { return }
  }
  throw "所有下载源都失败：$url"
}

# ---------------- 诊断 ----------------
Line "== 1. 权限 =="
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
  [Security.Principal.WindowsBuiltInRole]::Administrator)
if ($isAdmin) { Line "  [OK] 管理员" "Green" }
else { Line "  [X] 不是管理员，TUN 建不起来：右键 PowerShell -> 以管理员身份运行" "Red" }

Line "== 2. 跳板机端口 =="
foreach ($pair in @(@($HttpPort, "HTTP 代理"), @($SocksPort, "SOCKS5/UDP"))) {
  if (Test-Tcp $Server $pair[0]) { Line ("  [OK] {0}:{1} ({2})" -f $Server, $pair[0], $pair[1]) "Green" }
  else { Line ("  [X]  {0}:{1} ({2}) 连不上" -f $Server, $pair[0], $pair[1]) "Red" }
}

Line "== 3. 代理出网 =="
if (-not $Password) {
  Line "  [--] 没给 -Password，跳过（加 -Password 才能测）" "Yellow"
} else {
  $r = Invoke-Curl @("--ssl-no-revoke", "-sS", "-m", "20", "-x", $proxy,
                     "--proxy-header", "Proxy-Authorization: Bearer $Password",
                     "-o", "NUL", "-w", "%{http_code}", "https://api.github.com/zen")
  $code = ($r.Out -split "`n" | Select-Object -Last 1).Trim()
  if ($code -eq "200") { Line "  [OK] api.github.com 200" "Green" }
  elseif ($code -eq "407") { Line "  [X] 407 令牌不对：-Password 要和跳板机 TOKEN 一致" "Red" }
  elseif ($code -eq "403") { Line "  [X] 403 目标不在 --allow 白名单" "Red" }
  else { Line ("  [X] 代理没出网（返回 '{0}'）" -f $code) "Red" }
  Line "  [注] github.com / codeload.github.com 不通是链路问题，与代理无关" "DarkGray"
}

Line "== 4. 客户端组件 =="
Line ("  sing-box.exe : " + $(if (Test-Path $exe) { "已存在" } else { "缺失（启动时自动下载）" }))
Line ("  wintun.dll   : " + $(if (Test-Path (Join-Path $dir "wintun.dll")) { "已存在" } else { "缺失（启动时自动下载）" }))
$tun = Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object { $_.InterfaceDescription -match "sing|wintun|tun" }
if ($tun) { Line "  [--] 已有 TUN 网卡：$($tun.Name -join ', ')" "Yellow" }
else { Line "  [--] 当前没有 TUN 网卡（未运行）" }

if ($Check) { Line "`n诊断结束（-Check，未做任何改动）。" "Cyan"; exit 0 }

if (-not $isAdmin) { exit 1 }
if ([string]::IsNullOrWhiteSpace($Password)) {
  Line "`n[!] 缺少 -Password（跳板机启动时 TOKEN 的值）" "Red"; exit 1
}
if (-not (Test-Path $tplPath)) { Line "`n[!] 找不到 sing-box.json，请与脚本放同一目录" "Red"; exit 1 }

# ---------------- 准备 sing-box（锁 1.11.x，配置格式匹配）----------------
if (-not (Test-Path $exe)) {
  Line "`n== 下载 sing-box =="
  $api = "https://api.github.com/repos/SagerNet/sing-box/releases?per_page=100"
  $r = Invoke-Curl @("--ssl-no-revoke", "-s", "-m", "40", "-x", $proxy,
                     "--proxy-header", "Proxy-Authorization: Bearer $Password", $api)
  $rels = $r.Out | ConvertFrom-Json
  $rel = $rels | Where-Object { $_.tag_name -like "v1.11.*" } | Select-Object -First 1
  if (-not $rel) { $rel = $rels | Select-Object -First 1 }
  $asset = $rel.assets | Where-Object { $_.name -like "*windows-amd64.zip" } | Select-Object -First 1
  if (-not $asset) { throw "API 里没有 windows-amd64 包" }
  Line ("  版本 " + $rel.tag_name + " / " + $asset.name)
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
    Line "  [!] wintun 下载失败，请手动把 amd64 版 wintun.dll 放到 $dir" "Yellow"
  }
}

# ---------------- 生成配置并启动 ----------------
$tpl  = Get-Content $tplPath -Raw -Encoding UTF8
$conf = $tpl.Replace("__SERVER__", $Server).Replace("__PORT__", "$SocksPort").
             Replace("__USER__", $User).Replace("__PASS__", $Password)
Set-Content -Path $cfgPath -Value $conf -Encoding UTF8
Line "`n== 启动 TUN =="
Line "  服务器 $Server`:$SocksPort  user=$User" "Green"
Line "  Ctrl+C 或关窗口停止；另开窗口验证：" "Cyan"
Line "    curl.exe --ssl-no-revoke https://ifconfig.me     # 应是跳板机出口 IP" "DarkGray"
Line "    nslookup uu.163.com 8.8.8.8                      # UDP 是否通" "DarkGray"
Write-Host ""
& $exe run -c $cfgPath
