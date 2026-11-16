"""Silent launcher for watch.py - no console window."""
import os, subprocess, sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "logs" / "watch-scheduler.log"
os.chdir(ROOT)
os.environ.setdefault("TG_PROXY", "socks5://127.0.0.1:10808")
# Force UTF-8 for child process stdout/stderr (prevents UnicodeEncodeError
# when writing ₽, ±, etc. to log file via cp1251 codepage on Windows)
os.environ["PYTHONIOENCODING"] = "utf-8"
os.environ["PYTHONUTF8"] = "1"
LOG.parent.mkdir(exist_ok=True)
with LOG.open("a", encoding="utf-8") as fh:
    ts = datetime.now().isoformat(timespec="seconds")
    fh.write(f"\n=== {ts} === start ===\n")
    fh.flush()
    try:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "watch.py")],
            stdout=fh, stderr=subprocess.STDOUT, cwd=ROOT, timeout=600)
        fh.write(f"=== exit code {result.returncode} ===\n")
    except subprocess.TimeoutExpired:
        fh.write("=== TIMEOUT ===\n"); sys.exit(2)
    except Exception as exc:
        ename = type(exc).__name__
        fh.write(f"=== EXCEPTION: {ename}: {exc} ===\n"); sys.exit(1)
