"""Generate the ~40-minute Polish narration script with Claude Opus 4.8."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anthropic

from .config import Config
from .util import LookbackWindow, OUTPUT_DIR, PROMPTS_DIR, polish_date_phrase

log = logging.getLogger("dailybrief.script")

SECTION_RE = re.compile(r"\[\[SECTION:([^|\]]+)\|([^\]]*)\]\]")
SUMMARY_RE = re.compile(r"\[\[SUMMARY\]\](.*?)\[\[/SUMMARY\]\]", re.DOTALL)
TITLE_RE = re.compile(r"^\s*TITLE:\s*(.+)$", re.MULTILINE)


@dataclass
class BriefScript:
    title: str
    summary: str
    sections: list[dict] = field(default_factory=list)   # {id,title,text,words}
    raw: str = ""

    @property
    def total_words(self) -> int:
        return sum(s["words"] for s in self.sections)

    def full_narration(self) -> str:
        return "\n\n".join(s["text"].strip() for s in self.sections if s["text"].strip())


def _word_count(text: str) -> int:
    return len(re.findall(r"\S+", text))


def _section_targets(cfg: Config) -> list[dict]:
    wpm = cfg.wpm
    out = []
    for s in cfg.sections:
        out.append({
            "id": s["id"], "title": s["title"],
            "target_words": round(s.get("target_min", 3) * wpm),
            "emphasis": bool(s.get("emphasis")),
            "feeds": s.get("feeds", []),
        })
    return out


def _outline_text(targets: list[dict]) -> str:
    lines = []
    for i, t in enumerate(targets, 1):
        star = "  (SEKCJA KLUCZOWA — najwięcej uwagi)" if t["emphasis"] else ""
        lines.append(
            f'{i}. id="{t["id"]}", tytuł="{t["title"]}", '
            f'cel ~{t["target_words"]} słów{star}'
        )
    return "\n".join(lines)


def _parse(raw: str, targets: list[dict]) -> BriefScript:
    title_m = TITLE_RE.search(raw)
    title = title_m.group(1).strip() if title_m else "Poranny Brief"
    sum_m = SUMMARY_RE.search(raw)
    summary = sum_m.group(1).strip() if sum_m else ""

    sections: list[dict] = []
    matches = list(SECTION_RE.finditer(raw))
    for i, m in enumerate(matches):
        sid = m.group(1).strip()
        stitle = m.group(2).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        text = raw[start:end].strip()
        sections.append({"id": sid, "title": stitle, "text": text,
                         "words": _word_count(text)})

    # if model ignored markers, fall back to a single block
    if not sections:
        body = raw
        if sum_m:
            body = raw[sum_m.end():]
        sections = [{"id": "full", "title": "Brief", "text": body.strip(),
                     "words": _word_count(body)}]
    return BriefScript(title=title, summary=summary, sections=sections, raw=raw)


def _client(cfg: Config) -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=cfg.require_env("ANTHROPIC_API_KEY"))


def _system_blocks(cfg: Config, targets: list[dict]) -> list[dict]:
    tmpl = (PROMPTS_DIR / "system_brief.md").read_text(encoding="utf-8")
    total_words = round(cfg.target_minutes * cfg.wpm)
    system = tmpl.format(
        minutes=cfg.target_minutes,
        total_words=total_words,
        section_outline=_outline_text(targets),
    )
    return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]


def _stream_text(client: anthropic.Anthropic, **kwargs) -> str:
    """Stream the completion. Streaming is required for large max_tokens (the SDK
    refuses non-streaming requests that could exceed 10 minutes)."""
    parts: list[str] = []
    with client.messages.stream(**kwargs) as stream:
        for chunk in stream.text_stream:
            parts.append(chunk)
    return "".join(parts)


def _user_message(window: LookbackWindow, research_text: str) -> str:
    return (
        f"Dzisiaj jest {polish_date_phrase(window.now)}. Przygotuj brief obejmujący "
        f"wydarzenia z {window.label_pl}.\n\n"
        f"Poniżej materiał źródłowy (dane rynkowe, newsy, FinTwit/analitycy). "
        f"Wykorzystaj go, syntetyzuj, łącz wątki i nadawaj im sens — nie czytaj go "
        f"punkt po punkcie. Jeśli czegoś brakuje, powiedz to krótko.\n\n"
        f"{research_text}"
    )


def generate_script(cfg: Config, window: LookbackWindow, research_text: str) -> BriefScript:
    client = _client(cfg)
    targets = _section_targets(cfg)
    model = cfg.get("claude", "model", default="claude-opus-4-8")
    max_tokens = int(cfg.get("claude", "max_tokens", default=32000))

    log.info("generating script with %s (target ~%d words)...",
             model, round(cfg.target_minutes * cfg.wpm))
    raw = _stream_text(
        client,
        model=model,
        max_tokens=max_tokens,
        system=_system_blocks(cfg, targets),
        messages=[{"role": "user", "content": _user_message(window, research_text)}],
    )
    script = _parse(raw, targets)
    log.info("script v1: %d words across %d sections",
             script.total_words, len(script.sections))

    target_total = round(cfg.target_minutes * cfg.wpm)
    if script.total_words < 0.85 * target_total:
        script = _expand(client, cfg, window, research_text, script, targets, model,
                         max_tokens)
    return script


def _expand(client, cfg, window, research_text, script, targets, model,
            max_tokens) -> BriefScript:
    by_id = {t["id"]: t for t in targets}
    short = [s for s in script.sections
             if s["id"] in by_id and s["words"] < 0.8 * by_id[s["id"]]["target_words"]]
    if not short:
        return script
    deficits = "\n".join(
        f'- {s["id"]} ("{s["title"]}"): masz ~{s["words"]} słów, cel '
        f'~{by_id[s["id"]]["target_words"]} słów'
        for s in short
    )
    log.info("script too short (%d w); expanding %d sections",
             script.total_words, len(short))
    instruction = (
        "Twój poprzedni brief jest za krótki. Poniżej masz pełny dotychczasowy "
        "skrypt. Zwróć GO PONOWNIE W CAŁOŚCI w tym samym formacie (TITLE, "
        "[[SUMMARY]], sekcje z markerami [[SECTION:id|Tytuł]]), ale rozbuduj "
        "wskazane sekcje do docelowej długości — pogłębiając analizę, kontekst, "
        "mechanizmy i implikacje, bez zmyślania nowych liczb. Pozostałe sekcje "
        "możesz zostawić bez zmian.\n\n"
        f"Sekcje do rozbudowania:\n{deficits}\n\n"
        "=== DOTYCHCZASOWY SKRYPT ===\n"
        f"{script.raw}\n\n"
        "=== MATERIAŁ ŹRÓDŁOWY (do wykorzystania przy rozbudowie) ===\n"
        f"{research_text}"
    )
    raw = _stream_text(
        client, model=model, max_tokens=max_tokens,
        system=_system_blocks(cfg, targets),
        messages=[{"role": "user", "content": instruction}],
    )
    expanded = _parse(raw, targets)
    if expanded.total_words > script.total_words:
        log.info("expanded to %d words", expanded.total_words)
        return expanded
    log.info("expansion did not help; keeping original (%d words)", script.total_words)
    return script


def save_script(script: BriefScript, date_str: str) -> dict[str, Path]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    txt = OUTPUT_DIR / f"script_{date_str}.txt"
    txt.write_text(script.full_narration(), encoding="utf-8")
    js = OUTPUT_DIR / f"script_{date_str}.json"
    js.write_text(json.dumps({
        "title": script.title, "summary": script.summary,
        "total_words": script.total_words,
        "sections": script.sections,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("script saved -> %s", txt.name)
    return {"txt": txt, "json": js}
