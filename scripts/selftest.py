"""Offline self-test: exercises pure logic with synthetic data (no network/API).

Run:  .venv\\Scripts\\python.exe scripts\\selftest.py
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dailybrief.util import compute_window, polish_date_phrase  # noqa: E402
from dailybrief.generate_script import _parse, _section_targets, BriefScript  # noqa: E402
from dailybrief.synthesize import clean_for_tts, _split_sentences  # noqa: E402
from dailybrief.collectors.market_data import format_market_text, _quote  # noqa: E402
from dailybrief.config import load_config  # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


print("== config ==")
cfg = load_config()
check("config loads", cfg.target_minutes == 40, f"got {cfg.target_minutes}")
check("sections present", len(cfg.sections) >= 8, f"got {len(cfg.sections)}")
targets = _section_targets(cfg)
check("rates section flagged emphasis",
      any(t["emphasis"] and t["id"] == "rates" for t in targets))

print("== time window ==")
tz = "Europe/Warsaw"
mon = datetime(2026, 6, 15, 6, 0, tzinfo=ZoneInfo(tz))   # a Monday
tue = datetime(2026, 6, 16, 6, 0, tzinfo=ZoneInfo(tz))   # a Tuesday
wmon = compute_window(tz, True, reference=mon)
wtue = compute_window(tz, True, reference=tue)
check("monday -> weekend lookback", wmon.is_monday_after_weekend)
check("monday recency=week", wmon.perplexity_recency == "week")
check("tuesday -> 24h", not wtue.is_monday_after_weekend and wtue.perplexity_recency == "day")
check("polish date phrase", "czerwca 2026" in polish_date_phrase(mon),
      polish_date_phrase(mon))

print("== script parsing ==")
raw = """TITLE: Test odcinka
[[SUMMARY]]
Krótkie streszczenie odcinka na dziś.
[[/SUMMARY]]
[[SECTION:open|Otwarcie i TL;DR dnia]]
Dzień dobry. To jest otwarcie z kilkoma słowami treści tu i tam.
[[SECTION:rates|Stopy i obligacje]]
Rentowności dziesięcioletnich treasuries wzrosły o pięć punktów bazowych.
Bund zachowywał się spokojnie a polskie POLGBs były pod presją podaży.
"""
script = _parse(raw, targets)
check("title parsed", script.title == "Test odcinka", script.title)
check("summary parsed", script.summary.startswith("Krótkie"), script.summary)
check("two sections", len(script.sections) == 2, str(len(script.sections)))
check("section ids", [s["id"] for s in script.sections] == ["open", "rates"],
      str([s["id"] for s in script.sections]))
check("word counts > 0", all(s["words"] > 0 for s in script.sections))
check("full narration excludes markers", "[[" not in script.full_narration())

print("== tts cleaning ==")
dirty = "## Nagłówek\n- punkt *gwiazdka* [[SECTION:x|y]] http://example.com koniec."
clean = clean_for_tts(dirty)
check("markers removed", "[[" not in clean and "SECTION" not in clean, clean)
check("markdown removed", "#" not in clean and "*" not in clean, clean)
check("url removed", "http" not in clean, clean)
long_text = ("Zdanie testowe numer jeden. " * 400)
chunks = _split_sentences(long_text, max_chars=2800)
check("long text chunked", len(chunks) > 1, f"{len(chunks)} chunks")
check("chunks within limit", all(len(c) <= 2900 for c in chunks))
pron = cfg.get("voice", "pronunciations", default={})
spoken = clean_for_tts("Ton hawkish, DXY i VIX; risk-off, higher for longer.", pron)
check("pronunciations applied (EN jargon respelled)",
      "hołkisz" in spoken and "di eks łaj" in spoken and "wiks" in spoken
      and "hajer for longer" in spoken, spoken)
check("non-listed words untouched", "Ton" in spoken)

print("== market formatting ==")
fake = {
    "rates_cores": [
        _quote("US 10Y", 4.23, 4.18, "%", "2026-06-12", True),
        _quote("UST 2s10s", 0.35, 0.40, "bp", "2026-06-12", True),
        _quote("Bund 10Y", None, None, "%", None, True),
    ],
    "rates_cee": [_quote("PL 10Y", 5.55, 5.61, "%", "2026-06-12", True)],
    "equities": [_quote("S&P 500", 5400.0, 5430.0, "pts", "2026-06-12", False)],
    "crypto": [_quote("BTC", 68000.0, None, "USD", None, False)],
    "fear_greed": {"value": 61, "classification": "Greed"},
}
txt = format_market_text(fake)
check("market text has bp move", "punkt" in txt.lower() or "bp" in txt, txt[:200])
check("missing data labelled", "brak danych" in txt, txt[:300])

print("== rss feed build ==")
from datetime import timezone  # noqa: E402
from dailybrief import publish  # noqa: E402
fake_eps = [{
    "date": "20260613",
    "title": "Poranny Brief — 20260613",
    "summary": "Streszczenie testowe.",
    "mp3_key": "episodes/brief_20260613.mp3",
    "url": "https://pub-xxxx.r2.dev/episodes/brief_20260613.mp3",
    "duration_s": 2410,
    "size_bytes": 14_000_000,
    "pubdate": datetime(2026, 6, 13, 4, 30, tzinfo=timezone.utc).isoformat(),
}]
xml = publish._build_feed(cfg, fake_eps, "https://pub-xxxx.r2.dev").decode("utf-8")
check("feed has channel title", "Poranny Brief" in xml)
check("feed has enclosure", "<enclosure" in xml and "audio/mpeg" in xml)
check("feed has itunes duration", "40:10" in xml or "duration" in xml)
check("feed has episode title", "20260613" in xml)

print("== economic calendar ==")
from datetime import timedelta  # noqa: E402
from dailybrief.collectors import econ_calendar as ec  # noqa: E402

warsaw = ZoneInfo("Europe/Warsaw")
eastern = ZoneInfo("America/New_York")
now_w = datetime.now(warsaw)
today_ev = now_w.replace(hour=14, minute=30, second=0, microsecond=0)
yest_ev = today_ev - timedelta(days=1)
fake_json = [
    {"title": "CPI y/y", "country": "USD",
     "date": today_ev.astimezone(eastern).isoformat(),
     "impact": "High", "forecast": "3.1%", "previous": "3.0%"},
    {"title": "Low importance thing", "country": "EUR",
     "date": today_ev.astimezone(eastern).isoformat(),
     "impact": "Low", "forecast": "", "previous": ""},
    {"title": "Yesterday event", "country": "GBP",
     "date": yest_ev.astimezone(eastern).isoformat(),
     "impact": "High", "forecast": "", "previous": ""},
]


class _FakeResp:
    def __init__(self, data): self._d = data
    def raise_for_status(self): pass
    def json(self): return self._d


ec.requests.get = lambda *a, **k: _FakeResp(fake_json)  # type: ignore
win_today = compute_window(cfg.tz, True)
cal = ec.collect_calendar(cfg, win_today)
evs = cal["events"]
check("only today's events kept", len(evs) == 1, f"{len(evs)} events")
check("low impact filtered out", all(e["impact"] != "Low" for e in evs))
check("time converted to 14:30 Warsaw", evs and evs[0]["time"] == "14:30",
      evs[0]["time"] if evs else "none")
caltxt = ec.format_calendar_text(cal, win_today)
check("calendar text has forecast/prev", "prog. 3.1%" in caltxt and "poprz. 3.0%" in caltxt,
      caltxt[:200])
check("calendar text mentions USA", "USA" in caltxt)

print("== perplexity tuning ==")
from dailybrief.collectors import news_perplexity as npx  # noqa: E402
df_cee = npx._domain_filter(cfg, "cee")
df_rates = npx._domain_filter(cfg, "rates")
df_crypto = npx._domain_filter(cfg, "crypto")
df_ai = npx._domain_filter(cfg, "ai_tech")
check("cee uses allowlist (no '-')", bool(df_cee) and all(not d.startswith("-") for d in df_cee))
check("other topics use denylist ('-')", bool(df_rates) and all(d.startswith("-") for d in df_rates))
check("crypto allowlist incl. coindesk", bool(df_crypto) and "coindesk.com" in df_crypto
      and all(not d.startswith("-") for d in df_crypto))
check("ai_tech allowlist incl. arxiv", bool(df_ai) and "arxiv.org" in df_ai)
check("all domain filters <= 20",
      all(len(x) <= 20 for x in (df_cee, df_rates, df_crypto, df_ai)))
check("ai_tech recency override = week", npx._recency(cfg, "ai_tech", win_today) == "week")
check("rates recency inherits window default",
      npx._recency(cfg, "rates", win_today) == win_today.perplexity_recency)
check("sources from search_results",
      npx._extract_sources({"search_results": [{"title": "T", "url": "http://u"}]}) == ["T — http://u"])
check("sources fallback to citations",
      npx._extract_sources({"citations": ["http://x"]}) == ["http://x"])

print("== grok tuning ==")
import os  # noqa: E402
os.environ["XAI_API_KEY"] = "xai-realkey"
from dailybrief.collectors import social_grok as sg  # noqa: E402
cfg.env["XAI_API_KEY"] = "xai-realkey"
t = sg._x_search_tool("2026-06-01", "2026-06-02", True, False, allowed=["a", "b"])
check("x_search tool image flag on", t.get("enable_image_understanding") is True)
check("x_search tool allowed set", t.get("allowed_x_handles") == ["a", "b"])
groups_cfg = sg._topic_groups(cfg)
check("topic_groups incl. cee, crypto, ai_tech (>=5)",
      len(groups_cfg) >= 5 and {"cee", "crypto", "ai_tech"} <= set(groups_cfg),
      str(list(groups_cfg)))
grok_calls = []
sg._call = lambda prompt, tools, c, k: (grok_calls.append(tools[0]) or "x")
res = sg.collect_social(cfg, win_today)
xs = [c for c in grok_calls if c.get("type") == "x_search"]
topic_tools = [c for c in xs if "excluded_x_handles" in c]
prio_tools = [c for c in xs if "allowed_x_handles" in c]
check("one x_search per topic group", len(topic_tools) == len(groups_cfg), str(len(topic_tools)))
check("topic groups exclude core (<=20)",
      all(len(c["excluded_x_handles"]) <= 20 for c in topic_tools))
check("all x_search have image understanding",
      all(c.get("enable_image_understanding") for c in xs))
check("priority batches present", len(prio_tools) >= 1)
check("topics merged with group labels",
      "[rates_macro]" in res["topics"]["text"] and "[ai_tech]" in res["topics"]["text"])

print("== CEE yields snapshot parse ==")
from dailybrief.collectors import cee_yields as cy  # noqa: E402
parsed = cy._parse_snapshot("PL=5.74,+3\nCZ=4.10,-1\nHU=6.85,na\ngarbage line")
check("CEE parse PL (yield+bp)", parsed.get("PL") == (5.74, 3), str(parsed.get("PL")))
check("CEE parse CZ (negative bp)", parsed.get("CZ") == (4.10, -1), str(parsed.get("CZ")))
check("CEE parse HU (level only)", parsed.get("HU") == (6.85, None), str(parsed.get("HU")))

print(f"\n== RESULT: {PASS} passed, {FAIL} failed ==")
sys.exit(1 if FAIL else 0)
