@echo off
setlocal
set "APPDIR=%LOCALAPPDATA%\ClipboardSync"
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "EXENAME=clipboard-sync-windows-v1.3.0.exe"

taskkill /F /IM clipboard-sync-windows-v1.1.0.exe >nul 2>&1
taskkill /F /IM clipboard-sync-windows-v1.1.1.exe >nul 2>&1
taskkill /F /IM clipboard-sync-windows-v1.1.2.exe >nul 2>&1
taskkill /F /IM clipboard-sync-windows-v1.2.0.exe >nul 2>&1
taskkill /F /IM clipboard-sync-windows-v1.3.0.exe >nul 2>&1

if not exist "%APPDIR%" mkdir "%APPDIR%"
if errorlevel 1 goto :failed

copy /Y "%~dp0%EXENAME%" "%APPDIR%\%EXENAME%" >nul
if errorlevel 1 goto :failed
copy /Y "%~dp0config.json" "%APPDIR%\config.json" >nul
if errorlevel 1 goto :failed

powershell -NoProfile -Command "$w=New-Object -ComObject WScript.Shell; $s=$w.CreateShortcut((Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup\Clipboard Sync.lnk')); $s.TargetPath=(Join-Path $env:LOCALAPPDATA 'ClipboardSync\clipboard-sync-windows-v1.3.0.exe'); $s.WorkingDirectory=(Join-Path $env:LOCALAPPDATA 'ClipboardSync'); $s.Save()"
if errorlevel 1 goto :failed

reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "Clipboard Sync" /f >nul 2>&1
del /F /Q "%APPDIR%\clipboard-sync-windows-v1.1.0.exe" >nul 2>&1
del /F /Q "%APPDIR%\clipboard-sync-windows-v1.1.1.exe" >nul 2>&1
del /F /Q "%APPDIR%\clipboard-sync-windows-v1.1.2.exe" >nul 2>&1
del /F /Q "%APPDIR%\clipboard-sync-windows-v1.2.0.exe" >nul 2>&1
start "" "%APPDIR%\%EXENAME%"
echo.
echo Clipboard Sync is installed and running.
echo It will start automatically after Windows sign-in.
echo Python and administrator rights are not required.
pause
exit /b 0

:failed
echo.
echo Installation failed.
echo Fully extract the ZIP and check whether security software blocked the files.
echo Administrator rights are not required.
pause
exit /b 1
