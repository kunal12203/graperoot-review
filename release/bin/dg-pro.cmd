@echo off
rem dg-pro — GrapeRoot Pro launcher for Codex (Windows cmd shim)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch_pro.ps1" --codex %*
exit /b %ERRORLEVEL%
