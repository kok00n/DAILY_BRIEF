"""News collector via Perplexity Sonar (web search + citations).

Runs a set of targeted topic queries in parallel, each scoped to the lookback
window. Returns concise, fact-dense English briefs the script generator turns
into Polish narration.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import requests

from ..config import Config
from ..util import LookbackWindow

log = logging.getLogger("dailybrief.news")

ENDPOINT = "https://api.perplexity.ai/chat/completions"
HTTP_TIMEOUT = 60

SYSTEM = (
    "You are a sharp financial markets news researcher for a professional "
    "rates/macro trader. Be concise and fact-dense. Always include concrete "
    "numbers, levels, basis-point moves, names, institutions and timing. "
    "Output tight bullet points only — no preamble, no fluff, no disclaimers. "
    "Prioritise what actually moved markets or changes the outlook."
)

# topic_id -> query template ({w} = window label, {N} = hours)
QUERIES: dict[str, str] = {
    "rates": (
        "Most important global rates and bond-market developments in the last {w}: "
        "US Treasuries (2y/10y/30y, curve), Fed speakers/minutes/dots, ECB and Bund, "
        "and CEE — Poland (NBP), Czechia (CNB), Hungary (MNB) — yields, auctions, "
        "and any CPI/inflation or jobs prints. Give bp moves and key levels."
    ),
    "macro": (
        "Key macroeconomic data releases and economic headlines globally in the last {w} "
        "(US, euro area, CEE). Actual vs expected where available."
    ),
    "fx": (
        "Major FX developments in the last {w}: USD/DXY, EUR/USD, USD/JPY and the "
        "Polish zloty (USD/PLN, EUR/PLN). Drivers and levels."
    ),
    "equities": (
        "Key US equity index and notable single-stock / earnings / guidance news "
        "in the last {w}. Include index moves and what drove them."
    ),
    "commodities": (
        "Oil (WTI/Brent), gold and natural gas developments and drivers in the last {w}, "
        "with price levels and % moves."
    ),
    "crypto": (
        "Most important crypto market news in the last {w}: BTC, ETH, ETF/flows, "
        "regulation, liquidations. Levels and % moves."
    ),
    "cee": (
        "Central & Eastern Europe (Poland, Czechia, Hungary) economic, political and "
        "market news in the last {w} relevant to rates and FX traders: central banks, "
        "fiscal/budget, politics, bond supply."
    ),
    "ai_tech": (
        "Most interesting AI and technology news in the last {w}: notable model/product "
        "releases, research, funding, and especially concrete new practical AI use-cases "
        "and applications worth knowing about."
    ),
}


def _recency(cfg: Config, topic: str, window: LookbackWindow) -> str:
    overrides = cfg.get("news", "recency_overrides", default={}) or {}
    return overrides.get(topic) or window.perplexity_recency


def _domain_filter(cfg: Config, topic: str) -> list[str] | None:
    """search_domain_filter (max 20). A topic with its own allowlist uses it
    (allowlist mode); every other topic uses the global denylist ('-' prefix)."""
    news = cfg.get("news", default={}) or {}
    allow_map = news.get("allow_domains") or {}
    allow = allow_map.get(topic)
    if not allow and topic == "cee":
        allow = news.get("cee_allow_domains")  # backward compat
    if allow:
        return allow[:20]
    deny = news.get("deny_domains") or []
    if deny:
        return [f"-{d}" for d in deny][:20]
    return None


def _extract_sources(data: dict) -> list[str]:
    """Prefer rich search_results (title + url); fall back to plain citations."""
    srs = data.get("search_results")
    if isinstance(srs, list) and srs:
        out = []
        for s in srs:
            if isinstance(s, dict):
                title, url = (s.get("title") or "").strip(), (s.get("url") or "").strip()
                out.append(f"{title} — {url}" if title and url else (title or url))
        out = [x for x in out if x]
        if out:
            return out
    urls = []
    for c in data.get("citations") or []:
        if isinstance(c, str):
            urls.append(c)
        elif isinstance(c, dict):
            urls.append(c.get("url") or c.get("link") or "")
    return [u for u in urls if u]


def _post(body: dict, headers: dict, attempts: int = 3) -> dict:
    """POST with retry on transient errors (429 / 5xx / network). 4xx fails fast
    and includes the response body so the real reason is visible in the logs."""
    last: Exception | None = None
    for i in range(1, attempts + 1):
        try:
            r = requests.post(ENDPOINT, json=body, headers=headers, timeout=HTTP_TIMEOUT)
        except requests.RequestException as e:
            last = e
            if i < attempts:
                time.sleep(2 * i)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            last = RuntimeError(f"retryable {r.status_code}: {r.text[:200]}")
            if i < attempts:
                time.sleep(2 * i)
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")  # 4xx: fail fast
        return r.json()
    raise last  # type: ignore[misc]


def _query(topic: str, prompt: str, cfg: Config, window: LookbackWindow,
           api_key: str) -> dict[str, Any]:
    base = {
        "model": cfg.get("perplexity", "model", default="sonar-pro"),
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": cfg.get("perplexity", "max_tokens_per_query", default=1100),
        "temperature": 0.2,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    full = dict(base, search_recency_filter=_recency(cfg, topic, window),
                search_after_date_filter=window.from_date_us)
    dom = _domain_filter(cfg, topic)
    if dom:
        full["search_domain_filter"] = dom
    try:
        data = _post(full, headers)
    except Exception as e:  # noqa: BLE001
        # auto-degrade: drop the optional search params (the likely culprit of a 400)
        log.warning("perplexity '%s' full request failed (%s); retrying minimal body",
                    topic, e)
        minimal = dict(base, search_recency_filter=_recency(cfg, topic, window))
        data = _post(minimal, headers)

    content = data["choices"][0]["message"]["content"]
    return {"topic": topic, "text": content.strip(), "citations": _extract_sources(data)}


def collect_news(cfg: Config, window: LookbackWindow) -> dict[str, Any]:
    api_key = cfg.require_env("PERPLEXITY_API_KEY")
    win_phrase = "weekend (since Friday)" if window.is_monday_after_weekend else "24 hours"
    results: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {
            ex.submit(_query, topic, tmpl.format(w=win_phrase), cfg, window, api_key): topic
            for topic, tmpl in QUERIES.items()
        }
        for fut in as_completed(futs):
            topic = futs[fut]
            try:
                results[topic] = fut.result()
            except Exception as e:  # noqa: BLE001
                log.warning("perplexity topic '%s' failed: %s", topic, e)
                results[topic] = {"topic": topic, "text": "", "citations": [], "error": str(e)}
    ok = sum(1 for r in results.values() if r.get("text"))
    log.info("news: %d/%d Perplexity topics returned content", ok, len(QUERIES))
    return results


def format_news_text(news: dict) -> str:
    titles = {
        "rates": "RATES & BONDS", "macro": "MACRO DATA", "fx": "FX",
        "equities": "EQUITIES", "commodities": "COMMODITIES", "crypto": "CRYPTO",
        "cee": "CEE REGION", "ai_tech": "AI & TECH",
    }
    blocks = []
    for topic, title in titles.items():
        r = news.get(topic) or {}
        if not r.get("text"):
            continue
        srcs = r.get("citations", [])[:6]
        src_line = ("\n  źródła: " + " | ".join(srcs)) if srcs else ""
        blocks.append(f"### {title}\n{r['text']}{src_line}")
    return "\n\n".join(blocks).strip()
