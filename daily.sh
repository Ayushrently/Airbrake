#!/usr/bin/env bash
# Create today's report once, after the network is up. Run by the systemd timer (see systemd/) or cron.
# Extra arguments are passed to check.py, e.g. ./daily.sh --env team.env
cd "$(dirname "$0")" || exit 1
notify() { command -v notify-send >/dev/null && notify-send "$@"; }
data=$(python3 -c 'import check; print(check.REPORTS)' "$@" 2>/dev/null) || data=reports
today=$(date +%F)
[ -f "$data/$today.json" ] && exit 0
for _ in $(seq 60); do
  curl -s -o /dev/null --max-time 5 https://api.airbrake.io && break
  sleep 10
done
python3 check.py "$@" > /dev/null || { notify -u critical "Airbrake report failed" "Check the logs"; exit 1; }
notify "Airbrake report ready" "$data/$today.html"
