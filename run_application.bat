@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
set "ROOT=%cd%"

echo [DFA] Preparing local environment (non-Docker baseline)...
where python >nul 2>&1 || (echo [DFA] Python is required and was not found in PATH.& goto :fail)
where npm >nul 2>&1 || (echo [DFA] npm is required and was not found in PATH.& goto :fail)

pushd "%ROOT%\backend" || goto :fail
python -m pip install -r requirements.txt || goto :fail_pop_backend
if not exist ".env" copy ".env.example" ".env" >nul
python -c "from app.db.session import Base,engine; import app.models.research; Base.metadata.create_all(bind=engine)" || goto :fail_pop_backend
popd

pushd "%ROOT%\frontend" || goto :fail
npm install || goto :fail_pop_frontend
if not exist ".env" copy ".env.example" ".env" >nul
popd

echo [DFA] Setup complete.

:menu
echo.
echo ===== Discord Friend Army =====
echo [1] Start backend API
echo [2] Start frontend dashboard
echo [3] Start backend + frontend
echo [4] Validate project (backend pytest + frontend lint/build)
echo [5] Open dashboard in browser
echo [6] Agent session helper (start all + open dashboard)
echo [0] Exit
set /p "choice=Select an option: "

if "%choice%"=="1" call :start_backend & goto :menu
if "%choice%"=="2" call :start_frontend & goto :menu
if "%choice%"=="3" call :start_backend & call :start_frontend & goto :menu
if "%choice%"=="4" call :validate & goto :menu
if "%choice%"=="5" start "" http://localhost:5173 & goto :menu
if "%choice%"=="6" call :start_backend & call :start_frontend & timeout /t 2 >nul & start "" http://localhost:5173 & goto :menu
if "%choice%"=="0" goto :eof
echo [DFA] Invalid option.
goto :menu

:start_backend
start "DFA Backend" cmd /k "cd /d ""%ROOT%\backend"" && python -m uvicorn app.main:app --host 0.0.0.0 --port 8000"
exit /b 0

:start_frontend
start "DFA Frontend" cmd /k "cd /d ""%ROOT%\frontend"" && npm run dev -- --host 0.0.0.0"
exit /b 0

:validate
echo [DFA] Running backend tests...
pushd "%ROOT%\backend" || exit /b 1
python -m pytest || (popd & exit /b 1)
popd
echo [DFA] Running frontend lint/build...
pushd "%ROOT%\frontend" || exit /b 1
npm run lint || (popd & exit /b 1)
npm run build || (popd & exit /b 1)
popd
echo [DFA] Validation complete.
exit /b 0

:fail_pop_backend
popd
goto :fail

:fail_pop_frontend
popd
goto :fail

:fail
echo [DFA] Setup failed. Fix the error above and rerun run_application.bat.
exit /b 1
