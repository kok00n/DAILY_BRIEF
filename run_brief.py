#!/usr/bin/env python
"""DAILY_BRIEF orchestrator.

Full pipeline:  collect -> aggregate -> generate script -> synthesize -> publish

Usage:
  python run_brief.py                 # full run
  python run_brief.py --skip-audio    # collect + write script only
  python run_brief.py --skip-publish  # everything except upload/RSS
  python run_brief.py --local         # force local publish (no R2)
  python run_brief.py --reuse-dossier # skip collectors, load today's dossier
  python run_brief.py --reuse-script  # skip Claude, load today's script
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from datetime import datetime

from dailybrief import aggregate, generate_script, publish, synthesize
from dailybrief.config import load_config
from dailybrief.generate_script import BriefScript
from dailybrief.util import OUTPUT_DIR, compute_window, setup_logging


def _script_json(date_str: str, edition_id: str = "pl") -> "Path":
    tag = "" if edition_id in ("", "pl") else f"_{edition_id}"
    return OUTPUT_DIR / f"script_{date_str}{tag}.json"


def _log_data_check(log, dossier: dict) -> None:
    """Concise data-ingestion summary for a --collect-only run, so you can verify
    in the Actions log that Stooq/Bundesbank/FRED/Yahoo/CoinGecko/news/social all
    came through on the production environment — before paying for script gen."""
    m = dossier.get("market", {}) or {}
    log.info("---------- DATA CHECK ----------")
    for cat in ("rates_cores", "rates_cee", "fx", "equities", "commodities", "crypto"):
        qs = m.get(cat, []) or []
        ok = sum(1 for q in qs if q.get("ok"))
        log.info("%-12s %d/%d ok", cat, ok, len(qs))
    # full detail on the yields (the thing we just reworked)
    for cat in ("rates_cores", "rates_cee"):
        for q in m.get(cat, []) or []:
            val = q.get("value")
            vs = f"{val:.3f}{q.get('unit', '')}" if val is not None else "brak danych"
            bp = q.get("change_bp")
            chg = f" ({'+' if (bp or 0) >= 0 else ''}{bp}bp)" if bp is not None else ""
            log.info("  %-16s %-14s src=%-14s asof=%-12s %s",
                     q.get("name", "?"), vs + chg, q.get("source", "?"),
                     q.get("asof", "?"), q.get("freshness", ""))
    cee = dossier.get("cee_yields", {}) or {}
    if cee.get("via"):
        log.info("  cee/bund providers: %s", cee["via"])
    sw = dossier.get("swaps", {}) or {}
    sw_curves = sw.get("curves") or {}
    log.info("%-12s %d curves ok%s", "swaps (IRS)", len(sw_curves),
             f" — {sw['via']}" if sw.get("via") else "")
    for ccy, c in sw_curves.items():
        lv = c.get("levels") or {}
        lvls = ", ".join(f"{t} {p['rate']:.2f}%" for t, p in lv.items())
        slps = ", ".join(f"{s['name']} {'+' if s['bp'] >= 0 else ''}{s['bp']}bp" for s in c.get("slopes") or [])
        log.info("  %-4s [stan %s] %s | %s", ccy, c.get("asof"), lvls, slps)
    from dailybrief.collectors import swap_rates as _swr
    asw = _swr.compute_asw((m.get("rates_cee") or []) + (m.get("rates_cores") or []), sw_curves)
    if asw:
        _by: dict = {}
        for a in asw:
            _by.setdefault(a["country"], []).append(a)
        log.info("%-12s govie-swap, bp:", "ASW")
        for _c, _items in _by.items():
            log.info("  %-4s %s", _c, ", ".join(
                f"{a['tenor']} {'+' if a['bp'] >= 0 else ''}{a['bp']}bp"
                for a in sorted(_items, key=lambda x: int(x['tenor'][:-1]))))
    for e in (m.get("errors") or [])[:12]:
        log.info("  market error: %s", e)
    for key in ("news", "social", "calendar"):
        d = dossier.get(key, {}) or {}
        log.info("%-12s %s", key, f"error: {d['error']}" if d.get("error") else "ok")
    log.info("--------------------------------")


def _load_script(date_str: str, edition_id: str = "pl") -> BriefScript:
    data = json.loads(_script_json(date_str, edition_id).read_text(encoding="utf-8"))
    return BriefScript(title=data["title"], summary=data["summary"],
                       sections=data["sections"], raw="")


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate the daily morning brief.")
    ap.add_argument("--skip-audio", action="store_true")
    ap.add_argument("--skip-publish", action="store_true")
    ap.add_argument("--local", action="store_true", help="force local publish mode")
    ap.add_argument("--reuse-dossier", action="store_true")
    ap.add_argument("--reuse-script", action="store_true")
    ap.add_argument("--collect-only", action="store_true",
                    help="collect + aggregate only; dump dossier/research + a data-check "
                         "summary, then stop (no Claude script, no audio, no publish)")
    args = ap.parse_args()

    log = setup_logging()
    t0 = time.time()
    cfg = load_config()
    if args.local:
        # neutralise R2 so publish() uses local mode
        for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
                  "R2_PUBLIC_BASE_URL"):
            cfg.env.pop(k, None)

    window = compute_window(cfg.tz, cfg.weekend_lookback)
    date_str = window.now.strftime("%Y%m%d")
    log.info("=== DAILY_BRIEF %s | window: %s ===",
             window.now.strftime("%Y-%m-%d %H:%M %Z"), window.label_pl)

    try:
        # 1) collect + aggregate
        if args.reuse_dossier and (OUTPUT_DIR / f"dossier_{date_str}.json").exists():
            dossier = json.loads(
                (OUTPUT_DIR / f"dossier_{date_str}.json").read_text(encoding="utf-8"))
            log.info("reusing existing dossier")
        else:
            dossier = aggregate.collect_all(cfg, window)
            aggregate.save_dossier(dossier, date_str)
        research_text = aggregate.build_research_text(dossier, cfg)
        (OUTPUT_DIR / f"research_{date_str}.txt").write_text(research_text, encoding="utf-8")

        if args.collect_only:
            _log_data_check(log, dossier)
            log.info("=== collect-only DONE in %.0fs | data in dossier_%s.json / research_%s.txt "
                     "(no script/audio/publish) ===", time.time() - t0, date_str, date_str)
            return 0

        # 2) per-edition: script (+ audio). Both editions feed one publish step.
        editions = cfg.get("editions", default=None) or [
            {"id": "pl", "language": "pl", "prompt": "system_brief.md",
             "apply_pronunciations": True, "title_prefix": ""}]
        items = []
        for ed in editions:
            eid = ed.get("id", "pl")
            if args.reuse_script and _script_json(date_str, eid).exists():
                script = _load_script(date_str, eid)
                log.info("[%s] reusing existing script (%d words)", eid, script.total_words)
            else:
                script = generate_script.generate_script(cfg, window, research_text, ed)
                generate_script.save_script(script, date_str, eid)
            log.info("[%s] script: \"%s\" | %d words (~%.1f min spoken)",
                     eid, script.title, script.total_words, script.total_words / cfg.wpm)
            if args.skip_audio:
                continue
            audio = synthesize.synthesize(cfg, script, date_str, ed)
            items.append({"edition": ed, "script": script, "audio": audio})

        # 3) publish (both editions into one feed)
        if args.skip_audio:
            log.info("done (audio skipped). Scripts in output/.")
            return 0
        if args.skip_publish:
            log.info("done (publish skipped). %d edition(s) synthesized.", len(items))
            return 0
        result = publish.publish(cfg, items, date_str)

        log.info("=== DONE in %.0fs | mode=%s | episodes=%d ===",
                 time.time() - t0, result["mode"], result["episodes"])
        if result.get("feed_url"):
            log.info("RSS feed: %s", result["feed_url"])
        for it in result.get("items", []):
            log.info("[%s] Episode: %s", it["edition"], it["episode_url"])
            if it.get("transcript_url"):
                log.info("[%s] Transcript: %s", it["edition"], it["transcript_url"])
        return 0

    except Exception as e:  # noqa: BLE001
        log.error("PIPELINE FAILED: %s", e)
        log.error(traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
