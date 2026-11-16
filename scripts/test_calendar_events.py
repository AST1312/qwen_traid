#!/usr/bin/env python3
"""Быстрый тест модуля calendar_events на реальных данных.

Запуск: py scripts/test_calendar_events.py
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from calendar_events import (  # noqa: E402
    build_expected_events_block,
    collect_expected_events,
    estimate_report_dates,
    extract_rating_last_dates,
)

ROOT = Path(__file__).resolve().parent.parent
state = json.loads((ROOT / "state" / "state.json").read_text(encoding="utf-8"))
portfolio = json.loads((ROOT / "data" / "portfolio.json").read_text(encoding="utf-8"))
today = date.today()

print("=== Тест 1: extract_rating_last_dates ===")
last_dates = extract_rating_last_dates(state, portfolio["positions"])
print(f"Найдено эмитентов с рейтинговыми действиями: {len(last_dates)}")
for issuer, info in list(last_dates.items())[:8]:
    action_short = (info["last_action"] or "")[:80]
    print(f"  {issuer}: {info['last_date']} | {action_short}...")

print()
print("=== Тест 2: estimate_report_dates ===")
reports = estimate_report_dates(portfolio["positions"], today, horizon_days=180)
print(f"Дедлайнов отчётности: {len(reports)}")
for r in reports[:8]:
    print(f"  {r['issuer']}: {r['event']} — {r['date']}")

print()
print("=== Тест 3: collect_expected_events (горизонт 90 дн.) ===")
events = collect_expected_events(portfolio["positions"], state, today, horizon_days=90)
print(f"Всего ожидаемых событий: {len(events)}")
for ev in events[:10]:
    print(f"  [{ev['category']}] {ev['issuer']}: {ev['event']} — через {ev['days_to']} дн.")

print()
print("=== Тест 4: build_expected_events_block (Telegram) ===")
block = build_expected_events_block(events, max_events=8)
if block:
    print(block)
else:
    print("(блок пустой — событий в горизонте 90 дней нет)")
