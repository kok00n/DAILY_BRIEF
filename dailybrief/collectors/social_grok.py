"""Social / FinTwit collector via xAI Grok Agent Tools (Responses API).

Uses server-side `x_search` (over a curated list of FinTwit / official handles)
and `web_search` (analytical Substacks + breaking commentary). No X API needed.

Endpoint : POST https://api.x.ai/v1/responses
Model     : grok-4.3
Tools     : x_search { allowed_x_handles, from_date, to_date }, web_search
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import requests

from ..config import Config
from ..util import LookbackWindow

log = logging.getLogger("dailybrief.social")

ENDPOINT = "https://api.x.ai/v1/responses"
HTTP_TIMEOUT = 120


def _parse_responses_output(data: dict) -> str:
    """Extract concatenated text from an xAI/OpenAI Responses-API payload."""
    if isinstance(data.get("output_text"), str) and data["output_text"].strip():
        return data["output_text"].strip()
    chunks: list[str] = []
    for item in data.get("output", []) or []:
        if item.get("type") == "message":
            for c in item.get("content", []):
                if c.get("type") in ("output_text", "text") and c.get("text"):
                    chunks.append(c["text"])
    # fallback: some payloads nest under choices
    if not chunks:
        try:
            chunks.append(data["choices"][0]["message"]["content"])
        except Exception:  # noqa: BLE001
            pass
    return "\n".join(chunks).strip()


def _x_search_tool(frm: str, to: str, img: bool, vid: bool,
                   allowed: list[str] | None = None,
                   excluded: list[str] | None = None) -> dict:
    tool: dict[str, Any] = {"type": "x_search", "from_date": frm, "to_date": to}
    if img:
        tool["enable_image_understanding"] = True
    if vid:
        tool["enable_video_understanding"] = True
    if allowed:
        tool["allowed_x_handles"] = allowed
    if excluded:
        tool["excluded_x_handles"] = excluded
    return tool


def _call(prompt: str, tools: list[dict], cfg: Config, api_key: str,
          attempts: int = 3) -> str:
    body = {
        "model": cfg.get("grok", "model", default="grok-4.3"),
        "input": [{"role": "user", "content": prompt}],
        "tools": tools,
        "max_output_tokens": cfg.get("grok", "max_output_tokens", default=4000),
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    last: Exception | None = None
    for i in range(1, attempts + 1):
        try:
            r = requests.post(ENDPOINT, json=body, headers=headers, timeout=HTTP_TIMEOUT)
            if r.status_code == 429 or r.status_code >= 500:
                raise RuntimeError(f"retryable xAI {r.status_code}: {r.text[:200]}")
            if r.status_code >= 400:
                raise RuntimeError(f"xAI {r.status_code}: {r.text[:300]}")  # fail fast
            return _parse_responses_output(r.json())
        except requests.RequestException as e:
            last = e
        except RuntimeError as e:
            if "retryable" not in str(e):
                raise
            last = e
        if i < attempts:
            time.sleep(2 * i)
    raise last  # type: ignore[misc]


QUALITY_BIAS = (
    "PRIORITISE credible and expert voices: official accounts (central banks, "
    "statistical offices, finance ministries), well-known sell-side strategists, "
    "economists and reputable journalists. IGNORE anonymous pump accounts, "
    "engagement-bait, perma-bears/bulls and unverified rumours — or, if a rumour "
    "is clearly market-relevant, flag it explicitly as UNVERIFIED. "
)


BATCH_SIZE = 20  # xAI x_search allowed_x_handles hard cap


def _priority_prompt(handles: list[str], frm: str, to: str, win: str) -> str:
    return (
        f"Search X posts from {frm} to {to} ONLY from these must-read accounts and "
        f"summarise their most important posts over {win} for a rates/macro trader: "
        f"{', '.join('@' + h for h in handles)}. Capture key claims, levels, forecasts "
        "and any disagreement between them. Attribute to @handle. Tight bullets, English."
    )


def _topic_prompt(themes: list[str], frm: str, to: str, win: str) -> str:
    theme_list = "\n".join(f"  - {t}" for t in themes)
    return (
        f"Search ALL of X (keyword + semantic, including cashtags) for posts from {frm} "
        f"to {to}. Surface the most important, most-discussed posts over {win} on these "
        f"themes:\n{theme_list}\n\n"
        f"{QUALITY_BIAS}For each theme give concrete facts, claims and numbers (levels, "
        "basis-point moves, prices). For MARKET themes also capture the dominant FinTwit "
        "narrative and positioning (the crowd's current trade), strong contrarian takes, and "
        "sentiment extremes (capitulation or euphoria). For AI/TECH themes capture what is "
        "genuinely new, why it matters, and concrete practical use-cases. Read charts and "
        "tables inside posts. Attribute to @handles. Tight bullet points, English, no fluff."
    )


def _topic_groups(cfg: Config) -> dict[str, list[str]]:
    """Prefer grok.topic_groups (name -> themes); fall back to a flat grok.topics."""
    groups = cfg.get("grok", "topic_groups", default=None)
    if isinstance(groups, dict) and groups:
        return {k: (v or []) for k, v in groups.items() if v}
    flat = cfg.get("grok", "topics", default=[]) or []
    return {"topics": flat} if flat else {}


def collect_social(cfg: Config, window: LookbackWindow) -> dict[str, Any]:
    api_key = cfg.require_env("XAI_API_KEY")
    groups = _topic_groups(cfg)
    priority = cfg.get("grok", "priority_handles", default=[]) or []
    frm, to = window.from_date_iso, window.now.date().isoformat()
    win = "the weekend (since Friday)" if window.is_monday_after_weekend else "the last 24 hours"

    web_prompt = (
        f"Find the most insightful analytical macro/rates writing and market commentary "
        f"published over {win} — especially Substack newsletters (macro, rates, fixed "
        "income, CEE) and notable analyst threads. Summarise the key theses, forecasts and "
        "arguments worth knowing, with author/source attribution. Also surface 1-3 genuinely "
        "interesting AI/technology items with practical use-cases. Tight bullets, English."
    )

    img = bool(cfg.get("grok", "enable_image_understanding", default=True))
    vid = bool(cfg.get("grok", "enable_video_understanding", default=False))
    dedup = bool(cfg.get("grok", "dedup_topics_from_priority", default=True))
    # topic passes search all of X but optionally exclude the core (already covered
    # by the priority pass) so they focus on discovery. excluded_x_handles cap = 20.
    excluded = priority[:20] if (dedup and priority) else None

    x_tool = [_x_search_tool(frm, to, img, vid, excluded=excluded)]
    web_tool = [{"type": "web_search"}]

    jobs: dict[str, tuple[str, list]] = {"analysts_web": (web_prompt, web_tool)}
    # one x_search per topic group (deeper coverage; rates gets its own pass)
    for gname, themes in groups.items():
        jobs[f"_topic::{gname}"] = (_topic_prompt(themes, frm, to, win), x_tool)
    # priority handles -> batches of <=20 (xAI cap), one x_search per batch
    batches = [priority[i:i + BATCH_SIZE] for i in range(0, len(priority), BATCH_SIZE)]
    for i, batch in enumerate(batches):
        tool = [_x_search_tool(frm, to, img, vid, allowed=batch)]
        jobs[f"_prio{i}"] = (_priority_prompt(batch, frm, to, win), tool)

    raw: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=max(2, len(jobs))) as ex:
        futs = {ex.submit(_call, p, t, cfg, api_key): k for k, (p, t) in jobs.items()}
        for fut in as_completed(futs):
            k = futs[fut]
            try:
                raw[k] = {"text": fut.result()}
            except Exception as e:  # noqa: BLE001
                log.warning("grok '%s' failed: %s", k, e)
                raw[k] = {"text": "", "error": str(e)}

    out: dict[str, Any] = {"analysts_web": raw.get("analysts_web", {})}
    # merge topic groups -> one 'topics' section, labelled by group
    topic_parts = []
    for k in sorted(key for key in raw if key.startswith("_topic::")):
        if raw[k].get("text"):
            topic_parts.append(f"[{k.split('::', 1)[1]}]\n{raw[k]['text']}")
    out["topics"] = {"text": "\n\n".join(topic_parts)}
    # merge the priority batches -> one 'priority' section
    prio_keys = sorted(k for k in raw if k.startswith("_prio"))
    prio_texts = [raw[k]["text"] for k in prio_keys if raw[k].get("text")]
    out["priority"] = {"text": "\n\n".join(prio_texts)}
    if not prio_texts:
        errs = [raw[k].get("error", "") for k in prio_keys]
        out["priority"]["error"] = "; ".join(e for e in errs if e)

    ok = sum(1 for v in out.values() if v.get("text"))
    log.info("social: %d sections with content (topics in %d group(s), "
             "priority %d handles in %d batch(es))",
             ok, len(groups), len(priority), len(batches))
    return out


def format_social_text(social: dict) -> str:
    blocks = []
    titles = {
        "topics": "X — TOPIC SEARCH (all of X, quality-biased)",
        "priority": "X — PRIORITY ACCOUNTS (never-miss core)",
        "analysts_web": "ANALYSTS / SUBSTACKS / WEB",
    }
    for k, title in titles.items():
        r = social.get(k) or {}
        if r.get("text"):
            blocks.append(f"### {title}\n{r['text']}")
    return "\n\n".join(blocks).strip()
