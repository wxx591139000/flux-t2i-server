@echo off
rem One-click launcher for FLUX service. Real logic is in start_service.ps1 (UTF-8 BOM).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_service.ps1"