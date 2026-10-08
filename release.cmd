@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0release-control.ps1" %*
exit /b %ERRORLEVEL%
