@echo off
setlocal

rem ============================================================
rem  Start Chrome with remote debugging port (for CDP mode)
rem  Data is stored in ".cdp_profile" next to this script,
rem  so your normal Chrome profile is NOT touched.
rem  Usage: double-click this file, keep the Chrome window open,
rem  then run the scraper.
rem ============================================================

set "CHROME="
if exist "C:\Program Files\Google\Chrome\Application\chrome.exe" (
  set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
  goto :found
)
if exist "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" (
  set "CHROME=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
  goto :found
)
if exist "C:\Program Files\Microsoft\Edge\Application\msedge.exe" (
  set "CHROME=C:\Program Files\Microsoft\Edge\Application\msedge.exe"
  goto :found
)
if exist "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" (
  set "CHROME=C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
  goto :found
)
:found

if "%CHROME%"=="" (
  echo [ERROR] Chrome or Edge not found. Please install Google Chrome first.
  pause
  exit /b 1
)

rem --- Step 1: check whether port 9222 is already open ---
netstat -ano | findstr ":9222" | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
  echo [OK] Port 9222 is already open. The scraper can connect now.
  echo      If the scraper still fails, a leftover Chrome window may hold a
  echo      stale profile: close it, end leftover chrome.exe in Task Manager,
  echo      then run this script again.
  pause
  exit /b 0
)

rem --- Step 2: port is free, start Chrome with the debug port ---
echo [INFO] Port 9222 is free. Starting Chrome with debug port...
start "" "%CHROME%" --remote-debugging-port=9222 --user-data-dir="%~dp0.cdp_profile" --no-first-run --no-default-browser-check

rem --- Step 3: wait a moment, then re-check the port ---
timeout /t 4 >nul

netstat -ano | findstr ":9222" | findstr "LISTENING" >nul 2>&1
if errorlevel 1 (
  echo.
  echo [WARN] Chrome did not open the debug port yet.
  echo        A leftover ".cdp_profile" Chrome instance may be blocking it.
  echo        Fix: close ALL Chrome windows that belong to .cdp_profile
  echo        (or end remaining chrome.exe in Task Manager), then rerun.
  pause
  exit /b 1
)

echo.
echo [OK] Chrome started with debug port 9222.
echo Keep this Chrome window open, then run the scraper.
echo.
pause
