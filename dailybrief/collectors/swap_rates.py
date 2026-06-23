"""Interest-rate SWAP (IRS) curves for PLN / HUF / CZK / EUR from BlueGamma's
public API (no key). Gives deterministic, dated par-swap mid rates per tenor plus
computed curve slopes (2s10s, 5s10s, … in bp, with a steepening/flattening tag),
so the brief can talk steepeners/flatteners on the swap curve and set it against
the cash (govie) curve.

Source: the public endpoint behind app.bluegamma.io/public/swap-rates/<ccy>.
  POST get_index_name_all_swap_rates  body {"body":{"index_name":"6M WIBOR"}}
  -> JSON array, one row per tenor:
     {index, currency, tenor, rate(%), change(pp d/d), data_timestamp, date, ...}
Snapshot is ~13:00 local (EURIBOR updates intraday); each row is dated via
data_timestamp, and we skip a curve whose latest stamp is stale.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

import requests

from ..config import Config
from ..util import LookbackWindow

log = logging.getLogger("dailybrief.swaps")

HTTP_TIMEOUT = 25
ENDPOINT = ("https://2vqt1h8xmd.execute-api.eu-west-2.amazonaws.com/public/"
            "get_index_name_all_swap_rates")
DEFAULT_CURVES = [{"ccy": "PLN", "index": "6M WIBOR"},
                  {"ccy": "HUF", "index": "6M BUBOR"},
                  {"ccy": "CZK", "index": "6M PRIBOR"},
                  {"ccy": "EUR", "index": "6M EURIBOR"}]
DEFAULT_TENORS = ["2Y", "5Y", "10Y", "20Y", "30Y"]
DEFAULT_SLOPES = [["2Y", "10Y"], ["5Y", "10Y"], ["2Y", "5Y"], ["10Y", "30Y"]]


# --------------------------------------------------------------------------- #
# Fetch + parse (parse/slopes are pure -> unit-tested offline)
# --------------------------------------------------------------------------- #
def _fetch_curve(endpoint: str, index_name: str) -> list[dict]:
    r = requests.post(endpoint, json={"body": {"index_name": index_name}},
                      headers={"Content-Type": "application/json"}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict):       # tolerate a {"body"/"data": [...]} envelope
        data = data.get("body") or data.get("data") or []
    return data if isinstance(data, list) else []


def _parse_curve(rows: list[dict]) -> tuple[dict[str, dict], str | None]:
    """rows -> ({TENOR: {"rate": float%, "chg_pp": float|None}}, asof_date_iso)."""
    points: dict[str, dict] = {}
    asof: str | None = None
    for row in rows:
        tenor = str(row.get("tenor", "")).strip().upper()
        rate = row.get("rate")
        if not tenor or rate is None:
            continue
        try:
            rate = float(rate)
        except (TypeError, ValueError):
            continue
        try:
            chg = float(row["change"]) if row.get("change") is not None else None
        except (TypeError, ValueError):
            chg = None
        points[tenor] = {"rate": rate, "chg_pp": chg}
        ts = row.get("data_timestamp")
        if ts and (asof is None or ts > asof):
            asof = ts
    return points, (asof.split("T")[0] if asof else None)


def _slopes(points: dict[str, dict], slopes_cfg: list) -> list[dict]:
    """Curve slopes in bp (long minus short) + daily change in bp (steepening>0)."""
    out: list[dict] = []
    for pair in slopes_cfg:
        short, lng = pair[0], pair[1]
        if short in points and lng in points:
            bp = round((points[lng]["rate"] - points[short]["rate"]) * 100)
            cs, cl = points[short]["chg_pp"], points[lng]["chg_pp"]
            dbp = round((cl - cs) * 100) if (cs is not None and cl is not None) else None
            out.append({"name": f"{short[:-1]}s{lng[:-1]}s", "bp": bp, "chg_bp": dbp})
    return out


def _recent(date_iso: str | None, window: LookbackWindow, max_days: int) -> bool:
    if not date_iso:
        return False
    try:
        d = datetime.fromisoformat(date_iso).date()
    except ValueError:
        return False
    return 0 <= (window.now.date() - d).days <= max_days


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def collect_swaps(cfg: Config, window: LookbackWindow) -> dict[str, Any]:
    sc = cfg.get("swaps", default={}) or {}
    if not sc.get("enabled", True):
        return {"curves": {}, "via": "disabled"}
    endpoint = sc.get("endpoint", ENDPOINT)
    curves_cfg = sc.get("curves") or DEFAULT_CURVES
    tenors = sc.get("tenors") or DEFAULT_TENORS
    slopes_cfg = sc.get("slopes") or DEFAULT_SLOPES
    max_stale = int(sc.get("max_stale_days", 7))

    out: dict[str, Any] = {}
    sources: list[str] = []
    for cv in curves_cfg:
        ccy, index = cv.get("ccy"), cv.get("index")
        if not ccy or not index:
            continue
        try:
            points, asof = _parse_curve(_fetch_curve(endpoint, index))
        except Exception as e:  # noqa: BLE001
            log.info("swaps %s (%s) failed: %s", ccy, index, type(e).__name__)
            sources.append(f"{ccy}:err")
            continue
        if not points or not _recent(asof, window, max_stale):
            log.info("swaps %s: no fresh data (asof=%s)", ccy, asof)
            sources.append(f"{ccy}:stale")
            continue
        levels = {t: points[t] for t in tenors if t in points}
        out[ccy] = {"index": index, "asof": asof, "levels": levels,
                    "slopes": _slopes(points, slopes_cfg)}
        sources.append(f"{ccy}:ok")

    ok = sum(1 for c in out.values() if c.get("levels"))
    log.info("swaps: %d/%d curves ok — %s", ok, len(curves_cfg), ", ".join(sources))
    return {"curves": out, "via": ", ".join(sources)}


# --------------------------------------------------------------------------- #
# Rendering for the LLM prompt
# --------------------------------------------------------------------------- #
def _bp(bp: int) -> str:
    return f"{'+' if bp >= 0 else ''}{bp} bp"


def format_swaps_text(data: dict | None) -> str:
    curves = (data or {}).get("curves") or {}
    if not curves:
        return ""
    lines = ["SWAPY IRS — krzywe i nachylenia (steepenery/flattenery)"]
    for ccy, c in curves.items():
        lv = c.get("levels") or {}
        if not lv:
            continue
        pts = []
        for t, p in lv.items():
            chg = p.get("chg_pp")
            cs = f" ({_bp(round(chg * 100))})" if chg is not None else ""
            pts.append(f"{t} {p['rate']:.3f}%{cs}")
        lines.append(f"  {ccy} ({c.get('index')}) [stan {c.get('asof')}]: " + " · ".join(pts))
        sl = c.get("slopes") or []
        if sl:
            sparts = []
            for s in sl:
                tag = ""
                if s.get("chg_bp") is not None:
                    d = s["chg_bp"]
                    direction = "stromienie" if d > 0 else ("spłaszczanie" if d < 0 else "płasko")
                    tag = f", dziś {_bp(d)} -> {direction}"
                sparts.append(f"{s['name']} {_bp(s['bp'])}{tag}")
            lines.append("    nachylenia: " + " · ".join(sparts))
    return "\n".join(lines).strip()


# --------------------------------------------------------------------------- #
# ASW / swap spreads: govbond yield (cbonds) minus the matched swap (BlueGamma)
# --------------------------------------------------------------------------- #
ASW_CCY = {"PL": "PLN", "CZ": "CZK", "HU": "HUF", "DE": "EUR"}   # bond country -> swap ccy
ASW_TENORS = ["5Y", "10Y"]
ASW_LABEL = {"PL": "PL vs PLN swap", "CZ": "CZ vs CZK swap",
             "HU": "HU vs HUF swap", "DE": "Bund vs EUR swap"}
_NAME_RE = re.compile(r"^([A-Z]{2})\s+(\d+Y)")


def compute_asw(govie_quotes: list[dict] | None, swaps_curves: dict | None,
                ccy_map: dict | None = None, tenors: list[str] | None = None) -> list[dict]:
    """ASW (bp) per (country, tenor) = govbond YTM (cbonds) − matched-tenor swap rate
    (BlueGamma). Positive => bond yields above the swap. Daily change in bp too."""
    ccy_map = ccy_map or ASW_CCY
    tenors = tenors or ASW_TENORS
    g: dict[tuple[str, str], dict] = {}
    for q in govie_quotes or []:
        if not q.get("ok") or not q.get("is_yield"):
            continue
        m = _NAME_RE.match(q.get("name", ""))
        if m:
            g[(m.group(1), m.group(2))] = q
    out: list[dict] = []
    for country, sccy in ccy_map.items():
        levels = ((swaps_curves or {}).get(sccy) or {}).get("levels") or {}
        for tenor in tenors:
            gq, sw = g.get((country, tenor)), levels.get(tenor)
            if not gq or gq.get("value") is None or not sw or sw.get("rate") is None:
                continue
            bp = round((gq["value"] - sw["rate"]) * 100)
            gchg, schg = gq.get("change_bp"), sw.get("chg_pp")
            chg = round(gchg - schg * 100) if (gchg is not None and schg is not None) else None
            out.append({"country": country, "ccy": sccy, "tenor": tenor, "bp": bp, "chg_bp": chg})
    return out


def format_asw_text(asw: list[dict] | None) -> str:
    if not asw:
        return ""
    by: dict[str, list] = {}
    for a in asw:
        by.setdefault(a["country"], []).append(a)
    lines = ["ASW / swap-spready (rentowność obligacji minus swap, bp; + = obligacja rentowniej od swapa):"]
    for country, items in by.items():
        parts = []
        for a in sorted(items, key=lambda x: int(x["tenor"][:-1])):
            ch = f", dziś {_bp(a['chg_bp'])}" if a.get("chg_bp") is not None else ""
            parts.append(f"{a['tenor']} {_bp(a['bp'])}{ch}")
        lines.append(f"  {ASW_LABEL.get(country, country)}: " + " · ".join(parts))
    return "\n".join(lines)
