@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
set "ROOT=%cd%"
set "BACKEND_URL=http://localhost:8000"
set "DASHBOARD_URL=%BACKEND_URL%/docs"

echo.
echo ============================================================
echo    Discord Friend Army
echo    Multi-Account Bot Mimic System
echo ============================================================
echo.

echo [DFA] Checking prerequisites...
where python >nul 2>&1 || (echo [DFA] ERROR: Python is required and was not found in PATH.& goto :fail)

echo [DFA] Installing backend dependencies...
pushd "%ROOT%\backend" || goto :fail
python -m pip install --quiet -r requirements.txt || goto :fail_pop_backend
popd

REM Check if frontend exists and install if so
if exist "%ROOT%\frontend\package.json" (
    where npm >nul 2>&1 || (echo [DFA] WARNING: npm not found, skipping frontend setup.)
    if not errorlevel 1 (
        echo [DFA] Installing frontend dependencies...
        pushd "%ROOT%\frontend" || goto :fail
        npm install --silent || goto :fail_pop_frontend
        if not exist ".env" if exist ".env.example" copy ".env.example" ".env" >nul
        popd
    )
)

echo [DFA] Setup complete.

:menu
echo.
echo ===== Discord Friend Army =====
echo [1] Start application (backend API with token/proxy loading)
echo [2] Start frontend dashboard
echo [3] Start backend + frontend
echo [4] Validate project (backend pytest + frontend lint/build)
echo [5] Open dashboard in browser
echo [6] Full launch (start all + open dashboard)
echo [0] Exit
set /p "choice=Select an option: "

if "%choice%"=="1" call :start_backend & goto :menu
if "%choice%"=="2" call :start_frontend & goto :menu
if "%choice%"=="3" call :start_backend & call :start_frontend & goto :menu
if "%choice%"=="4" call :validate & goto :menu
if "%choice%"=="5" start "" %DASHBOARD_URL% & goto :menu
if "%choice%"=="6" call :start_backend & call :start_frontend & timeout /t 3 >nul & start "" %DASHBOARD_URL% & goto :menu
if "%choice%"=="0" goto :eof
echo [DFA] Invalid option.
goto :menu

:start_backend
echo [DFA] Starting backend via start.py (loads tokens, proxies, config)...
start "DFA Backend" cmd /k "cd /d "%ROOT%" && python start.py"
exit /b 0

:start_frontend
if not exist "%ROOT%\frontend\package.json" (
    echo [DFA] Frontend not found, skipping.
    exit /b 0
)
start "DFA Frontend" cmd /k "cd /d "%ROOT%\frontend" && npm run dev -- --host 127.0.0.1"
exit /b 0

:validate
echo [DFA] Running backend tests...
pushd "%ROOT%\backend" || exit /b 1
python -m pytest || (popd & exit /b 1)
popd
if exist "%ROOT%\frontend\package.json" (
    echo [DFA] Running frontend lint/build...
    pushd "%ROOT%\frontend" || exit /b 1
    npm run lint || (popd & exit /b 1)
    npm run build || (popd & exit /b 1)
    popd
)
echo [DFA] Validation complete.
exit /b 0

:fail_pop_backend
popd
goto :fail

:fail_pop_frontend
popd
goto :fail

:fail
echo.
echo [DFA] Setup failed. Fix the error above and rerun run_application.bat.
pause
exit /b 1
