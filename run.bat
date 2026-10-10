@echo off
REM Lancement en une commande sous Windows : double-clic ou "run.bat"
cd /d "%~dp0"
if "%PORT%"=="" set PORT=8000
if not exist .venv (
  py -3 -m venv .venv || python -m venv .venv
)
call .venv\Scripts\activate.bat
pip install -q --disable-pip-version-check -r requirements.txt
start "" http://127.0.0.1:%PORT%/
uvicorn app.main:build --factory --host 127.0.0.1 --port %PORT%
