#!/usr/bin/env python3
"""Собирает человекочитаемый список портфеля: portfolio.md (+ portfolio.csv).

Берёт позиции из data/portfolio.json и оценку из reports/latest.json (если есть).
Запуск: python3 scripts/make_portfolio_doc.py
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import DATA, REPORTS, ROOT, fmt_money, load_json, now_msk  # noqa: E402

CLASS_RU = {
    "share": "Акции РФ", "fund": "БПИФ", "fund_mm": "БПИФ денежного рынка",
    "corp_bond": "Корпоративные облигации", "ofz": "ОФЗ",
    "foreign_sovereign": "Суверенные облигации (нерезидент)",
    "foreign_share": "Иностранные акции", "frozen": "Заблокировано / без торгов",
    "unknown": "Не определено",
}
ORDER = ["share", "foreign_share", "fund", "fund_mm", "corp_bond", "ofz", "foreign_sovereign", "frozen", "unknown"]


def main() -> None:
    pf = load_json(DATA / "portfolio.json")
    latest = load_json(REPORTS / "latest.json", {})
    val = {p["secid"]: p for p in latest.get("positions", [])}
    total = sum((p.get("value_rub") or 0) for p in val.values()) or None

    lines = [f"# Портфель — {pf.get('broker')}",
             f"\n_Источник: {pf.get('source_pdf')} · расчётная дата отчёта {pf.get('report_calc_date')} · "
             f"остатки на {(pf.get('period') or ['',''])[1]} · список обновлён {now_msk():%d.%m.%Y %H:%M} MSK_"]
    if total:
        lines.append(f"\n**Оценка по последним котировкам: {fmt_money(total)}** "
                     f"(без заблокированных бумаг) + денежные средства "
                     f"{', '.join(f'{v} {k}' for k, v in pf.get('cash', {}).items() if v)}")
    lines.append(f"\nПозиций: **{len(pf['positions'])}**. "
                 f"Закрыто в периоде отчёта: {', '.join(c['secid'] for c in pf.get('closed_in_period', [])) or '—'}.")

    rows_csv = []
    for cls in ORDER:
        group = [p for p in pf["positions"] if p["class"] == cls]
        if not group:
            continue
        lines.append(f"\n## {CLASS_RU.get(cls, cls)} — {len(group)}")
        if cls in ("corp_bond", "ofz", "foreign_sovereign"):
            lines.append("| Бумага | ISIN | Кол-во | Номинал | Купон | Купон до | Оферта | Погашение | Оценка | Доля |")
            lines.append("|---|---|---|---|---|---|---|---|---|---|")
        elif cls in ("frozen", "unknown"):
            lines.append("| Бумага | ISIN | Кол-во | Статус |")
            lines.append("|---|---|---|---|")
        else:
            lines.append("| Бумага | Тикер | ISIN | Кол-во | Площадка | Оценка | Доля |")
            lines.append("|---|---|---|---|---|---|---|")
        for p in sorted(group, key=lambda x: -((val.get(x["secid"], {}).get("value_rub")) or 0)):
            v = val.get(p["secid"], {}).get("value_rub")
            share = f"{v / total * 100:.1f}%" if v and total else "—"
            place = p.get("place") or {}
            board = f"{place.get('board','—')}" if place else (p.get("yahoo") or "—")
            if cls in ("corp_bond", "ofz", "foreign_sovereign"):
                lines.append(f"| {p['name']} | {p['isin']} | {p['qty']} | "
                             f"{p.get('face_value')} {p.get('face_unit','').replace('SUR','RUB')} | "
                             f"{p.get('coupon_percent')}% | {p.get('next_coupon') or '—'} | "
                             f"{p.get('offer_date') or '—'} | {p.get('maturity') or '—'} | {fmt_money(v)} | {share} |")
            elif cls in ("frozen", "unknown"):
                lines.append(f"| {p['name_report']} | {p['isin']} | {p['qty']} | {p.get('note') or 'нет котировок'} |")
            else:
                lines.append(f"| {p['name_report']} | {p['secid']} | {p['isin']} | {p['qty']} | {board} | "
                             f"{fmt_money(v)} | {share} |")
            rows_csv.append({
                "class": CLASS_RU.get(cls, cls), "name": p["name_report"], "secid": p["secid"],
                "isin": p["isin"], "qty": p["qty"], "issuer": p["issuer"],
                "board": board, "currency": p.get("currency"),
                "face_value": p.get("face_value"), "coupon_percent": p.get("coupon_percent"),
                "next_coupon": p.get("next_coupon"), "offer_date": p.get("offer_date"),
                "maturity": p.get("maturity"), "value_rub": round(v, 2) if v else "",
                "weight_pct": round(v / total * 100, 2) if v and total else "",
                "rating_watch": p.get("rating_watch"), "earnings_watch": p.get("earnings_watch"),
            })

    corp = [p for p in pf["positions"] if p["class"] == "corp_bond"]
    if corp:
        lines.append("\n_Эмитенты корпоративных выпусков: "
                     + "; ".join(f"{p['name']} — {p['issuer']}" for p in corp) + "._")
        lines.append("_Обратите внимание: «ВСК 1P-04R» — Восточная стивидорная компания, а не страховая «ВСК»; "
                     "ключевые слова для новостей настроены именно на неё._")

    lines.append("\n## Что мониторится по каждой позиции")
    lines.append("- **Акции/БПИФ:** аномальный взлёт (день/неделя/сигмы), близость к 52-недельному максимуму, "
                 "новости об отчётности и дивидендах, рейтинговые действия по эмитенту.")
    lines.append("- **Корпоративные облигации:** понижение рейтинга и негативный прогноз (АКРА, Эксперт РА), "
                 "слабая отчётность, кредитные события, падение цены и расширение Z-спреда, "
                 "календарь купонов/оферт/погашений.")
    lines.append("- **ОФЗ и суверенные:** цена/доходность/дюрация и календарь выплат; корпоративные новости не сканируются.")
    lines.append("- **Заблокированные бумаги:** только новости (ликвидация, выплаты, разблокировка); в оценку не входят.")
    lines.append("\n_Оценка считается по последним ценам MOEX/Yahoo; облигации — цена в % номинала + НКД, "
                 "валютный номинал пересчитывается по курсу. Это не инвестиционная рекомендация._")

    (ROOT / "portfolio.md").write_text("\n".join(lines), encoding="utf-8")
    with (ROOT / "portfolio.csv").open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows_csv[0].keys()))
        writer.writeheader()
        writer.writerows(rows_csv)
    print(f"portfolio.md и portfolio.csv обновлены: {len(rows_csv)} позиций, оценка {fmt_money(total)}")


if __name__ == "__main__":
    main()
