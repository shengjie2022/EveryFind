@echo off
cd /d "%~dp0"
if exist .venv\Scripts\python.exe goto install
py -3.12 -m venv .venv
if errorlevel 1 python -m venv .venv
if errorlevel 1 goto failed
:install
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto failed
.venv\Scripts\python.exe run.py %*
if errorlevel 1 goto failed
exit /b 0
:failed
echo Startup failed. Install Python 3.12 for OCR, then retry.
pause
exit /b 1
