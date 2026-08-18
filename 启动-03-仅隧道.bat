@echo off
rem 只启动 cloudflared 隧道 xhs-tunnel（公网 flux/xhs 域名共用）。
rem 一般为自动，仅当两个服务已手动起但公网不通时用它补隧道。
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_service.ps1" -Target tunnel