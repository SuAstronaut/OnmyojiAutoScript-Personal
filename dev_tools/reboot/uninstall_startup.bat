@echo off
chcp 65001 > nul
echo Uninstalling OAS Daemon from Windows Startup...

REM Delete scheduled task
schtasks /delete /tn "OASDaemon" /f >nul 2>&1
if %errorLevel% equ 0 (
    echo [OK] Scheduled task "OASDaemon" deleted
) else (
    echo Scheduled task not found, skipping
)

REM Remove old startup method (registry + VBS)
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "OASDaemon" /f >nul 2>&1
if exist "%TEMP%\oas_daemon_start.vbs" (
    del "%TEMP%\oas_daemon_start.vbs"
    echo [OK] Temp startup script deleted
)

echo.
echo OAS Daemon has been uninstalled from Windows startup.
echo.
pause
