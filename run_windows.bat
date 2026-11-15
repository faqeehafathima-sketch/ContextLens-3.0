@echo off
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  py -3 -m venv .venv
  if errorlevel 1 goto :error
)
.venv\Scripts\python.exe -m pip install --upgrade pip
if errorlevel 1 goto :error
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto :error
echo.
echo ContextLens is starting. Open http://127.0.0.1:7863
.venv\Scripts\python.exe server.py
if errorlevel 1 goto :error
goto :eof
:error
echo.
echo Setup or startup failed. Read the error above.
pause
exit /b 1
