"""Text-to-speech via edge-tts (free Microsoft neural voices, Polish).

Each section is split into sentence-bounded chunks, synthesised with retries,
then concatenated into one MP3 (ffmpeg if available, else a binary concat that
podcast players handle fine for same-format MP3 frames)."""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess
import time
from pathlib import Path

import edge_tts

from .config import Config
from .generate_script import BriefScript
from .util import OUTPUT_DIR

log = logging.getLogger("dailybrief.tts")

MARKER_RE = re.compile(r"\[\[[^\]]*\]\]")
SPEAKER_RE = re.compile(r"\[\[\s*([AB])\s*\]\]")
MD_RE = re.compile(r"[#*_`>]+")
MAX_CHARS = 2800


def _split_turns(text: str, default: str = "A") -> list[tuple[str, str]]:
    """Split a dialogue section into (speaker, text) turns by [[A]]/[[B]] markers."""
    turns: list[tuple[str, str]] = []
    pos, cur = 0, default
    for m in SPEAKER_RE.finditer(text):
        seg = text[pos:m.start()].strip()
        if seg:
            turns.append((cur, seg))
        cur = m.group(1)
        pos = m.end()
    seg = text[pos:].strip()
    if seg:
        turns.append((cur, seg))
    return turns or [(default, text)]


def _apply_pronunciations(text: str, mapping: dict[str, str]) -> str:
    """Respell English jargon/tickers phonetically so the Polish voice approximates
    English (edge-tts has no SSML, so a single-language voice reads everything in
    Polish phonetics). e.g. 'hawkish' -> 'hołkisz', 'DXY' -> 'di eks łaj'."""
    for term, say in mapping.items():
        # match whole token, case-insensitive, not glued to other word chars
        text = re.sub(rf"(?<!\w){re.escape(term)}(?!\w)", say, text, flags=re.IGNORECASE)
    return text


def clean_for_tts(text: str, pronunciations: dict[str, str] | None = None) -> str:
    text = MARKER_RE.sub("", text)
    text = MD_RE.sub("", text)
    text = re.sub(r"https?://\S+", "", text)
    if pronunciations:
        text = _apply_pronunciations(text, pronunciations)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _split_sentences(text: str, max_chars: int = MAX_CHARS) -> list[str]:
    sentences = re.split(r"(?<=[.!?…])\s+", text)
    chunks: list[str] = []
    cur = ""
    for s in sentences:
        if not s:
            continue
        if len(cur) + len(s) + 1 > max_chars and cur:
            chunks.append(cur.strip())
            cur = s
        else:
            cur = f"{cur} {s}".strip()
    if cur.strip():
        chunks.append(cur.strip())
    return chunks or [text]


async def _synth_chunk(text: str, voice: str, rate: str, pitch: str,
                       out_path: Path, attempts: int = 5) -> None:
    last = None
    for i in range(1, attempts + 1):
        try:
            comm = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
            await comm.save(str(out_path))
            if out_path.exists() and out_path.stat().st_size > 0:
                return
            raise RuntimeError("empty audio file")
        except Exception as e:  # noqa: BLE001
            last = e
            log.warning("tts chunk attempt %d/%d failed: %s", i, attempts, e)
            if i < attempts:
                await asyncio.sleep(min(20, 3 * i))   # 3,6,9,12s — ride out throttling
    raise RuntimeError(f"TTS failed after {attempts} attempts: {last}")


async def _synth_all(parts: list[tuple[int, str, str]], rate: str,
                     pitch: str, parts_dir: Path) -> list[Path]:
    out_paths: list[Path] = []
    skipped = 0
    # sequential to stay gentle on the free endpoint (avoid throttling/blocks)
    for idx, text, voice in parts:
        if not re.search(r"\w", text):      # nothing speakable -> skip silently
            continue
        p = parts_dir / f"part_{idx:03d}.mp3"
        try:
            await _synth_chunk(text, voice, rate, pitch, p)
            out_paths.append(p)
        except Exception as e:  # noqa: BLE001
            skipped += 1
            # one stubborn chunk must NOT sink the whole (already-paid-for) episode
            log.warning("tts: skipping chunk %d after retries (%s)", idx, e)
    if skipped:
        log.warning("tts: %d/%d chunks skipped; synthesised %d", skipped, len(parts), len(out_paths))
    if not out_paths:
        raise RuntimeError("TTS produced no audio (every chunk failed)")
    return out_paths


def _concat(parts: list[Path], out_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        listfile = out_path.with_suffix(".txt")
        listfile.write_text(
            "\n".join(f"file '{p.as_posix()}'" for p in parts), encoding="utf-8")
        cmd = [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i",
               str(listfile), "-c", "copy", str(out_path)]
        res = subprocess.run(cmd, capture_output=True, text=True)
        listfile.unlink(missing_ok=True)
        if res.returncode == 0 and out_path.exists():
            log.info("concatenated %d parts with ffmpeg", len(parts))
            return
        log.warning("ffmpeg concat failed (%s); falling back to binary concat",
                    res.stderr[-200:])
    with open(out_path, "wb") as out:
        for p in parts:
            out.write(p.read_bytes())
    log.info("concatenated %d parts (binary)", len(parts))


def _duration_seconds(path: Path) -> float | None:
    try:
        from mutagen.mp3 import MP3
        return float(MP3(str(path)).info.length)
    except Exception as e:  # noqa: BLE001
        log.warning("could not read duration: %s", e)
        return None


def synthesize(cfg: Config, script: BriefScript, date_str: str,
               edition: dict | None = None) -> dict:
    edition = edition or {"id": "pl", "apply_pronunciations": True}
    edition_id = edition.get("id", "pl")
    rate = cfg.get("voice", "rate", default="+0%")
    pitch = cfg.get("voice", "pitch", default="+0Hz")
    voice = edition.get("voice") or cfg.get("voice", "name", default="pl-PL-MarekNeural")
    voices = edition.get("voices") or {}
    dialogue = edition.get("format") == "dialogue" and bool(voices)
    pron = (cfg.get("voice", "pronunciations", default={}) or {}) \
        if edition.get("apply_pronunciations", True) else {}

    tag = "" if edition_id in ("", "pl") else f"_{edition_id}"
    parts_dir = OUTPUT_DIR / f"tts_parts_{date_str}{tag}"
    parts_dir.mkdir(parents=True, exist_ok=True)
    for old in parts_dir.glob("*.mp3"):
        old.unlink()

    # build ordered (idx, chunk, voice) list across all sections; in dialogue mode
    # each [[A]]/[[B]] turn is read by its speaker's voice
    parts: list[tuple[int, str, str]] = []
    idx = 0
    for sec in script.sections:
        turns = _split_turns(sec["text"]) if dialogue else [("_", sec["text"])]
        for speaker, turn_text in turns:
            clean = clean_for_tts(turn_text, pron)
            if not clean:
                continue
            v = voices.get(speaker, voice) if dialogue else voice
            for chunk in _split_sentences(clean):
                parts.append((idx, chunk, v))
                idx += 1

    if not parts:
        raise RuntimeError("nothing to synthesize (empty script)")

    nvoices = len({p[2] for p in parts})
    log.info("[%s] synthesizing %d chunks (%s, %d voice%s) ...", edition_id, len(parts),
             "dialogue" if dialogue else "monologue", nvoices, "s" if nvoices != 1 else "")
    t0 = time.time()
    part_paths = asyncio.run(_synth_all(parts, rate, pitch, parts_dir))

    out_mp3 = OUTPUT_DIR / f"brief_{date_str}{tag}.mp3"
    _concat(part_paths, out_mp3)
    dur = _duration_seconds(out_mp3)
    size_mb = out_mp3.stat().st_size / 1e6
    log.info("[%s] audio ready: %s (%.1f MB, %s, %.0fs synth)",
             edition_id, out_mp3.name, size_mb,
             f"{dur/60:.1f} min" if dur else "duration n/a", time.time() - t0)
    return {"path": out_mp3, "duration_s": dur, "size_bytes": out_mp3.stat().st_size}
