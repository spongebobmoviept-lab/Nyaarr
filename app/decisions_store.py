import asyncio
import datetime
import json
import os
from typing import Any, Literal

from .config import settings

ItemKind = Literal["episode", "movie"]


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class DecisionsStore:
    """Deliberately much lighter than Reclaimarr's jobs.py: no
    preserved_files, no rename bookkeeping, no multi-day monitor-loop
    state. Nyaarr never touches a file Sonarr/Radarr don't already own, so
    there is nothing to protect and nothing to revert — the two things
    that DO need a home here are (1) which release guids have already been
    tried for an episode/movie, so a dead one isn't retried forever, and
    (2) an audit log of *why* a release was picked, since Sonarr/Radarr's
    own history only records "grabbed", not the reasoning.
    """

    MAX_HISTORY = 500

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self.tried_guids: dict[str, list[str]] = {}  # "episode:123" -> [guid, ...]
        self.history: list[dict[str, Any]] = []

    async def load(self) -> None:
        if not os.path.exists(self._path):
            return
        with open(self._path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        self.tried_guids = raw.get("tried_guids", {})
        self.history = raw.get("history", [])

    async def _save(self) -> None:
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        payload = {"tried_guids": self.tried_guids, "history": self.history}
        tmp_path = self._path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp_path, self._path)

    def _key(self, kind: ItemKind, item_id: int) -> str:
        return f"{kind}:{item_id}"

    def get_tried_guids(self, kind: ItemKind, item_id: int) -> list[str]:
        return self.tried_guids.get(self._key(kind, item_id), [])

    async def record_tried_guid(self, kind: ItemKind, item_id: int, guid: str) -> None:
        async with self._lock:
            key = self._key(kind, item_id)
            self.tried_guids.setdefault(key, []).append(guid)
            await self._save()

    async def record_decision(
        self,
        kind: ItemKind,
        item_id: int,
        title: str,
        outcome: str,
        detail: str,
        poster_url: str | None = None,
    ) -> None:
        """outcome: e.g. 'grabbed', 'needs_review', 'no_release', 'not_aired', 'dedupe_removed', 'failed'"""
        async with self._lock:
            self.history.insert(
                0,
                {
                    "kind": kind,
                    "item_id": item_id,
                    "title": title,
                    "outcome": outcome,
                    "detail": detail,
                    "poster_url": poster_url,
                    "at": _now(),
                },
            )
            self.history = self.history[: self.MAX_HISTORY]
            await self._save()


store = DecisionsStore(os.path.join(settings.data_dir, "decisions.json"))
