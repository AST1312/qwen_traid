"""Общие утилиты: HTTP без внешних зависимостей, доступ к MOEX ISS, конфиг, логи."""
from __future__ import annotations

import gzip
import io
import json
import os
import re
import socket
import ssl
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CONFIG = ROOT / "config"
REPORTS = ROOT / "reports"
STATE = ROOT / "state"
LOGS = ROOT / "logs"

UA = "Mozilla/5.0 (X11; Linux x86_64) portfolio-watch/1.0"
MSK = ZoneInfo("Europe/Moscow")


def now_msk() -> datetime:
    return datetime.now(MSK)


# Ensure stdout uses UTF-8 on Windows to avoid UnicodeEncodeError with ₽, ±, etc.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass


def log(msg: str) -> None:
    line = f"[{now_msk():%Y-%m-%d %H:%M:%S} MSK] {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        # Fallback: strip non-encodable characters
        print(line.encode("utf-8", errors="replace").decode("utf-8", errors="replace"), flush=True)
    LOGS.mkdir(exist_ok=True)
    with (LOGS / "watch.log").open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


# --------------------------------------------------------------------------- HTTP
def _open(url: str, timeout: int = 25, insecure: bool = False):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip", "Accept": "*/*"})
    ctx = None
    if insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
    raw = resp.read()
    if resp.headers.get("Content-Encoding") == "gzip":
        raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
    return raw


def get_text(url: str, timeout: int = 25, insecure: bool = False, retries: int = 2) -> str | None:
    for attempt in range(retries + 1):
        try:
            return _open(url, timeout, insecure).decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001 - сетевые ошибки не должны ронять проверку
            if attempt == retries:
                log(f"HTTP fail {url[:110]} -> {type(exc).__name__}: {str(exc)[:120]}")
                return None
            time.sleep(1.5 * (attempt + 1))
    return None


def get_json(url: str, timeout: int = 25, insecure: bool = False, retries: int = 2):
    txt = get_text(url, timeout, insecure, retries)
    if not txt:
        return None
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        log(f"JSON fail {url[:110]}")
        return None


# --------------------------------------------------------------------------- SOCKS5 proxy
def _socks5_connect(proxy_host: str, proxy_port: int, dest_host: str, dest_port: int,
                    timeout: int = 15, username: str = "", password: str = "") -> socket.socket:
    """Устанавливает TCP-соединение с dest_host:dest_port через SOCKS5-прокси.
    Возвращает готовый к использованию сокет (для HTTPS — оборачивайте в ssl).
    """
    sock = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
    sock.settimeout(timeout)

    # --- Auth negotiation (RFC 1928 + 1929)
    if username and password:
        sock.sendall(b"\x05\x02\x00\x02")  # methods: no-auth + username/password
    else:
        sock.sendall(b"\x05\x01\x00")  # method: no-auth

    resp = sock.recv(2)
    if len(resp) < 2 or resp[0] != 0x05:
        sock.close()
        raise OSError("SOCKS5: invalid greeting")
    method = resp[1]

    if method == 0x02:  # need username/password auth
        if not username:
            sock.close()
            raise OSError("SOCKS5: proxy requires authentication")
        uname = username.encode("utf-8")
        passwd = password.encode("utf-8")
        sock.sendall(b"\x01" + bytes([len(uname)]) + uname + bytes([len(passwd)]) + passwd)
        auth_resp = sock.recv(2)
        if len(auth_resp) < 2 or auth_resp[1] != 0x00:
            sock.close()
            raise OSError("SOCKS5: auth failed")
    elif method != 0x00:
        sock.close()
        raise OSError(f"SOCKS5: unsupported auth method {method}")

    # --- CONNECT request: VER(1) CMD(1) RSV(1) ATYP(1) DST.ADDR DST.PORT(2)
    dest_bytes = dest_host.encode("idna")
    if len(dest_bytes) > 255:
        sock.close()
        raise OSError(f"SOCKS5: destination host too long: {dest_host}")
    req = b"\x05\x01\x00\x03" + bytes([len(dest_bytes)]) + dest_bytes + struct.pack("!H", dest_port)
    sock.sendall(req)

    resp = sock.recv(10)  # min: VER REP RSV ATYP (4 bytes)
    if len(resp) < 4:
        sock.close()
        raise OSError("SOCKS5: truncated CONNECT response")
    if resp[0] != 0x05:
        sock.close()
        raise OSError("SOCKS5: invalid CONNECT response version")
    if resp[1] != 0x00:
        sock.close()
        raise OSError(f"SOCKS5: CONNECT failed with code {resp[1]}")

    return sock


def _parse_proxy_url(url: str) -> tuple[str, int, str, str]:
    """Парсит socks5://user:pass@host:port -> (host, port, user, pass)."""
    p = urllib.parse.urlparse(url)
    host = p.hostname or ""
    port = p.port or 1080
    return host, port, p.username or "", p.password or ""


def tg_request(url: str, data: bytes | None = None, timeout: int = 25,
               proxy_url: str = "") -> bytes:
    """HTTP-запрос к Telegram API через SOCKS5-прокси (без pysocks).
    Если proxy_url пуст — идёт напрямую через urllib.
    """
    if not proxy_url:
        req = urllib.request.Request(url, data=data,
                                     headers={"User-Agent": UA, "Accept": "*/*"})
        return urllib.request.urlopen(req, timeout=timeout).read()

    parsed = urllib.parse.urlparse(url)
    use_tls = parsed.scheme == "https"
    dest_host = parsed.hostname or ""
    dest_port = parsed.port or (443 if use_tls else 80)
    path = parsed.path + ("?" + parsed.query if parsed.query else "")

    proxy_host, proxy_port, user, passwd = _parse_proxy_url(proxy_url)
    raw_sock = _socks5_connect(proxy_host, proxy_port, dest_host, dest_port,
                               timeout=timeout, username=user, password=passwd)
    try:
        if use_tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = True
            ctx.verify_mode = ssl.CERT_REQUIRED
            sock = ctx.wrap_socket(raw_sock, server_hostname=dest_host)
        else:
            sock = raw_sock

        method = "POST" if data is not None else "GET"
        body = data or b""
        headers = (
            f"{method} {path} HTTP/1.1\r\n"
            f"Host: {dest_host}\r\n"
            f"User-Agent: {UA}\r\n"
            f"Accept: */*\r\n"
            f"Connection: close\r\n"
        )
        if data is not None:
            headers += (
                f"Content-Type: application/x-www-form-urlencoded\r\n"
                f"Content-Length: {len(body)}\r\n"
            )
        headers += "\r\n"
        sock.sendall(headers.encode("latin-1") + body)

        # Читаем HTTP-ответ: заголовки + тело
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        header_part, _, body_part = buf.partition(b"\r\n\r\n")
        status_line = header_part.split(b"\r\n", 1)[0].decode("latin-1")
        # Status: HTTP/1.1 200 OK
        parts = status_line.split(" ", 2)
        status_code = int(parts[1]) if len(parts) >= 2 else 0
        if status_code not in (200, 204):
            raise OSError(f"HTTP {status_code}: {status_line}")

        # Собираем оставшееся тело (для chunked — упрощённо: читаем до закрытия)
        while True:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            body_part += chunk
        return body_part
    finally:
        try:
            raw_sock.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- MOEX ISS
ISS = "https://iss.moex.com/iss"


def iss_block(payload: dict, block: str) -> list[dict]:
    """ISS отдаёт {block: {columns: [...], data: [[...]]}} -> список словарей."""
    if not payload or block not in payload:
        return []
    blk = payload[block]
    cols = blk.get("columns", [])
    return [dict(zip(cols, row)) for row in blk.get("data", [])]


def resolve_board(secid: str) -> dict | None:
    """Определяем торговую площадку (engine/market/board) по secid."""
    url = f"{ISS}/securities/{secid}.json?iss.meta=off&iss.only=boards&boards.columns=boardid,engine,market,is_traded,is_primary"
    rows = iss_block(get_json(url, insecure=True), "boards")
    live = [r for r in rows if r.get("is_traded") == 1 and r.get("engine") == "stock"
            and r.get("market") in ("shares", "bonds")]
    if not live:
        return None
    live.sort(key=lambda r: (0 if r.get("is_primary") else 1, r["boardid"]))
    r = live[0]
    return {"engine": r["engine"], "market": r["market"], "board": r["boardid"]}


def security_info(secid: str, place: dict) -> dict:
    url = (f"{ISS}/engines/{place['engine']}/markets/{place['market']}/boards/{place['board']}"
           f"/securities/{secid}.json?iss.meta=off")
    payload = get_json(url, insecure=True)
    sec = iss_block(payload, "securities")
    md = iss_block(payload, "marketdata")
    yld = iss_block(payload, "marketdata_yields")
    out = dict(sec[0]) if sec else {}
    out["_marketdata"] = md[0] if md else {}
    out["_yields"] = yld[0] if yld else {}
    return out


def candles(secid: str, place: dict, days: int = 400) -> list[dict]:
    start = (now_msk() - timedelta(days=days)).strftime("%Y-%m-%d")
    url = (f"{ISS}/engines/{place['engine']}/markets/{place['market']}/boards/{place['board']}"
           f"/securities/{secid}/candles.json?from={start}&interval=24&iss.meta=off"
           f"&candles.columns=open,close,high,low,value,volume,end")
    rows = iss_block(get_json(url, insecure=True), "candles")
    return [r for r in rows if r.get("close")]


def fx_rub(currency: str) -> float | None:
    """Курс валюты к рублю: сначала MOEX CETS, затем Yahoo."""
    if currency in ("RUB", "SUR", "RUR"):
        return 1.0
    secid = {"USD": "USDRUB_TOM", "EUR": "EURRUB_TOM", "CNY": "CNYRUB_TOM", "HKD": "HKDRUB_TOM"}.get(currency)
    if secid:
        url = f"{ISS}/engines/currency/markets/selt/boards/CETS/securities/{secid}.json?iss.meta=off&marketdata.columns=SECID,LAST,WAPRICE"
        for row in iss_block(get_json(url, insecure=True), "marketdata"):
            price = row.get("LAST") or row.get("WAPRICE")
            if price:
                return float(price)
    txt = get_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{currency}RUB=X?range=1d&interval=1d")
    try:
        return float(txt["chart"]["result"][0]["meta"]["regularMarketPrice"])
    except Exception:  # noqa: BLE001
        return None


def yahoo_quote(symbol: str, lookback: str = "1y") -> dict | None:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={lookback}&interval=1d"
    payload = get_json(url)
    try:
        res = payload["chart"]["result"][0]
        meta = res["meta"]
        closes_raw = res["indicators"]["quote"][0].get("close", [])
        vols_raw = res["indicators"]["quote"][0].get("volume", []) or []
        ts = res.get("timestamp", []) or []
        series = [{"close": c, "volume": v, "end": datetime.fromtimestamp(t, MSK).strftime("%Y-%m-%d")}
                  for t, c, v in zip(ts, closes_raw, vols_raw + [None] * len(ts)) if c]
        return {
            "currency": meta.get("currency"),
            "last": meta.get("regularMarketPrice"),
            "prev_close": meta.get("chartPreviousClose") or meta.get("previousClose"),
            "series": series,
            "exchange": meta.get("fullExchangeName"),
        }
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- конфиг / состояние
def load_json(path: Path, default=None):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log(f"битый JSON: {path}")
    return default if default is not None else {}


def save_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_secrets() -> dict:
    """config/secrets.env (KEY=VALUE) + переменные окружения имеют приоритет."""
    out = {}
    env_file = CONFIG / "secrets.env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    for key in ("TG_BOT_TOKEN", "TG_CHAT_ID", "TG_TOPIC_ID", "TG_PROXY"):
        if os.environ.get(key):
            out[key] = os.environ[key]
    return out


def strip_html(html: str) -> str:
    html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    html = (html.replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", '"')
            .replace("&laquo;", "«").replace("&raquo;", "»").replace("&mdash;", "—")
            .replace("&#39;", "'").replace("&lt;", "<").replace("&gt;", ">"))
    return " ".join(html.split())


def fmt_money(value: float | None, currency: str = "RUB") -> str:
    if value is None:
        return "—"
    sym = {"RUB": "₽", "USD": "$", "HKD": "HK$", "CNY": "¥", "EUR": "€"}.get(currency, currency)
    return f"{value:,.0f} {sym}".replace(",", " ")


def fmt_pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:+.2f}%"


def moex_trading_date() -> str | None:
    """Текущая торговая дата MOEX. Если не совпадает с сегодня — биржа сегодня не торгует."""
    url = (f"{ISS}/engines/stock/markets/shares/boards/TQBR/securities/SBER.json"
           f"?iss.meta=off&iss.only=dataversion")
    rows = iss_block(get_json(url, retries=1, insecure=True), "dataversion")
    return rows[0].get("trade_date") if rows else None
