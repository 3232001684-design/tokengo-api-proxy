# TokenGo 一键启动 + 公网隧道 + 自动更新域名
# 由 start.bat 调用，请勿直接运行

$ErrorActionPreference = "Continue"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

$LogFile      = Join-Path $ScriptDir "tunnel_log.txt"
$UrlFile      = Join-Path $ScriptDir "public_url.txt"
$MainPy       = Join-Path $ScriptDir "main.py"
$PyPort       = 8000

function Write-Step($msg) { Write-Host "[$(Get-Date -Format 'HH:mm:ss')] $msg" -ForegroundColor Cyan }
function Write-OK($msg)   { Write-Host "[$(Get-Date -Format 'HH:mm:ss')] [OK] $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "[$(Get-Date -Format 'HH:mm:ss')] [!] $msg" -ForegroundColor Yellow }

# ---- 1. 初始化 public_url.txt（默认值，用 .NET 写入避免 BOM）----
if (-not (Test-Path $UrlFile)) {
    [System.IO.File]::WriteAllText($UrlFile, "https://tokengo.serveo.net")
    Write-OK "已创建 public_url.txt（默认值）"
}

# ---- 2. 启动 Python 服务（若未运行）----
$pyRunning = $false
try {
    $conn = Get-NetTCPConnection -LocalPort $PyPort -State Listen -ErrorAction Stop
    if ($conn) { $pyRunning = $true }
} catch {}

if (-not $pyRunning) {
    Write-Step "启动 Python 服务 (端口 $PyPort) ..."
    Start-Process -FilePath "py" -ArgumentList "main.py" -WorkingDirectory $ScriptDir -WindowStyle Minimized
    Start-Sleep -Seconds 3
    Write-OK "Python 服务已启动"
} else {
    Write-OK "Python 服务已在运行"
}

# ---- 3. 从日志提取 serveo.net 公网地址并写入 public_url.txt ----
function Update-PublicUrl {
    if (-not (Test-Path $LogFile)) { return $false }
    $log = Get-Content $LogFile -Raw -ErrorAction SilentlyContinue
    if (-not $log) { return $false }
    # serveo.net 输出格式：Forwarding: https://xxx.serveo.net -> http://localhost:8000
    $m = [regex]::Match($log, 'Forwarding:\s*(https?://[a-zA-Z0-9.\-]+)')
    if (-not $m.Success) { return $false }
    $url = $m.Groups[1].Value.Trim()
    if ($url -notmatch '^https?://[a-zA-Z0-9.\-]+$') { return $false }
    # 写入 public_url.txt（用 .NET 方法避免 BOM，中间件会自动读取，无需重启服务）
    [System.IO.File]::WriteAllText($UrlFile, $url)
    Write-OK "公网地址已更新: $url"
    return $true
}

# ---- 4. 主循环：连接隧道 + 自动重连（后台运行，避免窗口闪烁）----
Write-Step "TokenGo 服务已就绪"
Write-Host "  本地地址: http://localhost:$PyPort" -ForegroundColor White
Write-Host "  公网地址: $(Get-Content $UrlFile -Raw)" -ForegroundColor White
Write-Host "========================================" -ForegroundColor DarkGray

function Connect-Tunnel {
    $sshArgs = @(
        "-o", "StrictHostKeyChecking=no",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        "-o", "ExitOnForwardFailure=yes",
        "-R", "80:localhost:$PyPort",
        "nokey@serveo.net"
    )
    
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = "ssh"
    $psi.Arguments = $sshArgs -join " "
    $psi.WorkingDirectory = $ScriptDir
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $psi
    $process.Start() | Out-Null
    
    return $process
}

while ($true) {
    Write-Step "连接 serveo.net 隧道 ..."
    
    $sshProcess = Connect-Tunnel
    
    while ($sshProcess.HasExited -eq $false) {
        Start-Sleep -Seconds 1
        if (Test-Path $LogFile) {
            $log = Get-Content $LogFile -Raw -ErrorAction SilentlyContinue
            if ($log -match 'Forwarding:\s*(https?://[a-zA-Z0-9.\-]+)') {
                $url = $matches[1].Trim()
                if ($url -match '^https?://[a-zA-Z0-9.\-]+$') {
                    [System.IO.File]::WriteAllText($UrlFile, $url)
                    Write-OK "公网地址已更新: $url"
                }
            }
        }
    }
    
    Write-Warn "SSH 隧道断开，尝试更新公网地址 ..."
    $updated = Update-PublicUrl
    if (-not $updated) { Write-Warn "未能从日志提取新地址，保留上次地址" }

    Write-Warn "3 秒后自动重连 ..."
    Start-Sleep -Seconds 3
}
