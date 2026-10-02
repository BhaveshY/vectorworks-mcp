@echo off
rem Plugin shim: forwards to the per-user install (%USERPROFILE%\.vectorworks-mcp\bin).
setlocal EnableExtensions DisableDelayedExpansion
set "VW_CTL=%USERPROFILE%\.vectorworks-mcp\bin\vectorworksctl.cmd"
if exist "%VW_CTL%" goto run
>&2 echo Vectorworks MCP is not installed for this user. Run install.ps1 from the Vectorworks MCP folder: powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
exit /b 3
:run
call "%VW_CTL%" %*
exit /b %ERRORLEVEL%
