"""Market data collector — all free sources.

FRED      : US daily yields, spreads, SOFR, fed funds, breakevens, HY OAS
Stooq      : CEE + EU government bond yields (daily), WIG20
Yahoo      : indices, FX, commodities, VIX (via yfinance)
CoinGecko  : crypto spot + 24h change, global mcap / BTC dominance
alternative.me : crypto Fear & Greed index

Every symbol is fetched defensively: a failure is logged and the symbol is
skipped, never fatal.
"""
from __future__ import annotations

import csv
import io
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from typing import Any

import requests

from ..config import Config
from ..util import LookbackWindow

log = logging.getLogger("dailybrief.market")

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) DailyBrief/1.0"}
HTTP_TIMEOUT = 20


def _quote(name: str, value: float | None, prev: float | None,
           unit: str, asof: str | None, is_yield: bool) -> dict[str, Any]:
    change = pct = change_bp = None
    if value is not None and prev is not None:
        change = round(value - prev, 4)
        if prev != 0:
            pct = round((value - prev) / abs(prev) * 100, 2)
        if is_yield:
            change_bp = round((value - prev) * 100)
    return {
        "name": name, "value": value, "prev": prev, "unit": unit,
        "change": change, "change_bp": change_bp, "pct_change": pct,
        "asof": asof, "is_yield": is_yield, "ok": value is not None,
    }


# --------------------------------------------------------------------------- #
# FRED
# --------------------------------------------------------------------------- #
def _fred_series(series_id: str, api_key: str) -> tuple[float | None, float | None, str | None]:
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {
        "series_id": series_id, "api_key": api_key, "file_type": "json",
        "sort_order": "desc", "limit": 12,
    }
    r = requests.get(url, params=params, timeout=HTTP_TIMEOUT, headers=UA)
    r.raise_for_status()
    obs = r.json().get("observations", [])
    vals: list[tuple[str, float]] = []
    for o in obs:
        v = o.get("value")
        if v not in (".", "", None):
            try:
                vals.append((o["date"], float(v)))
            except ValueError:
                continue
        if len(vals) >= 2:
            break
    if not vals:
        return None, None, None
    latest = vals[0]
    prev = vals[1][1] if len(vals) > 1 else None
    return latest[1], prev, latest[0]


# --------------------------------------------------------------------------- #
# Stooq (daily CSV)
# --------------------------------------------------------------------------- #
import re as _re


def _stooq_candidates(symbol: str) -> list[str]:
    """Stooq uses both '10ply.b' and '10yply.b' style yield symbols. Try the
    given form first, then the variant with the maturity 'y' toggled."""
    cands = [symbol]
    m = _re.match(r"^(\d+)(y?)(.+)$", symbol)
    if m:
        digits, y, rest = m.groups()
        alt = f"{digits}{'' if y else 'y'}{rest}"
        if alt != symbol:
            cands.append(alt)
    return cands


def _stooq_fetch_one(symbol: str, d1: str, d2: str) -> tuple[float | None, float | None, str | None]:
    url = "https://stooq.com/q/d/l/"
    params = {"s": symbol, "i": "d", "d1": d1, "d2": d2}
    r = requests.get(url, params=params, timeout=HTTP_TIMEOUT, headers=UA)
    r.raise_for_status()
    text = r.text.strip()
    if not text or text.lower().startswith("<") or "N/D" in text[:40] \
            or not text.lower().startswith("date"):
        raise ValueError(f"no CSV data for {symbol}: {text[:50]!r}")
    rows = list(csv.DictReader(io.StringIO(text)))
    closes = [(row["Date"], float(row["Close"])) for row in rows
              if row.get("Close") not in (None, "", "N/D")]
    if not closes:
        raise ValueError(f"empty series for {symbol}")
    latest = closes[-1]
    prev = closes[-2][1] if len(closes) > 1 else None
    return latest[1], prev, latest[0]


def _stooq_series(symbol: str, window: LookbackWindow) -> tuple[float | None, float | None, str | None]:
    d1 = (window.start - timedelta(days=10)).strftime("%Y%m%d")
    d2 = window.now.strftime("%Y%m%d")
    last_err: Exception | None = None
    for cand in _stooq_candidates(symbol):
        try:
            v, p, asof = _stooq_fetch_one(cand, d1, d2)
            if cand != symbol:
                log.info("stooq: '%s' resolved via variant '%s'", symbol, cand)
            return v, p, asof
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise last_err or ValueError(f"Stooq failed for {symbol}")


# --------------------------------------------------------------------------- #
# Yahoo (yfinance)
# --------------------------------------------------------------------------- #
def _yahoo_series(symbol: str) -> tuple[float | None, float | None, str | None]:
    import yfinance as yf  # imported lazily; heavy
    hist = yf.Ticker(symbol).history(period="7d", interval="1d", auto_adjust=False)
    closes = hist["Close"].dropna()
    if len(closes) == 0:
        return None, None, None
    latest = float(closes.iloc[-1])
    prev = float(closes.iloc[-2]) if len(closes) > 1 else None
    asof = closes.index[-1].strftime("%Y-%m-%d")
    return latest, prev, asof


def _fetch_one(item: dict, cfg: Config, window: LookbackWindow,
               fred_key: str | None) -> dict[str, Any]:
    name, source, sid = item["name"], item["source"], item["id"]
    unit = item.get("unit", "")
    is_yield = unit in ("%", "bp") and source in ("fred", "stooq")
    try:
        if source == "fred":
            if not fred_key:
                raise RuntimeError("FRED_API_KEY missing")
            v, p, asof = _fred_series(sid, fred_key)
        elif source == "stooq":
            v, p, asof = _stooq_series(sid, window)
        elif source == "yahoo":
            v, p, asof = _yahoo_series(sid)
        else:
            raise ValueError(f"unknown source {source}")
        return _quote(name, v, p, unit, asof, is_yield)
    except Exception as e:  # noqa: BLE001
        log.warning("market: %s (%s:%s) failed: %s", name, source, sid, e)
        q = _quote(name, None, None, unit, None, is_yield)
        q["error"] = str(e)
        return q


# --------------------------------------------------------------------------- #
# CoinGecko + Fear & Greed
# --------------------------------------------------------------------------- #
def _coingecko(items: list[dict]) -> tuple[list[dict], dict]:
    ids = ",".join(i["id"] for i in items)
    quotes: list[dict] = []
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": ids, "vs_currencies": "usd",
                    "include_24hr_change": "true", "include_market_cap": "true"},
            timeout=HTTP_TIMEOUT, headers=UA)
        r.raise_for_status()
        data = r.json()
        for it in items:
            d = data.get(it["id"], {})
            price = d.get("usd")
            chg = d.get("usd_24h_change")
            q = _quote(it["name"], price, None, "USD", None, False)
            q["pct_change"] = round(chg, 2) if chg is not None else None
            quotes.append(q)
    except Exception as e:  # noqa: BLE001
        log.warning("coingecko prices failed: %s", e)
    glob: dict[str, Any] = {}
    try:
        g = requests.get("https://api.coingecko.com/api/v3/global",
                         timeout=HTTP_TIMEOUT, headers=UA).json().get("data", {})
        glob = {
            "total_mcap_usd": g.get("total_market_cap", {}).get("usd"),
            "mcap_change_24h_pct": round(g.get("market_cap_change_percentage_24h_usd", 0), 2),
            "btc_dominance_pct": round(g.get("market_cap_percentage", {}).get("btc", 0), 2),
        }
    except Exception as e:  # noqa: BLE001
        log.warning("coingecko global failed: %s", e)
    return quotes, glob


def _fear_greed() -> dict[str, Any]:
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1",
                         timeout=HTTP_TIMEOUT, headers=UA)
        d = r.json()["data"][0]
        return {"value": int(d["value"]), "classification": d["value_classification"]}
    except Exception as e:  # noqa: BLE001
        log.warning("fear&greed failed: %s", e)
        return {}


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def collect_market_data(cfg: Config, window: LookbackWindow) -> dict[str, Any]:
    fred_key = cfg.env.get("FRED_API_KEY")
    markets = cfg.get("markets", default={})
    result: dict[str, Any] = {"asof": window.now.isoformat(), "errors": []}

    # categories that use the generic fetcher
    generic_cats = ["rates_cores", "rates_cee", "fx", "equities", "commodities"]
    jobs: list[tuple[str, dict]] = []
    for cat in generic_cats:
        for item in markets.get(cat, []):
            jobs.append((cat, item))

    cat_out: dict[str, list] = {c: [] for c in generic_cats}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(_fetch_one, item, cfg, window, fred_key): (cat, item)
                for cat, item in jobs}
        for fut in as_completed(futs):
            cat, _ = futs[fut]
            cat_out[cat].append(fut.result())
    # preserve config order
    for cat in generic_cats:
        order = {it["name"]: i for i, it in enumerate(markets.get(cat, []))}
        cat_out[cat].sort(key=lambda q, o=order: o.get(q["name"], 999))
        result[cat] = cat_out[cat]

    crypto_items = markets.get("crypto", [])
    result["crypto"], result["crypto_global"] = _coingecko(crypto_items)
    result["fear_greed"] = _fear_greed()

    n_ok = sum(1 for cat in generic_cats for q in result[cat] if q.get("ok"))
    n_total = sum(len(result[cat]) for cat in generic_cats)
    result["errors"] = [f"{q['name']}: {q['error']}"
                        for cat in generic_cats for q in result[cat]
                        if q.get("error")]
    log.info("market data: %d/%d symbols ok across %d categories",
             n_ok, n_total, len(generic_cats) + 1)
    return result


# --------------------------------------------------------------------------- #
# Rendering for the LLM prompt
# --------------------------------------------------------------------------- #
def _fmt_quote(q: dict) -> str:
    if not q.get("ok"):
        return f"  - {q['name']}: brak danych"
    val, unit = q["value"], q["unit"]
    if q["is_yield"]:
        bp = q.get("change_bp")
        chg = f"{'+' if (bp or 0) >= 0 else ''}{bp} bp" if bp is not None else "—"
        return f"  - {q['name']}: {val:.3f}% (zmiana {chg}) [stan {q['asof']}]"
    pct = q.get("pct_change")
    chg = f"{'+' if (pct or 0) >= 0 else ''}{pct}%" if pct is not None else "—"
    asof = f" [stan {q['asof']}]" if q.get("asof") else ""
    # adaptive precision: FX/ratios (<10) need more decimals than index points
    lvl = f"{val:.4f}" if abs(val) < 10 else f"{val:,.2f}"
    return f"  - {q['name']}: {lvl} {unit} ({chg}){asof}".replace(",", " ")


def format_market_text(data: dict) -> str:
    titles = {
        "rates_cores": "STOPY / OBLIGACJE — CORES (US, Niemcy)",
        "rates_cee": "STOPY / OBLIGACJE — CEE (PL, CZ, HU)",
        "fx": "FX",
        "equities": "AKCJE / INDEKSY",
        "commodities": "SUROWCE",
        "crypto": "KRYPTO",
    }
    lines: list[str] = []
    for cat, title in titles.items():
        quotes = data.get(cat, [])
        if not quotes:
            continue
        lines.append(title)
        lines.extend(_fmt_quote(q) for q in quotes)
        lines.append("")
    g = data.get("crypto_global") or {}
    if g:
        lines.append(
            f"  - Krypto global: dominacja BTC {g.get('btc_dominance_pct')}%, "
            f"zmiana kapitalizacji 24h {g.get('mcap_change_24h_pct')}%")
    fg = data.get("fear_greed") or {}
    if fg:
        lines.append(f"  - Crypto Fear & Greed: {fg.get('value')} ({fg.get('classification')})")
    return "\n".join(lines).strip()
