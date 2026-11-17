#!/usr/bin/env python3
"""Ежедневная утренняя проверка портфеля.

Что делает:
  1. Тянет котировки MOEX ISS (акции, БПИФ, ОФЗ, корп. облигации) и Yahoo (иностранные акции).
  2. Считает движение за день/неделю, отклонение в сигмах, всплеск объёма, близость к 52-нед. максимуму.
  3. По облигациям ловит кредитный стресс: падение цены, скачок доходности и Z-спреда.
  4. Сканирует рейтинговые действия (АКРА, Эксперт РА) и новости (Интерфакс, РБК, Коммерсантъ)
     по эмитентам портфеля: понижение рейтинга / негативный прогноз / слабая отчётность / дефолт.
  5. Формирует отчёт reports/YYYY-MM-DD_morning.md + alerts JSON и отправляет сводку в Telegram.

Запуск: python3 scripts/watch.py [--force] [--no-telegram] [--dry-run]
Зависимости: только стандартная библиотека Python 3.11+.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import (CONFIG, DATA, REPORTS, STATE, candles, fmt_money, fmt_pct, fx_rub,  # noqa: E402
                 get_json, get_text, iss_block, load_json, load_secrets, log, now_msk,
                 moex_trading_date, save_json, security_info, strip_html, tg_request, yahoo_quote)
from calendar_events import build_expected_events_block, collect_expected_events  # noqa: E402

SEV_ORDER = {"CRITICAL": 0, "TAKE_PROFIT": 1, "WARNING": 2, "INFO": 3}
SEV_ICON = {"CRITICAL": "🔴", "TAKE_PROFIT": "🚀", "WARNING": "🟠", "INFO": "🔵"}
SEV_TITLE = {
    "CRITICAL": "Критично: рейтинг / кредитное событие",
    "TAKE_PROFIT": "Аномальный взлёт — повод зафиксировать прибыль",
    "WARNING": "Предупреждение: отчётность / кредитный стресс",
    "INFO": "К сведению",
}

# ---------------------------------------------------------------- паттерны событий
RATING_DOWN = [
    r"понизил[аи]?\s+(?:кредитный\s+)?рейтинг", r"понижени[ея]\s+(?:кредитного\s+)?рейтинг",
    r"рейтинг\s+понижен", r"снизил[аи]?\s+(?:кредитный\s+)?рейтинг", r"снижени[ея]\s+рейтинг",
    r"рейтинг\s+снижен", r"ухудшил[аи]?\s+прогноз", r"негативный\s+прогноз",
    r"прогноз\s+.{0,25}негативн", r"поместил[аи]?\s+.{0,30}под\s+наблюдени",
    r"рейтинг\s+под\s+наблюдени", r"отозвал[аи]?\s+.{0,40}рейтинг", r"рейтинг\s+отозван",
    r"downgrade", r"negative\s+outlook",
]
RATING_UP = [
    r"повысил[аи]?\s+(?:кредитный\s+)?рейтинг", r"рейтинг\s+повышен", r"позитивный\s+прогноз",
    r"подтвердил[аи]?\s+(?:кредитный\s+)?рейтинг", r"присвоил[аи]?\s+(?:кредитный\s+)?рейтинг",
    r"upgrade", r"снят[оа]?\s+.{0,20}наблюдени",
]
CREDIT_EVENT = [
    r"техническ\w*\s+дефолт", r"\bдефолт", r"не\s+исполнил\w*\s+обязательств",
    r"не\s+выплатил\w*\s+купон", r"пропустил\w*\s+купон", r"реструктуризац\w*\s+долг",
    r"банкрот", r"нарушил\w*\s+ковенант", r"просрочк\w*\s+по\s+(?:долг|облигац)",
    r"предбанкротн", r"делистинг",
]
BAD_EARNINGS = [
    r"чист\w*\s+убыт\w*", r"получил\w*\s+убыт\w*", r"убыт\w*\s+(?:вырос|увеличил|составил|в\s+\d)",
    r"прибыл\w*\s+(?:упал|снизил|сократил|сжал|обвалил)", r"(?:упал|снизил|сократил)\w*\s+прибыл",
    r"выручк\w*\s+(?:упал|снизил|сократил)", r"ebitda\s+(?:упал|снизил|сократил)",
    r"снизил\w*\s+прогноз", r"понизил\w*\s+прогноз", r"ухудшил\w*\s+прогноз",
    r"отказ\w*\s+от\s+дивиденд", r"отменил\w*\s+дивиденд", r"не\s+будет\s+дивиденд",
    r"дивиденд\w*\s+не\s+(?:будет|рекоменд)", r"без\s+дивиденд",
    r"сокраща\w*\s+инвестпрограмм", r"списан\w*\s+.{0,20}актив",
]
GOOD_EARNINGS = [
    r"прибыл\w*\s+(?:вырос|увеличил|удвоил)", r"выручк\w*\s+(?:вырос|увеличил)",
    r"рекордн\w*\s+(?:прибыл|выручк|ebitda)", r"рекомендовал\w*\s+дивиденд",
    r"повысил\w*\s+прогноз",
]
CONTEXT_WORDS = ["облигац", "купон", "оферт", "рейтинг", "эмитент", "дефолт", "размещен",
                 "мсфо", "рсбу", "отчет", "отчёт", "прибыл", "выручк", "ebitda", "долг", "дивиденд"]


def match_any(patterns: list[str], text: str) -> str | None:
    low = text.lower()
    for pat in patterns:
        m = re.search(pat, low)
        if m:
            return m.group(0)[:60]
    return None


WORD_EDGE_L = r"(?<![а-яёa-z0-9])"
WORD_EDGE_R = r"(?![а-яёa-z0-9])"
# Технические отзывы рейтинга: погашение выпуска, истечение срока — не кредитное событие
RATING_TECHNICAL = [
    r"в связи с .{0,40}погашен", r"без подтверждения.{0,60}погашен",
    r"в связи с истечением", r"в связи с окончанием срока", r"прекращени\w* обращения",
]
RATING_WITHDRAW_INITIATIVE = [r"по инициативе рейтингуемого", r"по просьбе", r"по инициативе компании"]
ISSUE_LEVEL = [r"рейтинг\w*\s+облигаций", r"рейтинг\w*\s+выпуск", r"рейтинг\w*\s+серии",
               r"рейтинг\w*\s+отдельн"]
RELATED_MARKERS = ["нпф", "страхован", "лизинг", "факторинг", "микрофинанс", "брокер",
                   "управляющая компания", "дочерн", "дзо", "капитал"]


def keyword_spans(pos: dict, text: str) -> list[tuple[int, int, str]]:
    """Ищем ключевые слова по границам слов, иначе 'РЖД' находится внутри 'подтверждения'."""
    low = text.lower()
    spans = []
    for kw in pos["keywords"]:
        kw = kw.strip()
        if not kw:
            continue
        esc = re.escape(kw.lower())
        abbrev = kw.isupper() or len(kw) <= 4
        tail = WORD_EDGE_R if abbrev else r"[а-яё]{0,3}" + WORD_EDGE_R
        for m in re.finditer(WORD_EDGE_L + esc + tail, low):
            spans.append((m.start(), m.end(), kw))
    return spans


def is_related_entity(text: str, spans: list[tuple[int, int, str]]) -> bool:
    """'НПФ Сбербанка', 'Сбербанк Страхование' — это не сам эмитент, а связанная структура."""
    low = text.lower()
    for start, end, _ in spans:
        window = low[max(0, start - 35):end + 35]
        if any(marker in window for marker in RELATED_MARKERS):
            return True
    return False


def issuer_hit(pos: dict, text: str) -> bool:
    spans = keyword_spans(pos, text)
    if not spans:
        return False
    if pos.get("keyword_strict"):
        low = text.lower()
        return any(w in low for w in CONTEXT_WORDS)
    return True


# ---------------------------------------------------------------- метрики по бумаге
def series_metrics(series: list[dict], last: float | None, today: str) -> dict:
    """series — дневные свечи по возрастанию. Последняя может быть внутридневной."""
    closes = [float(r["close"]) for r in series if r.get("close")]
    vols = [float(r.get("volume") or 0) for r in series]
    intraday = bool(series) and series[-1]["end"][:10] == today
    hist = closes[:-1] if intraday else closes[:]
    hist_vols = vols[:-1] if intraday else vols[:]
    cur = last or (closes[-1] if closes else None)
    out = {"price": cur, "prev_close": hist[-1] if hist else None,
           "day_pct": None, "week_pct": None, "month_pct": None,
           "sigma_pct": None, "z": None, "vol_x": None,
           "high_52w": None, "low_52w": None, "from_high_pct": None, "intraday": intraday}
    if cur is None or not hist:
        return out
    out["day_pct"] = (cur / hist[-1] - 1) * 100
    if len(hist) >= 6:
        out["week_pct"] = (cur / hist[-6] - 1) * 100
    if len(hist) >= 22:
        out["month_pct"] = (cur / hist[-22] - 1) * 100
    rets = [(hist[i] / hist[i - 1] - 1) * 100 for i in range(max(1, len(hist) - 20), len(hist))]
    if len(rets) >= 8:
        sd = statistics.pstdev(rets)
        out["sigma_pct"] = sd
        if sd > 0.05:
            out["z"] = out["day_pct"] / sd
    if intraday and hist_vols[-20:]:
        avg = statistics.fmean([v for v in hist_vols[-20:] if v]) if any(hist_vols[-20:]) else 0
        if avg > 0:
            out["vol_x"] = (vols[-1] or 0) / avg
    window = closes[-250:]
    out["high_52w"], out["low_52w"] = max(window), min(window)
    out["from_high_pct"] = (cur / max(window) - 1) * 100
    return out


def days_to(date_str: str | None, today: str) -> int | None:
    if not date_str or date_str in ("0000-00-00",):
        return None
    try:
        d = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
        return (d - datetime.strptime(today, "%Y-%m-%d").date()).days
    except ValueError:
        return None


def collect_position(pos: dict, cfg: dict, fx: dict, prev_state: dict, today: str) -> dict:
    th = cfg["thresholds"]
    row = dict(pos)
    row["alerts"] = []
    row["metrics"] = {}
    row["value_rub"] = None
    place = pos.get("place")

    if pos["class"] == "frozen" or (not place and not pos.get("yahoo")):
        row["status"] = "нет котировок"
        if pos.get("yahoo"):
            q = yahoo_quote(pos["yahoo"], "5d")
            if q:
                row["underlying"] = {"symbol": pos["yahoo"], "price": q["last"], "currency": q["currency"]}
        row["alerts"].append({
            "severity": "INFO", "category": "DATA_GAP", "secid": pos["secid"],
            "text": f"{pos['name']}: котировок нет ({pos.get('note') or 'торги приостановлены'}). "
                    f"Позиция {pos['qty']} шт. исключена из оценки портфеля.",
        })
        return row

    if place:
        info = security_info(pos["secid"], place)
        md = info.get("_marketdata", {})
        yld = info.get("_yields", {})
        hist = candles(pos["secid"], place, days=400)
        last = md.get("LAST") or md.get("LCURRENTPRICE") or info.get("PREVPRICE")
        m = series_metrics(hist, float(last) if last else None, today)
        row["quote_time"] = md.get("UPDATETIME") or md.get("SYSTEMTIME")
        row["trading_status"] = md.get("TRADINGSTATUS")
        row["metrics"] = m
        row["status"] = "ok" if m["price"] else "нет цены"
        if pos["class"] in ("ofz", "corp_bond", "foreign_sovereign"):
            face_ccy = (info.get("FACEUNIT") or "SUR").replace("SUR", "RUB")
            face = float(info.get("FACEVALUE") or pos.get("face_value") or 1000)
            nkd = float(info.get("ACCRUEDINT") or 0)
            price_pct = m["price"]
            row["bond"] = {
                "price_pct": price_pct, "face": face, "face_ccy": face_ccy, "nkd": nkd,
                "yield": md.get("YIELD") or yld.get("YIELD"),
                "yield_prev": info.get("YIELDATPREVWAPRICE"),
                "duration_days": yld.get("DURATION"),
                "zspread_bp": yld.get("ZSPREADBP"),
                "next_coupon": info.get("NEXTCOUPON"), "coupon_value": info.get("COUPONVALUE"),
                "maturity": info.get("MATDATE"), "offer_date": info.get("OFFERDATE"),
            }
            if price_pct:
                unit = face * price_pct / 100 * fx.get(face_ccy, 1.0) + nkd
                row["value_rub"] = unit * pos["qty"]
                row["unit_price_rub"] = unit
            prev = prev_state.get(pos["secid"], {})
            zs_prev = prev.get("zspread_bp")
            row["bond"]["zspread_prev"] = zs_prev
            row["bond"]["zspread_delta"] = (row["bond"]["zspread_bp"] - zs_prev
                                            if zs_prev is not None and row["bond"]["zspread_bp"] is not None else None)
            y_prev = prev.get("yield")
            row["bond"]["yield_delta_bp"] = (round((row["bond"]["yield"] - y_prev) * 100, 1)
                                             if y_prev is not None and row["bond"]["yield"] else None)
        else:
            ccy = pos.get("currency", "RUB")
            if m["price"]:
                row["value_rub"] = m["price"] * pos["qty"] * fx.get(ccy, 1.0)
                row["unit_price_rub"] = m["price"] * fx.get(ccy, 1.0)
    elif pos.get("yahoo"):
        q = yahoo_quote(pos["yahoo"], "1y")
        if not q:
            row["status"] = "нет котировок (Yahoo)"
            return row
        m = series_metrics(q["series"], q["last"], today)
        if not m["prev_close"] and q.get("prev_close"):
            m["prev_close"] = q["prev_close"]
            m["day_pct"] = (m["price"] / q["prev_close"] - 1) * 100 if m["price"] else None
        row["metrics"] = m
        row["status"] = "ok"
        row["currency"] = q["currency"] or pos.get("currency", "USD")
        row["quote_time"] = q.get("exchange")
        if m["price"]:
            row["value_rub"] = m["price"] * pos["qty"] * fx.get(row["currency"], 1.0)
            row["unit_price_rub"] = m["price"] * fx.get(row["currency"], 1.0)

    m = row.get("metrics") or {}
    if not m or m.get("price") is None:
        row["alerts"].append({"severity": "INFO", "category": "DATA_GAP", "secid": pos["secid"],
                              "text": f"{pos['name']}: не удалось получить цену — проверить источник."})
        return row

    name = f"{pos['name']} ({pos['secid']})"
    day, week, z, volx = m.get("day_pct"), m.get("week_pct"), m.get("z"), m.get("vol_x")

    # --- аномальный взлёт (кроме денежного рынка)
    if pos["class"] != "fund_mm":
        if day is not None and day >= th["spike_day_pct"]:
            extra = f", объём ×{volx:.1f} к среднему" if volx and volx >= th["volume_spike_x"] else ""
            row["alerts"].append({
                "severity": "TAKE_PROFIT", "category": "SPIKE_DAY", "secid": pos["secid"],
                "text": f"{name}: {fmt_pct(day)} за день ({m['prev_close']:.2f} → {m['price']:.2f}){extra}. "
                        f"Порог {th['spike_day_pct']}% пройден — рассмотреть фиксацию.",
                "day_pct": day, "week_pct": week, "z": z,
            })
        if week is not None and week >= th["spike_week_pct"]:
            row["alerts"].append({
                "severity": "TAKE_PROFIT", "category": "SPIKE_WEEK", "secid": pos["secid"],
                "text": f"{name}: {fmt_pct(week)} за 5 сессий — недельный порог {th['spike_week_pct']}% пройден. "
                        f"Кандидат на частичную фиксацию.",
                "day_pct": day, "week_pct": week,
            })
        if (z is not None and z >= th["spike_sigma"] and day is not None
                and day >= th["sigma_min_move_pct"]
                and not any(a["category"].startswith("SPIKE") for a in row["alerts"])):
            row["alerts"].append({
                "severity": "TAKE_PROFIT", "category": "SPIKE_SIGMA", "secid": pos["secid"],
                "text": f"{name}: движение {fmt_pct(day)} = {z:.1f}σ от обычной волатильности "
                        f"(σ≈{m['sigma_pct']:.2f}%/день) — статистическая аномалия.",
                "day_pct": day, "z": z,
            })
        if (m.get("from_high_pct") is not None and m["from_high_pct"] >= -th["near_52w_high_pct"]
                and week is not None and week >= th["spike_week_pct"] / 2):
            row["alerts"].append({
                "severity": "INFO", "category": "NEAR_HIGH", "secid": pos["secid"],
                "text": f"{name}: у 52-недельного максимума ({m['high_52w']:.2f}), "
                        f"неделя {fmt_pct(week)} — зона для лестницы продаж.",
            })
        if day is not None and day <= -th["big_drop_day_pct"]:
            row["alerts"].append({
                "severity": "WARNING" if th["alert_on_drop"] else "INFO", "category": "BIG_DROP",
                "secid": pos["secid"],
                "text": f"{name}: {fmt_pct(day)} за день — резкое падение, проверить новости эмитента.",
            })

    # --- кредитный стресс по облигациям
    b = row.get("bond")
    if b:
        if day is not None and day <= -th["bond_price_drop_day_pct"]:
            row["alerts"].append({
                "severity": "WARNING", "category": "CREDIT_STRESS", "secid": pos["secid"],
                "text": f"{name}: цена {fmt_pct(day)} за день ({b['price_pct']:.2f}% номинала), "
                        f"доходность {b['yield'] or '—'}%. Возможный кредитный стресс — проверить рейтинг и новости.",
            })
        if week is not None and week <= -th["bond_price_drop_week_pct"]:
            row["alerts"].append({
                "severity": "WARNING", "category": "CREDIT_STRESS_WEEK", "secid": pos["secid"],
                "text": f"{name}: {fmt_pct(week)} за неделю по цене — расширенный спред, проверить эмитента.",
            })
        if b.get("zspread_delta") is not None and b["zspread_delta"] >= th["zspread_jump_bp"]:
            row["alerts"].append({
                "severity": "WARNING", "category": "ZSPREAD_JUMP", "secid": pos["secid"],
                "text": f"{name}: Z-спред +{b['zspread_delta']:.0f} б.п. за сутки "
                        f"({b['zspread_prev']:.0f} → {b['zspread_bp']:.0f}) — рынок закладывает риск эмитента.",
            })
        if b.get("yield_delta_bp") is not None and b["yield_delta_bp"] >= th["bond_yield_jump_bp"]:
            row["alerts"].append({
                "severity": "WARNING", "category": "YIELD_JUMP", "secid": pos["secid"],
                "text": f"{name}: доходность +{b['yield_delta_bp']:.0f} б.п. за сутки до {b['yield']:.2f}%.",
            })
        for label, key in (("оферта", "offer_date"), ("погашение", "maturity"), ("купон", "next_coupon")):
            d = days_to(b.get(key), today)
            if d is not None and 0 <= d <= th["calendar_lookahead_days"]:
                row["alerts"].append({
                    "severity": "INFO", "category": "CALENDAR", "secid": pos["secid"],
                    "text": f"{name}: {label} {b[key]} (через {d} дн.)"
                            + (f", купон {b['coupon_value']} {b.get('face_ccy','RUB')} на бумагу"
                               if label == "купон" and b.get("coupon_value") else ""),
                })
    return row


# ---------------------------------------------------------------- новости и рейтинги
def fetch_rss(url: str) -> list[dict]:
    txt = get_text(url)
    if not txt:
        return []
    try:
        root = ET.fromstring(txt.encode("utf-8", "replace"))
    except ET.ParseError:
        try:
            root = ET.fromstring(re.sub(r"&(?!amp;|lt;|gt;|quot;|apos;|#)", "&amp;", txt))
        except ET.ParseError:
            log(f"RSS parse fail {url}")
            return []
    items = []
    for it in root.iter("item"):
        title = strip_html(it.findtext("title") or "")
        link = (it.findtext("link") or "").strip()
        desc = strip_html(it.findtext("description") or "")[:400]
        pub = it.findtext("pubDate") or ""
        items.append({"title": title, "link": link, "summary": desc, "published": pub, "source": urllib.parse.urlparse(url).netloc})
    return items


def fetch_acra() -> list[dict]:
    html = get_text("https://www.acra-ratings.ru/press-releases/", insecure=True)
    if not html:
        return []
    items = []
    for m in re.finditer(r'<a class="item__title" href="(/press-releases/\d+/)">\s*(.*?)\s*</a>', html, re.S):
        items.append({"title": strip_html(m.group(2)), "link": "https://www.acra-ratings.ru" + m.group(1),
                      "summary": "", "published": "", "source": "АКРА"})
    return items


def fetch_expert_ra(days: int = 2, cache: dict | None = None) -> list[dict]:
    """Список релизов Эксперт РА рендерится JS-ом, но страницы релизов отдают <title>.
    Перебираем URL вида /releases/2026/nov12a по датам последних дней."""
    cache = cache if cache is not None else {}
    items = []
    fetched = 0
    for back in range(days):
        day = now_msk() - timedelta(days=back)
        stem = f"{day.year}/{day.strftime('%b').lower()}{day.day:02d}"
        misses = 0
        for letter in "abcdefghijklmn":
            url = f"https://raexpert.ru/releases/{stem}{letter}"
            if url in cache:
                title = cache[url]
            else:
                if fetched >= 24:
                    break
                html = get_text(url, timeout=15, retries=0)
                fetched += 1
                title = None
                if html:
                    mt = re.search(r"<title>(.*?)</title>", html, re.S)
                    if mt:
                        title = strip_html(mt.group(1)).split("|")[0].strip()
                if title:
                    cache[url] = title
                time.sleep(0.1)
            if not title:
                misses += 1
                if misses >= 4:
                    break
                continue
            misses = 0
            items.append({"title": title, "link": url, "summary": "", "published": day.strftime("%Y-%m-%d"), "source": "Эксперт РА"})
    return items


def news_age_days(item: dict) -> float | None:
    pub = item.get("published") or ""
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(pub.strip(), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=now_msk().tzinfo)
            return (now_msk() - dt).total_seconds() / 86400
        except ValueError:
            continue
    return None


def scan_news(portfolio: dict, cfg: dict, state: dict) -> tuple[list[dict], list[dict], dict]:
    th, src = cfg["thresholds"], cfg["sources"]
    items: list[dict] = []
    errors: list[str] = []
    if src.get("acra"):
        got = fetch_acra()
        items += got
        if not got:
            errors.append("АКРА: не удалось получить пресс-релизы")
    if src.get("expert_ra"):
        cache = state.get("expert_ra_titles", {})
        got = fetch_expert_ra(days=2, cache=cache)
        state["expert_ra_titles"] = {k: v for k, v in list(cache.items())[-400:]}
        items += got
        if not got:
            errors.append("Эксперт РА: релизов за последние 2 дня не найдено")
    for url in src.get("rss", []):
        got = fetch_rss(url)
        items += got
        if not got:
            errors.append(f"RSS недоступен: {url}")

    seen = state.get("seen_news", {})
    ttl = th["dedup_ttl_days"]
    today = now_msk().strftime("%Y-%m-%d")
    cutoff = (now_msk() - timedelta(days=ttl)).strftime("%Y-%m-%d")
    seen = {k: v for k, v in seen.items() if v >= cutoff}

    alerts, relevant = [], []
    for it in items:
        age = news_age_days(it)
        if age is not None and age > th["news_lookback_days"]:
            continue
        text = f"{it['title']} {it.get('summary','')}"
        matched = [p for p in portfolio["positions"] if issuer_hit(p, text)]
        if not matched:
            continue
        down = match_any(RATING_DOWN, text)
        credit = match_any(CREDIT_EVENT, text)
        bad = match_any(BAD_EARNINGS, text)
        up = match_any(RATING_UP, text)
        good = match_any(GOOD_EARNINGS, text)
        if not any([down, credit, bad, up, good]):
            continue
        key = hashlib.sha1((it["title"][:160] + it["link"]).encode("utf-8")).hexdigest()[:16]
        issuers = ", ".join(sorted({p["issuer"] for p in matched}))
        secids = sorted({p["secid"] for p in matched})
        spans = [s for p in matched for s in keyword_spans(p, text)]
        related = is_related_entity(text, spans)
        technical = bool(match_any(RATING_TECHNICAL, text))
        initiative = bool(match_any(RATING_WITHDRAW_INITIATIVE, text))
        issue_only = bool(match_any(ISSUE_LEVEL, text))
        entry = {**it, "issuers": issuers, "secids": secids, "related_entity": related,
                 "technical": technical, "issue_level": issue_only,
                 "trigger": down or credit or bad or up or good, "hash": key, "fresh": key not in seen}
        relevant.append(entry)
        if key in seen:
            continue
        if credit:
            sev, cat = "CRITICAL", "CREDIT_EVENT"
        elif down:
            sev, cat = "CRITICAL", "RATING_DOWNGRADE"
        elif bad:
            sev, cat = "WARNING", "BAD_EARNINGS"
        elif up:
            sev, cat = "INFO", "RATING_UP"
        else:
            sev, cat = "INFO", "GOOD_EARNINGS"

        notes = []
        if down and technical:
            sev, cat = "INFO", "RATING_TECHNICAL"
            notes.append("технический отзыв (погашение/истечение срока), не кредитный риск")
        elif down and initiative:
            sev = "WARNING"
            notes.append("отзыв по инициативе эмитента — уточнить причину")
        if related and sev in ("CRITICAL", "WARNING"):
            sev = "WARNING" if sev == "CRITICAL" else "INFO"
            notes.append("связанная структура, не сам эмитент")
        elif related:
            notes.append("связанная структура")
        if issue_only and sev == "CRITICAL":
            sev = "WARNING"
            notes.append("рейтинг отдельного выпуска, а не эмитента")

        alert_text = f"{issuers}: {it['title']}"
        if notes:
            alert_text += " [" + "; ".join(notes) + "]"
        alerts.append({"severity": sev, "category": cat, "secid": ",".join(secids),
                       "text": alert_text, "link": it["link"], "source": it["source"],
                       "trigger": entry["trigger"], "notes": notes})
        seen[key] = today
    state["seen_news"] = seen
    return alerts, relevant, {"items_scanned": len(items), "matched": len(relevant), "errors": errors}


# ---------------------------------------------------------------- Telegram
def tg_send(text: str, secrets: dict, cfg: dict) -> tuple[bool, str]:
    token, chat = secrets.get("TG_BOT_TOKEN"), secrets.get("TG_CHAT_ID")
    if not token or not chat:
        return False, "Telegram не настроен (нет TG_BOT_TOKEN / TG_CHAT_ID в config/secrets.env)"
    limit = cfg["delivery"]["max_tg_chars"]
    chunks, buf = [], ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > limit:
            chunks.append(buf)
            buf = line
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        chunks.append(buf)
    ok_all, msgs = True, []
    proxy = secrets.get("TG_PROXY", "")
    for i, chunk in enumerate(chunks):
        payload = {"chat_id": chat, "text": chunk, "parse_mode": "HTML", "disable_web_page_preview": True}
        if secrets.get("TG_TOPIC_ID"):
            payload["message_thread_id"] = secrets["TG_TOPIC_ID"]
        data = urllib.parse.urlencode(payload).encode()
        try:
            raw = tg_request(f"https://api.telegram.org/bot{token}/sendMessage",
                             data=data, proxy_url=proxy)
            resp = json.loads(raw.decode())
            if not resp.get("ok"):
                ok_all = False
                msgs.append(str(resp.get("description"))[:150])
        except Exception as exc:  # noqa: BLE001
            ok_all = False
            msgs.append(f"{type(exc).__name__}: {str(exc)[:150]}")
        time.sleep(0.4)
    return ok_all, "; ".join(msgs) if msgs else f"отправлено {len(chunks)} сообщ."


def html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------- отчёты
def build_report(rows: list[dict], alerts: list[dict], news_meta: dict, cfg: dict,
                 fx: dict, portfolio: dict, today: str, mode: str,
                 expected_events: list[dict] | None = None,
                 data_quality_ok: bool = True) -> tuple[str, str, dict]:
    total = sum(r["value_rub"] for r in rows if r.get("value_rub"))
    cash_rub = sum(v * fx.get(k, 1.0) for k, v in portfolio.get("cash", {}).items())
    prev_total = load_json(STATE / "portfolio_value.json", {}).get("total_rub")
    # day_abs показываем только если данные надёжные — иначе «падение» это артефакт сбоя
    day_abs = (total - prev_total) if (prev_total and data_quality_ok) else None

    by_sev: dict[str, list[dict]] = {}
    for a in alerts:
        by_sev.setdefault(a["severity"], []).append(a)

    cls_group = {
        "Акции и фонды": ["share", "fund", "fund_mm", "foreign_share"],
        "Корпоративные облигации": ["corp_bond"],
        "ОФЗ и суверенные": ["ofz", "foreign_sovereign"],
        "Без котировок / заблокировано": ["frozen", "unknown"],
    }
    md, tg = [], []
    stamp = now_msk().strftime("%d.%m.%Y %H:%M MSK")
    md.append(f"# Утренняя проверка портфеля — {today}")
    md.append(f"\n_{stamp} · режим: {mode} · брокер: {portfolio.get('broker')} · остатки на {portfolio.get('period',['',''])[1]}_")
    md.append(f"\n**Оценка портфеля: {fmt_money(total)}** + деньги {fmt_money(cash_rub)}"
              + (f" · изменение к прошлой проверке: {fmt_money(day_abs)}" if day_abs is not None else ""))
    tg.append(f"<b>📋 Портфель · {stamp}</b>")
    tg.append(f"Оценка: <b>{fmt_money(total)}</b>" + (f" ({fmt_money(day_abs)} к прошлой проверке)" if day_abs is not None else ""))

    if not data_quality_ok:
        valid_count = sum(1 for r in rows if r.get("value_rub") is not None)
        total_count = len(rows)
        warn_msg = (f"⚠️ <b>Внимание:</b> получены цены только по {valid_count}/{total_count} позиций. "
                    f"Оценка может быть неточной. История изменений не обновлена.")
        tg.append(f"\n{warn_msg}")
        md.append(f"\n**⚠️ Внимание:** получены цены только по {valid_count}/{total_count} позиций. "
                  f"Оценка может быть неточной. История изменений не обновлена.")

    hot = [a for a in alerts if a["severity"] in ("CRITICAL", "TAKE_PROFIT", "WARNING")]
    if hot:
        tg.append(f"\nСобытий, требующих решения: <b>{len(hot)}</b>")
        for a in sorted(hot, key=lambda x: SEV_ORDER[x["severity"]])[:12]:
            tg.append(f"{SEV_ICON[a['severity']]} {html_escape(a['text'])}")
            if a.get("link"):
                tg.append(f'   <a href="{a["link"]}">{html_escape(a.get("source","источник"))}</a>')
    else:
        tg.append("\n✅ Триггеров нет: рейтинговых понижений, слабой отчётности и аномальных взлётов не найдено.")

    for sev in ("CRITICAL", "TAKE_PROFIT", "WARNING", "INFO"):
        group = by_sev.get(sev, [])
        if not group:
            continue
        md.append(f"\n## {SEV_ICON[sev]} {SEV_TITLE[sev]} ({len(group)})")
        for a in group:
            link = f" — [{a.get('source','источник')}]({a['link']})" if a.get("link") else ""
            md.append(f"- {a['text']}{link}")

    # --- Всегда: топ роста и топ падения (независимо от порогов)
    movers = [r for r in rows if (r.get("metrics") or {}).get("day_pct") is not None]
    top_gainers = sorted(movers, key=lambda r: r["metrics"]["day_pct"], reverse=True)[:3]
    top_losers  = sorted(movers, key=lambda r: r["metrics"]["day_pct"])[:3]
    top_gainers = [r for r in top_gainers if (r["metrics"]["day_pct"] or 0) > 0]
    top_losers  = [r for r in top_losers  if (r["metrics"]["day_pct"] or 0) < 0]
    if top_gainers:
        tg.append("\n<b>🚀 Лидеры роста:</b>")
        for r in top_gainers:
            m = r["metrics"]
            tg.append(f"• {html_escape(r['name'])}: {fmt_pct(m['day_pct'])}"
                      + (f" (неделя {fmt_pct(m['week_pct'])})" if m.get("week_pct") is not None else ""))
    if top_losers:
        tg.append("\n<b>📉 Лидеры падения:</b>")
        for r in top_losers:
            m = r["metrics"]
            tg.append(f"• {html_escape(r['name'])}: {fmt_pct(m['day_pct'])}"
                      + (f" (неделя {fmt_pct(m['week_pct'])})" if m.get("week_pct") is not None else ""))
    if not top_gainers and not top_losers:
        tg.append("\n<i>Движения по портфелю за день нет.</i>")

    # --- Календарь облигаций: погашение, оферта, купон (всегда, независимо от алертов)
    cal_lookahead = cfg["thresholds"].get("calendar_lookahead_days", 30)
    calendar_by_sec: dict[str, dict] = {}
    for r in rows:
        b = r.get("bond")
        if not b:
            continue
        for label, key in (("купон", "next_coupon"), ("оферта", "offer_date"), ("погашение", "maturity")):
            d = days_to(b.get(key), today)
            if d is not None and 0 <= d <= cal_lookahead:
                extra = ""
                if label == "купон" and b.get("coupon_value"):
                    extra = f", {b['coupon_value']:.2f} {b.get('face_ccy','RUB')}"
                calendar_by_sec.setdefault(r["secid"], {"name": r["name"], "events": []})
                calendar_by_sec[r["secid"]]["events"].append(
                    {"days": d, "label": label, "date": b[key], "extra": extra})
    if calendar_by_sec:
        sorted_secs = sorted(calendar_by_sec.items(),
                             key=lambda kv: min(e["days"] for e in kv[1]["events"]))
        tg.append(f"\n<b>📅 Ближайшие события ({cal_lookahead} дн.):</b>")
        for secid, data in sorted_secs[:10]:
            evts = sorted(data["events"], key=lambda e: e["days"])
            evt_strs = [f"{e['label']} {e['date']} (через {e['days']} дн.){e['extra']}" for e in evts]
            tg.append(f"• {html_escape(data['name'])}: {'; '.join(evt_strs)}")
        # Также в Markdown-отчёт
        md.append(f"\n## 📅 Календарь событий ({cal_lookahead} дн.)")
        for secid, data in sorted_secs:
            evts = sorted(data["events"], key=lambda e: e["days"])
            evt_strs = [f"**{e['label']}** {e['date']} (через {e['days']} дн.){e['extra']}" for e in evts]
            md.append(f"- {data['name']}: {'; '.join(evt_strs)}")

    # --- 🗓 Ожидаемые события (прогноз пересмотров рейтингов и дедлайнов отчётности) ---
    if expected_events:
        tg_block = build_expected_events_block(expected_events)
        if tg_block:
            tg.append(f"\n{tg_block}")

        ratings = [e for e in expected_events if e["category"] == "rating_review"]
        reports = [e for e in expected_events if e["category"] == "report_deadline"]

        md.append("\n## 🗓 Ожидаемые события (прогноз)")
        md.append("_Пересмотры кредитных рейтингов и дедлайны раскрытия отчётности._")

        if ratings:
            md.append("\n### Пересмотры рейтингов")
            for ev in ratings:
                days = ev["days_to"]
                cert = " ⚠️" if ev.get("certainty") == "estimated" else ""
                md.append(f"- **{ev['issuer']}**: {ev['event']} — {ev['date']} "
                          f"(через {days} дн.){cert} — {ev.get('note', '')}")

        if reports:
            md.append("\n### Дедлайны отчётности")
            for ev in reports[:30]:
                days = ev["days_to"]
                md.append(f"- **{ev['issuer']}**: {ev['event']} — {ev['date']} "
                          f"(через {days} дн.)")
            if len(reports) > 30:
                md.append(f"_…и ещё {len(reports) - 30} дедлайнов_")

    for title, classes in cls_group.items():
        group = [r for r in rows if r["class"] in classes]
        if not group:
            continue
        md.append(f"\n## {title}")
        if title == "Корпоративные облигации" or title == "ОФЗ и суверенные":
            md.append("| Бумага | Кол-во | Цена, % ном. | День | Неделя | Доходность | Z-спред, б.п. | Оферта/погашение | Оценка |")
            md.append("|---|---|---|---|---|---|---|---|---|")
            for r in sorted(group, key=lambda x: -(x.get("value_rub") or 0)):
                b, m = r.get("bond", {}), r.get("metrics", {})
                zs = f"{b.get('zspread_bp'):.0f}" if b.get("zspread_bp") is not None else "—"
                if b.get("zspread_delta"):
                    zs += f" ({b['zspread_delta']:+.0f})"
                md.append(f"| {r['name']} | {r['qty']} | {b.get('price_pct') or '—'} | {fmt_pct(m.get('day_pct'))} | "
                          f"{fmt_pct(m.get('week_pct'))} | {b.get('yield') or '—'}% | {zs} | "
                          f"{b.get('offer_date') or b.get('maturity') or '—'} | {fmt_money(r.get('value_rub'))} |")
        elif title == "Без котировок / заблокировано":
            md.append("| Бумага | Кол-во | Статус | Примечание |")
            md.append("|---|---|---|---|")
            for r in group:
                und = r.get("underlying")
                note = r.get("note") or ""
                if und:
                    note += f" · базовый актив {und['symbol']}: {und['price']} {und['currency']}"
                md.append(f"| {r['name_report']} | {r['qty']} | {r.get('status','—')} | {note} |")
        else:
            md.append("| Бумага | Кол-во | Цена | День | Неделя | σ дня | Z | Объём × | От 52н макс | Оценка |")
            md.append("|---|---|---|---|---|---|---|---|---|---|")
            for r in sorted(group, key=lambda x: -(x.get("value_rub") or 0)):
                m = r.get("metrics", {})
                sigma = f"{m['sigma_pct']:.2f}%" if m.get("sigma_pct") else "—"
                zval = f"{m['z']:.1f}" if m.get("z") is not None else "—"
                volx = f"{m['vol_x']:.1f}" if m.get("vol_x") else "—"
                price = f"{m['price']:.2f}" if m.get("price") else "—"
                md.append(f"| {r['name']} | {r['qty']} | {price} {r.get('currency','')} | "
                          f"{fmt_pct(m.get('day_pct'))} | {fmt_pct(m.get('week_pct'))} | "
                          f"{sigma} | {zval} | {volx} | "
                          f"{fmt_pct(m.get('from_high_pct'))} | {fmt_money(r.get('value_rub'))} |")

    md.append("\n## Что проверено")
    md.append(f"- Котировки и история: MOEX ISS + Yahoo Finance, позиций обработано {len(rows)}.")
    md.append(f"- Новостей/релизов просмотрено: {news_meta['items_scanned']}, из них по эмитентам портфеля: {news_meta['matched']}.")
    md.append(f"- Пороги: взлёт ≥{cfg['thresholds']['spike_day_pct']}% за день или ≥{cfg['thresholds']['spike_week_pct']}% за неделю, "
              f"аномалия ≥{cfg['thresholds']['spike_sigma']}σ; облигации: цена −{cfg['thresholds']['bond_price_drop_day_pct']}% за день, "
              f"Z-спред +{cfg['thresholds']['zspread_jump_bp']} б.п.")
    md.append(f"- Курсы: " + ", ".join(f"{k}/RUB {v:.2f}" for k, v in fx.items() if k != "RUB"))
    if news_meta["errors"]:
        md.append("\n**Пробелы в данных:**")
        for e in news_meta["errors"]:
            md.append(f"- {e}")
    if portfolio.get("closed_in_period"):
        md.append("\n_Закрыто в периоде отчёта: " + ", ".join(
            f"{c['name']} ({c['secid']}) {c['qty_in']} шт." for c in portfolio["closed_in_period"]) + " — в мониторинг не входят._")
    md.append("\n---\n_Эвристика по ключевым словам и публичным лентам: возможны пропуски и ложные срабатывания. "
              "Это не инвестиционная рекомендация._")

    summary = {
        "date": today, "generated_at": now_msk().isoformat(timespec="seconds"), "mode": mode,
        "total_rub": total, "cash_rub": cash_rub, "change_vs_prev_rub": day_abs,
        "alerts_count": {k: len(v) for k, v in by_sev.items()},
        "alerts": sorted(alerts, key=lambda a: SEV_ORDER[a["severity"]]),
        "positions": [{k: r.get(k) for k in ("secid", "name", "class", "qty", "status", "value_rub", "metrics", "bond")}
                      for r in rows],
        "news": news_meta,
        "expected_events": [{**ev, "date": ev["date"].isoformat()} for ev in (expected_events or [])],
    }
    return "\n".join(md), "\n".join(tg), summary


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="игнорировать проверку 'уже запускались сегодня'")
    ap.add_argument("--no-telegram", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="не менять состояние и не отправлять")
    ap.add_argument("--simulate", action="store_true",
                    help="тест механики: добавить помеченные [ТЕСТ] алерты (взлёт + понижение рейтинга)")
    args = ap.parse_args()

    cfg = load_json(CONFIG / "config.json")
    portfolio = load_json(DATA / "portfolio.json")
    if not portfolio:
        log("Нет data/portfolio.json — сначала build_portfolio.py")
        return 2
    state = load_json(STATE / "state.json", {})
    today = now_msk().strftime("%Y-%m-%d")
    mode = "ручной запуск" if args.force else "плановая проверка"
    if state.get("last_run_date") == today and not args.force:
        log("Проверка сегодня уже выполнялась — пропуск (запустите с --force).")
        return 0

    if cfg["schedule"].get("skip_moex_holidays") and not args.force:
        trade_date = moex_trading_date()
        if trade_date and trade_date != today:
            log(f"MOEX сегодня не торгует (торговая дата {trade_date}) — проверка пропущена.")
            return 0

    log(f"=== Старт проверки: {len(portfolio['positions'])} позиций, режим '{mode}' ===")
    fx = {"RUB": 1.0}
    for ccy in ("USD", "HKD", "CNY", "EUR"):
        rate = fx_rub(ccy)
        if rate:
            fx[ccy] = rate
    if "HKD" not in fx:  # HKD/RUB на MOEX не торгуется — считаем через USD и привязку HKD
        usdhkd = yahoo_quote("USDHKD=X", "5d")
        if usdhkd and usdhkd.get("last") and fx.get("USD"):
            fx["HKD"] = fx["USD"] / usdhkd["last"]
    log(f"курсы: {fx}")

    prev_metrics = state.get("metrics", {})
    rows = []
    for pos in portfolio["positions"]:
        try:
            row = collect_position(pos, cfg, fx, prev_metrics, today)
        except Exception as exc:  # noqa: BLE001 - одна бумага не должна ронять всю проверку
            log(f"ошибка по {pos['secid']}: {type(exc).__name__}: {exc}")
            row = {**pos, "alerts": [{"severity": "INFO", "category": "DATA_GAP", "secid": pos["secid"],
                                      "text": f"{pos['name']}: ошибка получения данных ({type(exc).__name__})"}],
                   "metrics": {}, "status": "ошибка", "value_rub": None}
        rows.append(row)
        log(f"{pos['secid']:<16} {row.get('status','?'):<12} день {fmt_pct((row.get('metrics') or {}).get('day_pct')):>9} "
            f"оценка {fmt_money(row.get('value_rub'))}")
        time.sleep(0.12)

    # --- 🔒 Проверка качества данных ---
    # Если больше половины позиций не получили цену — данные ненадёжны.
    # В этом случае НЕ сохраняем state и portfolio_value (чтобы не перезаписать
    # корректную историю мусором), и показываем предупреждение в отчёте.
    total_positions = len(portfolio["positions"])
    valid_rows = [r for r in rows if r.get("value_rub") is not None]
    valid_count = len(valid_rows)
    data_quality_ok = valid_count >= max(1, total_positions * 0.5)
    if not data_quality_ok:
        log(f"⚠️  Качество данных низкое: цены получены только по {valid_count}/{total_positions} "
            f"позиций (< 50%). Сохранение state и portfolio_value.json пропущено.")

    news_alerts, relevant, news_meta = scan_news(portfolio, cfg, state)
    alerts = [a for r in rows for a in r.get("alerts", [])] + news_alerts

    # --- 🗓 Ожидаемые события (прогноз пересмотров рейтингов + дедлайны отчётности) ---
    # Собираем после scan_news, чтобы использовать актуальный state.expert_ra_titles
    today_date = now_msk().date()
    try:
        events_horizon = cfg["thresholds"].get("expected_events_lookahead_days", 7)
        expected_events = collect_expected_events(
            portfolio["positions"], state, today_date,
            horizon_days=events_horizon,
        )
        log(f"ожидаемые события: {len(expected_events)} (горизонт {events_horizon} дн.)")
    except Exception as exc:  # noqa: BLE001 — прогноз не должен ронять основную проверку
        log(f"ожидаемые события: ошибка прогноза ({type(exc).__name__}: {exc})")
        expected_events = []

    if args.simulate:
        alerts += [
            {"severity": "TAKE_PROFIT", "category": "SPIKE_DAY", "secid": "SBER",
             "text": "[ТЕСТ] Сбербанк (SBER): +9.40% за день, объём ×5.2 к среднему. "
                     "Порог 8.0% пройден — рассмотреть фиксацию."},
            {"severity": "CRITICAL", "category": "RATING_DOWNGRADE", "secid": "RU000A100N12",
             "text": "[ТЕСТ] АФК Система: АКРА понизило кредитный рейтинг до A+(RU), прогноз «негативный».",
             "link": "https://www.acra-ratings.ru/press-releases/", "source": "АКРА (тестовая запись)"},
            {"severity": "WARNING", "category": "BAD_EARNINGS", "secid": "BSPB",
             "text": "[ТЕСТ] Банк Санкт-Петербург: чистая прибыль по МСФО за квартал снизилась на 34% г/г."},
        ]
        mode += " + ТЕСТОВЫЕ АЛЕРТЫ"
    md, tg, summary = build_report(rows, alerts, news_meta, cfg, fx, portfolio, today, mode,
                                    expected_events=expected_events,
                                    data_quality_ok=data_quality_ok)

    REPORTS.mkdir(exist_ok=True)
    stem = f"{today}_TEST" if args.simulate else f"{today}_morning"
    md_path = REPORTS / f"{stem}.md"
    md_path.write_text(md, encoding="utf-8")
    save_json(REPORTS / f"{stem.replace('_morning','_alerts')}.json", summary)
    if not args.simulate:
        (REPORTS / "latest.md").write_text(md, encoding="utf-8")
        save_json(REPORTS / "latest.json", summary)
    log(f"отчёт: {md_path.relative_to(md_path.parents[1])}")

    sent, msg = False, "отключено флагом"
    if cfg["delivery"]["telegram"] and not args.no_telegram and not args.dry_run:
        hot = [a for a in alerts if a["severity"] in ("CRITICAL", "TAKE_PROFIT", "WARNING")]
        if hot or cfg["delivery"]["send_if_no_alerts"]:
            sent, msg = tg_send(tg, load_secrets(), cfg)
    log(f"Telegram: {msg}")
    summary["telegram"] = {"sent": sent, "message": msg}
    save_json(REPORTS / (f"{today}_TEST.json" if args.simulate else f"{today}_alerts.json"), summary)

    if not args.dry_run:
        state["last_run_date"] = today
        state["last_run_at"] = now_msk().isoformat(timespec="seconds")
        state["metrics"] = {
            r["secid"]: {"yield": (r.get("bond") or {}).get("yield"),
                         "zspread_bp": (r.get("bond") or {}).get("zspread_bp"),
                         "price": (r.get("metrics") or {}).get("price")}
            for r in rows
        }
        # Сохраняем прогноз ожидаемых событий для истории
        state["expected_events"] = [
            {**ev, "date": ev["date"].isoformat()}
            for ev in expected_events
        ]
        save_json(STATE / "state.json", state)
        if data_quality_ok:
            save_json(STATE / "portfolio_value.json", {"date": today, "total_rub": summary["total_rub"]})
        else:
            log("⚠️  portfolio_value.json не обновлён (недостоверные данные).")

    hot = summary["alerts_count"]
    log(f"=== Готово. Алерты: {hot}. Оценка портфеля: {fmt_money(summary['total_rub'])} ===")
    print("\n" + "\n".join(md.split("\n")[:40]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
