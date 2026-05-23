@echo off
rem dgc-pro — GrapeRoot Pro launcher (Windows cmd shim)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch_pro.ps1" %*
exit /b %ERRORLEVEL%
