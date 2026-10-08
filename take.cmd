@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0take-control.ps1" %*
exit /b %ERRORLEVEL%
