#!/usr/bin/env python3
"""Шаг 2. Обогащение портфеля: площадки MOEX, номиналы, купоны, оферты, эмитенты, ключевые слова.

Вход:  data/holdings_raw.json  (из parse_report.py)
Выход: data/portfolio.json     (рабочий список для ежедневного мониторинга)

Без внешних зависимостей: python3 scripts/build_portfolio.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import (DATA, candles, get_json, iss_block, load_json, log, now_msk,  # noqa: E402
                 resolve_board, save_json, security_info)

# Ручные настройки для бумаг, которых нет в торгах MOEX (иностранные/заблокированные)
OVERRIDES = {
    "9988": {
        "class": "foreign_share", "issuer": "Alibaba Group", "yahoo": "9988.HK",
        "currency": "HKD", "place": None, "tradable": True,
        "keywords": ["Alibaba", "Алибаба", "BABA"], "note": "HKEX, хранение Т-ИНВЕСТ ЛАБ",
    },
    "VIPS-RM": {
        "class": "frozen", "issuer": "Vipshop Holdings", "yahoo": "VIPS",
        "currency": "USD", "place": None, "tradable": False,
        "keywords": ["Vipshop", "Випшоп", "VIPS"],
        "note": "ADR, торги на MOEX/СПБ остановлены 29.03.2024 — цена ориентировочная по базовому активу",
    },
    "IE00BD3QFB18": {
        "class": "frozen", "issuer": "FinEx / FXCN", "yahoo": None,
        "currency": "USD", "place": None, "tradable": False,
        "keywords": ["FinEx", "ФинЭкс", "FXCN"],
        "note": "FinEx China ETF, торги приостановлены с 2022; следим за новостями о ликвидации/выплатах",
    },
}

# Эмитент -> ключевые слова для поиска новостей и рейтинговых действий.
# strict=True — слово слишком общее, требуется кредитно/отчётный контекст в той же новости.
ISSUERS = {
    "SBER": ("Сбербанк", ["Сбербанк", "Сбербанку", "Сбербанка", "Сбер "], False, True),
    "BSPB": ("Банк Санкт-Петербург", ["Банк Санкт-Петербург", "Банка Санкт-Петербург", "БСПБ", "BSPB"], False, True),
    "RU000A100N12": ("АФК Система", ["АФК Система", "АФК «Система»", "АФК Системы"], False, True),
    "RU000A1008D7": ("РЖД", ["РЖД", "Российские железные дороги", "Российских железных дорог"], False, True),
    "RU000A105CM4": ("ХК Новотранс", ["Новотранс"], False, True),
    "RU000A105YB1": ("ГК Медси", ["Медси", "Медси "], False, True),
    "RU000A106PW3": ("Новые технологии", ["Новые технологии", "Новых технологий"], True, True),
    "RU000A106P06": ("Восточная стивидорная компания (ВСК)", ["Восточная стивидорная", "Восточной стивидорной", "Global Ports", "Глобал Портс"], False, True),
    "RU000A106YH6": ("Газпромбанк", ["Газпромбанк", "Газпромбанка", "ГПБ"], False, True),
    "RU000A108G70": ("НОВАТЭК", ["НОВАТЭК", "Новатэк", "Novatek"], False, True),
    "RU000A108L81": ("Полюс", ["Полюс", "Polyus"], False, True),
    "RU000A10AXW4": ("СИБУР Холдинг", ["СИБУР", "Сибур", "SIBUR"], False, True),
    "RU000A101RZ3": ("Республика Казахстан", ["Казахстан", "Казахстана"], True, False),
    "TMON@": ("Т-Капитал (денежный рынок)", ["Т-Капитал", "Т-Банк", "ТБанк"], True, False),
}
# Коды СПБ Биржи с '@' на MOEX пишутся без него
SECID_ALIASES = {"TMON@": "TMON", "TGLD@": "TGLD", "TBRU@": "TBRU", "TMOS@": "TMOS"}

CONTEXT_WORDS = ["облигац", "бонд", "купон", "оферт", "рейтинг", "эмитент", "дефолт",
                 "размещен", "МСФО", "отчет", "отчёт", "прибыл", "выручк", "EBITDA", "долг"]


def classify(raw: dict, place: dict | None, info: dict) -> str:
    secid = raw["secid"]
    if secid in OVERRIDES:
        return OVERRIDES[secid]["class"]
    if place and place["market"] == "bonds":
        if secid.startswith("SU") or "МИНИСТЕРСТВО ФИНАНСОВ" in (info.get("ISSUERNAME") or "").upper():
            return "ofz"
        if "иностр" in raw["name"].lower() or "Казахстан" in raw["name"]:
            return "foreign_sovereign"
        return "corp_bond"
    if place and place["market"] == "shares":
        low = raw["name"].lower()
        if "бпиф" in low or "etf" in low or "паи" in low:
            return "fund_mm" if "денрынок" in low.replace(" ", "") else "fund"
        return "share"
    return "unknown"


def main() -> None:
    raw = load_json(DATA / "holdings_raw.json")
    if not raw:
        sys.exit("Нет data/holdings_raw.json — сначала запустите scripts/parse_report.py")

    positions = []
    for item in raw["positions"]:
        secid = item["secid"]
        moex_secid = SECID_ALIASES.get(secid, secid)
        ov = OVERRIDES.get(secid, {})
        place = ov.get("place", None) if secid in OVERRIDES else resolve_board(moex_secid)
        info = security_info(moex_secid, place) if place else {}
        cls = classify(item, place, info)
        issuer, keywords, strict, earnings_watch = ISSUERS.get(
            secid, (item["name"], [item["name"]], True, cls in ("share", "corp_bond"))
        )
        if secid in OVERRIDES:
            issuer = ov.get("issuer", issuer)
            keywords = ov.get("keywords", keywords)
            strict, earnings_watch = False, True

        pos = {
            "secid": moex_secid,
            "secid_report": secid,
            "isin": item["isin"],
            "name_report": item["name"],
            "name": info.get("SHORTNAME") or info.get("SECNAME") or item["name"],
            "full_name": info.get("SECNAME") or item["name"],
            "qty": item["qty"],
            "class": cls,
            "place": place,
            "depository": item["depository"],
            "issuer": issuer,
            "keywords": keywords,
            "keyword_strict": strict,
            "earnings_watch": earnings_watch,
            "rating_watch": cls not in ("ofz", "fund_mm", "fund"),
            "yahoo": ov.get("yahoo"),
            "tradable": ov.get("tradable", bool(place)),
            "note": ov.get("note"),
            "currency": (info.get("CURRENCYID") or info.get("FACEUNIT") or ov.get("currency") or "RUB"),
        }
        if pos["currency"] in ("SUR", "RUR"):
            pos["currency"] = "RUB"

        if cls in ("ofz", "corp_bond", "foreign_sovereign"):
            pos.update({
                "face_value": info.get("FACEVALUE"),
                "face_unit": info.get("FACEUNIT"),
                "coupon_value": info.get("COUPONVALUE"),
                "coupon_percent": info.get("COUPONPERCENT"),
                "coupon_period_days": info.get("COUPONPERIOD"),
                "next_coupon": info.get("NEXTCOUPON"),
                "maturity": info.get("MATDATE"),
                "offer_date": info.get("OFFERDATE") or info.get("PUTOPTIONDATE"),
                "buyback_date": info.get("BUYBACKDATE"),
                "list_level": info.get("LISTLEVEL"),
                "bond_subtype": info.get("BONDSUBTYPE"),
            })
        if cls in ("share", "fund", "fund_mm"):
            pos["lot_size"] = info.get("LOTSIZE")

        # короткая история нужна для расчёта волатильности уже на первом прогоне
        if place:
            hist = candles(moex_secid, place, days=400)
            pos["history_len"] = len(hist)
            pos["history_from"] = hist[0]["end"][:10] if hist else None
        positions.append(pos)
        log(f"обработан {moex_secid:<16} {cls:<17} {pos['name']}")
        time.sleep(0.12)

    portfolio = {
        "generated_at": now_msk().isoformat(timespec="seconds"),
        "source_pdf": raw.get("source_pdf"),
        "report_calc_date": raw.get("report_calc_date"),
        "period": raw.get("period"),
        "broker": "АО «ТБанк»",
        "cash": raw.get("cash", {}),
        "closed_in_period": raw.get("closed_in_period", []),
        "positions": positions,
    }
    save_json(DATA / "portfolio.json", portfolio)

    by_class: dict[str, int] = {}
    for p in positions:
        by_class[p["class"]] = by_class.get(p["class"], 0) + 1
    log(f"готово: {len(positions)} позиций, разбивка {by_class}")
    print(json.dumps(by_class, ensure_ascii=False))


if __name__ == "__main__":
    main()
