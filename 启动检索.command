#!/bin/sh
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
  if command -v python3.12 >/dev/null 2>&1; then python3.12 -m venv .venv; else python3 -m venv .venv; fi
fi
.venv/bin/python -m pip install -r requirements.txt || exit 1
exec .venv/bin/python run.py "$@"
