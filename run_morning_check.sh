#!/usr/bin/env bash
# Ручной запуск утренней проверки. Флаги пробрасываются в watch.py:
#   bash run_morning_check.sh --force        # даже если сегодня уже проверяли
#   bash run_morning_check.sh --simulate     # тестовые алерты (помечены [ТЕСТ])
#   bash run_morning_check.sh --no-telegram  # без отправки в Telegram
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
exec python3 scripts/watch.py "$@"
