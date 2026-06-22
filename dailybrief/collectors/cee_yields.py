"""CEE 10Y government bond yields (PL / CZ / HU).

There is no free, keyless, daily, exact API for CEE govvies, so this uses a
hybrid: best-effort scrape of TradingEconomics first (works from a residential
IP / local run), with a Perplexity live snapshot as the fallback (works from
cloud / datacenter IPs and reads the same published numbers, with citations).

Note: scraping TradingEconomics is against their ToS and is reliably blocked by
Cloudflare from datacenter IPs — hence the fallback does the real work in the
GitHub Actions deployment.
"""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import requests

from ..config import Config
from ..util import LookbackWindow

log = logging.getLogger("dailybrief.cee")

HTTP_TIMEOUT = 20
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,pl;q=0.8",
}

# code -> (display name, TradingEconomics slug)
COUNTRIES = {
    "PL": ("PL 10Y", "poland"),
    "CZ": ("CZ 10Y", "czech-republic"),
    "HU": ("HU 10Y", "hungary"),
}


def _quote(name: str, value: float | None, change_bp: int | None,
           asof: str | None, source: str) -> dict[str, Any]:
    return {
        "name": name, "value": value, "prev": None, "unit": "%",
        "change": None, "change_bp": change_bp, "pct_change": None,
        "asof": asof, "is_yield": True, "ok": value is not None, "source": source,
    }


# --------------------------------------------------------------------------- #
# Best-effort TradingEconomics scrape (usually blocked from datacenter IPs)
# --------------------------------------------------------------------------- #
def _scrape_te(slug: str) -> tuple[float | None, int | None]:
    url = f"https://tradingeconomics.com/{slug}/government-bond-yield"
    r = requests.get(url, headers=BROWSER_HEADERS, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    html = r.text
    if "cf-browser-verification" in html or "Just a moment" in html or len(html) < 2000:
        raise ValueError("blocked / challenge page")
    # TE embeds a description like "...was 5.74 percent on ..." — grab the level
    m = re.search(r"was\s+(\d+\.\d+)\s+percent", html, re.IGNORECASE)
    if not m:
        m = re.search(r'name="description"[^>]*content="[^"]*?(\d+\.\d+)\s*percent',
                      html, re.IGNORECASE)
    if not m:
        raise ValueError("yield not found in HTML")
    return float(m.group(1)), None   # scrape gives level; change handled elsewhere


# --------------------------------------------------------------------------- #
# Perplexity fallback — structured, parseable snapshot
# --------------------------------------------------------------------------- #
def _parse_snapshot(text: str) -> dict[str, tuple[float, int | None]]:
    out: dict[str, tuple[float, int | None]] = {}
    for code in COUNTRIES:
        m = re.search(rf"{code}\s*=\s*(\d+\.\d+)\s*,\s*([+-]?\d+)", text)
        if m:
            out[code] = (float(m.group(1)), int(m.group(2)))
            continue
        m2 = re.search(rf"{code}\s*=\s*(\d+\.\d+)", text)
        if m2:
            out[code] = (float(m2.group(1)), None)
    return out


def _perplexity_snapshot(cfg: Config) -> dict[str, tuple[float, int | None]]:
    api_key = cfg.env.get("PERPLEXITY_API_KEY", "")
    if not api_key or api_key.endswith("..."):
        return {}
    prompt = (
        "Return ONLY the latest 10-year government bond yields and their daily change "
        "in basis points for Poland, Czechia and Hungary, using the most recent market "
        "close. Output EXACTLY three lines, nothing else, no prose:\n"
        "PL=<yield>,<change_bp>\nCZ=<yield>,<change_bp>\nHU=<yield>,<change_bp>\n"
        "where <yield> is a percent number like 5.74 and <change_bp> is a signed integer "
        "like +3 or -2. If a value is unknown, write na."
    )
    body = {
        "model": cfg.get("perplexity", "model", default="sonar-pro"),
        "messages": [
            {"role": "system", "content": "You are a precise bond-market data assistant. "
             "Never invent numbers; if unsure, output na."},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 200,
        "temperature": 0.1,
        "search_recency_filter": "day",
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    out: dict[str, tuple[float, int | None]] = {}
    try:
        r = requests.post("https://api.perplexity.ai/chat/completions",
                          json=body, headers=headers, timeout=60)
        r.raise_for_status()
        out = _parse_snapshot(r.json()["choices"][0]["message"]["content"])
    except Exception as e:  # noqa: BLE001
        log.warning("CEE Perplexity snapshot failed: %s", e)
    return out


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def collect_cee_yields(cfg: Config, window: LookbackWindow) -> dict[str, Any]:
    asof = window.now.date().isoformat()
    scraped: dict[str, float] = {}

    # 1) best-effort scrape (parallel, short)
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {ex.submit(_scrape_te, slug): code
                for code, (_, slug) in COUNTRIES.items()}
        for fut in as_completed(futs):
            code = futs[fut]
            try:
                val, _ = fut.result()
                if val is not None:
                    scraped[code] = val
            except Exception as e:  # noqa: BLE001
                log.info("CEE scrape %s failed (%s) — will use Perplexity", code, type(e).__name__)

    # 2) Perplexity — fills gaps the scrape missed AND supplies the daily bp change
    snap = _perplexity_snapshot(cfg)

    quotes: list[dict] = []
    for code, (name, _) in COUNTRIES.items():
        if code in scraped:
            chg = snap.get(code, (None, None))[1]
            quotes.append(_quote(name, scraped[code], chg, asof, "tradingeconomics"))
        elif code in snap:
            val, chg = snap[code]
            quotes.append(_quote(name, val, chg, asof, "perplexity"))
        else:
            quotes.append(_quote(name, None, None, None, "n/a"))

    ok = sum(1 for q in quotes if q["ok"])
    via = "scrape+perplexity" if scraped else ("perplexity" if snap else "none")
    log.info("CEE yields: %d/%d ok (via %s)", ok, len(COUNTRIES), via)
    return {"quotes": quotes, "via": via}
