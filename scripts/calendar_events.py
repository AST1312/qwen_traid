"""Прогноз ожидаемых событий по эмитентам портфеля.

Источники данных:
1. Рейтинговые действия — извлекаются из state.expert_ra_titles (уже парсится в watch.py)
   + прогноз следующего пересмотра через ~365 дней (правило АКРА/Эксперт РА: раз в год).
2. Раскрытие отчётности — детерминированные сроки по закону:
   - МСФО годовая: до 30 апреля следующего года
   - МСФО полугодовая: до 30 сентября текущего года
   - МСФО Q1: до 31 мая, Q3: до 30 ноября
   - РСБУ годовая: до 31 марта следующего года
   - РСБУ квартальная: 30 дней после квартала (до 30.04 / 31.07 / 31.10 / 31.01)

Запуск: импортируется из watch.py.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

# Месяцы в URL Эксперт РА: jan/feb/mar/.../dec
_MONTH_NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Паттерн URL пресс-релиза Эксперт РА: releases/2026/nov16c
_RE_RELEASE_URL = re.compile(r"/releases/(\d{4})/([a-z]{3})(\d{1,2})")

# Паттерны для отсева технических отзывов (не рейтинговые действия)
_TECHNICAL_PATTERNS = [
    r"отозвал.*?в связи с .*?погашен",
    r"отозвал.*?без подтверждения",
    r"прекращени\w* обращени",
    r"в связи с истечением срока",
]

# Паттерны рейтинговых действий (то, что считаем настоящим событием)
_RATING_ACTION_PATTERNS = [
    r"присвоил.*?рейтинг",
    r"повысил.*?рейтинг",
    r"понизил.*?рейтинг",
    r"подтвердил.*?рейтинг",
    r"установил.*?наблюдение",
    r"снят.*?наблюдение",
    r"пересмотрел.*?рейтинг",
    r"пересмотрел.*?прогноз",
]


def _parse_release_date(url: str) -> date | None:
    """Извлечь дату из URL пресс-релиза Эксперт РА.

    >>> _parse_release_date("https://raexpert.ru/releases/2026/nov16c")
    datetime.date(2026, 11, 16)
    """
    m = _RE_RELEASE_URL.search(url)
    if not m:
        return None
    year = int(m.group(1))
    month = _MONTH_NAMES.get(m.group(2).lower())
    day = int(m.group(3))
    if not month or day < 1 or day > 31:
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _is_technical_review(title: str) -> bool:
    """Определить, является ли отзыв техническим (погашение, истечение срока)."""
    low = title.lower()
    return any(re.search(pat, low) for pat in _TECHNICAL_PATTERNS)


def _is_rating_action(title: str) -> bool:
    """Определить, является ли пресс-релиз рейтинговым действием."""
    low = title.lower()
    return any(re.search(pat, low) for pat in _RATING_ACTION_PATTERNS)


def _issuer_match_in_text(issuer_keywords: list[str], text: str) -> bool:
    """Проверить, упоминается ли эмитент в тексте (по ключевым словам)."""
    low = text.lower()
    for kw in issuer_keywords:
        if not kw:
            continue
        # Границы слов: для аббревиатур строгое совпадение, иначе с окончанием
        esc = re.escape(kw.lower())
        if kw.isupper() or len(kw) <= 4:
            pattern = r"(?<![а-яёa-z0-9])" + esc + r"(?![а-яёa-z0-9])"
        else:
            pattern = r"(?<![а-яёa-z0-9])" + esc + r"[а-яё]{0,3}(?![а-яёa-z0-9])"
        if re.search(pattern, low):
            return True
    return False


def extract_rating_last_dates(
    state: dict,
    positions: list[dict],
) -> dict[str, dict]:
    """Извлечь даты последних рейтинговых действий по эмитентам портфеля.

    Возвращает: {issuer: {last_date: date, last_url: str, last_action: str, source: str}}
    """
    titles = state.get("expert_ra_titles", {}) or {}
    if not titles:
        return {}

    # Собираем события по эмитентам
    issuer_events: dict[str, list[tuple[date, str, str]]] = {}
    for pos in positions:
        if not pos.get("rating_watch"):
            continue
        issuer = pos.get("issuer") or pos.get("name")
        keywords = pos.get("keywords") or ([issuer] if issuer else [])
        if not issuer:
            continue

        for url, title in titles.items():
            if not title or not isinstance(title, str):
                continue
            if _is_technical_review(title):
                continue
            if not _is_rating_action(title):
                continue
            if not _issuer_match_in_text(keywords, title):
                continue

            dt = _parse_release_date(url)
            if dt is None:
                continue

            issuer_events.setdefault(issuer, []).append((dt, url, title))

    # Для каждого эмитента — самая свежая дата
    result: dict[str, dict] = {}
    for issuer, events in issuer_events.items():
        if not events:
            continue
        events.sort(key=lambda x: x[0], reverse=True)
        last_dt, last_url, last_title = events[0]
        result[issuer] = {
            "last_date": last_dt,
            "last_url": last_url,
            "last_action": last_title,
            "source": "Эксперт РА",
        }
    return result


def estimate_next_review(
    last_date: date,
    today: date,
    interval_days: int = 365,
    tolerance_days: int = 30,
) -> date:
    """Прогноз следующей плановой даты пересмотра рейтинга.

    Правило: эмитент обязан проходить пересмотр не реже раза в год.
    Обычно агентство делает пересмотр ~в ту же дату + год.
    """
    candidate = last_date + timedelta(days=interval_days)
    # Если кандидат уже прошёл — сдвигаем на следующий год
    while candidate < today:
        candidate += timedelta(days=365)
    return candidate


def _is_traded_entity(pos: dict) -> bool:
    """Нужно ли следить за отчётностью этой бумаги."""
    if not pos.get("earnings_watch"):
        return False
    cls = pos.get("class")
    # ОФЗ, БПИФ, замороженные — не отчитываются как компании
    return cls in ("share", "corp_bond", "foreign_share")


def estimate_report_dates(
    positions: list[dict],
    today: date,
    horizon_days: int = 180,
) -> list[dict]:
    """Рассчитать ближайшие дедлайны раскрытия отчётности.

    Возвращает список событий вида:
    {"issuer": ..., "event": "МСФО годовая 2026", "date": date(...), "type": "ifrs_annual"}
    """
    year = today.year
    events: list[dict] = []
    seen: set[tuple[str, str]] = set()  # (issuer, event_type)

    for pos in positions:
        if not _is_traded_entity(pos):
            continue
        issuer = pos.get("issuer") or pos.get("name")
        if not issuer:
            continue

        # --- МСФО ---
        # Годовая: до 30 апреля следующего года
        ifrs_annual = date(year + 1, 4, 30)
        if (ifrs_annual - today).days <= horizon_days and (ifrs_annual - today).days >= 0:
            key = (issuer, "ifrs_annual")
            if key not in seen:
                seen.add(key)
                events.append({
                    "issuer": issuer,
                    "event": f"МСФО годовая {year}",
                    "date": ifrs_annual,
                    "type": "ifrs_annual",
                    "note": "срок раскрытия до 30.04",
                })

        # Полугодовая: до 30 сентября текущего года
        ifrs_h1 = date(year, 9, 30)
        if (ifrs_h1 - today).days <= horizon_days and (ifrs_h1 - today).days >= 0:
            key = (issuer, "ifrs_h1")
            if key not in seen:
                seen.add(key)
                events.append({
                    "issuer": issuer,
                    "event": f"МСФО полугодовая {year}",
                    "date": ifrs_h1,
                    "type": "ifrs_h1",
                    "note": "срок раскрытия до 30.09",
                })

        # Q1: до 31 мая, Q3: до 30 ноября (не обязательны, но крупные компании публикуют)
        ifrs_q1 = date(year, 5, 31)
        if (ifrs_q1 - today).days <= horizon_days and (ifrs_q1 - today).days >= 0:
            key = (issuer, "ifrs_q1")
            if key not in seen:
                seen.add(key)
                events.append({
                    "issuer": issuer,
                    "event": f"МСФО Q1 {year}",
                    "date": ifrs_q1,
                    "type": "ifrs_q1",
                    "note": "квартальная отчётность",
                })

        ifrs_q3 = date(year, 11, 30)
        if (ifrs_q3 - today).days <= horizon_days and (ifrs_q3 - today).days >= 0:
            key = (issuer, "ifrs_q3")
            if key not in seen:
                seen.add(key)
                events.append({
                    "issuer": issuer,
                    "event": f"МСФО Q3 {year}",
                    "date": ifrs_q3,
                    "type": "ifrs_q3",
                    "note": "квартальная отчётность",
                })

        # --- РСБУ ---
        # Годовая бухгалтерская: до 31 марта следующего года
        rsbu_annual = date(year + 1, 3, 31)
        if (rsbu_annual - today).days <= horizon_days and (rsbu_annual - today).days >= 0:
            key = (issuer, "rsbu_annual")
            if key not in seen:
                seen.add(key)
                events.append({
                    "issuer": issuer,
                    "event": f"РСБУ годовая {year}",
                    "date": rsbu_annual,
                    "type": "rsbu_annual",
                    "note": "бухгалтерская отчётность",
                })

        # Квартальные РСБУ: через ~30-45 дней после конца квартала
        rsbu_q1 = date(year, 5, 5)
        rsbu_h1 = date(year, 8, 1)
        rsbu_9m = date(year, 11, 1)

        for dt, label, key_suffix in [
            (rsbu_q1, f"РСБУ Q1 {year}", "rsbu_q1"),
            (rsbu_h1, f"РСБУ полугодие {year}", "rsbu_h1"),
            (rsbu_9m, f"РСБУ 9 месяцев {year}", "rsbu_9m"),
        ]:
            if (dt - today).days <= horizon_days and (dt - today).days >= 0:
                key = (issuer, key_suffix)
                if key not in seen:
                    seen.add(key)
                    events.append({
                        "issuer": issuer,
                        "event": label,
                        "date": dt,
                        "type": key_suffix,
                        "note": "бухгалтерская отчётность",
                    })

    events.sort(key=lambda e: e["date"])
    return events


def collect_expected_events(
    positions: list[dict],
    state: dict,
    today: date,
    horizon_days: int = 90,
    review_interval_days: int = 365,
) -> list[dict]:
    """Собрать все ожидаемые события по эмитентам портфеля.

    Возвращает отсортированный список событий:
    [
        {
            "category": "rating_review" | "report_deadline",
            "issuer": str,
            "event": str,          # "Пересмотр рейтинга Эксперт РА"
            "date": date,
            "days_to": int,
            "note": str,
            "source": str,
            "certainty": "confirmed" | "estimated",  # подтверждено или прогноз
        },
        ...
    ]
    """
    result: list[dict] = []

    # --- 1. Прогноз пересмотров рейтингов ---
    last_dates = extract_rating_last_dates(state, positions)
    for issuer, info in last_dates.items():
        next_dt = estimate_next_review(info["last_date"], today, review_interval_days)
        days_to = (next_dt - today).days
        if 0 <= days_to <= horizon_days:
            result.append({
                "category": "rating_review",
                "issuer": issuer,
                "event": "Пересмотр рейтинга Эксперт РА",
                "date": next_dt,
                "days_to": days_to,
                "note": f"прогноз от последней даты ({info['last_date']:%d.%m.%Y})",
                "source": info["source"],
                "certainty": "estimated",
                "last_url": info.get("last_url"),
            })

    # --- 2. Дедлайны отчётности ---
    report_events = estimate_report_dates(positions, today, horizon_days)
    for ev in report_events:
        days_to = (ev["date"] - today).days
        result.append({
            "category": "report_deadline",
            "issuer": ev["issuer"],
            "event": ev["event"],
            "date": ev["date"],
            "days_to": days_to,
            "note": ev.get("note", ""),
            "source": "сроки по закону",
            "certainty": "estimated",
        })

    # Сортировка: сначала по дате, затем по категории (рейтинги — раньше)
    cat_order = {"rating_review": 0, "report_deadline": 1}
    result.sort(key=lambda e: (e["date"], cat_order.get(e["category"], 99)))

    return result


def format_event_for_tg(event: dict) -> str:
    """Форматировать одно событие для Telegram-сообщения.

    Примеры вывода:
      • Сбербанк: пересмотр рейтинга Эксперт РА (прогноз, через 45 дн.)
      • Лукойл: МСФО годовая 2026 (через 120 дн., до 30.04)
    """
    issuer = event["issuer"]
    ev = event["event"]
    days = event["days_to"]
    note = event.get("note", "")
    certainty = event.get("certainty", "estimated")

    cert_label = "прогноз, " if certainty == "estimated" else ""
    time_str = f"через {days} дн." if days > 0 else "сегодня"

    parts = [f"• {issuer}: {ev} ({cert_label}{time_str})"]
    if note:
        parts.append(f" — {note}")

    return "".join(parts)


def build_expected_events_block(
    events: list[dict],
    max_events: int = 10,
) -> str | None:
    """Построить блок «🗓 Ожидаемые события» для Telegram.

    Возвращает строку или None, если событий нет.
    """
    if not events:
        return None

    lines = ["<b>🗓 Ожидаемые события (прогноз):</b>"]
    for ev in events[:max_events]:
        lines.append(format_event_for_tg(ev))

    if len(events) > max_events:
        lines.append(f"<i>…и ещё {len(events) - max_events} событий (см. Markdown-отчёт)</i>")

    return "\n".join(lines)
