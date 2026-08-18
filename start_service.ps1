# ============================================================
#  FLUX 文生图服务 / 小红书发布服务  一键启动（由 start_service.bat 或各"启动-*.bat"调用）
#  -Target flux   : 只起 FLUX 文生图服务 (port 9620) + 确保隧道
#  -Target xhs    : 只起 小红书发布服务 (port 8800) + 确保隧道
#  -Target tunnel : 只起 cloudflared 隧道 (xhs-tunnel, 公网 flux/xhs 域名共用)
#  -Target all    : flux + xhs + 隧道（默认）
#  均幂等：已在运行则跳过，关掉窗口不中断服务。
#
#  隧道约定：本地只跑 xhs-tunnel(27da88b4)，FLUX 与小红书域名都走它。
#  严禁用旧 transcribe-bot 隧道(1779cc80) 重启本地 cloudflared（会抢 VPS 隧道）。
#  详见 C:\Users\Dancing\.cloudflared\COORDINATION.md
# ============================================================

param([string]$Target = "all")

$ErrorActionPreference = 'SilentlyContinue'

$ProjFlux   = 'E:\myClaudCodeWorkspace\flux-t2i-server'
$ProjXhs    = 'E:\myClaudCodeWorkspace\xhs-note-publish'
$Python     = 'C:\Users\Dancing\AppData\Local\Programs\Python\Python311\python.exe'
$Cloudflared = 'C:\Users\Dancing\AppData\Local\Microsoft\WinGet\Packages\Cloudflare.cloudflared_Microsoft.Winget.Source_8wekyb3d8bbwe\cloudflared.exe'
$CfDir      = 'C:\Users\Dancing\.cloudflared'

function Test-Port($port) {
    return [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
}
function Ensure-Tunnel {
    if (Get-Process cloudflared -ErrorAction SilentlyContinue) {
        Write-Host "  隧道 xhs-tunnel 已在运行，跳过"
        return
    }
    Write-Host "  启动 cloudflared 隧道 xhs-tunnel (公网 flux/xhs 域名)..."
    # 正确做法：cd 到 .cloudflared 目录让 cloudflared 自动加载 config.yml；不加 --config 避免参数拆分 bug
    Start-Process -FilePath $Cloudflared -ArgumentList 'tunnel run xhs-tunnel' `
        -WorkingDirectory $CfDir -WindowStyle Hidden `
        -RedirectStandardError  "$CfDir\cf_err.log" `
        -RedirectStandardOutput "$CfDir\cf_out.log"
}

Write-Host ""
Write-Host "============================================"
Write-Host "  服务启动 ($Target)"
Write-Host "============================================"

if ($Target -eq "flux" -or $Target -eq "all") {
    Write-Host " [FLUX] 文生图服务 (port 9620)..."
    if (Test-Port 9620) {
        Write-Host "      - flux_service 已在运行，跳过"
    } else {
        Start-Process -FilePath $Python -ArgumentList 'manager/flux_service.py' `
            -WorkingDirectory $ProjFlux -WindowStyle Hidden `
            -RedirectStandardOutput "$ProjFlux\manager\flux_service.out.log" `
            -RedirectStandardError  "$ProjFlux\manager\flux_service.err.log"
        Write-Host "      - 已后台启动 (日志: manager\flux_service.out.log)"
    }
    Ensure-Tunnel
}

if ($Target -eq "xhs" -or $Target -eq "all") {
    Write-Host " [XHS] 小红书发布服务 (port 8800)..."
    if (Test-Port 8800) {
        Write-Host "      - xhs 发布服务已在运行，跳过"
    } else {
        Start-Process -FilePath $Python -ArgumentList 'app.py' `
            -WorkingDirectory $ProjXhs -WindowStyle Hidden `
            -RedirectStandardOutput "$ProjXhs\server.out.log" `
            -RedirectStandardError  "$ProjXhs\server.err.log"
        Write-Host "      - 已后台启动 (日志: server.err.log)"
    }
    Ensure-Tunnel
}

if ($Target -eq "tunnel") {
    Ensure-Tunnel
}

Write-Host ""
Write-Host "============================================"
Write-Host "  OK 完成!  检查:"
Write-Host "     FLUX:   http://localhost:9620  | https://flux.zhuanlu.xyz"
Write-Host "     XHS:    http://localhost:8800  | https://xhs.zhuanlu.xyz"
Write-Host "============================================"
Start-Sleep -Seconds 2