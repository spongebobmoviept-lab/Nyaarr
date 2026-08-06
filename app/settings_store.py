import json
import os

from .config import settings

EDITABLE_KEYS = [
    "anime_tag_name",
    "min_seeders",
    "known_fansub_groups",
    "default_language_preference",
    "scan_interval_minutes",
    "max_episodes_per_scan",
    "auto_grab_high_confidence",
    "movie_min_runtime_minutes",
    "movie_min_vote_count",
    "default_movie_quality_profile_id",
    "default_movie_root_folder",
]

_overrides_path = os.path.join(settings.data_dir, "settings_overrides.json")


def load_overrides() -> None:
    if not os.path.exists(_overrides_path):
        return
    with open(_overrides_path, "r", encoding="utf-8") as f:
        overrides = json.load(f)
    for key, value in overrides.items():
        if key in EDITABLE_KEYS:
            setattr(settings, key, value)


def _persist_all() -> None:
    os.makedirs(settings.data_dir, exist_ok=True)
    payload = {key: getattr(settings, key) for key in EDITABLE_KEYS}
    tmp_path = _overrides_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp_path, _overrides_path)


def current_editable() -> dict:
    return {key: getattr(settings, key) for key in EDITABLE_KEYS}


def save_overrides(update: dict) -> dict:
    for key, value in update.items():
        if key in EDITABLE_KEYS:
            setattr(settings, key, value)
    _persist_all()
    return current_editable()
