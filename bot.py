#!/usr/bin/env python3

from __future__ import annotations

import argparse
import html
import logging
import os
import random
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

import requests
import yt_dlp

LOG = logging.getLogger("yt-thumb-bot")

TG_API = "https://api.telegram.org/bot{token}/{method}"
USER_AGENT = "yt-thumb-bot/1.0"
THUMB_URLS = (
    "https://i.ytimg.com/vi/{vid}/maxresdefault.jpg",
    "https://i.ytimg.com/vi/{vid}/sddefault.jpg",
    "https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
)
DEFAULT_CAPTION = '<a href="{url}">{title}</a>'
CAPTION_LIMIT = 1024
MAX_PHOTO_BYTES = 9 * 1024 * 1024
VIDEO_ID_LEN = 11


@dataclass(frozen=True)
class Config:
    channel: str
    chat_id: str
    bot_token: str
    state_file: Path
    caption_template: str
    max_scan: int
    exclude_shorts: bool
    exclude_live: bool
    timeout: int
    cookies_file: str | None


@dataclass(frozen=True)
class Video:
    id: str
    title: str
    duration: int | None = None


def load_config(path: Path) -> Config:
    if not path.exists():
        raise SystemExit(f"config: файл не найден: {path}")
    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    yt = raw.get("youtube") or {}
    tg = raw.get("telegram") or {}
    pub = raw.get("publish") or {}
    st = raw.get("state") or {}
    net = raw.get("network") or {}

    channel = str(yt.get("channel") or "").strip()
    if not channel:
        raise SystemExit("config: не задан [youtube] channel")

    bot_token = (os.environ.get("TG_BOT_TOKEN") or str(tg.get("bot_token") or "")).strip()
    if not bot_token:
        raise SystemExit("config: нет токена бота - [telegram] bot_token или env TG_BOT_TOKEN")

    chat_id = (os.environ.get("TG_CHAT_ID") or str(tg.get("chat_id") or "")).strip()
    if not chat_id:
        raise SystemExit("config: не задан [telegram] chat_id")

    return Config(
        channel=channel,
        chat_id=chat_id,
        bot_token=bot_token,
        state_file=Path(str(st.get("file") or "/data/posted.txt")),
        caption_template=str(pub.get("caption_template") or DEFAULT_CAPTION),
        max_scan=max(1, int(yt.get("max_scan") or 200)),
        exclude_shorts=bool(yt.get("exclude_shorts", True)),
        exclude_live=bool(yt.get("exclude_live", True)),
        timeout=max(5, int(net.get("timeout") or 30)),
        cookies_file=(str(yt["cookies_file"]).strip() if yt.get("cookies_file") else None),
    )


def read_state(path: Path) -> set[str]:
    if not path.exists():
        return set()
    ids: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            ids.add(line)
    return ids


def append_state(path: Path, video_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(video_id + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def normalize_channel_url(value: str) -> str:
    v = value.strip()
    if not v:
        raise ValueError("пустая ссылка на канал")
    if v.startswith("UC") and len(v) == 24 and "/" not in v:
        return f"https://www.youtube.com/channel/{v}/videos"
    if v.startswith("@"):
        return f"https://www.youtube.com/{v}/videos"
    if "://" not in v:
        return f"https://www.youtube.com/{v.lstrip('/')}/videos"
    v = v.rstrip("/")
    if "list=" in v:
        return v
    tail = v.rsplit("/", 1)[-1]
    if tail in {"featured", "about"}:
        return v.rsplit("/", 1)[0] + "/videos"
    if tail not in {"videos", "shorts", "streams"}:
        v += "/videos"
    return v


def list_channel_videos(cfg: Config) -> tuple[list[Video], str]:
    url = normalize_channel_url(cfg.channel)
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "playlistend": cfg.max_scan,
        "ignoreerrors": True,
        "socket_timeout": cfg.timeout,
    }
    if cfg.cookies_file:
        opts["cookiefile"] = cfg.cookies_file

    LOG.info("читаю список видео: %s (лимит %d)", url, cfg.max_scan)
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError as exc:
            raise RuntimeError(f"yt-dlp не смог прочитать канал: {exc}") from exc

    if not info:
        raise RuntimeError(
            "yt-dlp вернул пустой ответ по каналу "
            "(канал приватный/удалён, либо YouTube требует подтверждения - см. cookies_file)"
        )

    channel_title = str(info.get("channel") or info.get("uploader") or info.get("title") or "")

    videos: list[Video] = []
    seen: set[str] = set()
    for entry in info.get("entries") or []:
        if not entry:
            continue
        vid = str(entry.get("id") or "")
        if len(vid) != VIDEO_ID_LEN or vid in seen:
            continue
        if cfg.exclude_live and (
            entry.get("is_live") or entry.get("live_status") in {"is_live", "is_upcoming"}
        ):
            continue
        duration = entry.get("duration")
        duration = int(duration) if isinstance(duration, (int, float)) else None
        if cfg.exclude_shorts and duration is not None and duration <= 60:
            continue
        seen.add(vid)
        videos.append(Video(id=vid, title=str(entry.get("title") or vid), duration=duration))

    return videos, channel_title


def fetch_thumbnail(video_id: str, timeout: int) -> bytes:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    last_error = "нет ответа"
    for template in THUMB_URLS:
        url = template.format(vid=video_id)
        try:
            resp = session.get(url, timeout=timeout)
        except requests.RequestException as exc:
            last_error = str(exc)
            continue
        if resp.status_code != 200:
            last_error = f"HTTP {resp.status_code} для {url}"
            continue
        if not resp.headers.get("Content-Type", "").startswith("image/"):
            last_error = f"не картинка: {url}"
            continue
        if not resp.content or len(resp.content) > MAX_PHOTO_BYTES:
            last_error = f"пустой или слишком большой файл: {url}"
            continue
        return resp.content
    raise RuntimeError(f"не удалось скачать тамбнейл {video_id}: {last_error}")


def build_caption(template: str, title: str, url: str, video_id: str, channel: str) -> str:
    def render(t: str) -> str:
        return template.format(
            title=html.escape(t),
            url=html.escape(url, quote=True),
            id=video_id,
            channel=html.escape(channel or ""),
        )

    caption = render(title)
    if len(caption) <= CAPTION_LIMIT:
        return caption

    lo, hi, best = 0, len(title), render("")
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = render(title[:mid].rstrip() + "…")
        if len(candidate) <= CAPTION_LIMIT:
            best, lo = candidate, mid + 1
        else:
            hi = mid - 1
    return best


def send_photo(cfg: Config, photo: bytes, caption: str) -> dict:
    api = TG_API.format(token=cfg.bot_token, method="sendPhoto")
    data = {"chat_id": cfg.chat_id}
    if caption:
        data["caption"] = caption
        data["parse_mode"] = "HTML"
    files = {"photo": ("thumbnail.jpg", photo, "image/jpeg")}

    last_error = "неизвестная ошибка"
    for attempt in range(1, 4):
        try:
            resp = requests.post(api, data=data, files=files, timeout=cfg.timeout)
        except requests.RequestException as exc:
            last_error = str(exc)
            if attempt < 3:
                time.sleep(3 * attempt)
                continue
            break

        try:
            payload = resp.json()
        except ValueError:
            payload = {"ok": False, "description": resp.text[:300]}

        if payload.get("ok"):
            return payload.get("result") or {}

        last_error = str(payload.get("description") or payload)
        if payload.get("error_code") == 429:
            retry_after = int((payload.get("parameters") or {}).get("retry_after") or 5)
            LOG.warning("Telegram: rate limit, жду %s c", retry_after)
            time.sleep(min(retry_after, 60))
            continue
        if resp.status_code >= 500 and attempt < 3:
            time.sleep(3 * attempt)
            continue
        break

    raise RuntimeError(f"Telegram API: {last_error}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Постит тамбнейл случайного видео с YouTube-канала в Telegram.")
    parser.add_argument("--config", default=os.environ.get("CONFIG_PATH", "/app/config.toml"))
    parser.add_argument("--dry-run", action="store_true", help="только показать, что было бы опубликовано")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    cfg = load_config(Path(args.config))
    posted = read_state(cfg.state_file)
    LOG.info("в базе уже %d видео (файл %s)", len(posted), cfg.state_file)

    videos, channel_title = list_channel_videos(cfg)
    LOG.info("найдено видео: %d (канал: %s)", len(videos), channel_title or "?")

    fresh = [v for v in videos if v.id not in posted]
    if not fresh:
        LOG.warning("свежих видео не осталось: все %d из просмотренных уже опубликованы", len(videos))
        return 0

    choice = random.choice(fresh)
    url = f"https://www.youtube.com/watch?v={choice.id}"
    LOG.info("выбрано: %s - %s", choice.id, choice.title)

    photo = fetch_thumbnail(choice.id, cfg.timeout)
    caption = build_caption(cfg.caption_template, choice.title, url, choice.id, channel_title)

    if args.dry_run:
        print(f"[dry-run] {choice.id} | {choice.title}\n[dry-run] {len(photo)} байт превью\n[dry-run] caption: {caption}")
        return 0

    send_photo(cfg, photo, caption)
    append_state(cfg.state_file, choice.id)
    LOG.info("опубликовано: %s", url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
