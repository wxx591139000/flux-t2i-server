@echo off
rem 只启动 FLUX 文生图服务 (port 9620) + 确保隧道。重启后手动点这个拉起 FLUX。
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_service.ps1" -Target flux