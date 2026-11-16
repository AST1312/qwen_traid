#!/usr/bin/env python3
"""Планировщик: один запуск в будни в заданное время (по умолчанию 10:15 Europe/Moscow).

Живёт как обычный процесс — годится для песочницы/контейнера/своего сервера.
Если нужен системный планировщик, смотрите deploy/crontab.txt и deploy/systemd/.

  python3 scripts/scheduler.py            # демон: спит до следующего буднего утра
  python3 scripts/scheduler.py --once     # проверить сейчас и выйти (тест)
  python3 scripts/scheduler.py --status    # показать следующий запуск
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import CONFIG, LOGS, ROOT, STATE, load_json, log, save_json  # noqa: E402


def cfg_schedule() -> tuple[str, ZoneInfo, bool]:
    cfg = load_json(CONFIG / "config.json")
    sch = cfg.get("schedule", {})
    return sch.get("time", "10:15"), ZoneInfo(sch.get("timezone", "Europe/Moscow")), sch.get("weekdays_only", True)


def next_run(now: datetime, hhmm: str, tz: ZoneInfo, weekdays_only: bool) -> datetime:
    hh, mm = (int(x) for x in hhmm.split(":"))
    local = now.astimezone(tz)
    candidate = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    if weekdays_only:
        while candidate.weekday() >= 5:
            candidate += timedelta(days=1)
    return candidate


def run_check(extra: list[str] | None = None) -> int:
    cmd = [sys.executable, str(ROOT / "scripts" / "watch.py")] + (extra or [])
    LOGS.mkdir(exist_ok=True)
    stamp = datetime.now(ZoneInfo("Europe/Moscow")).strftime("%Y-%m-%d")
    with (LOGS / f"run_{stamp}.log").open("a", encoding="utf-8") as fh:
        proc = subprocess.run(cmd, cwd=str(ROOT), stdout=fh, stderr=subprocess.STDOUT)
    return proc.returncode


def write_status(nxt: datetime, last: dict | None = None) -> None:
    payload = {"next_run": nxt.isoformat(timespec="seconds"),
               "updated_at": datetime.now(ZoneInfo("Europe/Moscow")).isoformat(timespec="seconds"),
               "pid_alive": True}
    if last:
        payload["last_run"] = last
    save_json(STATE / "scheduler_status.json", payload)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="запустить проверку немедленно и выйти")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--force", action="store_true", help="пробросить --force в watch.py")
    args = ap.parse_args()

    hhmm, tz, weekdays_only = cfg_schedule()
    now = datetime.now(tz)
    nxt = next_run(now, hhmm, tz, weekdays_only)

    if args.status:
        st = load_json(STATE / "scheduler_status.json", {})
        print(json.dumps({"now": now.isoformat(timespec="seconds"), "schedule": f"{hhmm} {tz}",
                          "weekdays_only": weekdays_only, "next_run": nxt.isoformat(timespec="seconds"),
                          "status_file": st}, ensure_ascii=False, indent=2))
        return 0

    if args.once:
        code = run_check(["--force"] if args.force else [])
        log(f"разовый запуск завершён с кодом {code}")
        return code

    log(f"Планировщик запущен: будни {hhmm} ({tz}). Следующий запуск {nxt:%Y-%m-%d %H:%M %Z}")
    write_status(nxt)

    # догоняющий запуск: если сегодня будний день, время уже прошло, а проверки не было
    state = load_json(STATE / "state.json", {})
    hh, mm = (int(x) for x in hhmm.split(":"))
    scheduled_today = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if (not weekdays_only or now.weekday() < 5) and now >= scheduled_today and state.get("last_run_date") != now.strftime("%Y-%m-%d"):
        log("Сегодняшнее окно уже прошло, а проверки не было — догоняющий запуск.")
        code = run_check()
        write_status(next_run(datetime.now(tz), hhmm, tz, weekdays_only),
                     {"at": datetime.now(tz).isoformat(timespec="seconds"), "exit_code": code})

    while True:
        now = datetime.now(tz)
        nxt = next_run(now, hhmm, tz, weekdays_only)
        write_status(nxt)
        wait = (nxt - now).total_seconds()
        log(f"Сон {wait/3600:.2f} ч до {nxt:%Y-%m-%d %H:%M %Z}")
        while wait > 0:
            time.sleep(min(wait, 300))
            wait = (nxt - datetime.now(tz)).total_seconds()
        log("=== Плановое окно: запускаю проверку ===")
        code = run_check()
        log(f"Проверка завершена с кодом {code}")
        write_status(next_run(datetime.now(tz), hhmm, tz, weekdays_only),
                     {"at": datetime.now(tz).isoformat(timespec="seconds"), "exit_code": code})
        time.sleep(60)


if __name__ == "__main__":
    sys.exit(main())
