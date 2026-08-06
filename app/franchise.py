"""Series -> candidate movies -> human confirms -> add to Radarr.

Sonarr (TVDB-keyed) and Radarr (TMDB-keyed) have no shared ID space, so
"does this anime series have related movies" can't be a lookup — it has to
be a search-and-confirm flow. This module never adds anything on its own;
every add is a direct result of the user clicking a specific candidate.
"""

from . import discord, prefs_store, radarr, sonarr, tmdb
from .logger import log


async def find_movies_for_series(series_title: str) -> list[dict]:
    candidates = await tmdb.find_candidate_movies(series_title)
    return [
        {
            "tmdb_id": c.tmdb_id,
            "title": c.title,
            "year": c.year,
            "overview": c.overview,
            "poster_url": c.poster_url,
            "runtime_minutes": c.runtime_minutes,
            "vote_count": c.vote_count,
        }
        for c in candidates
    ]


async def add_confirmed_movie(tmdb_id: int, source_series_id: int, quality_profile_id: int, root_folder_path: str) -> radarr.RadarrMovie:
    """Called only when the user has clicked "Add to Radarr" on a specific
    candidate — never automatically, per the explicit requirement that this
    ambiguity gets resolved by the user, not guessed by the tool.
    """
    lookup_result = await radarr.lookup_by_tmdb_id(tmdb_id)
    if lookup_result is None:
        raise ValueError(f"TMDB id {tmdb_id} not found via Radarr's own lookup")

    movie = await radarr.add_movie(lookup_result, quality_profile_id, root_folder_path, monitored=True)
    prefs_store.link_movie_to_series(movie.id, source_series_id)
    await log(f"nyaarr: added '{movie.title}' to Radarr (discovered via series {source_series_id}, tmdbId={tmdb_id})")

    all_series = await sonarr.list_series()
    source_series = next((s for s in all_series if s.id == source_series_id), None)
    await discord.movie_added(movie.title, source_series.title if source_series else f"series {source_series_id}", movie.poster_url)
    return movie
