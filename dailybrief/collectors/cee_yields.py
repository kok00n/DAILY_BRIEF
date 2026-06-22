"""Government bond yields not covered by FRED — CEE (PL/CZ/HU) and the German
core (Bund 10Y, Schatz 2Y).

There is no free, keyless, daily, exact API for these, so this uses a hybrid:
a best-effort scrape of TradingEconomics first (works from a residential IP /
local run) with a Perplexity live snapshot as the fallback (works from cloud /
datacenter IPs, reads the same published numbers, with citations).

Note: scraping TradingEconomics is against their ToS and is reliably blocked by
Cloudflare from datacenter IPs — so in the GitHub Actions deployment the
Perplexity fallback does the real work.
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

# key -> instrument. cat: which rates table it belongs to. te: TradingEconomics
# /government-bond-yield slug (10Y pages only); None = Perplexity-only. ppx: how
# to describe it to Perplexity.
INSTRUMENTS = [
    {"key": "PL",   "name": "PL 10Y",         "cat": "cee",   "te": "poland",
     "ppx": "Poland 10-year government bond yield"},
    {"key": "CZ",   "name": "CZ 10Y",         "cat": "cee",   "te": "czech-republic",
     "ppx": "Czechia 10-year government bond yield"},
    {"key": "HU",   "name": "HU 10Y",         "cat": "cee",   "te": "hungary",
     "ppx": "Hungary 10-year government bond yield"},
    {"key": "DE10", "name": "DE 10Y (Bund)",  "cat": "cores", "te": "germany",
     "ppx": "Germany 10-year Bund yield"},
    {"key": "DE2",  "name": "DE 2Y (Schatz)", "cat": "cores", "te": None,
     "ppx": "Germany 2-year Schatz yield"},
]


def _quote(name: str, value: float | None, change_bp: int | None, asof: str | None,
           source: str, cat: str) -> dict[str, Any]:
    return {
        "name": name, "value": value, "prev": None, "unit": "%",
        "change": None, "change_bp": change_bp, "pct_change": None,
        "asof": asof, "is_yield": True, "ok": value is not None,
        "source": source, "cat": cat,
    }


# --------------------------------------------------------------------------- #
# Best-effort TradingEconomics scrape (usually blocked from datacenter IPs)
# --------------------------------------------------------------------------- #
def _scrape_te(slug: str) -> float:
    url = f"https://tradingeconomics.com/{slug}/government-bond-yield"
    r = requests.get(url, headers=BROWSER_HEADERS, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    html = r.text
    if "cf-browser-verification" in html or "Just a moment" in html or len(html) < 2000:
        raise ValueError("blocked / challenge page")
    m = re.search(r"was\s+(\d+\.\d+)\s+percent", html, re.IGNORECASE) \
        or re.search(r'name="description"[^>]*content="[^"]*?(\d+\.\d+)\s*percent',
                     html, re.IGNORECASE)
    if not m:
        raise ValueError("yield not found in HTML")
    return float(m.group(1))


# --------------------------------------------------------------------------- #
# Perplexity fallback — structured, parseable snapshot
# --------------------------------------------------------------------------- #
def _parse_snapshot(text: str) -> dict[str, tuple[float, int | None]]:
    out: dict[str, tuple[float, int | None]] = {}
    for inst in INSTRUMENTS:
        key = inst["key"]
        m = re.search(rf"{key}\s*=\s*(\d+\.\d+)\s*,\s*([+-]?\d+)", text)
        if m:
            out[key] = (float(m.group(1)), int(m.group(2)))
            continue
        m2 = re.search(rf"{key}\s*=\s*(\d+\.\d+)", text)
        if m2:
            out[key] = (float(m2.group(1)), None)
    return out


def _perplexity_snapshot(cfg: Config) -> dict[str, tuple[float, int | None]]:
    api_key = cfg.env.get("PERPLEXITY_API_KEY", "")
    if not api_key or api_key.endswith("..."):
        return {}
    lines = "\n".join(f"{i['key']}=<yield>,<change_bp>   # {i['ppx']}" for i in INSTRUMENTS)
    prompt = (
        "Return ONLY the latest government bond yields and their daily change in basis "
        "points, using the most recent market close. Output EXACTLY these lines, nothing "
        f"else, no prose:\n{lines}\n"
        "where <yield> is a percent number like 5.74 and <change_bp> is a signed integer "
        "like +3 or -2. If a value is unknown write na. Do not add comments to your output."
    )
    body = {
        "model": cfg.get("perplexity", "model", default="sonar-pro"),
        "messages": [
            {"role": "system", "content": "You are a precise bond-market data assistant. "
             "Never invent numbers; if unsure, output na."},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 300,
        "temperature": 0.1,
        "search_recency_filter": "day",
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        r = requests.post("https://api.perplexity.ai/chat/completions",
                          json=body, headers=headers, timeout=60)
        r.raise_for_status()
        return _parse_snapshot(r.json()["choices"][0]["message"]["content"])
    except Exception as e:  # noqa: BLE001
        log.warning("CEE/Bund Perplexity snapshot failed: %s", e)
        return {}


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def collect_cee_yields(cfg: Config, window: LookbackWindow) -> dict[str, Any]:
    asof = window.now.date().isoformat()
    scraped: dict[str, float] = {}

    scrapeable = [i for i in INSTRUMENTS if i["te"]]
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(_scrape_te, i["te"]): i["key"] for i in scrapeable}
        for fut in as_completed(futs):
            key = futs[fut]
            try:
                scraped[key] = fut.result()
            except Exception as e:  # noqa: BLE001
                log.info("scrape %s failed (%s) — will use Perplexity", key, type(e).__name__)

    snap = _perplexity_snapshot(cfg)  # fills gaps + supplies daily bp changes

    quotes: list[dict] = []
    for inst in INSTRUMENTS:
        key, name, cat = inst["key"], inst["name"], inst["cat"]
        if key in scraped:
            quotes.append(_quote(name, scraped[key], snap.get(key, (None, None))[1],
                                 asof, "tradingeconomics", cat))
        elif key in snap:
            val, chg = snap[key]
            quotes.append(_quote(name, val, chg, asof, "perplexity", cat))
        else:
            quotes.append(_quote(name, None, None, None, "n/a", cat))

    ok = sum(1 for q in quotes if q["ok"])
    via = "scrape+perplexity" if scraped else ("perplexity" if snap else "none")
    log.info("CEE/Bund yields: %d/%d ok (via %s)", ok, len(INSTRUMENTS), via)
    return {"quotes": quotes, "via": via}
