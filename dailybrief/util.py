"""Shared helpers: logging, time windows, retries, formatting."""
from __future__ import annotations

import functools
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "output"
DATA_DIR = ROOT / "data"
PROMPTS_DIR = ROOT / "prompts"

POLISH_DAYS = {
    0: "poniedziałek", 1: "wtorek", 2: "środa", 3: "czwartek",
    4: "piątek", 5: "sobota", 6: "niedziela",
}
POLISH_MONTHS = {
    1: "stycznia", 2: "lutego", 3: "marca", 4: "kwietnia", 5: "maja",
    6: "czerwca", 7: "lipca", 8: "sierpnia", 9: "września",
    10: "października", 11: "listopada", 12: "grudnia",
}


def setup_logging(name: str = "dailybrief", logfile: Path | None = None) -> logging.Logger:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
                            datefmt="%H:%M:%S")
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    if logfile is None:
        logfile = OUTPUT_DIR / f"run_{datetime.now():%Y%m%d}.log"
    fh = logging.FileHandler(logfile, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    return logger


@dataclass
class LookbackWindow:
    """Time window the brief should cover."""
    now: datetime              # tz-aware, local
    start: datetime            # tz-aware, local — beginning of coverage window
    is_monday_after_weekend: bool
    label_pl: str              # human label, e.g. "ostatnich 24 godzin"

    @property
    def days(self) -> int:
        return max(1, round((self.now - self.start).total_seconds() / 86400))

    @property
    def from_date_iso(self) -> str:
        return self.start.date().isoformat()

    @property
    def from_date_us(self) -> str:
        return self.start.strftime("%m/%d/%Y")

    @property
    def perplexity_recency(self) -> str:
        return "week" if self.is_monday_after_weekend else "day"


def compute_window(tz_name: str, weekend_lookback: bool = True,
                   reference: datetime | None = None) -> LookbackWindow:
    """Last 24h normally; on Monday cover the weekend (since Friday morning)."""
    tz = ZoneInfo(tz_name)
    now = (reference or datetime.now(tz)).astimezone(tz)
    is_monday = now.weekday() == 0 and weekend_lookback
    if is_monday:
        # back to Friday same time (3 days)
        start = now - timedelta(days=3)
        label = "weekendu i ostatniej sesji piątkowej"
    else:
        start = now - timedelta(hours=24)
        label = "ostatnich 24 godzin"
    return LookbackWindow(now=now, start=start,
                          is_monday_after_weekend=is_monday, label_pl=label)


def polish_date_phrase(dt: datetime) -> str:
    """e.g. 'piątek, 13 czerwca 2026'."""
    return f"{POLISH_DAYS[dt.weekday()]}, {dt.day} {POLISH_MONTHS[dt.month]} {dt.year}"


def retry(times: int = 3, delay: float = 2.0, backoff: float = 2.0,
          exceptions: tuple = (Exception,), logger: logging.Logger | None = None):
    """Simple retry decorator with exponential backoff."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            d = delay
            last = None
            for attempt in range(1, times + 1):
                try:
                    return fn(*args, **kwargs)
                except exceptions as e:  # noqa: BLE001
                    last = e
                    if logger:
                        logger.warning("%s attempt %d/%d failed: %s",
                                       fn.__name__, attempt, times, e)
                    if attempt < times:
                        time.sleep(d)
                        d *= backoff
            raise last  # type: ignore[misc]
        return wrapper
    return deco


def fmt_change(value: float | None, unit: str = "") -> str:
    if value is None:
        return "b.d."
    sign = "+" if value >= 0 else ""
    if unit == "bp":
        return f"{sign}{value:.0f} bp"
    if unit == "%pt":
        return f"{sign}{value:.2f} pkt%"
    return f"{sign}{value:.2f}{unit}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
