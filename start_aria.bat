@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title ARIA Desktop

REM ============================================================
REM  start_aria.bat — Единый запуск ARIA Desktop
REM  Открывает нативное Tauri окно (dev mode) + бэкенд
REM ============================================================

set "ROOT=%~dp0"
set "BACKEND_DIR=%ROOT%backend"
set "DESKTOP_DIR=%ROOT%desktop"
set "VENV_DIR=%BACKEND_DIR%\.venv"
set "LOG_DIR=%ROOT%logs"

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

echo === ARIA Desktop Launcher ===
echo.

REM ── 1. Убить старые процессы на порту 8765 ──
echo [1/5] Чистим порт 8765...
for /f "tokens=5" %%a in ('netstat -ano ^| find ":8765" ^| find "LISTENING"') do (
    taskkill /f /pid %%a >nul 2>&1
)
timeout /t 1 /nobreak >nul

REM ── 2. Запустить бэкенд ──
echo [2/5] Запускаем бэкенд...
start "ARIA Backend" /B "%VENV_DIR%\Scripts\python.exe" "%BACKEND_DIR%\run_backend.py" > "%LOG_DIR%\backend.log" 2>&1

REM ── 3. Ждать готовности бэкенда ──
echo [3/5] Ждём бэкенд...
set "WAIT_COUNT=0"
:health_check
timeout /t 2 /nobreak >nul
set /a WAIT_COUNT+=1
if %WAIT_COUNT% gtr 15 (
    echo [ERROR] Бэкенд не стартанул за 30 секунд
    exit /b 1
)
curl -s --max-time 2 http://127.0.0.1:8765/health >nul 2>&1
if errorlevel 1 goto health_check
echo [OK] Бэкенд online

REM ── 4. Запустить Vite dev server ──
echo [4/5] Запускаем Vite dev server...
start "ARIA Vite" /B cmd /c "cd /d %DESKTOP_DIR% && npx vite --port 1420" > "%LOG_DIR%\vite.log" 2>&1

timeout /t 4 /nobreak >nul

REM ── 5. Запустить Tauri desktop ──
echo [5/5] Запускаем Tauri Desktop...

REM Используем production бинарник, если есть
if exist "%DESKTOP_DIR%\src-tauri\target\release\local-agent-ui.exe" (
    set "ARIA_BACKEND_URL=http://127.0.0.1:8765"
    start "" "%DESKTOP_DIR%\src-tauri\target\release\local-agent-ui.exe"
    goto done
)

REM Иначе dev mode
cd /d "%DESKTOP_DIR%"
set "LOCAL_AGENT_BACKEND_EXE=%BACKEND_DIR%\run_backend.py"
set "LOCAL_AGENT_BACKEND_PYTHON=%VENV_DIR%\Scripts\python.exe"
start "ARIA Tauri Dev" cmd /c "npx tauri dev" > "%LOG_DIR%\desktop.log" 2>&1

:done
echo.
echo === ARIA Desktop запущен! ===
echo Бэкенд: http://127.0.0.1:8765
echo Vite:   http://localhost:1420
echo Логи:   %LOG_DIR%
echo.
echo Чтобы остановить: start_aria.bat stop
exit /b 0

:stop
echo === Останавливаем ARIA ===
taskkill /f /im local-agent-ui.exe >nul 2>&1
taskkill /f /im node.exe >nul 2>&1
for /f "tokens=5" %%a in ('netstat -ano ^| find ":8765" ^| find "LISTENING"') do (
    taskkill /f /pid %%a >nul 2>&1
)
for /f "tokens=5" %%a in ('netstat -ano ^| find ":1420" ^| find "LISTENING"') do (
    taskkill /f /pid %%a >nul 2>&1
)
echo [OK] Всё остановлено
exit /b 0
