import datetime
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from .config import settings
from .logger import with_retry


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=f"{settings.sonarr_url}/api/v3",
        timeout=120,
        headers={"X-Api-Key": settings.sonarr_api_key},
    )


async def test_connection(url: str, api_key: str) -> dict:
    """Ad-hoc connectivity check for the setup wizard, using credentials the
    caller just typed in rather than whatever's already saved in settings.
    """
    async with httpx.AsyncClient(base_url=f"{url.rstrip('/')}/api/v3", timeout=15, headers={"X-Api-Key": api_key}) as client:
        status_resp = await client.get("/system/status")
        status_resp.raise_for_status()
        version = status_resp.json().get("version")

        series_resp = await client.get("/series")
        series_resp.raise_for_status()
        series_count = len(series_resp.json())

    return {"version": version, "series_count": series_count}


@dataclass
class SonarrSeries:
    id: int
    title: str
    tvdb_id: int
    series_type: str
    tag_ids: list[int]
    monitored: bool
    poster_url: Optional[str] = None
    raw: dict = field(default_factory=dict)

    @property
    def is_anime(self) -> bool:
        return self.series_type.lower() == "anime"


def _poster_from_raw(raw: dict) -> Optional[str]:
    for image in raw.get("images", []):
        if image.get("coverType") == "poster":
            # Only absolute http(s) URLs are usable in a Discord embed; the
            # relative /MediaCover path would make the whole webhook post fail.
            for candidate in (image.get("remoteUrl"), image.get("url")):
                if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
                    return candidate
    return None


@dataclass
class SonarrEpisode:
    id: int
    series_id: int
    episode_number: int
    season_number: int
    title: str
    air_date_utc: Optional[str]
    monitored: bool
    has_file: bool

    @property
    def has_aired(self) -> bool:
        if not self.air_date_utc:
            return False
        aired = datetime.datetime.fromisoformat(self.air_date_utc.replace("Z", "+00:00"))
        return aired <= datetime.datetime.now(datetime.timezone.utc)

    @property
    def days_since_aired(self) -> Optional[float]:
        if not self.air_date_utc:
            return None
        aired = datetime.datetime.fromisoformat(self.air_date_utc.replace("Z", "+00:00"))
        return (datetime.datetime.now(datetime.timezone.utc) - aired).total_seconds() / 86400


@dataclass
class SonarrRelease:
    guid: str
    indexer_id: int
    title: str
    seeders: int
    size: int
    rejected: bool
    rejections: list[str]
    raw: dict = field(default_factory=dict)


@with_retry(label="Sonarr: list tags")
async def list_tags() -> dict[int, str]:
    async with _client() as client:
        resp = await client.get("/tag")
        resp.raise_for_status()
        return {t["id"]: t["label"] for t in resp.json()}


@with_retry(label="Sonarr: list series")
async def list_series() -> list[SonarrSeries]:
    async with _client() as client:
        resp = await client.get("/series")
        resp.raise_for_status()
        data = resp.json()
    return [
        SonarrSeries(
            id=s["id"],
            title=s.get("title", ""),
            tvdb_id=s.get("tvdbId", 0),
            series_type=s.get("seriesType", ""),
            tag_ids=s.get("tags", []),
            monitored=s.get("monitored", False),
            poster_url=_poster_from_raw(s),
            raw=s,
        )
        for s in data
    ]


@with_retry(label="Sonarr: get one series")
async def get_series(series_id: int) -> Optional[SonarrSeries]:
    async with _client() as client:
        resp = await client.get(f"/series/{series_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        s = resp.json()
    return SonarrSeries(
        id=s["id"],
        title=s.get("title", ""),
        tvdb_id=s.get("tvdbId", 0),
        series_type=s.get("seriesType", ""),
        tag_ids=s.get("tags", []),
        monitored=s.get("monitored", False),
        poster_url=_poster_from_raw(s),
        raw=s,
    )


def is_anime_series(series: SonarrSeries, tag_labels: dict[int, str]) -> bool:
    """seriesType is the primary, structured signal. The configured anime
    tag name is a fallback for shows tagged manually instead of set via
    seriesType — matches the user's existing categorization either way,
    rather than requiring them to re-classify anything.
    """
    if series.is_anime:
        return True
    target = settings.anime_tag_name.strip().lower()
    return any(tag_labels.get(tid, "").strip().lower() == target for tid in series.tag_ids)


@with_retry(label="Sonarr: list episodes for series")
async def list_episodes(series_id: int) -> list[SonarrEpisode]:
    async with _client() as client:
        resp = await client.get("/episode", params={"seriesId": series_id})
        resp.raise_for_status()
        data = resp.json()
    return [
        SonarrEpisode(
            id=e["id"],
            series_id=e["seriesId"],
            episode_number=e.get("episodeNumber", 0),
            season_number=e.get("seasonNumber", 0),
            title=e.get("title", ""),
            air_date_utc=e.get("airDateUtc"),
            monitored=e.get("monitored", False),
            has_file=e.get("hasFile", False),
        )
        for e in data
    ]


@with_retry(label="Sonarr: get releases for episode")
async def get_releases(episode_id: int) -> list[SonarrRelease]:
    async with _client() as client:
        resp = await client.get("/release", params={"episodeId": episode_id})
        resp.raise_for_status()
        data = resp.json()
    return [
        SonarrRelease(
            guid=r.get("guid", ""),
            indexer_id=r.get("indexerId", 0),
            title=r.get("title", ""),
            seeders=r.get("seeders") or 0,
            size=r.get("size") or 0,
            rejected=r.get("rejected", False),
            rejections=r.get("rejections", []),
            raw=r,
        )
        for r in data
    ]


@with_retry(label="Sonarr: grab release")
async def grab_release(guid: str, indexer_id: int) -> None:
    if settings.dry_run:
        from .logger import log

        await log(f"[DRY RUN] would grab Sonarr release guid={guid} indexerId={indexer_id}")
        return
    async with _client() as client:
        resp = await client.post("/release", json={"guid": guid, "indexerId": indexer_id})
        resp.raise_for_status()


@with_retry(label="Sonarr: get queue")
async def get_queue() -> list[dict[str, Any]]:
    """Paginates through Sonarr's ENTIRE queue rather than assuming it fits
    in one page. A long-lived library's queue can grow to thousands of
    records (stuck/dead-swarm downloads that nothing cleans up); a flat
    pageSize of 200 meant queue_records_for_episode() below silently saw
    only the first 200 rows, so dedupe_episode_queue()'s duplicate-removal
    safety net could never see a duplicate entry sitting past position
    200. Those duplicates then piled up unchecked, and every fresh grab
    attempt for that episode collided with an already-queued copy (a
    409 from the download client / 500 from Sonarr) on every scan cycle.
    """
    records: list[dict[str, Any]] = []
    page = 1
    page_size = 250
    async with _client() as client:
        while True:
            resp = await client.get(
                "/queue", params={"page": page, "pageSize": page_size, "includeEpisode": True}
            )
            resp.raise_for_status()
            data = resp.json()
            batch = data.get("records", [])
            records.extend(batch)
            total = data.get("totalRecords", len(records))
            if len(records) >= total or not batch:
                break
            page += 1
    return records


async def queue_records_for_episode(episode_id: int) -> list[dict[str, Any]]:
    records = await get_queue()
    return [r for r in records if (r.get("episode") or {}).get("id") == episode_id]


@with_retry(label="Sonarr: remove queue item")
async def remove_queue_item(queue_id: int, remove_from_client: bool = True, blocklist: bool = False) -> None:
    if settings.dry_run:
        from .logger import log

        await log(f"[DRY RUN] would remove Sonarr queue item {queue_id}")
        return
    async with _client() as client:
        resp = await client.delete(
            f"/queue/{queue_id}",
            params={"removeFromClient": str(remove_from_client).lower(), "blocklist": str(blocklist).lower()},
        )
        resp.raise_for_status()
