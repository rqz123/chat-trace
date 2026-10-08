@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0sync-status.ps1" %*
exit /b %ERRORLEVEL%
