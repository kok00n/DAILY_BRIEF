"""Run all collectors, merge into one dossier, and render the research context
block that is fed to the script generator."""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .collectors import (cee_yields, econ_calendar, fx_rates, market_data,
                         news_perplexity, research_portals, social_grok, swap_rates)
from .config import Config
from .util import LookbackWindow, OUTPUT_DIR, polish_date_phrase

log = logging.getLogger("dailybrief.aggregate")

# Default per-collector toggles. The daily config has no `collectors:` block, so
# everything except `research` runs (daily behaviour unchanged). The weekly config
# flips these (e.g. social off, research on).
_COLLECTOR_DEFAULTS = {
    "market": True, "news": True, "social": True, "calendar": True,
    "cee_yields": True, "swaps": True, "fx": True, "research": False,
}


def _enabled(cfg: Config, name: str) -> bool:
    c = cfg.get("collectors", default=None)
    default = _COLLECTOR_DEFAULTS.get(name, True)
    if not isinstance(c, dict):
        return default
    return bool(c.get(name, default))


def collect_all(cfg: Config, window: LookbackWindow) -> dict[str, Any]:
    """Run market / news / social / research collectors concurrently. Partial
    failures are tolerated so the brief can still be produced from whatever
    succeeded. Which collectors run is config-driven (see `collectors:`)."""
    def safe(fn, name):
        try:
            return fn(cfg, window)
        except Exception as e:  # noqa: BLE001
            log.error("collector '%s' failed entirely: %s", name, e)
            return {"error": str(e)}

    registry = {
        "market": market_data.collect_market_data,
        "news": news_perplexity.collect_news,
        "social": social_grok.collect_social,
        "calendar": econ_calendar.collect_calendar,
        "cee_yields": cee_yields.collect_cee_yields,
        "swaps": swap_rates.collect_swaps,
        "fx": fx_rates.collect_fx,
        "research": research_portals.collect_research,
    }
    active = {name: fn for name, fn in registry.items() if _enabled(cfg, name)}
    with ThreadPoolExecutor(max_workers=max(2, len(active))) as ex:
        futs = {name: ex.submit(safe, fn, name) for name, fn in active.items()}
        res = {name: f.result() for name, f in futs.items()}
    market = res.get("market", {})
    news = res.get("news", {})
    social = res.get("social", {})
    calendar = res.get("calendar", {})
    cee = res.get("cee_yields", {})
    swaps = res.get("swaps", {})
    fxr = res.get("fx", {})
    research = res.get("research", {})

    # CEE + Bund yields come from a dedicated source (scrape + Perplexity); fold
    # them into the right market tables (PL/CZ/HU -> rates_cee, Bund -> rates_cores).
    if isinstance(market, dict) and cee.get("quotes"):
        cee_q = [q for q in cee["quotes"] if q.get("cat") == "cee"]
        core_q = [q for q in cee["quotes"] if q.get("cat") == "cores"]
        if cee_q:
            market["rates_cee"] = cee_q
        if core_q:
            market["rates_cores"] = (market.get("rates_cores") or []) + core_q

    # ECB FX (deterministic, dated) overrides Yahoo fx by name; Yahoo stays as the
    # per-pair fallback for any pair ECB couldn't supply.
    if isinstance(market, dict) and fxr.get("quotes"):
        ecb_by = {q["name"]: q for q in fxr["quotes"] if q.get("ok")}
        if ecb_by:
            merged, seen = [], set()
            for q in market.get("fx", []) or []:
                merged.append(ecb_by.get(q["name"], q))
                seen.add(q["name"])
            for name, q in ecb_by.items():
                if name not in seen:
                    merged.append(q)
            market["fx"] = merged

    dossier = {
        "generated_for": window.now.isoformat(),
        "date_phrase_pl": polish_date_phrase(window.now),
        "window": {
            "from": window.start.isoformat(),
            "to": window.now.isoformat(),
            "label_pl": window.label_pl,
            "is_monday_after_weekend": window.is_monday_after_weekend,
            "kind": window.kind,
        },
        "market": market,
        "news": news,
        "social": social,
        "calendar": calendar,
        "cee_yields": cee,
        "swaps": swaps,
        "research": research,
    }
    return dossier


def build_research_text(dossier: dict, cfg: Config) -> str:
    market = dossier.get("market", {})
    news = dossier.get("news", {})
    social = dossier.get("social", {})
    calendar = dossier.get("calendar", {})
    research = dossier.get("research", {})
    weekly = (dossier.get("window", {}) or {}).get("kind") == "weekly"

    parts: list[str] = []
    # For the weekly review the primary source IS the research desks — put it first.
    research_txt = research_portals.format_research_text(research)
    if research_txt:
        parts.append("===== ANALIZY OŚRODKÓW BADAWCZYCH (miniony tydzień) =====")
        parts.append(research_txt)
        parts.append("")

    parts.append("===== DANE RYNKOWE (poziomy i zmiany) =====")
    if weekly:
        parts.append("(Uwaga: dla cores/FX/indeksów zmiana = tydzień do tygodnia "
                     "tam, gdzie dostępne; dla CEE govies/swapów to bieżący poziom — "
                     "tygodniowe ruchy bp omawiaj na bazie analiz i newsów powyżej.)")
    parts.append(market_data.format_market_text(market) or "(brak danych rynkowych)")
    swaps_txt = swap_rates.format_swaps_text(dossier.get("swaps", {}))
    if swaps_txt:
        parts.append("\n===== SWAPY IRS (krzywe + nachylenia) =====")
        parts.append(swaps_txt)
    asw = swap_rates.compute_asw((market.get("rates_cee") or []) + (market.get("rates_cores") or []),
                                 (dossier.get("swaps") or {}).get("curves") or {})
    asw_txt = swap_rates.format_asw_text(asw)
    if asw_txt:
        parts.append("\n===== ASW / SWAP-SPREADY (govie minus swap) =====")
        parts.append(asw_txt)
    cal_hdr = ("KALENDARZ — NADCHODZĄCY TYDZIEŃ (forward-looking)" if weekly
               else "KALENDARZ NA DZIŚ (forward-looking)")
    parts.append(f"\n===== {cal_hdr} =====")
    parts.append(econ_calendar.format_calendar_text(calendar) or "(brak kalendarza)")
    parts.append("\n===== NEWSY (Perplexity, ostatnie okno) =====")
    parts.append(news_perplexity.format_news_text(news) or "(brak newsów)")
    parts.append("\n===== FINTWIT / ANALITYCY (Grok x_search + web) =====")
    parts.append(social_grok.format_social_text(social) or "(brak treści społecznościowych)")
    return "\n".join(parts).strip()


def save_dossier(dossier: dict, date_str: str) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"dossier_{date_str}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(dossier, f, ensure_ascii=False, indent=2, default=str)
    log.info("dossier saved -> %s", path.name)
    return path
