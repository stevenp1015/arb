#!/bin/zsh
# Rebuild the screener on a blank box in one go.
#
#   ./deploy_fresh.sh            # rebuilds onto tokyo2
#   ./deploy_fresh.sh someother  # any ssh alias from ~/.ssh/config
#
# Written 2026-10-09 after tokyo2 got wiped. Everything the screener needs is in
# this folder; the box only contributes a Japanese IP (Binance futures answers
# HTTP 451 to US addresses) and a python. What a wipe actually loses is bars.db,
# the on-disk minute history, and that reseeds ~8 hours of it on first boot.
# Stars, notes, marks and the watchlist live in the browser, so they survive.
#
# Safe to re-run. It reinstalls nothing that is already there and restarts the
# server so you are never looking at a stale copy.
set -eu

HOST="${1:-tokyo2}"
HERE="$(cd "$(dirname "$0")" && pwd)"
IP=$(ssh -G "$HOST" | awk '/^hostname /{print $2}')
OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=15)

# A reinstalled box comes back with a new host key, and ssh refuses to talk to
# it while the old one sits in known_hosts. That refusal would also leave the
# desk's tunnel loop redialling into a wall forever, so clear it up front.
ssh-keygen -R "$IP" >/dev/null 2>&1 || true
ssh-keygen -R "$HOST" >/dev/null 2>&1 || true

echo "1/5  python on $HOST ($IP)"
ssh $OPTS "$HOST" 'set -e
if command -v apk >/dev/null 2>&1; then
  apk add --no-cache python3 py3-pip curl >/dev/null
elif command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq && apt-get install -y -qq python3 python3-venv python3-pip curl >/dev/null
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y -q python3 python3-pip curl >/dev/null
else
  echo "no apk, apt-get or dnf on this box; install python3 by hand"; exit 1
fi
mkdir -p /root/screener
echo "     $(python3 --version), $(nproc) core(s), $(df -h / | awk "NR==2{print \$4}") free"'

echo "2/5  copying code"
scp -q $OPTS "$HERE/server.py" "$HERE/index.html" "$HERE/run.sh" "$HERE/README.md" "$HOST:/root/screener/"

echo "3/5  boot hook + firewall"
ssh $OPTS "$HOST" 'set -e
chmod +x /root/screener/run.sh
if [ -d /etc/local.d ] && command -v rc-update >/dev/null 2>&1; then
  printf "#!/bin/sh\n/root/screener/run.sh start\n" > /etc/local.d/screener.start
  chmod +x /etc/local.d/screener.start
  rc-update add local default >/dev/null 2>&1 || true
  echo "     openrc: /etc/local.d/screener.start"
elif command -v systemctl >/dev/null 2>&1; then
  cat > /etc/systemd/system/screener.service <<UNIT
[Unit]
Description=perp screener (/root/screener)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/root/screener/run.sh start
ExecStop=/root/screener/run.sh stop

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable screener.service >/dev/null 2>&1
  echo "     systemd: screener.service enabled"
fi
# The server binds 127.0.0.1 and is reached over the SSH tunnel, so the only
# port the outside world needs is 22. Never open 8777.
if command -v ufw >/dev/null 2>&1; then ufw allow 22/tcp >/dev/null 2>&1 || true; fi
# Alpine ships sshd with AllowTcpForwarding no. The tunnel then connects fine
# and refuses every forward ("administratively prohibited: open failed"), so
# the page just never loads. Hit on 2026-10-09. The tunnel only needs local
# forwarding. A tunnel opened before this change keeps the old rule until it
# reconnects.
if grep -q "^AllowTcpForwarding no" /etc/ssh/sshd_config 2>/dev/null; then
  sed -i "s/^AllowTcpForwarding no$/AllowTcpForwarding local/" /etc/ssh/sshd_config
  (rc-service sshd restart || systemctl restart ssh || systemctl restart sshd) >/dev/null 2>&1 || true
  echo "     sshd: allowed local port forwarding (Alpine blocks it by default)"
fi'

echo "4/5  starting (first start builds the venv, ~30s)"
ssh $OPTS "$HOST" '/root/screener/run.sh restart' | sed 's/^/     /'

echo "5/5  waiting for a live feed"
ssh $OPTS "$HOST" 'for i in $(seq 1 60); do
  h=$(curl -s -m 3 http://127.0.0.1:8777/healthz 2>/dev/null || true)
  code=$(curl -s -m 3 -o /dev/null -w "%{http_code}" http://127.0.0.1:8777/healthz 2>/dev/null || true)
  if [ "$code" = "200" ]; then echo "     healthy: $h"; exit 0; fi
  sleep 3
done
echo "     no live feed after 3 minutes. last 30 log lines:"
tail -30 /root/screener/screener.log; exit 1'

# A tunnel the desk opened before step 3 still carries the box's old "no
# forwarding" rule and will never load the page. desk.command redials in 3s,
# so dropping it is enough. (Hit on both 2026-10-09 and 2026-10-10.)
if pkill -f "L 8777:127.0.0.1:8777 $HOST" 2>/dev/null; then
  echo "     dropped the desk's old tunnel so it redials with the new rules"
fi

echo
echo "done. prices pause ~60s while anchors load. open it with screener.command or desk.command."
