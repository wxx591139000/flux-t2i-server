# ============================================================
#  FLUX 文生图服务 一键启动（由 start_service.bat 调用）
#  1. 启动 Web + 队列服务 flux_service.py (port 9620)
#  2. 启动 cloudflared 隧道 xhs-tunnel (公网 flux.zhuanlu.xyz)
#  两者均后台分离启动，关掉窗口不中断服务
# ============================================================

$ErrorActionPreference = 'SilentlyContinue'
$Project    = 'E:\myClaudCodeWorkspace\flux-t2i-server'
$Python     = 'C:\Users\Dancing\AppData\Local\Programs\Python\Python311\python.exe'
$Cloudflared = 'C:\Users\Dancing\AppData\Local\Microsoft\WinGet\Packages\Cloudflare.cloudflared_Microsoft.Winget.Source_8wekyb3d8bbwe\cloudflared.exe'
$Tunnel     = 'xhs-tunnel'
$CfDir      = 'C:\Users\Dancing\.cloudflared'

Write-Host ""
Write-Host "============================================"
Write-Host "  FLUX 文生图服务 一键启动"
Write-Host "============================================"

# ---- 1. Web + 队列服务 (port 9620) ----
Write-Host " [1/2] 启动 FLUX 服务 (port 9620)..."
$listen = Get-NetTCPConnection -LocalPort 9620 -State Listen -ErrorAction SilentlyContinue
if ($listen) {
    Write-Host "      - 服务已在运行，跳过"
} else {
    Start-Process -FilePath $Python -ArgumentList 'manager/flux_service.py' `
        -WorkingDirectory $Project -WindowStyle Hidden `
        -RedirectStandardOutput "$Project\manager\flux_service.out.log" `
        -RedirectStandardError  "$Project\manager\flux_service.err.log"
    Write-Host "      - 已后台启动，日志: manager\flux_service.out.log / .err.log"
}

# ---- 2. cloudflared 隧道 (xhs-tunnel) ----
Write-Host " [2/2] 启动 cloudflared 隧道 ($Tunnel)..."
if (Get-Process cloudflared -ErrorAction SilentlyContinue) {
    Write-Host "      - cloudflared 已在运行，跳过"
} else {
    Start-Process -FilePath $Cloudflared -ArgumentList 'tunnel','run',$Tunnel,'--config',"$CfDir\config.yml" `
        -WindowStyle Hidden `
        -RedirectStandardError  "$CfDir\cf_err.log" `
        -RedirectStandardOutput "$CfDir\cf_out.log"
    Write-Host "      - 已后台启动，日志: $CfDir\cf_err.log"
}

Write-Host ""
Write-Host "============================================"
Write-Host "  OK 启动完成!"
Write-Host "     本地:   http://localhost:9620"
Write-Host "     公网:   https://flux.zhuanlu.xyz"
Write-Host "     健康:   curl http://127.0.0.1:9620/health"
Write-Host "============================================"
Start-Sleep -Seconds 3