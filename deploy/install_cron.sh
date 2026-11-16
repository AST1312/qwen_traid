#!/usr/bin/env bash
# Установка cron-задания: будни 10:15 Europe/Moscow.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$(command -v python3)"
LINE="15 10 * * 1-5 cd $ROOT && $PY scripts/watch.py >> logs/cron.log 2>&1"

if ! command -v crontab >/dev/null 2>&1; then
  echo "crontab не найден. Варианты:"
  echo "  * systemd-таймер: deploy/systemd/ (инструкция в README.md)"
  echo "  * процесс-планировщик: python3 scripts/scheduler.py"
  exit 1
fi

mkdir -p "$ROOT/logs"
TMP="$(mktemp)"
crontab -l 2>/dev/null | grep -v "scripts/watch.py" > "$TMP" || true
if crontab -l 2>/dev/null | grep -qi "CRON_TZ"; then
  echo "CRON_TZ уже задан в crontab — оставляю как есть"
else
  echo "CRON_TZ=Europe/Moscow" >> "$TMP"
fi
echo "$LINE" >> "$TMP"
crontab "$TMP"
rm -f "$TMP"
echo "Установлено:"
crontab -l | tail -3
