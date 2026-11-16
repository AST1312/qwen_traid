#!/usr/bin/env python3
"""Проверка доставки в Telegram: python3 scripts/test_telegram.py [--simulate]"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import CONFIG, load_json, load_secrets, now_msk  # noqa: E402
from watch import tg_send  # noqa: E402


def main() -> int:
    cfg = load_json(CONFIG / "config.json")
    secrets = load_secrets()
    if not secrets.get("TG_BOT_TOKEN") or not secrets.get("TG_CHAT_ID"):
        print("Заполните config/secrets.env (TG_BOT_TOKEN, TG_CHAT_ID). Образец: config/secrets.env.example")
        return 2
    text = (f"<b>✅ Тест доставки портфельных алертов</b>\n"
            f"{now_msk():%d.%m.%Y %H:%M MSK}\n"
            f"Плановая проверка: будни {cfg['schedule']['time']} {cfg['schedule']['timezone']}.\n"
            f"Порог фиксации: +{cfg['thresholds']['spike_day_pct']}% за день / "
            f"+{cfg['thresholds']['spike_week_pct']}% за неделю.\n\n"
            f"Пример алерта:\n🚀 Сбербанк (SBER): +9.4% за день — рассмотреть фиксацию.\n"
            f"🔴 АФК Система: понижение рейтинга до A+(RU), прогноз «негативный».")
    ok, msg = tg_send(text, secrets, cfg)
    print("OK:", msg) if ok else print("ОШИБКА:", msg)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
