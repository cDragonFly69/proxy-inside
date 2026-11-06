<#
  run-tun.ps1 —— 在 Windows 上把整机流量（含 UDP）导到跳板机，给 UU 远程这类应用做出网。
  需要：管理员权限、能访问跳板机的 18081(HTTP) 与 18082(SOCKS5)。

  用法（管理员 PowerShell）：
    .\run-tun.ps1 -Password 你的令牌
    .\run-tun.ps1 -Server 10.200.1.26 -SocksPort 18082 -User ccs -Password 你的令牌
#>
param(
  [string]$Server    = "10.200.1.26",
  [int]   $SocksPort = 18082,
  [string]$User      = "ccs",
  [string]$Password  = "",
  [string]$HttpProxy = "http://10.200.1.26:18081"
)

$ErrorActionPreference = "Stop"
$dir     = Split-Path -Parent $MyInvocation.MyCommand.Path
$exe     = Join-Path $dir "sing-box.exe"
$cfgPath = Join-Path $dir "config.json"
$tplPath = Join-Path $dir "sing-box.json"

# ---- 0. 前置检查 ----
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
  [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
  Write-Host "[!] 需要管理员权限：右键 PowerShell -> 以管理员身份运行" -ForegroundColor Red
  exit 1
}
if ([string]::IsNullOrWhiteSpace($Password)) {
  Write-Host "[!] 缺少 -Password（跳板机启动时 TOKEN 的值）" -ForegroundColor Red
  exit 1
}
if (-not (Test-Path $tplPath)) {
  Write-Host "[!] 找不到配置模板 sing-box.json，请与脚本放在同一目录" -ForegroundColor Red
  exit 1
}

function Fetch-File([string]$url, [string]$out) {
  # 内网机器没外网，统一下载走跳板机的 HTTP 代理；schannel 吊销检查必须关掉
  curl.exe --ssl-no-revoke -L --retry 3 -x $HttpProxy -o $out $url
  if ($LASTEXITCODE -ne 0 -or -not (Test-Path $out)) { throw "下载失败：$url" }
}

# ---- 1. 准备 sing-box.exe ----
if (-not (Test-Path $exe)) {
  Write-Host "[*] 解析 sing-box 最新版本 ..."
  $meta = curl.exe --ssl-no-revoke -s -x $HttpProxy "https://api.github.com/repos/SagerNet/sing-box/releases/latest" | ConvertFrom-Json
  $asset = $meta.assets | Where-Object { $_.name -like "*windows-amd64.zip" } | Select-Object -First 1
  if (-not $asset) { throw "没有找到 windows-amd64 发行包" }
  Write-Host "[*] 下载 $($asset.name) ..."
  $zip = Join-Path $dir "sing-box.zip"
  Fetch-File $asset.browser_download_url $zip
  Expand-Archive -Path $zip -DestinationPath $dir -Force
  $found = Get-ChildItem $dir -Recurse -Filter "sing-box.exe" | Where-Object { $_.FullName -ne $exe } | Select-Object -First 1
  if ($found) { Copy-Item $found.FullName $exe -Force }
  Remove-Item $zip -Force -ErrorAction SilentlyContinue
  if (-not (Test-Path $exe)) { throw "解压后没找到 sing-box.exe" }
}

# ---- 2. 准备 wintun.dll（TUN 网卡驱动）----
$wintun = Join-Path $dir "wintun.dll"
if (-not (Test-Path $wintun)) {
  Write-Host "[*] 下载 wintun ..."
  $wz = Join-Path $dir "wintun.zip"
  try {
    Fetch-File "https://www.wintun.net/builds/wintun-0.14.1.zip" $wz
    Expand-Archive -Path $wz -DestinationPath (Join-Path $dir "wintun") -Force
    $dll = Get-ChildItem (Join-Path $dir "wintun") -Recurse -Filter "wintun.dll" |
           Where-Object { $_.FullName -match "amd64" } | Select-Object -First 1
    if ($dll) { Copy-Item $dll.FullName $wintun -Force }
    Remove-Item $wz -Force -ErrorAction SilentlyContinue
  } catch {
    Write-Host "[!] wintun.dll 自动下载失败，请手动把 amd64 版 wintun.dll 放到 $dir" -ForegroundColor Yellow
  }
}

# ---- 3. 生成运行配置 ----
$tpl  = Get-Content $tplPath -Raw -Encoding UTF8
$conf = $tpl.Replace("__SERVER__", $Server).
             Replace("__PORT__", "$SocksPort").
             Replace("__USER__", $User).
             Replace("__PASS__", $Password)
Set-Content -Path $cfgPath -Value $conf -Encoding UTF8
Write-Host "[*] 配置已生成：$cfgPath" -ForegroundColor Green
Write-Host "[*] $Server`:$SocksPort  user=$User" -ForegroundColor Green

# ---- 4. 跑起来 ----
Write-Host "[*] 启动 TUN，整机流量（含 UDP）走跳板机；Ctrl+C 或关窗口即停 ..." -ForegroundColor Cyan
& $exe run -c $cfgPath
