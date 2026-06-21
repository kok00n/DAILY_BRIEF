"""Run all collectors, merge into one dossier, and render the research context
block that is fed to the script generator."""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .collectors import econ_calendar, market_data, news_perplexity, social_grok
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

    with ThreadPoolExecutor(max_workers=4) as ex:
        f_market = ex.submit(safe, market_data.collect_market_data, "market")
        f_news = ex.submit(safe, news_perplexity.collect_news, "news")
        f_social = ex.submit(safe, social_grok.collect_social, "social")
        f_cal = ex.submit(safe, econ_calendar.collect_calendar, "calendar")
        market = f_market.result()
        news = f_news.result()
        social = f_social.result()
        calendar = f_cal.result()

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
    }
    return dossier


def build_research_text(dossier: dict, cfg: Config) -> str:
    market = dossier.get("market", {})
    news = dossier.get("news", {})
    social = dossier.get("social", {})
    calendar = dossier.get("calendar", {})

    parts = ["===== DANE RYNKOWE (poziomy i zmiany) ====="]
    parts.append(market_data.format_market_text(market) or "(brak danych rynkowych)")
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
