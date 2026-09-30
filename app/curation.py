"""Scan loop, release picking, and grab orchestration — the workflows.py
analogue. Shared between the episode (Sonarr) and movie (Radarr) sides
since the pipeline shape is identical; only the API client differs.
"""

import asyncio
import datetime
import re
from typing import Any, Literal

from . import decisions_store, discord, prefs_store, radarr, sonarr
from .classifier import classify_release, matches_preference
from .config import settings
from .logger import log

Status = Literal["not_aired", "aired_no_release", "needs_review", "downloading", "stalled", "done", "not_yet_checked"]

# Optional hard block (settings.block_opus_audio, off by default). Some
# playback clients can't handle Opus audio properly (no sound, or a forced
# bad transcode), so for those setups an Opus release is never worth
# grabbing even if it's the only option available. Anime release titles
# reliably carry audio-codec info directly (e.g. "[Opus]", "Opus 2.0"),
# unlike movie releases.
_OPUS_AUDIO_PATTERN = re.compile(r"\bopus\b", re.IGNORECASE)


def is_opus_release(title: str) -> bool:
    return bool(_OPUS_AUDIO_PATTERN.search(title or ""))


_stop_requested = False

# Confirmed live: a series with a long production history (Bleach) can have
# hundreds of monitored-but-missing episodes once you count every arc, not
# just the current one — a dashboard page load that live-searches releases
# for every single one of them is genuinely too slow to be a page load.
# Candidate search only ever happens via the background scan loop or an
# explicit per-episode "check now" call; a normal page view reads whatever
# was found last time, instantly, with a visible "last checked" timestamp.
_episode_cache: dict[int, dict] = {}
_movie_cache: dict[int, dict] = {}


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _cached_candidate(cache: dict[int, dict], item_id: int, guid: str) -> dict:
    """Look up a candidate's title/classification from the last scan's cache
    by guid — grab_episode/grab_movie only ever get called with a guid the
    user picked from an already-rendered candidate list, so the metadata for
    the Discord notification is already sitting here for free, no extra API
    call needed.
    """
    cached = cache.get(item_id) or {}
    for c in cached.get("candidates", []):
        if c["guid"] == guid:
            return {"title": c["title"], "kind": c["classification"]["kind"], "confidence": c["classification"]["confidence"]}
    return {"title": "", "kind": "unknown", "confidence": "unknown"}


def get_episode_status(episode: "sonarr.SonarrEpisode") -> dict:
    """Zero-I/O status for dashboard page loads. not_aired/done are always
    computed fresh — they're free, already-fetched episode fields, and
    reflect real Sonarr state, never stale cache. Anything requiring an
    actual release search reads the cache from the last real scan instead
    of blocking the request.
    """
    if not episode.has_aired:
        return {"status": "not_aired", "candidates": [], "scanned_at": None}
    if episode.has_file:
        return {"status": "done", "candidates": [], "scanned_at": None}
    cached = _episode_cache.get(episode.id)
    if cached:
        return cached
    return {"status": "not_yet_checked", "candidates": [], "scanned_at": None}


def get_movie_status(movie: "radarr.RadarrMovie") -> dict:
    if movie.has_file:
        return {"status": "done", "candidates": [], "scanned_at": None}
    cached = _movie_cache.get(movie.id)
    if cached:
        return cached
    return {"status": "not_yet_checked", "candidates": [], "scanned_at": None}


def _pick_ranked_candidates(releases: list, tried_guids: list[str], preference: str, limit: int = 5) -> list[dict]:
    """Shared ranking pipeline for both Sonarr and Radarr releases (same
    shape: guid/indexer_id/title/seeders/size/rejected).

    1. Hard-filter dead releases first — a naive "best quality, seeders as
       tiebreaker" sort still lets a 0-seeder release win if it scores well
       on other axes. Confirmed live: this happened twice during manual
       Bleach curation before the filter was seeders > 0, not just a sort key.
    2. Never re-offer a guid already tried and abandoned for this item.
    3. If block_opus_audio is on, hard-block Opus-audio releases (see
       _OPUS_AUDIO_PATTERN above).
    4. Classify every survivor and filter by the effective sub/dub preference.
    5. Rank: preferred resolution terms > seeders, descending.
    """
    alive = [
        r
        for r in releases
        if r.seeders >= max(settings.min_seeders, 1)
        and r.guid not in tried_guids
        and not (settings.block_opus_audio and is_opus_release(r.title))
    ]
    if not alive:
        return []

    ranked = []
    for r in alive:
        classification = classify_release(r.title, settings.known_fansub_groups)
        if not matches_preference(classification, preference):
            continue
        quality_score = 1 if ("2160p" in r.title.lower() or "4k" in r.title.lower()) else 0
        ranked.append(
            {
                "guid": r.guid,
                "indexer_id": r.indexer_id,
                "title": r.title,
                "seeders": r.seeders,
                "size_gb": round(r.size / (1024**3), 2) if r.size else 0,
                "classification": {
                    "kind": classification.kind,
                    "confidence": classification.confidence,
                    "reason": classification.reason,
                },
                "_sort_key": (quality_score, r.seeders),
            }
        )
    ranked.sort(key=lambda c: c["_sort_key"], reverse=True)
    for c in ranked:
        del c["_sort_key"]
    return ranked[:limit]


# ---------------------------------------------------------------------------
# Episodes (Sonarr)
# ---------------------------------------------------------------------------


async def scan_episode(episode: sonarr.SonarrEpisode, series_title: str, poster_url: str | None = None) -> dict:
    """The expensive path — a live release search + classify. Called by the
    background scan loop and the explicit per-episode "check now" endpoint
    only; normal dashboard page loads use get_episode_status's cache read
    instead (see module docstring above).
    """
    if not episode.has_aired:
        result = {"status": "not_aired", "candidates": []}
        _episode_cache.pop(episode.id, None)
        return result
    if episode.has_file:
        result = {"status": "done", "candidates": []}
        _episode_cache.pop(episode.id, None)
        return result

    releases = await sonarr.get_releases(episode.id)
    tried = decisions_store.store.get_tried_guids("episode", episode.id)
    preference = prefs_store.get_preference(episode.series_id)
    candidates = _pick_ranked_candidates(releases, tried, preference)

    if not candidates:
        result = {"status": "aired_no_release", "candidates": []}
    else:
        top = candidates[0]
        if settings.auto_grab_high_confidence and top["classification"]["confidence"] == "high":
            result = {"status": "downloading", "candidates": candidates}
            # Cache must be written BEFORE grab_episode runs -- it calls
            # _cached_candidate() to look up this exact guid's title/kind/
            # confidence for the Discord "Episode Grabbed" post. Writing the
            # cache after the await (as this used to) meant grab_episode
            # always read a stale/missing entry, so every notification showed
            # "(unknown release)" and UNKNOWN confidence no matter what was
            # actually grabbed.
            _episode_cache[episode.id] = {**result, "scanned_at": _now_iso()}
            episode_label = f"S{episode.season_number:02d}E{episode.episode_number:02d}"
            await grab_episode(episode.id, series_title, top["guid"], top["indexer_id"], episode_label=episode_label, poster_url=poster_url)
            return result
        else:
            result = {"status": "needs_review", "candidates": candidates}

    _episode_cache[episode.id] = {**result, "scanned_at": _now_iso()}
    return result


async def grab_episode(episode_id: int, series_title: str, guid: str, indexer_id: int, episode_label: str = "", poster_url: str | None = None) -> None:
    meta = _cached_candidate(_episode_cache, episode_id, guid)
    try:
        await sonarr.grab_release(guid, indexer_id)
    except Exception:
        # Whatever the cause (most often a duplicate-hash 409 from
        # qBittorrent because this exact release - or its underlying
        # torrent - is already sitting in the queue, occasionally a 404
        # from a stale guid/indexerId pairing), this guid must still be
        # recorded as tried. If it were only recorded on the success path,
        # a failed grab would be silently forgotten and the exact same
        # top-ranked candidate would be re-picked and re-failed on every
        # subsequent scan cycle, forever. Recording it here lets the next
        # scan advance to the next-best candidate instead.
        await decisions_store.store.record_tried_guid("episode", episode_id, guid)
        await decisions_store.store.record_decision(
            "episode", episode_id, series_title, "grab_failed", f"grab failed, not retrying guid={guid}"
        )
        raise
    await decisions_store.store.record_tried_guid("episode", episode_id, guid)
    await decisions_store.store.record_decision("episode", episode_id, series_title, "grabbed", f"grabbed guid={guid}")
    await log(f"nyaarr: grabbed episode {episode_id} ({series_title})")
    await discord.episode_grabbed(series_title, episode_label or f"episode {episode_id}", meta["title"] or "(unknown release)", meta["kind"], meta["confidence"], poster_url)
    await dedupe_episode_queue(episode_id, series_title, poster_url=poster_url)


async def dedupe_episode_queue(episode_id: int, series_title: str, poster_url: str | None = None) -> None:
    """Mandatory post-grab safety net — Sonarr's own automatic search
    grabbing a redundant copy in parallel with a curated pick is a
    confirmed, repeated live failure mode (not hypothetical), including
    once re-adding a duplicate immediately after a stalled grab had just
    been manually replaced. Same shape as Reclaimarr's _dedupe_queue.
    """
    records = await sonarr.queue_records_for_episode(episode_id)
    if len(records) <= 1:
        return

    def sort_key(r: dict) -> tuple:
        title = r.get("title", "")
        return ("remux" in title.lower(), -(r.get("seeders") or 0))

    records.sort(key=sort_key)
    keep, *extras = records
    await log(f"nyaarr: {len(records)} simultaneous queue entries for episode {episode_id} ({series_title}) — keeping '{keep.get('title')}', removing {len(extras)} duplicate(s)")
    for extra in extras:
        await sonarr.remove_queue_item(extra["id"])
    await decisions_store.store.record_decision(
        "episode", episode_id, series_title, "dedupe_removed",
        f"removed {len(extras)} duplicate grab(s), kept '{keep.get('title')}'",
    )
    await discord.duplicate_removed(series_title, keep.get("title", "?"), len(extras), poster_url)


# ---------------------------------------------------------------------------
# Movies (Radarr) — same pipeline shape, different API surface
# ---------------------------------------------------------------------------


async def scan_movie(movie: radarr.RadarrMovie) -> dict:
    """A movie inherits its parent anime series' sub/dub preference (via
    prefs_store's movie->series link, set when franchise.py adds it) since
    it was never independently classified as anime on its own — Radarr has
    no seriesType equivalent. Falls back to the global default otherwise.
    """
    if movie.has_file:
        result = {"status": "done", "candidates": []}
        _movie_cache.pop(movie.id, None)
        return result

    releases = await radarr.get_releases(movie.id)
    tried = decisions_store.store.get_tried_guids("movie", movie.id)
    preference = prefs_store.get_preference_for_movie(movie.id)
    candidates = _pick_ranked_candidates(releases, tried, preference)

    if not candidates:
        result = {"status": "aired_no_release", "candidates": []}
    else:
        top = candidates[0]
        if settings.auto_grab_high_confidence and top["classification"]["confidence"] == "high":
            result = {"status": "downloading", "candidates": candidates}
            # Same fix as scan_episode() above: write the cache before
            # grab_movie runs, since it reads this cache to build the
            # Discord notification.
            _movie_cache[movie.id] = {**result, "scanned_at": _now_iso()}
            await grab_movie(movie.id, movie.title, top["guid"], top["indexer_id"], poster_url=movie.poster_url)
            return result
        else:
            result = {"status": "needs_review", "candidates": candidates}

    _movie_cache[movie.id] = {**result, "scanned_at": _now_iso()}
    return result


async def grab_movie(movie_id: int, title: str, guid: str, indexer_id: int, poster_url: str | None = None) -> None:
    meta = _cached_candidate(_movie_cache, movie_id, guid)
    try:
        await radarr.grab_release(guid, indexer_id)
    except Exception:
        # Same fix as grab_episode() above, applied here for consistency -
        # a failed grab must still be recorded as tried so it isn't
        # re-offered identically forever.
        await decisions_store.store.record_tried_guid("movie", movie_id, guid)
        await decisions_store.store.record_decision(
            "movie", movie_id, title, "grab_failed", f"grab failed, not retrying guid={guid}"
        )
        raise
    await decisions_store.store.record_tried_guid("movie", movie_id, guid)
    await decisions_store.store.record_decision("movie", movie_id, title, "grabbed", f"grabbed guid={guid}")
    await log(f"nyaarr: grabbed movie {movie_id} ({title})")
    await discord.movie_grabbed(title, meta["title"] or "(unknown release)", meta["kind"], meta["confidence"], poster_url)
    await dedupe_movie_queue(movie_id, title, poster_url=poster_url)


async def dedupe_movie_queue(movie_id: int, title: str, poster_url: str | None = None) -> None:
    records = await radarr.queue_records_for_movie(movie_id)
    if len(records) <= 1:
        return

    def sort_key(r: dict) -> tuple:
        t = r.get("title", "")
        return ("remux" in t.lower(), -(r.get("seeders") or 0))

    records.sort(key=sort_key)
    keep, *extras = records
    await log(f"nyaarr: {len(records)} simultaneous queue entries for movie {movie_id} ({title}) — keeping '{keep.get('title')}', removing {len(extras)} duplicate(s)")
    for extra in extras:
        await radarr.remove_queue_item(extra["id"])
    await decisions_store.store.record_decision(
        "movie", movie_id, title, "dedupe_removed",
        f"removed {len(extras)} duplicate grab(s), kept '{keep.get('title')}'",
    )
    await discord.duplicate_removed(title, keep.get("title", "?"), len(extras), poster_url)


# ---------------------------------------------------------------------------
# Background scan loop
# ---------------------------------------------------------------------------


def request_stop_scan() -> None:
    global _stop_requested
    _stop_requested = True


def _needs_fresh_scan(episode_id: int) -> bool:
    cached = _episode_cache.get(episode_id)
    if cached is None or cached.get("scanned_at") is None:
        return True
    scanned_at = datetime.datetime.fromisoformat(cached["scanned_at"])
    age_minutes = (datetime.datetime.now(datetime.timezone.utc) - scanned_at).total_seconds() / 60
    return age_minutes >= settings.scan_interval_minutes


def _within_new_episode_window(days_since_aired: float | None) -> bool:
    """new_episodes_only_days > 0 limits scanning to episodes that aired
    within that many days; 0 (or less) means no limit, i.e. the whole
    missing backlog is eligible."""
    limit = settings.new_episodes_only_days
    if not limit or limit <= 0:
        return True
    return days_since_aired is not None and days_since_aired <= limit


async def run_full_scan() -> dict[str, Any]:
    """One pass over anime series' missing+aired episodes. Read-only for
    classification/status; only grabs if auto_grab_high_confidence is on
    (off by default).

    Bounded by max_episodes_per_scan and skips anything already scanned
    within the last scan_interval_minutes — across a library with hundreds
    of anime series (confirmed live: 500 in this case), "search every
    missing episode every cycle" would make each cycle take hours and
    hammer indexers for no benefit on entries that haven't changed.

    Work goes newest-air-date-first, not series-list order. Every series'
    missing/aired episodes (within new_episodes_only_days, when that is
    above 0) are pooled into one flat list, sorted by
    days_since_aired ascending (most recently aired first), then the
    per-scan budget is applied to that globally-sorted list — so a cycle
    always spends its budget on the newest gaps across the whole library
    before ever touching older ones, and a show with many gaps still gets
    covered incrementally across several cycles rather than all at once.
    """
    tag_labels = await sonarr.list_tags()
    all_series = await sonarr.list_series()
    anime_series = [s for s in all_series if sonarr.is_anime_series(s, tag_labels)]

    results = {"series_scanned": len(anime_series), "episodes": [], "skipped_fresh": 0, "hit_cap": False}

    candidates: list[tuple[Any, Any]] = []  # (series, episode) pairs, pooled across all series
    for series in anime_series:
        episodes = await sonarr.list_episodes(series.id)
        missing = [
            e
            for e in episodes
            if not e.has_file
            and e.monitored
            and e.has_aired
            and _within_new_episode_window(e.days_since_aired)
        ]
        candidates.extend((series, e) for e in missing)

    # Ascending = most recently aired first; unknown air dates sort last.
    candidates.sort(key=lambda pair: pair[1].days_since_aired if pair[1].days_since_aired is not None else float("inf"))

    budget = settings.max_episodes_per_scan
    for series, ep in candidates:
        if budget <= 0:
            results["hit_cap"] = True
            break
        if not _needs_fresh_scan(ep.id):
            results["skipped_fresh"] += 1
            continue
        try:
            outcome = await scan_episode(ep, series.title, poster_url=series.poster_url)
        except Exception as exc:  # noqa: BLE001
            # One episode's search/grab blowing up (a transient Sonarr 500,
            # a flaky indexer, etc.) must not sacrifice the rest of this
            # cycle's budget - without this, a single mid-cycle grab
            # failure (e.g. a Sonarr /release 500) would kill every
            # remaining episode's worth of budget outright. Log and move on
            # to the next candidate instead.
            await log(f"nyaarr: scan_episode failed for {series.title} S{ep.season_number:02d}E{ep.episode_number:02d}: {exc} - skipping, continuing scan")
            results["episodes"].append({"series": series.title, "episode": f"S{ep.season_number:02d}E{ep.episode_number:02d}", "status": "error", "candidates": [], "error": str(exc)})
            budget -= 1
            continue
        results["episodes"].append({"series": series.title, "episode": f"S{ep.season_number:02d}E{ep.episode_number:02d}", **outcome})
        budget -= 1
    return results


async def scan_loop() -> None:
    while True:
        await asyncio.sleep(settings.scan_interval_minutes * 60)
        try:
            await run_full_scan()
        except Exception as exc:  # noqa: BLE001
            await log(f"nyaarr: scan loop error: {exc} — will retry next cycle")
