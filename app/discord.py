from typing import Optional

import httpx

from .config import settings
from .logger import log

GOLD = 0xD4AF37
TEAL = 0x4FB6A8
PURPLE = 0x9B7ED9

AUTHOR = {"name": "Nyaarr"}

_KIND_LABEL = {"sub": "\U0001f4ac Sub", "dub": "\U0001f3a4 Dub", "dual": "\U0001f310 Dual-Audio", "unknown": "❓ Unknown"}
_CONFIDENCE_LABEL = {"high": "HIGH confidence", "medium": "MEDIUM confidence", "unknown": "UNKNOWN confidence"}


async def _send(title: str, description: str, color: int, poster_url: Optional[str] = None, fields: Optional[list[dict]] = None) -> None:
    if not settings.discord_webhook_url:
        return

    embed = {"title": title, "description": description, "color": color, "author": AUTHOR}
    if fields:
        embed["fields"] = fields
    if poster_url:
        embed["thumbnail"] = {"url": poster_url}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(settings.discord_webhook_url, json={"embeds": [embed]})
            resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 — a failed notification should never break curation
        await log(f"discord: failed to send notification: {exc}")


def _classification_line(kind: str, confidence: str) -> str:
    return f"{_KIND_LABEL.get(kind, kind)} ({_CONFIDENCE_LABEL.get(confidence, confidence)})"


async def episode_grabbed(series_title: str, episode_label: str, release_title: str, kind: str, confidence: str, poster_url: Optional[str] = None) -> None:
    await _send(
        "\U0001f4e5 Episode Grabbed",
        f"**{series_title}** — {episode_label}",
        GOLD,
        poster_url,
        fields=[
            {"name": "Release", "value": release_title[:1024], "inline": False},
            {"name": "Language", "value": _classification_line(kind, confidence), "inline": False},
        ],
    )


async def movie_grabbed(movie_title: str, release_title: str, kind: str, confidence: str, poster_url: Optional[str] = None) -> None:
    await _send(
        "\U0001f4e5 Movie Grabbed",
        f"**{movie_title}**",
        GOLD,
        poster_url,
        fields=[
            {"name": "Release", "value": release_title[:1024], "inline": False},
            {"name": "Language", "value": _classification_line(kind, confidence), "inline": False},
        ],
    )


async def duplicate_removed(item_title: str, kept_title: str, removed_count: int, poster_url: Optional[str] = None) -> None:
    await _send(
        "\U0001f9f9 Duplicate Grab Cleaned Up",
        f"**{item_title}** had {removed_count + 1} simultaneous downloads — kept the best one, removed the rest.",
        TEAL,
        poster_url,
        fields=[{"name": "Kept", "value": kept_title[:1024], "inline": False}],
    )


async def movie_added(movie_title: str, source_series_title: str, poster_url: Optional[str] = None) -> None:
    await _send(
        "\U0001f37f Franchise Movie Added",
        f"**{movie_title}** added to Radarr — found via **{source_series_title}**.",
        PURPLE,
        poster_url,
    )


async def startup_online() -> None:
    await _send("✅ Nyaarr Online", "Watching Sonarr and Radarr for anime curation. Ready to go.", GOLD)
