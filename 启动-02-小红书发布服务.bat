@echo off
rem 只启动小红书发布服务 (port 8800) + 确保隧道。重启后手动点这个拉起小红书。
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_service.ps1" -Target xhs