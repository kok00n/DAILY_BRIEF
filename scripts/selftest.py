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
check("monday -> 48h lookback", wmon.lookback_hours == 48
      and abs((wmon.now - wmon.start).total_seconds() - 48 * 3600) < 1)
check("tuesday -> 24h lookback", not wtue.is_monday_after_weekend and wtue.lookback_hours == 24
      and abs((wtue.now - wtue.start).total_seconds() - 24 * 3600) < 1)
from dailybrief.collectors.news_perplexity import _time_filter  # noqa: E402
check("Perplexity Tue-Fri -> 24h recency", _time_filter(wtue) == {"search_recency_filter": "day"})
check("Perplexity Mon -> 48h after-date (no recency)",
      "search_after_date_filter" in _time_filter(wmon)
      and "search_recency_filter" not in _time_filter(wmon), str(_time_filter(wmon)))
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

print("== dialogue mode ==")
from dailybrief.synthesize import _split_turns  # noqa: E402
from dailybrief.generate_script import _system_blocks  # noqa: E402
_turns = _split_turns("[[A]] Dzień dobry, rates. [[B]] Tak, Bund wczoraj. [[A]] I PL.")
check("dialogue split into 3 turns", len(_turns) == 3, str(_turns))
check("turn speakers A/B/A", [t[0] for t in _turns] == ["A", "B", "A"])
check("turn text carries no markers", all("[[" not in t[1] for t in _turns))
check("clean_for_tts strips leftover speaker marker", "[[" not in clean_for_tts("[[A]] tekst", {}))
check("no-marker section -> 1 default-A turn", _split_turns("zwykły tekst") == [("A", "zwykły tekst")])
_dsys = _system_blocks(cfg, targets, "system_dialogue.md", "pl")[0]["text"]
check("PL dialogue prompt loads + formats", "[[A]]" in _dsys and "DIALOG" in _dsys.upper()
      and "{minutes}" not in _dsys, _dsys[:60])
_densys = _system_blocks(cfg, _section_targets(cfg, "en"), "system_dialogue_en.md", "en")[0]["text"]
check("EN dialogue prompt loads + formats", "[[A]]" in _densys and "DIALOGUE" in _densys.upper()
      and "{total_words}" not in _densys)

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
check("perplexity time filter is window-based, one filter only",
      npx._time_filter(win_today) in ({"search_recency_filter": "day"},
                                      {"search_after_date_filter": win_today.from_date_us}))
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

print("== CEE/Bund yields: deterministic source parsers ==")
from dailybrief.collectors import cee_yields as cy  # noqa: E402
from dailybrief.collectors.market_data import _fmt_quote  # noqa: E402

# Bundesbank SDMX-JSON: positional observations + dates dimension; null skipped
bb = cy._parse_bundesbank_json({"data": {
    "dataSets": [{"series": {"0:0:0": {"observations": {"0": [2.99], "1": [None], "2": [3.02]}}}}],
    "structure": {"dimensions": {"observation": [
        {"id": "TIME_PERIOD", "values": [{"id": "2026-06-18"}, {"id": "2026-06-19"},
                                         {"id": "2026-06-22"}]}]}}}})
d, v, prev = cy._last_two(bb)
check("Bundesbank JSON parsed (skips null)", bb == [("2026-06-18", 2.99), ("2026-06-22", 3.02)], str(bb))
check("Bundesbank latest + change_bp", (d, v, cy._change_bp(v, prev)) == ("2026-06-22", 3.02, 3),
      f"{d} {v} {cy._change_bp(v, prev)}")

ecb = cy._parse_ecb_csv("KEY,TIME_PERIOD,OBS_VALUE\nA,2026-06-18,2.99\nA,2026-06-19,3.04\n")
check("ECB csvdata parsed", ecb == [("2026-06-18", 2.99), ("2026-06-19", 3.04)], str(ecb))

cnb = cy._parse_cnb_csv("indicator_id;snapshot_id;period;value\n"
                        "X;;20260619;4,75\nX;;20260620;4.78\n")  # tolerate comma or dot
check("CNB ARAD csv parsed (YYYYMMDD + decimal)", cnb == [("2026-06-19", 4.75), ("2026-06-20", 4.78)],
      str(cnb))

fred = cy._parse_fred_csv("observation_date,IRLTLT01PLM156N\n2026-03-01,5.80\n2026-04-01,.\n2026-05-01,5.74\n")
check("FRED monthly csv parsed (skips '.')", fred[-1] == ("2026-05-01", 5.74), str(fred))

stq = cy._parse_stooq_csv("Date,Open,High,Low,Close,Volume\n"
                          "2026-06-19,5.60,5.62,5.58,5.61,0\n2026-06-22,5.61,5.65,5.60,5.64,0\n")
check("Stooq CSV parsed (Close = yield, col 4)", stq == [("2026-06-19", 5.61), ("2026-06-22", 5.64)],
      str(stq))
check("Stooq symbol candidates toggle 'y'", cy._stooq_candidates("10yply.b") == ["10yply.b", "10ply.b"])
try:
    cy._parse_stooq_csv("Access denied")
    _denied_ok = False
except ValueError:
    _denied_ok = True
check("Stooq 'Access denied' rejected (not parsed as data)", _denied_ok)

print("== cbonds index parse (PL/CZ/HU 10Y YTM) ==")
_cb_html = ('x var indexInfo = {"country_id":{"id":1,"lbl":"Polska"},'
            '"description":"Yield to maturity on 10-year Government Bonds of Poland",'
            '"actual_value":"5,44","actual_value.numeric":5.44,"actual_date":"2026-06-23",'
            '"prev_value":"5,42","prev_value.numeric":5.422,"prev_date":"2026-06-22"}; y')
_cbp = cy._parse_cbonds(_cb_html)
check("cbonds parsed (prev+actual, dot-decimal)",
      _cbp == [("2026-06-22", 5.422), ("2026-06-23", 5.44)], str(_cbp))
_cbd, _cbv, _cbpv = cy._last_two(_cbp)
check("cbonds latest + change_bp",
      (_cbd, _cbv, cy._change_bp(_cbv, _cbpv)) == ("2026-06-23", 5.44, 2),
      f"{_cbd} {_cbv} {cy._change_bp(_cbv, _cbpv)}")
check("cbonds comma-decimal fallback (no .numeric)",
      cy._parse_cbonds('var x = {"actual_value":"4,29","actual_date":"2026-06-23"};')
      == [("2026-06-23", 4.29)])

print("== PL snapshot parse (hardened) ==")
check("PL dated line", cy._parse_pl_line("PL=5.74,+3,2026-06-19") == (5.74, 3, "2026-06-19"))
check("PL change-only line", cy._parse_pl_line("PL=5.74,-2") == (5.74, -2, None))
check("PL level-only line", cy._parse_pl_line("PL=5.74") == (5.74, None, None))
check("PL=na -> rejected", cy._parse_pl_line("PL=na") is None)

print("== validation guards ==")
check("within bounds", cy._within(5.74, -2.0, 25.0) and not cy._within(None, -2, 25)
      and not cy._within(40.0, -2, 25))
check("near anchor band", cy._near_anchor(5.74, 5.70, 1.5) and not cy._near_anchor(8.0, 5.70, 1.5)
      and cy._near_anchor(5.74, None, 1.5))
check("recent date check", cy._recent("2026-06-16", wtue) and not cy._recent("2026-05-01", wtue)
      and not cy._recent(None, wtue))

print("== freshness labelling (never stale-as-fresh) ==")
mq = cy._mk_quote("PL 10Y", 5.74, None, "2026-05-31", "fred/oecd (monthly)", "cee", "monthly")
dq = cy._mk_quote("DE 10Y (Bund)", 3.02, 3, "2026-06-22", "bundesbank", "cores", "daily")
check("monthly quote flagged 'dane miesięczne'", "dane miesięczne" in _fmt_quote(mq), _fmt_quote(mq))
check("daily quote not flagged", "dane miesięczne" not in _fmt_quote(dq)
      and "3.020%" in _fmt_quote(dq), _fmt_quote(dq))
check("quote schema matches market_data (cat/freshness/is_yield)",
      mq["cat"] == "cee" and dq["cat"] == "cores" and mq["is_yield"] and dq["ok"])

print("== swap rates (IRS, BlueGamma) ==")
from dailybrief.collectors import swap_rates as sw  # noqa: E402
_rows = [
    {"tenor": "2Y", "rate": 4.0568, "change": -0.063, "data_timestamp": "2026-06-22T13:00:00", "currency": "PLN"},
    {"tenor": "5Y", "rate": 4.1370, "change": -0.050, "data_timestamp": "2026-06-22T13:00:00", "currency": "PLN"},
    {"tenor": "10Y", "rate": 4.3935, "change": -0.030, "data_timestamp": "2026-06-22T13:00:00", "currency": "PLN"},
]
_pts, _asof = sw._parse_curve(_rows)
check("swap curve parsed (asof date + rate)", _asof == "2026-06-22" and _pts["10Y"]["rate"] == 4.3935,
      f"{_asof} {_pts.get('10Y')}")
_sl = sw._slopes(_pts, [["2Y", "10Y"], ["5Y", "10Y"]])
_s210 = next(s for s in _sl if s["name"] == "2s10s")
check("2s10s level (bp)", _s210["bp"] == 34, str(_s210))
check("2s10s daily change = steepening +3bp", _s210["chg_bp"] == 3, str(_s210))
_txt = sw.format_swaps_text({"curves": {"PLN": {"index": "6M WIBOR", "asof": "2026-06-22",
        "levels": _pts, "slopes": _sl}}})
check("swap text: levels + slope + direction",
      "2s10s +34 bp" in _txt and "stromienie" in _txt and "10Y 4.39" in _txt, _txt)
check("empty swaps -> empty text", sw.format_swaps_text({"curves": {}}) == "")

print("== ASW (cbonds govie - BlueGamma swap) ==")
_gov = [
    {"name": "PL 10Y", "value": 5.44, "change_bp": 2, "is_yield": True, "ok": True},
    {"name": "PL 5Y", "value": 4.80, "change_bp": 1, "is_yield": True, "ok": True},
]
_cur = {"PLN": {"levels": {"5Y": {"rate": 4.50, "chg_pp": -0.01},
                           "10Y": {"rate": 4.39, "chg_pp": -0.03}}}}
_asw = sw.compute_asw(_gov, _cur)
_a10 = next(a for a in _asw if a["tenor"] == "10Y")
_a5 = next(a for a in _asw if a["tenor"] == "5Y")
check("ASW 10Y level (govie minus swap, bp)", _a10["bp"] == 105, str(_a10))
check("ASW 10Y daily change (2 - (-3))", _a10["chg_bp"] == 5, str(_a10))
check("ASW 5Y level", _a5["bp"] == 30, str(_a5))
_aswtxt = sw.format_asw_text(_asw)
check("ASW text: level + swap ccy", "10Y +105 bp" in _aswtxt and "PLN swap" in _aswtxt, _aswtxt)
check("empty ASW -> empty text", sw.format_asw_text([]) == "")

print("== FX (ECB deterministic) ==")
from dailybrief.collectors import fx_rates as fx  # noqa: E402
_exr = fx._parse_exr_csv("KEY,TIME_PERIOD,OBS_VALUE\nA,2026-06-23,1.1480\nA,2026-06-24,1.1523\n")
check("ECB EXR csv parsed", _exr == [("2026-06-23", 1.148), ("2026-06-24", 1.1523)], str(_exr))
_ex = {"USD": 1.15, "PLN": 4.25, "JPY": 160.0, "GBP": 0.85, "CAD": 1.55, "SEK": 11.0, "CHF": 0.92}
check("EUR/USD = ECB USD-per-EUR", fx._pair_value("EUR/USD", _ex) == 1.15)
check("EUR/PLN = ECB PLN-per-EUR", fx._pair_value("EUR/PLN", _ex) == 4.25)
check("USD/PLN derived (PLN/USD)", abs(fx._pair_value("USD/PLN", _ex) - 4.25 / 1.15) < 1e-9)
check("USD/JPY derived (JPY/USD)", abs(fx._pair_value("USD/JPY", _ex) - 160.0 / 1.15) < 1e-9)
_dxyv = fx._pair_value("DXY", _ex)
check("DXY computed from basket (sane range)", 80 < _dxyv < 120, f"{_dxyv:.2f}")

print("== cover art ==")
from dailybrief import cover as coverm  # noqa: E402
png = coverm.generate_cover("Poranny Brief", "Makro & Rates")
check("cover is a PNG >= 1KB", png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 1000, str(len(png)))
xml_cov = publish._build_feed(cfg, fake_eps, "https://pub-xxxx.r2.dev",
                              "https://pub-xxxx.r2.dev/cover.png").decode("utf-8")
check("feed carries itunes:image when cover set",
      "itunes:image" in xml_cov and "cover.png" in xml_cov)

print("== editions (PL + EN) ==")
from dailybrief.generate_script import _outline_text  # noqa: E402
eds = cfg.get("editions", default=[])
check("two editions configured", len(eds) >= 2, str([e.get("id") for e in eds]))
en = next((e for e in eds if e.get("id") == "en"), None)
check("EN edition: lang=en, en-* voice, no pronunciations",
      bool(en) and en["language"] == "en" and en["voice"].startswith("en-")
      and en["apply_pronunciations"] is False, str(en))
t_en = _section_targets(cfg, "en")
rates_en = next((t for t in t_en if t["id"] == "rates"), {})
check("EN section titles applied", "Rates and bonds" in rates_en.get("title", ""),
      rates_en.get("title"))
check("EN outline wording", "target ~" in _outline_text(t_en, "en"))
two = [
    {"date": "20260622", "edition": "pl", "title": "Brief PL — 20260622", "summary": "s",
     "mp3_key": "episodes/brief_20260622.mp3", "url": "https://x/a.mp3", "duration_s": 2400,
     "size_bytes": 1, "pubdate": datetime(2026, 6, 22, 4, 30, tzinfo=timezone.utc).isoformat()},
    {"date": "20260622", "edition": "en", "title": "[EN] Brief — 20260622", "summary": "s",
     "mp3_key": "episodes/brief_20260622_en.mp3", "url": "https://x/b.mp3", "duration_s": 2400,
     "size_bytes": 1, "pubdate": datetime(2026, 6, 22, 4, 31, tzinfo=timezone.utc).isoformat()},
]
xml2 = publish._build_feed(cfg, two, "https://pub-xxxx.r2.dev",
                           "https://pub-xxxx.r2.dev/cover.jpg").decode("utf-8")
check("feed has 2 items (PL+EN)", xml2.count("<item>") == 2, str(xml2.count("<item>")))
check("feed includes [EN] item", "[EN]" in xml2)

print("== weekly: window + config extends ==")
from dailybrief.util import ROOT, compute_weekly_window  # noqa: E402
wk_ref = datetime(2026, 6, 24, 18, 0, tzinfo=ZoneInfo(tz))   # a Wednesday eve
wwin = compute_weekly_window(tz, 7, reference=wk_ref)
check("weekly window kind=weekly + is_weekly", wwin.kind == "weekly" and wwin.is_weekly)
check("weekly window spans ~7 days", abs((wwin.now - wwin.start).total_seconds() - 7 * 86400) < 1)
check("weekly Perplexity -> 'week' recency",
      npx._time_filter(wwin) == {"search_recency_filter": "week"}, str(npx._time_filter(wwin)))

wcfg = load_config(ROOT / "config_weekly.yaml")
check("weekly config: cadence=weekly", wcfg.get("general", "cadence") == "weekly")
check("weekly config: 35 min target", wcfg.target_minutes == 35, str(wcfg.target_minutes))
check("weekly inherits pronunciations (extends)",
      "hawkish" in (wcfg.get("voice", "pronunciations", default={}) or {}))
check("weekly inherits market universe (extends)",
      bool(wcfg.get("markets", "rates_cores", default=[])))
check("weekly research enabled + sources",
      wcfg.get("research", "enabled") is True and len(wcfg.get("research", "sources", default=[])) >= 3)
check("weekly collectors: social off, research on",
      wcfg.get("collectors", "social") is False and wcfg.get("collectors", "research") is True)
check("weekly publish key_prefix", wcfg.get("publish", "key_prefix") == "weekly/")
check("weekly perplexity topics_include incl cee_research",
      "cee_research" in (wcfg.get("perplexity", "topics_include", default=[]) or []))
_war = wcfg.get("news", "allow_domains", default={}).get("cee_research", [])
check("weekly cee_research allowlist present + <=20", bool(_war) and len(_war) <= 20, str(len(_war)))
check("weekly inherits daily cee allowlist (deep-merge)",
      bool(wcfg.get("news", "allow_domains", default={}).get("cee")))
_weds = wcfg.get("editions", default=[])
check("weekly editions use weekly prompts",
      len(_weds) == 2 and all("weekly" in e.get("dialogue_prompt", "") for e in _weds))
_wsec_ids = [s["id"] for s in wcfg.sections]
check("weekly sections incl rates + week_ahead + research_views",
      {"rates", "week_ahead", "research_views"} <= set(_wsec_ids), str(_wsec_ids))

print("== weekly: research_portals (RSS/Atom digest) ==")
from dailybrief.collectors import research_portals as rp  # noqa: E402
_rss = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>Feed</title>
<item><title>Poland rates: NBP seen on hold</title><link>https://think.ing.com/a</link>
<pubDate>Mon, 22 Jun 2026 08:00:00 GMT</pubDate>
<description>&lt;p&gt;POLGB yields fell 5bp.&lt;/p&gt;</description></item>
<item><title>US tech earnings preview</title><link>https://x/b</link>
<pubDate>Mon, 22 Jun 2026 09:00:00 GMT</pubDate><description>Nasdaq</description></item>
<item><title>Old Poland note</title><link>https://x/c</link>
<pubDate>Wed, 01 Jan 2020 08:00:00 GMT</pubDate><description>stale</description></item>
</channel></rss>"""
_items = rp.parse_feed(_rss)
check("RSS parsed (3 items)", len(_items) == 3, str(len(_items)))
check("RSS strips HTML in summary", _items[0]["summary"] == "POLGB yields fell 5bp.", _items[0]["summary"])
check("RSS link + date parsed", _items[0]["link"] == "https://think.ing.com/a"
      and _items[0]["date"] is not None)
_sel = rp.select_items(_items, wwin, ["poland"], 10)
check("select keeps in-window keyword match only", [i["title"] for i in _sel] == ["Poland rates: NBP seen on hold"],
      str([i["title"] for i in _sel]))
_atom = ('<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>CNB holds rates</title>'
         '<link href="https://www.cnb.cz/x" rel="alternate"/>'
         '<updated>2026-06-23T10:00:00Z</updated><summary>statement</summary></entry></feed>')
_aitems = rp.parse_feed(_atom)
check("Atom parsed (link href + updated)", len(_aitems) == 1
      and _aitems[0]["link"] == "https://www.cnb.cz/x" and _aitems[0]["date"] is not None,
      str(_aitems))
_rtext = rp.format_research_text({"enabled": True, "max_total_chars": 18000, "sources": [
    {"name": "ING THINK", "lang": "en", "items": [
        {"title": "Poland rates", "link": "https://think.ing.com/a", "date": "2026-06-22",
         "summary": "POLGB yields fell."}]}]})
check("research text: source + dated item + summary",
      "ING THINK" in _rtext and "[2026-06-22]" in _rtext and "POLGB yields fell." in _rtext, _rtext[:160])
check("research text empty when disabled", rp.format_research_text({"enabled": False}) == "")

print("== weekly: news query set + market W/W + publish prefix ==")
_qs = npx._query_set(wcfg)
check("weekly query set adds cee_research, drops crypto",
      "cee_research" in _qs and "crypto" not in _qs, str(sorted(_qs)))
from dailybrief.collectors.market_data import _prev_by_lag  # noqa: E402
_vd = [("2026-06-24", 4.30), ("2026-06-23", 4.28), ("2026-06-17", 4.10), ("2026-06-16", 4.05)]
check("market W/W prev picks ~7d-ago value", _prev_by_lag(_vd, 7) == 4.10, str(_prev_by_lag(_vd, 7)))
check("publish state file per-prefix",
      publish._local_state_file("weekly/").name == "weekly_episodes.json"
      and publish._local_state_file("").name == "episodes.json")
_wxml = publish._build_feed(wcfg, fake_eps, "https://pub-xxxx.r2.dev", None,
                            feed_key="weekly/feed.xml").decode("utf-8")
check("weekly feed self-link uses weekly/feed.xml", "weekly/feed.xml" in _wxml)
check("weekly feed title", "Tygodnik Rynkowy CEE" in _wxml)

print("== weekly: calendar week-ahead ==")
soon_ev = (now_w + timedelta(days=3)).replace(hour=10, minute=0, second=0, microsecond=0)
fake_week = [
    {"title": "Poland CPI", "country": "PLN",
     "date": now_w.replace(hour=10, minute=0, second=0, microsecond=0).astimezone(eastern).isoformat(),
     "impact": "High", "forecast": "4.0%", "previous": "4.2%"},
    {"title": "Czech rate decision", "country": "CZK",
     "date": soon_ev.astimezone(eastern).isoformat(),
     "impact": "High", "forecast": "", "previous": ""},
    {"title": "Way out event", "country": "USD",
     "date": (now_w + timedelta(days=20)).astimezone(eastern).isoformat(),
     "impact": "High", "forecast": "", "previous": ""},
]
ec.requests.get = lambda *a, **k: _FakeResp(fake_week)  # type: ignore
wcal = ec.collect_calendar(wcfg, compute_weekly_window(cfg.tz, 7))
check("weekly calendar flagged weekly", wcal.get("weekly") is True)
check("weekly calendar keeps today + +3d, drops +20d", len(wcal["events"]) == 2, str(len(wcal["events"])))
check("weekly events carry a day label", all(e.get("day") for e in wcal["events"]))
wcaltxt = ec.format_calendar_text(wcal)
check("weekly calendar header is week-ahead", "NADCHODZĄCY TYDZIEŃ" in wcaltxt, wcaltxt[:80])

print(f"\n== RESULT: {PASS} passed, {FAIL} failed ==")
sys.exit(1 if FAIL else 0)
