#!/bin/bash
# JARVIS native app launcher: voice server (if needed) + Qt GUI
set -m
export DISPLAY="${DISPLAY:-:0}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
PY="/home/tekinaltun/.hermes/tools/python-3.14.7+20260901-linux-x64/bin/python3"
DIR=/home/tekinaltun/jarvis_ai
if [ ! -x "$PY" ]; then
  PY="$(find /home/tekinaltun/.hermes/tools -maxdepth 2 -type d -name 'python*' -print -quit 2>/dev/null)/bin/python3"
fi
if [ ! -x "$PY" ]; then PY=/usr/bin/python3; fi

# 1) voice server on 8765 — start if not listening
if ! ss -tln 2>/dev/null | grep -q ":8765 "; then
  (cd "$DIR/server" && nohup "$PY" server.py >/tmp/jarvis-server.log 2>&1 &)
  # wait up to 30 s for the port
  for _ in $(seq 1 30); do
    ss -tln 2>/dev/null | grep -q ":8765 " && break
    sleep 1
  done
fi

# 2) GUI
# Ensure only one instance runs
pkill -f "jarvis_app.py" || true
exec "$PY" "$DIR/jarvis_app.py"
