"""Deterministic FX from the ECB Data Portal daily euro reference rates (no key,
datacenter-OK). ECB publishes one daily mid per currency vs EUR (~16:00 CET); we
read those, derive the USD crosses, and compute DXY from the standard basket.
Each quote is dated via the ECB TIME_PERIOD. Yahoo stays as the per-pair fallback
(the fold happens in aggregate.py), so a missing ECB leg never blanks a pair.
"""
from __future__ import annotations

import csv
import io
import logging
from typing import Any

import requests

from ..config import Config
from ..util import LookbackWindow
from .market_data import _quote

log = logging.getLogger("dailybrief.fx")

HTTP_TIMEOUT = 25
ECB_EXR = "https://data-api.ecb.europa.eu/service/data/EXR/D.{ccy}.EUR.SP00.A"
UA = {"User-Agent": "Mozilla/5.0 (compatible; DailyBrief/1.0)"}

# ECB currencies each output pair needs (so we only fetch what's used)
_PAIR_CCY = {
    "EUR/USD": ["USD"], "EUR/PLN": ["PLN"], "EUR/CZK": ["CZK"], "EUR/HUF": ["HUF"],
    "EUR/GBP": ["GBP"], "USD/PLN": ["USD", "PLN"], "USD/JPY": ["USD", "JPY"],
    "DXY": ["USD", "JPY", "GBP", "CAD", "SEK", "CHF"],
}
DEFAULT_PAIRS = ["EUR/USD", "USD/PLN", "EUR/PLN", "USD/JPY", "DXY"]


# --------------------------------------------------------------------------- #
# Fetch + parse (parse/compute are pure -> unit-tested offline)
# --------------------------------------------------------------------------- #
def _parse_exr_csv(text: str) -> list[tuple[str, float]]:
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


def _exr(ccy: str) -> list[tuple[str, float]]:
    r = requests.get(ECB_EXR.format(ccy=ccy),
                     params={"lastNObservations": 2, "format": "csvdata"},
                     headers=UA, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return _parse_exr_csv(r.text)


def _dxy(ex: dict[str, float]) -> float:
    """ICE U.S. Dollar Index from the six component crosses (ex = CCY-per-EUR)."""
    eurusd = ex["USD"]
    usdjpy = ex["JPY"] / ex["USD"]
    gbpusd = ex["USD"] / ex["GBP"]
    usdcad = ex["CAD"] / ex["USD"]
    usdsek = ex["SEK"] / ex["USD"]
    usdchf = ex["CHF"] / ex["USD"]
    return (50.14348112 * eurusd ** -0.576 * usdjpy ** 0.136 * gbpusd ** -0.119
            * usdcad ** 0.091 * usdsek ** 0.042 * usdchf ** 0.036)


def _pair_value(name: str, ex: dict[str, float]) -> float | None:
    """ex maps CCY -> units of CCY per 1 EUR (the ECB convention)."""
    if name == "DXY":
        return _dxy(ex)
    if name in ("EUR/USD", "EUR/PLN", "EUR/CZK", "EUR/HUF", "EUR/GBP"):
        return ex[name.split("/")[1]]
    if name == "USD/PLN":
        return ex["PLN"] / ex["USD"]
    if name == "USD/JPY":
        return ex["JPY"] / ex["USD"]
    return None


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def collect_fx(cfg: Config, window: LookbackWindow) -> dict[str, Any]:
    fc = cfg.get("fx", default={}) or {}
    if not fc.get("enabled", True):
        return {"quotes": []}
    pairs = fc.get("pairs") or DEFAULT_PAIRS
    needed = sorted({c for p in pairs for c in _PAIR_CCY.get(p, [])})

    latest: dict[str, float] = {}
    prev: dict[str, float] = {}
    asof: str | None = None
    for ccy in needed:
        try:
            obs = _exr(ccy)
        except Exception as e:  # noqa: BLE001
            log.info("ECB EXR %s failed: %s", ccy, type(e).__name__)
            continue
        if not obs:
            continue
        latest[ccy] = obs[-1][1]
        if len(obs) > 1:
            prev[ccy] = obs[-2][1]
        asof = max(asof, obs[-1][0]) if asof else obs[-1][0]

    quotes: list[dict] = []
    for name in pairs:
        ccys = _PAIR_CCY.get(name, [])
        if not ccys or not all(c in latest for c in ccys):
            continue  # missing a leg -> leave it for the Yahoo fallback
        val = _pair_value(name, latest)
        pv = _pair_value(name, prev) if all(c in prev for c in ccys) else None
        q = _quote(name, val, pv, "", asof, False)
        q["source"] = "ecb"
        quotes.append(q)

    log.info("FX (ECB): %d/%d pairs ok [stan %s]", len(quotes), len(pairs), asof)
    return {"quotes": quotes}
