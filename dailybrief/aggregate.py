"""Run all collectors, merge into one dossier, and render the research context
block that is fed to the script generator."""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .collectors import (cee_yields, econ_calendar, market_data,
                         news_perplexity, social_grok, swap_rates)
from .config import Config
from .util import LookbackWindow, OUTPUT_DIR, polish_date_phrase

log = logging.getLogger("dailybrief.aggregate")


def collect_all(cfg: Config, window: LookbackWindow) -> dict[str, Any]:
    """Run market / news / social collectors concurrently. Partial failures are
    tolerated so the brief can still be produced from whatever succeeded."""
    def safe(fn, name):
        try:
            return fn(cfg, window)
        except Exception as e:  # noqa: BLE001
            log.error("collector '%s' failed entirely: %s", name, e)
            return {"error": str(e)}

    with ThreadPoolExecutor(max_workers=6) as ex:
        f_market = ex.submit(safe, market_data.collect_market_data, "market")
        f_news = ex.submit(safe, news_perplexity.collect_news, "news")
        f_social = ex.submit(safe, social_grok.collect_social, "social")
        f_cal = ex.submit(safe, econ_calendar.collect_calendar, "calendar")
        f_cee = ex.submit(safe, cee_yields.collect_cee_yields, "cee_yields")
        f_swaps = ex.submit(safe, swap_rates.collect_swaps, "swaps")
        market = f_market.result()
        news = f_news.result()
        social = f_social.result()
        calendar = f_cal.result()
        cee = f_cee.result()
        swaps = f_swaps.result()

    # CEE + Bund yields come from a dedicated source (scrape + Perplexity); fold
    # them into the right market tables (PL/CZ/HU -> rates_cee, Bund -> rates_cores).
    if isinstance(market, dict) and cee.get("quotes"):
        cee_q = [q for q in cee["quotes"] if q.get("cat") == "cee"]
        core_q = [q for q in cee["quotes"] if q.get("cat") == "cores"]
        if cee_q:
            market["rates_cee"] = cee_q
        if core_q:
            market["rates_cores"] = (market.get("rates_cores") or []) + core_q

    dossier = {
        "generated_for": window.now.isoformat(),
        "date_phrase_pl": polish_date_phrase(window.now),
        "window": {
            "from": window.start.isoformat(),
            "to": window.now.isoformat(),
            "label_pl": window.label_pl,
            "is_monday_after_weekend": window.is_monday_after_weekend,
        },
        "market": market,
        "news": news,
        "social": social,
        "calendar": calendar,
        "cee_yields": cee,
        "swaps": swaps,
    }
    return dossier


def build_research_text(dossier: dict, cfg: Config) -> str:
    market = dossier.get("market", {})
    news = dossier.get("news", {})
    social = dossier.get("social", {})
    calendar = dossier.get("calendar", {})

    parts = ["===== DANE RYNKOWE (poziomy i zmiany) ====="]
    parts.append(market_data.format_market_text(market) or "(brak danych rynkowych)")
    swaps_txt = swap_rates.format_swaps_text(dossier.get("swaps", {}))
    if swaps_txt:
        parts.append("\n===== SWAPY IRS (krzywe + nachylenia) =====")
        parts.append(swaps_txt)
    parts.append("\n===== KALENDARZ NA DZIŚ (forward-looking) =====")
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
