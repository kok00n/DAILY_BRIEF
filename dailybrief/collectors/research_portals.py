"""Weekly CEE research digest from public research-portal feeds.

For the WEEKLY review podcast (config_weekly.yaml). Pulls the last N days of
items from configured RSS/Atom feeds of bank & institutional research desks
(ING THINK, ČNB, MNB-ish, PIE, ...), filters by recency and optional keywords,
and renders a compact "what the houses published this week" digest that grounds
the brief in primary sources.

Design mirrors the other collectors: every source is fetched defensively (a
failure is logged and the source skipped, never fatal), values are dated, and a
browser User-Agent is sent (several bank WAFs 403 the default fetcher). Feeds are
server-rendered XML, so no JS/headless browser is needed. Non-RSS portals (KB,
Erste, the Polish banks' HTML/PDF) are covered analytically via the Perplexity
`cee_research` allowlist (see config news.allow_domains) rather than scraped here.
"""
from __future__ import annotations

import html
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import requests

from ..config import Config
from ..util import LookbackWindow

log = logging.getLogger("dailybrief.research")

HTTP_TIMEOUT = 25
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
    "Accept-Language": "en-US,en;q=0.9,pl;q=0.8",
}

DEFAULT_MAX_ITEMS = 12
DEFAULT_SUMMARY_CHARS = 600
DEFAULT_TOTAL_CHARS = 18000

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


# --------------------------------------------------------------------------- #
# Parsing helpers (pure — unit-tested offline)
# --------------------------------------------------------------------------- #
def _local(tag: str) -> str:
    """Strip an XML namespace: '{http://...}title' -> 'title'."""
    return tag.rsplit("}", 1)[-1].lower()


def _child_text(el: ET.Element, names: set[str]) -> str:
    for ch in el:
        if _local(ch.tag) in names and (ch.text or "").strip():
            return ch.text.strip()
    return ""


def _child_link(el: ET.Element) -> str:
    """RSS <link>text</link>; Atom <link href="..."/> (prefer rel=alternate)."""
    href_alt = href_any = text_link = ""
    for ch in el:
        if _local(ch.tag) != "link":
            continue
        href = (ch.attrib.get("href") or "").strip()
        if href:
            rel = (ch.attrib.get("rel") or "alternate").strip().lower()
            if rel == "alternate" and not href_alt:
                href_alt = href
            if not href_any:
                href_any = href
        elif (ch.text or "").strip() and not text_link:
            text_link = ch.text.strip()
    return href_alt or text_link or href_any


def _strip_html(text: str) -> str:
    text = html.unescape(text or "")
    text = _TAG_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def _parse_date(raw: str) -> datetime | None:
    """RFC 822 (RSS pubDate) or ISO 8601 (Atom) -> tz-aware UTC datetime."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        dt = None
    if dt is None:
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_feed(xml_text: str) -> list[dict[str, Any]]:
    """Parse an RSS 2.0 or Atom feed into [{title, link, date(dt), summary}]."""
    try:
        root = ET.fromstring(xml_text.strip())
    except ET.ParseError as e:
        raise ValueError(f"not parseable XML: {e}") from e
    items: list[dict[str, Any]] = []
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        title = _strip_html(_child_text(el, {"title"}))
        summary = _strip_html(
            _child_text(el, {"description", "summary", "encoded", "content"}))
        date_raw = _child_text(el, {"pubdate", "published", "updated", "date"})
        items.append({
            "title": title, "link": _child_link(el),
            "date": _parse_date(date_raw), "summary": summary,
        })
    return items


def _matches_keywords(item: dict, keywords: list[str]) -> bool:
    if not keywords:
        return True
    hay = f"{item.get('title', '')} {item.get('summary', '')}".lower()
    return any(k.lower() in hay for k in keywords)


def _in_window(item: dict, window: LookbackWindow) -> bool:
    """Keep an item if its date is within the window. Items WITHOUT a parseable
    date are kept (better to over-include a recent feed than silently drop it)."""
    d = item.get("date")
    if d is None:
        return True
    start = window.start.astimezone(timezone.utc)
    end = window.now.astimezone(timezone.utc)
    return start <= d <= end


def select_items(raw_items: list[dict], window: LookbackWindow,
                 keywords: list[str], max_items: int) -> list[dict]:
    kept = [it for it in raw_items
            if it.get("title") and _in_window(it, window)
            and _matches_keywords(it, keywords)]
    # newest first (dateless items sink to the bottom)
    kept.sort(key=lambda it: it.get("date") or datetime.min.replace(tzinfo=timezone.utc),
              reverse=True)
    return kept[:max_items]


# --------------------------------------------------------------------------- #
# Network + orchestration
# --------------------------------------------------------------------------- #
def _fetch(url: str) -> str:
    r = requests.get(url, headers=BROWSER_HEADERS, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.text


def _collect_one(src: dict, window: LookbackWindow, summary_chars: int) -> dict[str, Any]:
    name = src.get("name", "?")
    out = {"name": name, "lang": src.get("lang", ""), "items": [], "error": None}
    try:
        xml_text = _fetch(src["url"])
        raw = parse_feed(xml_text)
        sel = select_items(raw, window, src.get("keywords") or [],
                           int(src.get("max_items", DEFAULT_MAX_ITEMS)))
        for it in sel:
            d = it.get("date")
            out["items"].append({
                "title": it["title"],
                "link": it.get("link", ""),
                "date": d.date().isoformat() if d else "",
                "summary": (it.get("summary") or "")[:summary_chars],
            })
    except Exception as e:  # noqa: BLE001
        log.warning("research source '%s' failed: %s", name, e)
        out["error"] = str(e)
    return out


def collect_research(cfg: Config, window: LookbackWindow) -> dict[str, Any]:
    rc = cfg.get("research", default={}) or {}
    if not rc.get("enabled", False):
        return {"enabled": False, "sources": []}
    sources = rc.get("sources") or []
    summary_chars = int(rc.get("summary_chars", DEFAULT_SUMMARY_CHARS))
    from concurrent.futures import ThreadPoolExecutor
    results: list[dict] = []
    if sources:
        with ThreadPoolExecutor(max_workers=min(8, len(sources))) as ex:
            results = list(ex.map(
                lambda s: _collect_one(s, window, summary_chars), sources))
    n_items = sum(len(s["items"]) for s in results)
    n_ok = sum(1 for s in results if s["items"])
    log.info("research: %d items across %d/%d sources",
             n_items, n_ok, len(sources))
    return {"enabled": True, "sources": results,
            "n_items": n_items,
            "max_total_chars": int(rc.get("max_total_chars", DEFAULT_TOTAL_CHARS))}


# --------------------------------------------------------------------------- #
# Rendering for the LLM prompt
# --------------------------------------------------------------------------- #
def format_research_text(research: dict) -> str:
    if not research or not research.get("enabled"):
        return ""
    cap = int(research.get("max_total_chars", DEFAULT_TOTAL_CHARS))
    lines: list[str] = []
    for s in research.get("sources", []):
        items = s.get("items") or []
        if not items:
            continue
        lang = f" [{s['lang']}]" if s.get("lang") else ""
        lines.append(f"## {s['name']}{lang}")
        for it in items:
            date = f"[{it['date']}] " if it.get("date") else ""
            link = f"  ({it['link']})" if it.get("link") else ""
            summary = f"\n    {it['summary']}" if it.get("summary") else ""
            lines.append(f"  - {date}{it['title']}{link}{summary}")
        lines.append("")
    text = "\n".join(lines).strip()
    if len(text) > cap:
        text = text[:cap].rstrip() + "\n[... przycięto ...]"
    return text
