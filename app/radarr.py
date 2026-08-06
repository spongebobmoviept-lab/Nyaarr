from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from .config import settings
from .logger import log, with_retry


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=f"{settings.radarr_url}/api/v3",
        timeout=30,
        headers={"X-Api-Key": settings.radarr_api_key},
    )


async def test_connection(url: str, api_key: str) -> dict:
    async with httpx.AsyncClient(base_url=f"{url.rstrip('/')}/api/v3", timeout=15, headers={"X-Api-Key": api_key}) as client:
        status_resp = await client.get("/system/status")
        status_resp.raise_for_status()
        version = status_resp.json().get("version")

        profiles_resp = await client.get("/qualityprofile")
        profiles_resp.raise_for_status()
        profiles = [{"id": p["id"], "name": p["name"]} for p in profiles_resp.json()]

        root_resp = await client.get("/rootfolder")
        root_resp.raise_for_status()
        root_folders = [r["path"] for r in root_resp.json()]

    return {"version": version, "quality_profiles": profiles, "root_folders": root_folders}


@dataclass
class RadarrMovie:
    id: int
    title: str
    tmdb_id: int
    has_file: bool
    monitored: bool
    poster_url: Optional[str] = None
    raw: dict = field(default_factory=dict)


def _poster_from_raw(raw: dict) -> Optional[str]:
    for image in raw.get("images", []):
        if image.get("coverType") == "poster":
            return image.get("remoteUrl") or image.get("url")
    return None


@dataclass
class RadarrRelease:
    guid: str
    indexer_id: int
    title: str
    seeders: int
    size: int
    rejected: bool
    rejections: list[str]
    raw: dict = field(default_factory=dict)


@with_retry(label="Radarr: list library options")
async def list_library_options() -> dict:
    """Quality profiles + root folders from the CONFIGURED Radarr connection
    (unlike test_connection, which takes ad-hoc creds for the setup wizard).
    Used to populate the "default profile/folder for movies Nyaarr adds"
    pickers in Settings.
    """
    async with _client() as client:
        profiles_resp = await client.get("/qualityprofile")
        profiles_resp.raise_for_status()
        profiles = [{"id": p["id"], "name": p["name"]} for p in profiles_resp.json()]

        root_resp = await client.get("/rootfolder")
        root_resp.raise_for_status()
        root_folders = [r["path"] for r in root_resp.json()]

    return {"quality_profiles": profiles, "root_folders": root_folders}


@with_retry(label="Radarr: list movies")
async def list_movies() -> list[RadarrMovie]:
    async with _client() as client:
        resp = await client.get("/movie")
        resp.raise_for_status()
        data = resp.json()
    return [
        RadarrMovie(
            id=m["id"],
            title=m.get("title", ""),
            tmdb_id=m.get("tmdbId", 0),
            has_file=m.get("hasFile", False),
            monitored=m.get("monitored", False),
            poster_url=_poster_from_raw(m),
            raw=m,
        )
        for m in data
    ]


@with_retry(label="Radarr: lookup movie by tmdb id")
async def lookup_by_tmdb_id(tmdb_id: int) -> Optional[dict]:
    """Radarr's own lookup — used right before adding, so the payload we
    POST to /movie matches exactly what Radarr expects (title, images,
    year, etc.) rather than us constructing it by hand from TMDB's response
    shape, which doesn't line up 1:1 with Radarr's.
    """
    async with _client() as client:
        resp = await client.get("/movie/lookup/tmdb", params={"tmdbId": tmdb_id})
        resp.raise_for_status()
        data = resp.json()
    return data if data else None


@with_retry(label="Radarr: add movie")
async def add_movie(lookup_result: dict, quality_profile_id: int, root_folder_path: str, monitored: bool = True) -> RadarrMovie:
    if settings.dry_run:
        await log(f"[DRY RUN] would add movie '{lookup_result.get('title')}' (tmdbId={lookup_result.get('tmdbId')}) to Radarr")
        return RadarrMovie(id=-1, title=lookup_result.get("title", ""), tmdb_id=lookup_result.get("tmdbId", 0), has_file=False, monitored=monitored, poster_url=_poster_from_raw(lookup_result), raw=lookup_result)

    payload = dict(lookup_result)
    payload["qualityProfileId"] = quality_profile_id
    payload["rootFolderPath"] = root_folder_path
    payload["monitored"] = monitored
    payload["addOptions"] = {"searchForMovie": False}  # Nyaarr's own curation pipeline handles the search, not Radarr's default

    async with _client() as client:
        resp = await client.post("/movie", json=payload)
        resp.raise_for_status()
        m = resp.json()
    return RadarrMovie(id=m["id"], title=m.get("title", ""), tmdb_id=m.get("tmdbId", 0), has_file=m.get("hasFile", False), monitored=m.get("monitored", False), poster_url=_poster_from_raw(m), raw=m)


@with_retry(label="Radarr: get releases for movie")
async def get_releases(movie_id: int) -> list[RadarrRelease]:
    async with _client() as client:
        resp = await client.get("/release", params={"movieId": movie_id})
        resp.raise_for_status()
        data = resp.json()
    return [
        RadarrRelease(
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


@with_retry(label="Radarr: grab release")
async def grab_release(guid: str, indexer_id: int) -> None:
    if settings.dry_run:
        await log(f"[DRY RUN] would grab Radarr release guid={guid} indexerId={indexer_id}")
        return
    async with _client() as client:
        resp = await client.post("/release", json={"guid": guid, "indexerId": indexer_id})
        resp.raise_for_status()


@with_retry(label="Radarr: get queue")
async def get_queue() -> list[dict[str, Any]]:
    async with _client() as client:
        resp = await client.get("/queue", params={"pageSize": 200})
        resp.raise_for_status()
        return resp.json().get("records", [])


async def queue_records_for_movie(movie_id: int) -> list[dict[str, Any]]:
    records = await get_queue()
    return [r for r in records if r.get("movieId") == movie_id]


@with_retry(label="Radarr: remove queue item")
async def remove_queue_item(queue_id: int, remove_from_client: bool = True, blocklist: bool = False) -> None:
    if settings.dry_run:
        await log(f"[DRY RUN] would remove Radarr queue item {queue_id}")
        return
    async with _client() as client:
        resp = await client.delete(
            f"/queue/{queue_id}",
            params={"removeFromClient": str(remove_from_client).lower(), "blocklist": str(blocklist).lower()},
        )
        resp.raise_for_status()
