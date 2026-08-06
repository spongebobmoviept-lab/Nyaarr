from dataclasses import dataclass

import httpx

from .config import settings
from .logger import with_retry

_BASE_URL = "https://api.themoviedb.org/3"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=_BASE_URL, timeout=15, params={"api_key": settings.tmdb_api_key})


@dataclass
class TmdbMovieCandidate:
    tmdb_id: int
    title: str
    year: str
    overview: str
    poster_url: str
    vote_count: int
    popularity: float
    runtime_minutes: int


@with_retry(label="TMDB: search movies", attempts=2)
async def _search_movies(query: str) -> list[dict]:
    async with _client() as client:
        resp = await client.get("/search/movie", params={"query": query})
        resp.raise_for_status()
        return resp.json().get("results", [])


@with_retry(label="TMDB: get movie details", attempts=2)
async def _get_details(tmdb_id: int) -> dict:
    async with _client() as client:
        resp = await client.get(f"/movie/{tmdb_id}")
        resp.raise_for_status()
        return resp.json()


async def find_candidate_movies(series_title: str, limit: int = 8) -> list[TmdbMovieCandidate]:
    """Search TMDB for movies matching a series title, apply basic noise
    filtering, and return ranked candidates for human confirmation.

    Deliberately does NOT try to guess which single result is "the" answer
    — a franchise search legitimately returns several real movies (Spy x
    Family has more than one). The job here is narrowing "20 mostly
    unrelated results" down to "a handful of plausible ones", not picking
    a winner. The user confirms from there.
    """
    raw_results = await _search_movies(series_title)
    if not raw_results:
        return []

    # Cheap pre-filter on search-result fields alone (no extra API calls
    # yet) before spending a details call on each survivor.
    prefiltered = [
        r for r in raw_results
        if (r.get("vote_count") or 0) >= settings.movie_min_vote_count
    ]
    prefiltered.sort(key=lambda r: r.get("popularity", 0), reverse=True)
    prefiltered = prefiltered[: limit * 2]  # headroom before the runtime filter trims further

    candidates: list[TmdbMovieCandidate] = []
    for r in prefiltered:
        details = await _get_details(r["id"])
        runtime = details.get("runtime") or 0
        if runtime < settings.movie_min_runtime_minutes:
            continue  # excludes shorts/OVA-bundle-as-a-single-TMDB-entry noise
        candidates.append(
            TmdbMovieCandidate(
                tmdb_id=r["id"],
                title=r.get("title", ""),
                year=(r.get("release_date") or "")[:4],
                overview=r.get("overview", ""),
                poster_url=f"https://image.tmdb.org/t/p/w342{r['poster_path']}" if r.get("poster_path") else "",
                vote_count=r.get("vote_count") or 0,
                popularity=r.get("popularity") or 0,
                runtime_minutes=runtime,
            )
        )
        if len(candidates) >= limit:
            break

    return candidates
