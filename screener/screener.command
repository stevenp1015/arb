#!/bin/sh
# Double-click this to open the screener.
# Holds an SSH tunnel to the Tokyo box; close the window to shut it down.
PORT=8777

echo "opening tunnel  localhost:$PORT  ->  tokyo:127.0.0.1:$PORT"

if lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; then
  echo "port $PORT already forwarded — reusing it"
  open "http://localhost:$PORT"
  echo "close this window when you're done with the screener."
  while lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; do sleep 5; done
  exit 0
fi

ssh -N -L $PORT:127.0.0.1:$PORT tokyo2 &
SSH_PID=$!
trap 'kill $SSH_PID 2>/dev/null' EXIT INT TERM

n=0
while [ $n -lt 30 ]; do
  if nc -z 127.0.0.1 $PORT 2>/dev/null; then break; fi
  sleep 0.5
  n=$((n+1))
done

if ! nc -z 127.0.0.1 $PORT 2>/dev/null; then
  echo "tunnel failed to come up. is the server running?  ssh tokyo2 /root/screener/run.sh status"
  read -r _
  exit 1
fi

open "http://localhost:$PORT"
echo "screener is at http://localhost:$PORT"
echo "leave this window open. closing it drops the tunnel."
wait $SSH_PID
