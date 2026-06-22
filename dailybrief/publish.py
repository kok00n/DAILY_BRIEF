"""Publish the episode: upload MP3 to Cloudflare R2, (re)build the private
podcast RSS feed, upload the feed, and prune old episodes.

If R2 env vars are missing, runs in LOCAL mode: writes feed.xml + keeps the MP3
under output/ so you can still test end-to-end."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from feedgen.feed import FeedGenerator

from .config import Config
from .util import DATA_DIR, OUTPUT_DIR

log = logging.getLogger("dailybrief.publish")

EPISODES_FILE = DATA_DIR / "episodes.json"
EPISODES_KEY = "episodes.json"   # feed-state object in R2 (source of truth in cloud)
COVER_KEY = "cover.png"          # generated-cover object key in R2


def _hhmmss(seconds: float | None) -> str:
    if not seconds:
        return "00:40:00"
    s = int(seconds)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def _load_episodes(client=None, bucket: str = "") -> list[dict]:
    """In the cloud the runner is ephemeral, so R2 is the source of truth for the
    feed state. Fall back to the local file (useful on a persistent VPS / dev)."""
    if client and bucket:
        try:
            raw = client.get_object(Bucket=bucket, Key=EPISODES_KEY)["Body"].read()
            return json.loads(raw.decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            log.info("no episodes.json in R2 yet (%s); starting from local/empty",
                     type(e).__name__)
    if EPISODES_FILE.exists():
        try:
            return json.loads(EPISODES_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return []
    return []


def _save_episodes(eps: list[dict], client=None, bucket: str = "") -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(eps, ensure_ascii=False, indent=2)
    EPISODES_FILE.write_text(blob, encoding="utf-8")
    if client and bucket:
        try:
            client.put_object(Bucket=bucket, Key=EPISODES_KEY,
                              Body=blob.encode("utf-8"), ContentType="application/json")
        except Exception as e:  # noqa: BLE001
            log.warning("failed to upload episodes.json to R2: %s", e)


# --------------------------------------------------------------------------- #
# R2
# --------------------------------------------------------------------------- #
def _r2_client(cfg: Config):
    import boto3
    account = cfg.env.get("R2_ACCOUNT_ID")
    key = cfg.env.get("R2_ACCESS_KEY_ID")
    secret = cfg.env.get("R2_SECRET_ACCESS_KEY")
    if not all([account, key, secret]):
        return None
    return boto3.client(
        "s3",
        endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
        aws_access_key_id=key,
        aws_secret_access_key=secret,
        region_name="auto",
    )


def _r2_put(client, bucket: str, key: str, body: bytes, content_type: str) -> None:
    client.put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type)


def _r2_delete(client, bucket: str, key: str) -> None:
    try:
        client.delete_object(Bucket=bucket, Key=key)
    except Exception as e:  # noqa: BLE001
        log.warning("R2 delete %s failed: %s", key, e)


# --------------------------------------------------------------------------- #
# Feed
# --------------------------------------------------------------------------- #
def _build_feed(cfg: Config, episodes: list[dict], public_base: str,
                cover_url: str | None = None) -> bytes:
    pub = cfg.get("publish", default={})
    feed_url = f"{public_base}/{pub.get('feed_filename', 'feed.xml')}"
    title = pub.get("podcast_title", "Poranny Brief")

    fg = FeedGenerator()
    fg.load_extension("podcast")
    fg.title(title)
    fg.link(href=feed_url, rel="self")
    fg.link(href=public_base, rel="alternate")
    fg.description(pub.get("podcast_description", "Codzienny brief makro."))
    fg.language(pub.get("podcast_language", "pl"))
    fg.author({"name": pub.get("podcast_author", "DAILY_BRIEF"),
               "email": pub.get("podcast_email", "")})
    fg.podcast.itunes_author(pub.get("podcast_author", "DAILY_BRIEF"))
    fg.podcast.itunes_category("News", "Business News")
    fg.podcast.itunes_explicit("no")
    fg.podcast.itunes_owner(name=pub.get("podcast_author", "DAILY_BRIEF"),
                            email=pub.get("podcast_email", ""))
    if cover_url:  # required by Spotify/Apple
        fg.image(url=cover_url, title=title, link=public_base)
        fg.podcast.itunes_image(cover_url)

    # newest last in list; feedgen prepends so add oldest->newest
    for ep in sorted(episodes, key=lambda e: e["pubdate"]):
        fe = fg.add_entry()
        fe.id(ep["url"])
        fe.title(ep["title"])
        fe.description(ep.get("summary", ""))
        fe.enclosure(ep["url"], str(ep.get("size_bytes", 0)), "audio/mpeg")
        fe.published(datetime.fromisoformat(ep["pubdate"]))
        fe.podcast.itunes_duration(_hhmmss(ep.get("duration_s")))
        if cover_url:
            fe.podcast.itunes_image(cover_url)
    return fg.rss_str(pretty=True)


def _ensure_cover(cfg: Config, client, bucket: str, public_base: str,
                  use_r2: bool) -> str | None:
    """Use the configured cover image URL if set; otherwise generate a default
    cover with Pillow and upload it to R2."""
    pub = cfg.get("publish", default={})
    if pub.get("cover_image_url"):
        return pub["cover_image_url"]
    if not pub.get("cover_generate", True):
        return None
    try:
        from . import cover as cover_mod
        png = cover_mod.generate_cover(pub.get("podcast_title", "Poranny Brief"),
                                       pub.get("podcast_subtitle", ""))
    except Exception as e:  # noqa: BLE001
        log.warning("cover generation failed (%s); feed will have no artwork", e)
        return None
    local = OUTPUT_DIR / COVER_KEY
    local.write_bytes(png)
    if use_r2:
        _r2_put(client, bucket, COVER_KEY, png, "image/png")
        return f"{public_base}/{COVER_KEY}"
    return local.resolve().as_uri()


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def publish(cfg: Config, audio: dict, script, date_str: str) -> dict[str, Any]:
    pub = cfg.get("publish", default={})
    keep = int(pub.get("keep_episodes", 30))
    feed_name = pub.get("feed_filename", "feed.xml")
    bucket = cfg.env.get("R2_BUCKET", "daily-brief")
    public_base = (cfg.env.get("R2_PUBLIC_BASE_URL") or "").rstrip("/")

    mp3_path: Path = audio["path"]
    mp3_key = f"episodes/brief_{date_str}.mp3"
    client = _r2_client(cfg)
    use_r2 = bool(client and public_base)

    episodes = _load_episodes(client if use_r2 else None, bucket)
    episodes = [e for e in episodes if e["date"] != date_str]  # replace same-day

    txt_key = f"episodes/brief_{date_str}.txt"
    transcript_url = None
    if client and public_base:
        log.info("uploading MP3 to R2 (%s)...", mp3_key)
        _r2_put(client, bucket, mp3_key, mp3_path.read_bytes(), "audio/mpeg")
        ep_url = f"{public_base}/{mp3_key}"
        mode = "r2"
        # companion transcript (the Opus script) — handy for review / show notes
        try:
            _r2_put(client, bucket, txt_key,
                    script.full_narration().encode("utf-8"), "text/plain; charset=utf-8")
            transcript_url = f"{public_base}/{txt_key}"
            log.info("transcript -> %s", transcript_url)
        except Exception as e:  # noqa: BLE001
            log.warning("transcript upload failed: %s", e)
    else:
        ep_url = mp3_path.resolve().as_uri()
        mode = "local"
        log.warning("R2 not configured -> LOCAL mode (feed will reference local file)")

    episodes.append({
        "date": date_str,
        "title": f"{script.title} — {date_str}",
        "summary": script.summary,
        "mp3_key": mp3_key,
        "url": ep_url,
        "duration_s": audio.get("duration_s"),
        "size_bytes": audio.get("size_bytes", 0),
        "pubdate": datetime.now(timezone.utc).isoformat(),
    })

    # prune (drop the MP3 and its companion transcript)
    episodes.sort(key=lambda e: e["pubdate"])
    while len(episodes) > keep:
        old = episodes.pop(0)
        if client and old.get("mp3_key"):
            _r2_delete(client, bucket, old["mp3_key"])
            _r2_delete(client, bucket, old["mp3_key"].rsplit(".", 1)[0] + ".txt")
        log.info("pruned old episode %s", old["date"])

    cover_url = _ensure_cover(cfg, client, bucket, public_base, use_r2)
    feed_bytes = _build_feed(cfg, episodes, public_base or OUTPUT_DIR.as_uri(), cover_url)
    feed_local = OUTPUT_DIR / feed_name
    feed_local.write_bytes(feed_bytes)

    feed_url = None
    if client and public_base:
        _r2_put(client, bucket, feed_name, feed_bytes, "application/rss+xml")
        feed_url = f"{public_base}/{feed_name}"
        log.info("feed published -> %s", feed_url)
    else:
        log.info("feed written locally -> %s", feed_local)

    _save_episodes(episodes, client if use_r2 else None, bucket)
    return {"mode": mode, "episode_url": ep_url, "feed_url": feed_url,
            "transcript_url": transcript_url, "feed_local": feed_local,
            "episodes": len(episodes)}
