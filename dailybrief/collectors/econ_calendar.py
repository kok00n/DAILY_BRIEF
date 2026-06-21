"""Today's economic agenda (forward-looking).

Two sources:
  1. FairEconomy / ForexFactory weekly JSON — structured majors (US/EU/UK/JP/...)
     with scheduled times, impact, forecast and previous. Times come in US
     Eastern and are converted to the user's timezone.
  2. Perplexity supplement — fills the CEE gap (Poland/Czechia/Hungary events and
     NBP/CNB/MNB decisions are NOT in the FF feed) plus today's Fed/ECB speakers.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests

from ..config import Config
from ..util import LookbackWindow, polish_date_phrase

log = logging.getLogger("dailybrief.calendar")

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) DailyBrief/1.0"}
HTTP_TIMEOUT = 30

COUNTRY_PL = {
    "USD": "USA", "EUR": "strefa euro", "GBP": "W. Brytania", "JPY": "Japonia",
    "CHF": "Szwajcaria", "CAD": "Kanada", "AUD": "Australia", "NZD": "Nowa Zelandia",
    "CNY": "Chiny", "PLN": "Polska", "CZK": "Czechy", "HUF": "Węgry",
}
IMPACT_PL = {"High": "wysoki", "Medium": "średni", "Low": "niski", "Holiday": "święto"}


def _fetch_structured(cfg: Config, window: LookbackWindow) -> list[dict]:
    url = cfg.get("calendar", "faireconomy_url",
                  default="https://nfs.faireconomy.media/ff_calendar_thisweek.json")
    allowed = set(cfg.get("calendar", "min_impact", default=["High", "Medium", "Holiday"]))
    tz = ZoneInfo(cfg.tz)
    today = window.now.astimezone(tz).date()

    r = requests.get(url, timeout=HTTP_TIMEOUT, headers=UA)
    r.raise_for_status()
    data = r.json()

    events: list[dict] = []
    for ev in data:
        impact = ev.get("impact", "")
        if impact not in allowed and impact != "Holiday":
            continue
        raw = ev.get("date")
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(raw).astimezone(tz)
        except Exception:  # noqa: BLE001
            continue
        if dt.date() != today:
            continue
        events.append({
            "time": dt.strftime("%H:%M"),
            "dt": dt,
            "country": ev.get("country", ""),
            "country_pl": COUNTRY_PL.get(ev.get("country", ""), ev.get("country", "")),
            "title": ev.get("title", "").strip(),
            "impact": impact,
            "impact_pl": IMPACT_PL.get(impact, impact),
            "forecast": (ev.get("forecast") or "").strip(),
            "previous": (ev.get("previous") or "").strip(),
        })
    events.sort(key=lambda e: e["dt"])
    return events


def _fetch_cee_supplement(cfg: Config, window: LookbackWindow) -> str:
    api_key = cfg.env.get("PERPLEXITY_API_KEY", "")
    if not api_key or api_key.endswith("..."):
        return ""
    tz = ZoneInfo(cfg.tz)
    today = window.now.astimezone(tz)
    date_iso = today.date().isoformat()
    prompt = (
        f"List the economic events SCHEDULED FOR TODAY, {date_iso}, in Poland, Czechia "
        f"and Hungary: data releases (CPI, GDP, PMI, labour market, current account, "
        f"budget, bond auctions), and any central bank actions — NBP/RPP, CNB, MNB — "
        f"rate decisions, minutes, press conferences or speeches. Give the scheduled "
        f"time in CET (Warsaw time), plus forecast and previous where available. "
        f"Also list today's notable Fed and ECB speakers or press conferences with times. "
        f"If a time is not confirmed, say 'godzina niepotwierdzona'. Tight bullet points. "
        f"If there are genuinely no scheduled CEE events today, say so explicitly."
    )
    body = {
        "model": cfg.get("perplexity", "model", default="sonar-pro"),
        "messages": [
            {"role": "system", "content": "You are a precise economic-calendar assistant. "
             "Only list events that are actually scheduled; never invent times."},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 800,
        "temperature": 0.1,
        "search_recency_filter": "week",
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        r = requests.post("https://api.perplexity.ai/chat/completions",
                          json=body, headers=headers, timeout=60)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:  # noqa: BLE001
        log.warning("calendar CEE supplement failed: %s", e)
        return ""


def collect_calendar(cfg: Config, window: LookbackWindow) -> dict[str, Any]:
    if not cfg.get("calendar", "enabled", default=True):
        return {"enabled": False, "events": [], "cee_text": ""}
    result: dict[str, Any] = {"enabled": True, "date": window.now.date().isoformat(),
                              "events": [], "cee_text": "", "errors": []}
    try:
        result["events"] = _fetch_structured(cfg, window)
    except Exception as e:  # noqa: BLE001
        log.warning("calendar structured feed failed: %s", e)
        result["errors"].append(f"structured: {e}")
    if cfg.get("calendar", "cee_supplement", default=True):
        result["cee_text"] = _fetch_cee_supplement(cfg, window)
    log.info("calendar: %d structured events today + CEE supplement %s",
             len(result["events"]), "ok" if result["cee_text"] else "—")
    return result


def format_calendar_text(cal: dict, window: LookbackWindow | None = None) -> str:
    if not cal.get("enabled", True):
        return ""
    lines: list[str] = []
    date_lbl = polish_date_phrase(window.now) if window else cal.get("date", "")
    lines.append(f"KALENDARZ NA DZIŚ ({date_lbl}), czas warszawski:")
    events = cal.get("events", [])
    if events:
        lines.append("Źródło strukturalne (majors):")
        for e in events:
            fp = []
            if e["forecast"]:
                fp.append(f"prog. {e['forecast']}")
            if e["previous"]:
                fp.append(f"poprz. {e['previous']}")
            extra = f" ({', '.join(fp)})" if fp else ""
            lines.append(
                f"  - {e['time']}  [{e['country_pl']}] {e['title']} "
                f"— impact {e['impact_pl']}{extra}")
    else:
        lines.append("  (brak zaplanowanych odczytów w feedzie majors lub feed niedostępny)")
    if cal.get("cee_text"):
        lines.append("\nCEE + mówcy banków centralnych (suplement):")
        lines.append(cal["cee_text"])
    return "\n".join(lines).strip()
