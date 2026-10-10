#!/usr/bin/env sh
# Lancement en une commande : ./run.sh   (port modifiable : PORT=8080 ./run.sh)
set -e
cd "$(dirname "$0")"
PORT="${PORT:-8000}"
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
. .venv/bin/activate
pip install -q --disable-pip-version-check -r requirements.txt
( sleep 2; python -m webbrowser "http://127.0.0.1:${PORT}/" >/dev/null 2>&1 || true ) &
exec uvicorn app.main:app --host 127.0.0.1 --port "${PORT}"
