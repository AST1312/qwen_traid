#!/usr/bin/env python3
"""Шаг 1. Разбор брокерского отчёта (PDF) -> data/holdings_raw.json

Ищет раздел "3.1 Движение по ценным бумагам инвестора" и берёт ИСХОДЯЩИЙ остаток
(последняя колонка "Плановый исходящий остаток" совпадает с исходящим).
Также вытаскивает остатки денежных средств из раздела 2.

Зависимость: pypdf (только для этого шага). Сам мониторинг (watch.py) — без зависимостей.
Запуск:  python3 scripts/parse_report.py "uploads/Брокерский отчет (...).pdf"
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

ROW = re.compile(
    r"^(?P<name>.+?)\s+(?P<secid>[A-Z0-9@_\-\.]{2,20})\s+"
    r"(?P<isin>[A-Z]{2}[A-Z0-9]{9}[0-9])\s+"
    r"(?P<dep>ТБанк|Т-ИНВЕСТ ЛАБ|[А-ЯЁA-Z][А-ЯЁA-Za-z\- ]{2,20})\s+"
    r"(?P<in>\d+)\s+(?P<credit>\d+)\s+(?P<debit>\d+)\s+(?P<out>\d+)\s+(?P<plan>\d+)\s*$"
)
CASH = re.compile(r"^(RUB|USD|EUR|CNY|HKD|GBP|JPY)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)")


def extract_text(pdf_path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(pdf_path))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def parse(text: str) -> dict:
    holdings, cash, closed = [], {}, []
    for raw in text.splitlines():
        line = " ".join(raw.split())
        m = ROW.match(line)
        if m:
            d = m.groupdict()
            item = {
                "name": d["name"].strip(),
                "secid": d["secid"],
                "isin": d["isin"],
                "depository": d["dep"].strip(),
                "qty_in": int(d["in"]),
                "qty_credit": int(d["credit"]),
                "qty_debit": int(d["debit"]),
                "qty": int(d["out"]),
            }
            (holdings if item["qty"] > 0 else closed).append(item)
            continue
        c = CASH.match(line)
        if c:
            cash[c.group(1)] = float(c.group(3).replace(",", ""))

    # дедупликация по ISIN (строки раздела 3.1 не повторяются, но подстрахуемся)
    uniq = {}
    for h in holdings:
        uniq[h["isin"]] = h
    return {
        "positions": list(uniq.values()),
        "closed_in_period": closed,
        "cash": cash,
    }


def main() -> None:
    pdf = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if pdf is None or not pdf.exists():
        cands = sorted((ROOT / "uploads").glob("*.pdf"))
        if not cands:
            sys.exit("PDF не найден: передайте путь аргументом")
        pdf = cands[-1]
    text = extract_text(pdf)
    (DATA / "report_raw.txt").write_text(text, encoding="utf-8")
    parsed = parse(text)
    parsed["source_pdf"] = pdf.name
    m = re.search(r"Дата расчета:\s*(\d{2}\.\d{2}\.\d{4})", text)
    parsed["report_calc_date"] = m.group(1) if m else None
    m = re.search(r"за период\s*(\d{2}\.\d{2}\.\d{4})\s*-\s*(\d{2}\.\d{2}\.\d{4})", text)
    parsed["period"] = [m.group(1), m.group(2)] if m else None
    DATA.mkdir(exist_ok=True)
    (DATA / "holdings_raw.json").write_text(
        json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"PDF: {pdf.name}")
    print(f"позиций с остатком: {len(parsed['positions'])}, закрыто в периоде: {len(parsed['closed_in_period'])}")
    print(f"деньги: {parsed['cash']}")


if __name__ == "__main__":
    main()
