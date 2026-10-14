#!/bin/sh
# Screener control script.  Lives on the Tokyo box at /root/screener/run.sh
# usage: ./run.sh {start|stop|restart|status|log}
set -u

DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$DIR/venv"
PY="$VENV/bin/python"
PORT="${SCREENER_PORT:-8777}"
BIND="${SCREENER_BIND:-127.0.0.1}"
PIDFILE="$DIR/screener.pid"
SUPPID="$DIR/supervisor.pid"
LOG="$DIR/screener.log"

ensure_venv() {
  if [ ! -x "$PY" ]; then
    echo "creating venv..."
    python3 -m venv "$VENV" || exit 1
    "$VENV/bin/pip" install --quiet --upgrade pip >/dev/null 2>&1
    "$VENV/bin/pip" install --quiet websockets || exit 1
  fi
}

running() {
  [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null
}

start() {
  ensure_venv
  if running; then echo "already running (pid $(cat "$PIDFILE"))"; exit 0; fi
  echo "starting on $BIND:$PORT"
  nohup sh -c '
    while true; do
      "'"$PY"'" "'"$DIR"'/server.py" --port '"$PORT"' --bind '"$BIND"' &
      echo $! > "'"$PIDFILE"'"
      wait $!
      echo "--- server exited, restarting in 5s ---"
      sleep 5
    done
  ' >>"$LOG" 2>&1 &
  echo $! > "$SUPPID"
  sleep 2
  running && echo "up. pid $(cat "$PIDFILE"), log $LOG" || { echo "failed, tail of log:"; tail -20 "$LOG"; exit 1; }
}

stop() {
  [ -f "$SUPPID" ] && kill "$(cat "$SUPPID")" 2>/dev/null
  [ -f "$PIDFILE" ] && kill "$(cat "$PIDFILE")" 2>/dev/null
  sleep 1
  [ -f "$PIDFILE" ] && kill -9 "$(cat "$PIDFILE")" 2>/dev/null
  rm -f "$PIDFILE" "$SUPPID"
  echo "stopped"
}

case "${1:-status}" in
  start)   start ;;
  stop)    stop ;;
  restart) stop; sleep 1; start ;;
  status)
    if running; then
      echo "running, pid $(cat "$PIDFILE")"
      wget -q -O - "http://127.0.0.1:$PORT/healthz" 2>/dev/null || \
        curl -s "http://127.0.0.1:$PORT/healthz"
      echo
    else
      echo "not running"
    fi ;;
  log)     tail -f "$LOG" ;;
  *)       echo "usage: $0 {start|stop|restart|status|log}"; exit 1 ;;
esac
