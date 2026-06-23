"""Government bond yields not covered by FRED's daily US series — the German
core (Bund 10Y, Schatz 2Y) and CEE (PL/CZ/HU 10Y).

Design goal: deterministic, *dated* values, and NEVER present a stale number as
today's. Per instrument we use the best free source reachable from a datacenter
IP, with a deterministic monthly anchor (FRED/OECD, keyless) acting as both a
fallback and a plausibility check:

  DE 10Y / 2Y : Bundesbank REST (daily, no key) -> ECB euro-AAA curve (daily proxy)
                -> Stooq (daily) -> FRED monthly
  PL 10Y      : Stooq (daily, keyless CSV via stooq.pl) -> hardened Perplexity
                snapshot (validated: explicit close date + plausibility band) -> FRED monthly
  CZ 10Y      : Stooq (daily, keyless) -> CNB ARAD REST (daily, free CNB_API_KEY) -> FRED monthly
  HU 10Y      : Stooq (daily, keyless) -> MNB .xls (monthly, optional) -> FRED monthly

A daily feed is only accepted if its latest point is plausible AND <=7 days old,
so a frozen/stale series can never read as today's print. Every quote carries its
real as-of date and a `freshness` tag (daily | monthly | na); the monthly path is
honest, not stale-disguised-as-fresh — the renderer flags it "dane miesięczne".

Why this replaced TradingEconomics: TE is Cloudflare-blocked from datacenter IPs,
and when it did parse, the meta-description regex often grabbed a prior print.
Stooq's keyless CSV works from stooq.pl (stooq.com tends to "Access denied").
"""
from __future__ import annotations

import calendar as _cal
import csv
import io
import logging
import re
from datetime import datetime
from typing import Any

import requests

from ..config import Config
from ..util import LookbackWindow

log = logging.getLogger("dailybrief.cee")

HTTP_TIMEOUT = 25
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9,pl;q=0.8",
}

# Stooq's anti-bot serves a JS challenge to BROWSER-like clients but plain CSV to
# simple ones — so use a minimal, non-browser UA here. This matches how plain
# requests / pandas / curl pull it (which work, incl. from GitHub Actions); a full
# Chrome fingerprint triggers the challenge and we get HTML instead of CSV.
STOOQ_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "text/csv,*/*"}

BUNDESBANK_BASE = "https://api.statistiken.bundesbank.de/rest/data/BBSIS"
# Bundesbank term-structure par yields (annual coupon) — only the maturity token
# differs: R10XX (10Y) vs R02XX (2Y). JSON avoids the German decimal-comma CSV.
BUNDESBANK_10Y = "D.I.ZAR.ZI.EUR.S1311.B.A604.R10XX.R.A.A._Z._Z.A"
BUNDESBANK_2Y = "D.I.ZAR.ZI.EUR.S1311.B.A604.R02XX.R.A.A._Z._Z.A"
# ECB euro-area AAA spot rate (Svensson) — close proxy for Bund (within a few bp).
ECB_10Y = "B.U2.EUR.4F.G_N_A.SV_C_YM.SR_10Y"
ECB_2Y = "B.U2.EUR.4F.G_N_A.SV_C_YM.SR_2Y"
# CNB ARAD daily single-bond yield, 10Y Czech govbond.
CNB_INDICATOR_10Y = "MIRFMSD10DRATPECD"
# FRED/OECD harmonised long-term (10Y) rates — MONTHLY, keyless via fredgraph.csv.
FRED_MONTHLY = {"PL": "IRLTLT01PLM156N", "CZ": "IRLTLT01CZM156N",
                "HU": "IRLTLT01HUM156N", "DE": "IRLTLT01DEM156N"}

# Plausibility bounds (percent) and max allowed deviation of the PL snapshot from
# the monthly anchor (percentage points). Override under cee_yields.sanity.
SANITY_MIN, SANITY_MAX, SNAPSHOT_MAX_DEV = -2.0, 25.0, 1.5

_MONTHS = {m.lower(): i for i, m in enumerate(_cal.month_name) if m}


# --------------------------------------------------------------------------- #
# Quote schema (matches market_data so it folds into the rates tables) + helpers
# --------------------------------------------------------------------------- #
def _mk_quote(name: str, value: float | None, change_bp: int | None,
              asof: str | None, source: str, cat: str, freshness: str) -> dict[str, Any]:
    return {
        "name": name, "value": value, "prev": None, "unit": "%",
        "change": None, "change_bp": change_bp, "pct_change": None,
        "asof": asof, "is_yield": True, "ok": value is not None,
        "source": source, "cat": cat, "freshness": freshness,
    }


def _last_two(pairs: list[tuple[str, float]]) -> tuple[str | None, float | None, float | None]:
    """pairs ascending by date -> (latest_date, latest_value, prev_value)."""
    if not pairs:
        return None, None, None
    latest = pairs[-1]
    prev = pairs[-2][1] if len(pairs) > 1 else None
    return latest[0], latest[1], prev


def _within(value: float | None, lo: float, hi: float) -> bool:
    return value is not None and lo <= value <= hi


def _near_anchor(value: float, anchor_val: float | None, max_dev: float) -> bool:
    return anchor_val is None or abs(value - anchor_val) <= max_dev


def _recent(date_iso: str | None, window: LookbackWindow, max_days: int = 6) -> bool:
    if not date_iso:
        return False
    try:
        d = datetime.fromisoformat(date_iso).date()
    except ValueError:
        return False
    return 0 <= (window.now.date() - d).days <= max_days


def _change_bp(latest: float | None, prev: float | None) -> int | None:
    if latest is None or prev is None:
        return None
    return round((latest - prev) * 100)


# --------------------------------------------------------------------------- #
# Parsers (pure — unit-tested offline)
# --------------------------------------------------------------------------- #
def _parse_bundesbank_json(obj: dict) -> list[tuple[str, float]]:
    """SDMX-JSON: observations are positional, dates live in the observation
    dimension's `values`. Missing observations (null) are skipped."""
    root = obj.get("data", obj)
    ds_list = root.get("dataSets") or []
    if not ds_list:
        return []
    series_map = ds_list[0].get("series") or {}
    dims = (root.get("structure", {}).get("dimensions", {}).get("observation") or [])
    dates: list[str] = []
    for d in dims:
        if d.get("id") in ("TIME_PERIOD", "TIME"):
            dates = [v.get("id") for v in d.get("values", [])]
            break
    if not dates and dims:
        dates = [v.get("id") for v in dims[0].get("values", [])]
    out: list[tuple[str, float]] = []
    for sval in series_map.values():
        for idx_str, arr in (sval.get("observations") or {}).items():
            try:
                i, val = int(idx_str), arr[0]
            except (ValueError, IndexError, TypeError):
                continue
            if val is None or i >= len(dates):
                continue
            try:
                out.append((dates[i], float(val)))
            except (TypeError, ValueError):
                continue
        break  # single series requested
    out.sort()
    return out


def _parse_ecb_csv(text: str) -> list[tuple[str, float]]:
    out: list[tuple[str, float]] = []
    for row in csv.DictReader(io.StringIO(text)):
        d, v = row.get("TIME_PERIOD"), row.get("OBS_VALUE")
        if not d or v in (None, "", "NaN"):
            continue
        try:
            out.append((d, float(v)))
        except ValueError:
            continue
    out.sort()
    return out


def _parse_cnb_csv(text: str) -> list[tuple[str, float]]:
    """ARAD /data: semicolon, header row, columns
    indicator_id; snapshot_id; period(YYYYMMDD); value. Decimal may be comma."""
    out: list[tuple[str, float]] = []
    rows = list(csv.reader(io.StringIO(text), delimiter=";"))
    for row in rows[1:]:
        if len(row) < 4:
            continue
        period, value = row[2].strip(), row[3].strip().replace(",", ".")
        if not re.fullmatch(r"\d{8}", period):
            continue
        try:
            v = float(value)
        except ValueError:
            continue
        out.append((f"{period[:4]}-{period[4:6]}-{period[6:8]}", v))
    out.sort()
    return out


def _parse_fred_csv(text: str) -> list[tuple[str, float]]:
    """fredgraph.csv: date,value with missing values written as '.'."""
    out: list[tuple[str, float]] = []
    rows = list(csv.reader(io.StringIO(text)))
    for row in rows[1:]:
        if len(row) < 2:
            continue
        d, v = row[0].strip(), row[1].strip()
        if v in (".", "", "NaN") or not re.match(r"\d{4}-\d{2}-\d{2}$", d):
            continue
        try:
            out.append((d, float(v)))
        except ValueError:
            continue
    out.sort()
    return out


def _stooq_candidates(symbol: str) -> list[str]:
    """Stooq spells yield symbols both '10yply.b' and '10ply.b' — try the given
    form, then the one with the leading-maturity 'y' toggled."""
    cands = [symbol]
    m = re.match(r"^(\d+)(y?)(.+)$", symbol)
    if m:
        digits, y, rest = m.groups()
        alt = f"{digits}{'' if y else 'y'}{rest}"
        if alt != symbol:
            cands.append(alt)
    return cands


def _parse_stooq_csv(text: str) -> list[tuple[str, float]]:
    """Daily CSV (Date,Open,High,Low,Close,Volume). Close = the yield. Tolerates
    an English or Polish header and reads Close positionally (column index 4)."""
    text = text.strip()
    head = text[:80].lower()
    if not text or "access denied" in head or "n/d" in text[:40].lower():
        raise ValueError(f"no CSV: {text[:60]!r}")
    out: list[tuple[str, float]] = []
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return []
    start = 1 if rows[0] and rows[0][0].strip().lower() in ("date", "data") else 0
    for row in rows[start:]:
        if len(row) < 5:
            continue
        d, close = row[0].strip(), row[4].strip()
        if not re.match(r"\d{4}-\d{2}-\d{2}$", d):
            continue
        try:
            out.append((d, float(close)))
        except ValueError:
            continue
    if not out:  # HTML challenge / block page / empty -> surface it, don't silently skip
        raise ValueError(f"no CSV rows (blocked/non-CSV?): {text[:60]!r}")
    out.sort()
    return out


_PL_DATED = re.compile(r"PL\s*=\s*(\d+(?:\.\d+)?)\s*,\s*([+-]?\d+|na)\s*,\s*(\d{4}-\d{2}-\d{2})", re.I)
_PL_CHG = re.compile(r"PL\s*=\s*(\d+(?:\.\d+)?)\s*,\s*([+-]?\d+|na)", re.I)
_PL_LVL = re.compile(r"PL\s*=\s*(\d+(?:\.\d+)?)", re.I)


def _parse_pl_line(text: str) -> tuple[float, int | None, str | None] | None:
    """Strict-first parse of the PL snapshot line: value, daily bp, close date."""
    m = _PL_DATED.search(text)
    if m:
        chg = None if m.group(2).lower() == "na" else int(m.group(2))
        return float(m.group(1)), chg, m.group(3)
    m = _PL_CHG.search(text)
    if m:
        chg = None if m.group(2).lower() == "na" else int(m.group(2))
        return float(m.group(1)), chg, None
    m = _PL_LVL.search(text)
    if m:
        return float(m.group(1)), None, None
    return None


def _parse_mnb_sheet(sheet, header_row: int, val_col: int) -> list[tuple[str, float]]:
    """MNB benchmark .xls: column 0 is a text month ('February 1997' then 'March'…)
    with the year implied — carry it forward. Returns month-end dated values."""
    out: list[tuple[str, float]] = []
    year: int | None = None
    prev_m = 0
    for ri in range(header_row + 1, sheet.nrows):
        label = str(sheet.cell_value(ri, 0)).strip()
        if not label or label.lower().startswith("source"):
            continue
        ym = re.search(r"(\d{4})", label)
        mname = re.search(r"[A-Za-z]+", label)
        if not mname:
            continue
        m = _MONTHS.get(mname.group(0).lower())
        if m is None:
            continue
        if ym:
            year = int(ym.group(1))
        elif year is not None and m < prev_m:
            year += 1
        prev_m = m
        if year is None:
            continue
        try:
            v = float(sheet.cell_value(ri, val_col))
        except (ValueError, TypeError):
            continue
        last = _cal.monthrange(year, m)[1]
        out.append((f"{year:04d}-{m:02d}-{last:02d}", v))
    out.sort()
    return out


# --------------------------------------------------------------------------- #
# Network fetchers (best-effort; raise on failure, caller catches)
# --------------------------------------------------------------------------- #
def _get(url: str, params: dict | None = None, timeout: int = HTTP_TIMEOUT,
         headers: dict | None = None) -> requests.Response:
    r = requests.get(url, params=params, headers=headers or BROWSER_HEADERS, timeout=timeout)
    r.raise_for_status()
    return r


def _bundesbank(series: str, base: str = BUNDESBANK_BASE, n: int = 15) -> list[tuple[str, float]]:
    r = _get(f"{base.rstrip('/')}/{series}", {"lastNObservations": n, "format": "json"})
    return _parse_bundesbank_json(r.json())


def _ecb(series: str, n: int = 15) -> list[tuple[str, float]]:
    r = _get(f"https://data-api.ecb.europa.eu/service/data/YC/{series}",
             {"lastNObservations": n, "format": "csvdata"})
    return _parse_ecb_csv(r.text)


def _cnb(indicator: str, api_key: str, months_before: int = 2) -> list[tuple[str, float]]:
    r = _get("https://www.cnb.cz/aradb/api/v1/data",
             {"indicator_id_list": indicator, "months_before": months_before,
              "decimal_separator": "point", "delimiter": "semicolon",
              "period_sort": "asc", "lang": "en", "api_key": api_key})
    r.encoding = "cp1250"
    return _parse_cnb_csv(r.text)


def _fred_monthly(series: str, api_key: str = "") -> list[tuple[str, float]]:
    """Monthly OECD long-term rate. Prefer the FRED API (api.stlouisfed.org, needs
    the key) — fast and reachable from datacenter IPs. The keyless fredgraph.csv
    host (fred.stlouisfed.org) tends to TIME OUT from GitHub runners, so it's only
    a last resort."""
    if api_key and not api_key.endswith("..."):
        r = _get("https://api.stlouisfed.org/fred/series/observations",
                 {"series_id": series, "api_key": api_key, "file_type": "json",
                  "sort_order": "desc", "limit": 6})
        out: list[tuple[str, float]] = []
        for o in r.json().get("observations", []):
            v = o.get("value")
            if v in (".", "", None):
                continue
            try:
                out.append((o["date"], float(v)))
            except (ValueError, KeyError):
                continue
        out.sort()
        return out
    r = _get("https://fred.stlouisfed.org/graph/fredgraph.csv", {"id": series})
    return _parse_fred_csv(r.text)


def _stooq(symbol: str, hosts: list[str]) -> list[tuple[str, float]]:
    """Keyless daily CSV. stooq.pl serves it; stooq.com tends to 'Access denied'
    from some IPs — try hosts in order, and both symbol spellings."""
    last_err: Exception | None = None
    for host in hosts:
        for cand in _stooq_candidates(symbol):
            try:
                pairs = _parse_stooq_csv(_get(f"https://{host}/q/d/l/",
                                              {"s": cand, "i": "d"},
                                              headers=STOOQ_HEADERS).text)
                if pairs:
                    if host != hosts[0] or cand != symbol:
                        log.info("stooq: '%s' via %s/'%s'", symbol, host, cand)
                    return pairs
            except Exception as e:  # noqa: BLE001
                last_err = e
    if last_err:
        raise last_err
    return []


def _mnb_xls(url: str, header_row: int, val_col: int) -> list[tuple[str, float]]:
    content = _get(url).content
    try:
        import xlrd  # optional; only needed if mnb_xls_url is configured
    except ImportError:
        log.info("MNB .xls skipped: xlrd not installed (pip install xlrd)")
        return []
    book = xlrd.open_workbook(file_contents=content)
    return _parse_mnb_sheet(book.sheet_by_index(0), header_row, val_col)


def _pl_snapshot(cfg: Config) -> tuple[float, int | None, str | None] | None:
    api_key = cfg.env.get("PERPLEXITY_API_KEY", "")
    if not api_key or api_key.endswith("..."):
        return None
    prompt = (
        "Return ONLY one line, no prose, no markdown:\n"
        "PL=<yield>,<change_bp>,<YYYY-MM-DD>\n"
        "where <yield> is the latest closing yield of the Poland 10-year government "
        "bond (POLGB) in percent (e.g. 5.74), <change_bp> is the signed daily change "
        "in basis points (e.g. +3 or -2), and <YYYY-MM-DD> is the exact trading date "
        "of that close. Use the most recent market close. If you are not certain of "
        "the exact close, write PL=na."
    )
    body = {
        "model": cfg.get("perplexity", "model", default="sonar-pro"),
        "messages": [
            {"role": "system", "content": "You are a precise bond-market data assistant. "
             "Never invent numbers or dates; if unsure, output na."},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 120, "temperature": 0.1, "search_recency_filter": "day",
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        r = requests.post("https://api.perplexity.ai/chat/completions",
                          json=body, headers=headers, timeout=60)
        r.raise_for_status()
        return _parse_pl_line(r.json()["choices"][0]["message"]["content"])
    except Exception as e:  # noqa: BLE001
        log.warning("PL snapshot failed: %s", e)
        return None


# --------------------------------------------------------------------------- #
# Per-instrument resolution: best deterministic source -> monthly anchor
# --------------------------------------------------------------------------- #
def _anchor_quote(name: str, anchor: tuple[str, float] | None, cat: str,
                  sources: list[str], tag: str) -> dict[str, Any]:
    if anchor:
        sources.append(f"{tag}:fred-monthly")
        return _mk_quote(name, anchor[1], None, anchor[0], "fred/oecd (monthly)", cat, "monthly")
    sources.append(f"{tag}:none")
    return _mk_quote(name, None, None, None, "n/a", cat, "na")


def _daily_quote(name: str, cat: str, label: str, pairs: list[tuple[str, float]],
                 lo: float, hi: float, window: LookbackWindow) -> dict[str, Any] | None:
    """Build a daily quote from ascending (date, value) pairs, but only if the
    latest value is plausible AND recent (<=7 days) — a frozen/stale series is
    rejected so it can never read as today's print."""
    d, v, prev = _last_two(pairs)
    if _within(v, lo, hi) and _recent(d, window, max_days=7):
        return _mk_quote(name, v, _change_bp(v, prev), d, label, cat, "daily")
    return None


def _try_stooq(name: str, cat: str, symbol: str | None, hosts: list[str],
               lo: float, hi: float, window: LookbackWindow,
               sources: list[str], tag: str) -> dict[str, Any] | None:
    if not symbol:
        return None
    try:
        q = _daily_quote(name, cat, "stooq", _stooq(symbol, hosts), lo, hi, window)
        if q:
            sources.append(f"{tag}:stooq")
            return q
    except Exception as e:  # noqa: BLE001
        log.info("%s Stooq failed (%s)", tag, type(e).__name__)
    return None


def _resolve_de(name: str, tag: str, bb_series: str | None, ecb_series: str | None,
                base: str, sym: str | None, hosts: list[str],
                anchor: tuple[str, float] | None, lo: float, hi: float,
                window: LookbackWindow, sources: list[str]) -> dict[str, Any]:
    if bb_series:
        try:
            q = _daily_quote(name, "cores", "bundesbank", _bundesbank(bb_series, base),
                             lo, hi, window)
            if q:
                sources.append(f"{tag}:bundesbank")
                return q
        except Exception as e:  # noqa: BLE001
            log.info("%s Bundesbank failed (%s) — trying ECB", tag, type(e).__name__)
    if ecb_series:
        try:
            q = _daily_quote(name, "cores", "ecb (euro AAA)", _ecb(ecb_series), lo, hi, window)
            if q:
                sources.append(f"{tag}:ecb")
                return q
        except Exception as e:  # noqa: BLE001
            log.info("%s ECB failed (%s) — trying Stooq", tag, type(e).__name__)
    q = _try_stooq(name, "cores", sym, hosts, lo, hi, window, sources, tag)
    if q:
        return q
    return _anchor_quote(name, anchor, "cores", sources, tag)


def _resolve_cz(name: str, cfg: Config, sc: dict, sym: str | None, hosts: list[str],
                anchor: tuple[str, float] | None, lo: float, hi: float,
                window: LookbackWindow, sources: list[str]) -> dict[str, Any]:
    q = _try_stooq(name, "cee", sym, hosts, lo, hi, window, sources, "CZ")
    if q:
        return q
    key = cfg.env.get("CNB_API_KEY", "")
    indicator = sc.get("cnb_indicator_10y") or CNB_INDICATOR_10Y
    if key and not key.endswith("...") and indicator:
        try:
            pairs = _cnb(indicator, key)
            q = _daily_quote(name, "cee", "cnb arad", pairs, lo, hi, window)
            if q:
                sources.append("CZ:cnb")
                return q
            log.info("CZ CNB no usable value (rows=%d, last=%s) — falling back to monthly",
                     len(pairs), pairs[-1] if pairs else None)
        except Exception as e:  # noqa: BLE001
            log.info("CZ CNB failed (%s) — falling back to monthly", type(e).__name__)
    return _anchor_quote(name, anchor, "cee", sources, "CZ")


def _resolve_hu(name: str, sc: dict, sym: str | None, hosts: list[str],
                anchor: tuple[str, float] | None, lo: float, hi: float,
                window: LookbackWindow, sources: list[str]) -> dict[str, Any]:
    q = _try_stooq(name, "cee", sym, hosts, lo, hi, window, sources, "HU")
    if q:
        return q
    url = (sc.get("mnb_xls_url") or "").strip()
    if url:
        try:
            d, v, _ = _last_two(_mnb_xls(url, int(sc.get("mnb_header_row", 3)),
                                         int(sc.get("mnb_col_10y", 7))))
            if _within(v, lo, hi):
                sources.append("HU:mnb")
                return _mk_quote(name, v, None, d, "mnb/ákk (monthly)", "cee", "monthly")
        except Exception as e:  # noqa: BLE001
            log.info("HU MNB .xls failed (%s) — falling back to FRED monthly", type(e).__name__)
    return _anchor_quote(name, anchor, "cee", sources, "HU")


def _resolve_pl(name: str, cfg: Config, sym: str | None, hosts: list[str],
                anchor: tuple[str, float] | None, lo: float, hi: float,
                max_dev: float, window: LookbackWindow,
                sources: list[str]) -> dict[str, Any]:
    # 1) Stooq daily CSV (keyless; stooq.pl) — the user's proven path
    q = _try_stooq(name, "cee", sym, hosts, lo, hi, window, sources, "PL")
    if q:
        return q
    # 2) hardened snapshot (validated against the anchor) -> 3) monthly anchor
    snap = _pl_snapshot(cfg)
    if snap:
        v, chg, d = snap
        anchor_val = anchor[1] if anchor else None
        if _within(v, lo, hi) and _near_anchor(v, anchor_val, max_dev) and _recent(d, window):
            sources.append("PL:snapshot")
            return _mk_quote(name, v, chg, d, "perplexity (validated)", "cee", "daily")
        log.info("PL snapshot rejected (v=%s date=%s anchor=%s) — using monthly", v, d, anchor_val)
    return _anchor_quote(name, anchor, "cee", sources, "PL")


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def collect_cee_yields(cfg: Config, window: LookbackWindow) -> dict[str, Any]:
    sc = cfg.get("cee_yields", default={}) or {}
    bb = sc.get("bundesbank", {}) or {}
    base = bb.get("base", BUNDESBANK_BASE)
    bb_10y = bb.get("series_10y", BUNDESBANK_10Y)
    bb_2y = bb.get("series_2y", BUNDESBANK_2Y)
    ecb = sc.get("ecb_fallback", {}) or {}
    ecb_10y = ecb.get("series_10y", ECB_10Y)
    ecb_2y = ecb.get("series_2y", ECB_2Y)
    fred_map = sc.get("fred_monthly", {}) or FRED_MONTHLY
    sanity = sc.get("sanity", {}) or {}
    lo = float(sanity.get("min", SANITY_MIN))
    hi = float(sanity.get("max", SANITY_MAX))
    max_dev = float(sanity.get("snapshot_max_dev_pp", SNAPSHOT_MAX_DEV))
    stq = sc.get("stooq", {}) or {}
    stq_hosts = stq.get("hosts") or ["stooq.pl", "stooq.com"]
    stq_syms = stq.get("symbols") or {"PL": "10yply.b", "CZ": "10yczy.b",
                                      "HU": "10yhuy.b", "DE": "10ydey.b"}

    # Deterministic monthly anchors (also the sanity reference for the PL snapshot).
    fred_key = cfg.env.get("FRED_API_KEY", "")
    anchors: dict[str, tuple[str, float]] = {}
    for k, sid in fred_map.items():
        try:
            pairs = _fred_monthly(sid, fred_key)
            if pairs:
                anchors[k] = (pairs[-1][0], pairs[-1][1])
        except Exception as e:  # noqa: BLE001
            log.info("FRED anchor %s (%s) failed: %s", k, sid, type(e).__name__)

    sources: list[str] = []
    quotes = [
        _resolve_de("DE 10Y (Bund)", "DE10", bb_10y, ecb_10y, base, stq_syms.get("DE"),
                    stq_hosts, anchors.get("DE"), lo, hi, window, sources),
        _resolve_de("DE 2Y (Schatz)", "DE2", bb_2y, ecb_2y, base, None,
                    stq_hosts, None, lo, hi, window, sources),   # no 2Y monthly anchor
        _resolve_pl("PL 10Y", cfg, stq_syms.get("PL"), stq_hosts,
                    anchors.get("PL"), lo, hi, max_dev, window, sources),
        _resolve_cz("CZ 10Y", cfg, sc, stq_syms.get("CZ"), stq_hosts,
                    anchors.get("CZ"), lo, hi, window, sources),
        _resolve_hu("HU 10Y", sc, stq_syms.get("HU"), stq_hosts,
                    anchors.get("HU"), lo, hi, window, sources),
    ]

    ok = sum(1 for q in quotes if q["ok"])
    daily = sum(1 for q in quotes if q.get("freshness") == "daily")
    log.info("CEE/Bund yields: %d/%d ok (%d daily) — %s",
             ok, len(quotes), daily, ", ".join(sources))
    return {"quotes": quotes, "via": ", ".join(sources)}
