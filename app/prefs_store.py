import json
import os

from .config import settings

_path = os.path.join(settings.data_dir, "show_prefs.json")

VALID_PREFERENCES = ("prefer_sub", "prefer_dub", "no_preference", "default")

# {series_id (str) -> "prefer_sub" | "prefer_dub" | "no_preference"}
# A show not present here just uses settings.default_language_preference.
_overrides: dict[str, str] = {}

# {movie_id (str) -> series_id (int)} — which anime series' "Find Movies"
# flow discovered this Radarr movie (see franchise.py), so it can inherit
# that series' sub/dub preference. Radarr has no seriesType/anime concept
# of its own, so a discovered movie has nothing to classify itself by
# other than "the series that led me here."
_movie_series_links: dict[str, int] = {}


def load() -> None:
    global _overrides, _movie_series_links
    if not os.path.exists(_path):
        return
    with open(_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    _overrides = data.get("overrides", {})
    _movie_series_links = data.get("movie_series_links", {})


def _persist() -> None:
    os.makedirs(settings.data_dir, exist_ok=True)
    tmp_path = _path + ".tmp"
    payload = {"overrides": _overrides, "movie_series_links": _movie_series_links}
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp_path, _path)


def link_movie_to_series(movie_id: int, series_id: int) -> None:
    _movie_series_links[str(movie_id)] = series_id
    _persist()


def is_movie_linked(movie_id: int) -> bool:
    """True if this movie came from Nyaarr's own franchise-discovery add
    flow — used to scope the dashboard to movies Nyaarr actually knows
    about, never an arbitrary/unrelated Radarr movie.
    """
    return str(movie_id) in _movie_series_links


def get_preference_for_movie(movie_id: int) -> str:
    series_id = _movie_series_links.get(str(movie_id))
    if series_id is not None:
        return get_preference(series_id)
    return settings.default_language_preference


def get_preference(series_id: int) -> str:
    override = _overrides.get(str(series_id))
    if override and override != "default":
        return override
    return settings.default_language_preference


def set_preference(series_id: int, preference: str) -> None:
    if preference not in VALID_PREFERENCES:
        raise ValueError(f"Invalid preference: {preference}")
    if preference == "default":
        _overrides.pop(str(series_id), None)
    else:
        _overrides[str(series_id)] = preference
    _persist()


def all_overrides() -> dict[str, str]:
    return dict(_overrides)
