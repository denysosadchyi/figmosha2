#!/usr/bin/env bash
# Start (or restart) the Figmosha bridge in the background — macOS / Linux / WSL.
#
#   bash start-bridge.sh           # start, or restart if it is already running
#   bash start-bridge.sh --stop    # stop it
#
# Runs inside a detached tmux session when tmux is installed (attach to watch it),
# otherwise as a plain background process. macOS has no tmux out of the box, so
# that fallback is what most Macs get. Log: /tmp/figmosha-bridge.log
#
# Python: ./venv/bin/python if the venv exists, else $FIGMOSHA_PYTHON, else
# python3 (macOS has no `python` command). Port: $FIGMOSHA_PORT, default 8787.
set -u
cd "$(dirname "$0")"

SESSION="figmosha-bridge"
PORT="${FIGMOSHA_PORT:-8787}"
LOG="/tmp/figmosha-bridge.log"
PIDFILE="/tmp/figmosha-bridge-$PORT.pid"

if [ -x ./venv/bin/python ]; then
    PY=./venv/bin/python
elif [ -n "${FIGMOSHA_PYTHON:-}" ]; then
    PY="$FIGMOSHA_PYTHON"
elif command -v python3 >/dev/null 2>&1; then
    PY=python3
else
    PY=python
fi

up() { curl -sf "http://127.0.0.1:$PORT/status" >/dev/null 2>&1; }
# Anything at all listening on the port — a bridge, or some other program.
taken() { (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; }

stop() {
    local was=1
    if command -v tmux >/dev/null 2>&1 && tmux has-session -t "$SESSION" 2>/dev/null; then
        tmux kill-session -t "$SESSION"; was=0
    fi
    if [ -f "$PIDFILE" ]; then
        kill "$(cat "$PIDFILE")" 2>/dev/null && was=0
        rm -f "$PIDFILE"
    fi
    # Wait for the port to free up, so a restart doesn't race the old process.
    for _ in $(seq 1 30); do taken || break; sleep 0.1; done
    if taken; then
        if up; then
            echo "[start-bridge] a bridge not started by this script is serving port $PORT."
        else
            echo "[start-bridge] port $PORT is taken by another program."
        fi
        echo "[start-bridge] see what it is:  lsof -i tcp:$PORT     stop it:  lsof -ti tcp:$PORT | xargs kill"
        echo "[start-bridge] or use another port:  FIGMOSHA_PORT=8788 bash start-bridge.sh"
        exit 1
    fi
    return $was
}

if [ "${1:-}" = "--stop" ]; then
    stop && echo "[start-bridge] stopped" || echo "[start-bridge] was not running"
    exit 0
fi

stop || true   # silent, unless the port is held by something else (then it exits)

if ! "$PY" -c "import aiohttp" 2>/dev/null; then
    echo "[start-bridge] $PY cannot import aiohttp. Set up the venv first:"
    echo "    python3 -m venv venv && ./venv/bin/pip install -r requirements.txt"
    exit 1
fi

# -u: without it Python block-buffers stdout into the log, and plugin
# connect/disconnect events stay invisible exactly when you need them.
if command -v tmux >/dev/null 2>&1; then
    tmux new-session -d -s "$SESSION" "$PY -u bridge.py --port $PORT 2>&1 | tee $LOG"
    how="tmux session '$SESSION' — watch: tmux attach -t $SESSION"
else
    nohup "$PY" -u bridge.py --port "$PORT" >"$LOG" 2>&1 &
    echo $! >"$PIDFILE"
    how="background process $(cat "$PIDFILE") — watch: tail -f $LOG"
fi

# First start of Python + aiohttp can take a few seconds on a cold Mac.
for i in $(seq 1 100); do
    if up; then
        echo "[start-bridge] up on 127.0.0.1:$PORT after $((i * 100))ms ($how)"
        echo "[start-bridge] stop with: bash start-bridge.sh --stop"
        echo "[start-bridge] now run the plugin: Figma > Plugins > Development > Figmosha Bridge"
        exit 0
    fi
    sleep 0.1
done

echo "[start-bridge] FAILED to start within 10s. Log ($LOG):"
cat "$LOG"
exit 1
