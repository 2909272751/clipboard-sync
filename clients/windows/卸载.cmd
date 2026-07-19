@echo off
setlocal
taskkill /F /IM clipboard-sync-windows-v1.1.0.exe >nul 2>&1
taskkill /F /IM clipboard-sync-windows-v1.1.1.exe >nul 2>&1
taskkill /F /IM clipboard-sync-windows-v1.1.2.exe >nul 2>&1
taskkill /F /IM clipboard-sync-windows-v1.2.0.exe >nul 2>&1
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "Clipboard Sync" /f >nul 2>&1
del /F /Q "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Clipboard Sync.lnk" >nul 2>&1
rmdir /S /Q "%LOCALAPPDATA%\ClipboardSync" >nul 2>&1
echo Clipboard Sync was removed.
pause
