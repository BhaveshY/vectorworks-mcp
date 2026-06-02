@echo off
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0vectorworksctl.ps1" %*
exit /b %ERRORLEVEL%
