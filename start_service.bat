@echo off
rem 一键启动全部（FLUX + 小红书 + 隧道）。Real logic in start_service.ps1.
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_service.ps1" -Target all