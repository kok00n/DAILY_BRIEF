"""Generate the ~40-minute Polish narration script with Claude Opus 4.8."""
from __future__ import annotations

import json
import logging
import re
import time
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


def _section_targets(cfg: Config, lang: str = "pl") -> list[dict]:
    wpm = cfg.wpm
    out = []
    for s in cfg.sections:
        title = s.get("title_en") if lang == "en" and s.get("title_en") else s["title"]
        out.append({
            "id": s["id"], "title": title,
            "target_words": round(s.get("target_min", 3) * wpm),
            "emphasis": bool(s.get("emphasis")),
            "feeds": s.get("feeds", []),
        })
    return out


def _outline_text(targets: list[dict], lang: str = "pl") -> str:
    if lang == "en":
        star_txt, tmpl = "  (KEY SECTION — most attention)", \
            '{i}. id="{id}", title="{title}", target ~{tw} words{star}'
    else:
        star_txt, tmpl = "  (SEKCJA KLUCZOWA — najwięcej uwagi)", \
            '{i}. id="{id}", tytuł="{title}", cel ~{tw} słów{star}'
    lines = []
    for i, t in enumerate(targets, 1):
        lines.append(tmpl.format(i=i, id=t["id"], title=t["title"],
                                 tw=t["target_words"], star=(star_txt if t["emphasis"] else "")))
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


def _system_blocks(cfg: Config, targets: list[dict],
                   prompt_file: str = "system_brief.md", lang: str = "pl") -> list[dict]:
    tmpl = (PROMPTS_DIR / prompt_file).read_text(encoding="utf-8")
    total_words = round(cfg.target_minutes * cfg.wpm)
    system = tmpl.format(
        minutes=cfg.target_minutes,
        total_words=total_words,
        section_outline=_outline_text(targets, lang),
    )
    return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]


# permanent client errors — never worth retrying
_NON_RETRYABLE = {400, 401, 403, 404, 422}


def _stream_text(client: anthropic.Anthropic, attempts: int = 6, **kwargs) -> str:
    """Stream the completion (required for large max_tokens). Retries everything
    except permanent client errors — covers overloaded_error, whose status_code is
    NOT reliably set when it arrives as a mid-stream SSE event."""
    last: Exception | None = None
    for i in range(1, attempts + 1):
        try:
            parts: list[str] = []
            with client.messages.stream(**kwargs) as stream:
                for chunk in stream.text_stream:
                    parts.append(chunk)
            return "".join(parts)
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
            last = e
            status = getattr(e, "status_code", None)
            if status in _NON_RETRYABLE or i == attempts:
                raise
            wait = min(60, 5 * 2 ** (i - 1))   # 5, 10, 20, 40, 60, 60s
            log.warning("Claude stream attempt %d/%d failed (%s); retry in %ds",
                        i, attempts, getattr(e, "message", str(e)), wait)
            time.sleep(wait)
    raise last  # type: ignore[misc]


def _user_message(window: LookbackWindow, research_text: str, lang: str = "pl") -> str:
    if lang == "en":
        win = ("the last 48 hours (the weekend)" if window.is_monday_after_weekend
               else "the last 24 hours")
        return (
            f"Today is {window.now.strftime('%A, %d %B %Y')}. Prepare a brief covering "
            f"events from {win}.\n\n"
            "Below is the source material (market data, news, FinTwit/analysts). Use it, "
            "synthesise, connect the threads and make sense of them — do not read it point "
            "by point. If something is missing, say so briefly.\n\n"
            f"{research_text}"
        )
    return (
        f"Dzisiaj jest {polish_date_phrase(window.now)}. Przygotuj brief obejmujący "
        f"wydarzenia z {window.label_pl}.\n\n"
        f"Poniżej materiał źródłowy (dane rynkowe, newsy, FinTwit/analitycy). "
        f"Wykorzystaj go, syntetyzuj, łącz wątki i nadawaj im sens — nie czytaj go "
        f"punkt po punkcie. Jeśli czegoś brakuje, powiedz to krótko.\n\n"
        f"{research_text}"
    )


def generate_script(cfg: Config, window: LookbackWindow, research_text: str,
                    edition: dict | None = None) -> BriefScript:
    edition = edition or {"id": "pl", "language": "pl", "prompt": "system_brief.md"}
    lang = edition.get("language", "pl")
    if edition.get("format") == "dialogue":
        default_dlg = "system_dialogue_en.md" if lang == "en" else "system_dialogue.md"
        prompt_file = edition.get("dialogue_prompt", default_dlg)
    else:
        prompt_file = edition.get("prompt", "system_brief_en.md" if lang == "en" else "system_brief.md")

    client = _client(cfg)
    targets = _section_targets(cfg, lang)
    model = cfg.get("claude", "model", default="claude-opus-4-8")
    fallback = cfg.get("claude", "fallback_model", default="claude-sonnet-4-6")
    max_tokens = int(cfg.get("claude", "max_tokens", default=32000))
    models = [model] + ([fallback] if fallback and fallback != model else [])

    system = _system_blocks(cfg, targets, prompt_file, lang)
    user = _user_message(window, research_text, lang)
    target_total = round(cfg.target_minutes * cfg.wpm)

    raw, used_model = "", model
    for j, mdl in enumerate(models):
        try:
            log.info("[%s] generating script with %s (target ~%d words)...",
                     edition.get("id", lang), mdl, target_total)
            raw = _stream_text(client, model=mdl, max_tokens=max_tokens, system=system,
                               messages=[{"role": "user", "content": user}])
            used_model = mdl
            break
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
            if j + 1 < len(models):
                log.warning("model %s unavailable (%s); falling back to %s",
                            mdl, getattr(e, "message", str(e)), models[j + 1])
            else:
                raise

    script = _parse(raw, targets)
    log.info("[%s] script v1: %d words across %d sections (%s)",
             edition.get("id", lang), script.total_words, len(script.sections), used_model)

    if script.total_words < 0.85 * target_total:
        script = _expand(client, cfg, window, research_text, script, targets, used_model,
                         max_tokens, lang, prompt_file)
    return script


def _expand(client, cfg, window, research_text, script, targets, model,
            max_tokens, lang="pl", prompt_file="system_brief.md") -> BriefScript:
    by_id = {t["id"]: t for t in targets}
    short = [s for s in script.sections
             if s["id"] in by_id and s["words"] < 0.8 * by_id[s["id"]]["target_words"]]
    if not short:
        return script
    log.info("script too short (%d w); expanding %d sections",
             script.total_words, len(short))
    if lang == "en":
        deficits = "\n".join(
            f'- {s["id"]} ("{s["title"]}"): ~{s["words"]} words, target '
            f'~{by_id[s["id"]]["target_words"]} words' for s in short)
        instruction = (
            "Your previous brief is too short. Below is the full current script. Return "
            "IT AGAIN IN FULL in the same format (TITLE, [[SUMMARY]], sections with "
            "[[SECTION:id|Title]] markers), but expand the listed sections to the target "
            "length — deepening analysis, context, mechanisms and implications, without "
            "inventing new numbers. You may leave the other sections unchanged.\n\n"
            f"Sections to expand:\n{deficits}\n\n"
            "=== CURRENT SCRIPT ===\n"
            f"{script.raw}\n\n"
            "=== SOURCE MATERIAL (to draw on when expanding) ===\n"
            f"{research_text}"
        )
    else:
        deficits = "\n".join(
            f'- {s["id"]} ("{s["title"]}"): masz ~{s["words"]} słów, cel '
            f'~{by_id[s["id"]]["target_words"]} słów' for s in short)
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
    try:
        raw = _stream_text(
            client, model=model, max_tokens=max_tokens,
            system=_system_blocks(cfg, targets, prompt_file, lang),
            messages=[{"role": "user", "content": instruction}],
        )
    except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
        log.warning("expansion call failed (%s); keeping v1 script", getattr(e, "message", str(e)))
        return script
    expanded = _parse(raw, targets)
    if expanded.total_words > script.total_words:
        log.info("expanded to %d words", expanded.total_words)
        return expanded
    log.info("expansion did not help; keeping original (%d words)", script.total_words)
    return script


def save_script(script: BriefScript, date_str: str, edition_id: str = "pl") -> dict[str, Path]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    tag = "" if edition_id in ("", "pl") else f"_{edition_id}"
    txt = OUTPUT_DIR / f"script_{date_str}{tag}.txt"
    txt.write_text(script.full_narration(), encoding="utf-8")
    js = OUTPUT_DIR / f"script_{date_str}{tag}.json"
    js.write_text(json.dumps({
        "title": script.title, "summary": script.summary,
        "total_words": script.total_words,
        "sections": script.sections,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("script saved -> %s", txt.name)
    return {"txt": txt, "json": js}
