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


def _load_script(date_str: str) -> BriefScript:
    js = OUTPUT_DIR / f"script_{date_str}.json"
    data = json.loads(js.read_text(encoding="utf-8"))
    return BriefScript(title=data["title"], summary=data["summary"],
                       sections=data["sections"], raw="")


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate the daily morning brief.")
    ap.add_argument("--skip-audio", action="store_true")
    ap.add_argument("--skip-publish", action="store_true")
    ap.add_argument("--local", action="store_true", help="force local publish mode")
    ap.add_argument("--reuse-dossier", action="store_true")
    ap.add_argument("--reuse-script", action="store_true")
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

        # 2) script
        if args.reuse_script and (OUTPUT_DIR / f"script_{date_str}.json").exists():
            script = _load_script(date_str)
            log.info("reusing existing script (%d words)", script.total_words)
        else:
            script = generate_script.generate_script(cfg, window, research_text)
            generate_script.save_script(script, date_str)

        est_min = script.total_words / cfg.wpm
        log.info("script: \"%s\" | %d words (~%.1f min spoken)",
                 script.title, script.total_words, est_min)

        if args.skip_audio:
            log.info("done (audio skipped). Script at output/script_%s.txt", date_str)
            return 0

        # 3) audio
        audio = synthesize.synthesize(cfg, script, date_str)

        # 4) publish
        if args.skip_publish:
            log.info("done (publish skipped). MP3 at %s", audio["path"])
            return 0
        result = publish.publish(cfg, audio, script, date_str)

        log.info("=== DONE in %.0fs | mode=%s | episodes=%d ===",
                 time.time() - t0, result["mode"], result["episodes"])
        if result.get("feed_url"):
            log.info("RSS feed: %s", result["feed_url"])
            log.info("Episode : %s", result["episode_url"])
        if result.get("transcript_url"):
            log.info("Transcript: %s", result["transcript_url"])
        return 0

    except Exception as e:  # noqa: BLE001
        log.error("PIPELINE FAILED: %s", e)
        log.error(traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
