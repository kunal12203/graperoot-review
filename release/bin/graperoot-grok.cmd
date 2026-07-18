@echo off
rem graperoot-grok — GrapeRoot Pro launcher for Grok CLI (Windows)
set INSTALL_DIR=%GRAPEROOT_PRO_HOME%
if "%INSTALL_DIR%"=="" set INSTALL_DIR=%USERPROFILE%\.graperoot-pro
"%INSTALL_DIR%\venv\Scripts\python.exe" "%INSTALL_DIR%\launch.py" %1 --grok %2 %3 %4 %5 %6 %7 %8 %9
