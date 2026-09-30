import asyncio
import datetime
import functools
import os
import re
from typing import Awaitable, Callable, TypeVar

from .config import settings

T = TypeVar("T")

_lock = asyncio.Lock()

# httpx exception messages include the full request URL — for TMDB calls
# that means the api_key query param rides along in plaintext into every
# retry/failure log line. Sonarr/Radarr use a header instead of a query
# param, so they're not exposed this way, but redact generically rather
# than special-casing which service uses which transport.
_REDACT_PATTERNS = [
    (re.compile(r"([?&](?:api_?key|token)=)[^&\s'\"]+", re.IGNORECASE), r"\1***redacted***"),
    # A Discord webhook URL is itself a credential (anyone holding it can
    # post), and httpx puts it in the failure message of a failed send.
    (re.compile(r"(discord(?:app)?\.com/api/webhooks/\d+/)[\w-]+", re.IGNORECASE), r"\1***redacted***"),
]


def _redact(message: str) -> str:
    for pattern, replacement in _REDACT_PATTERNS:
        message = pattern.sub(replacement, message)
    return message


def _line(message: str) -> str:
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    return f"[{ts}] {_redact(message)}"


async def log(message: str) -> None:
    line = _line(message)
    print(line, flush=True)
    os.makedirs(settings.data_dir, exist_ok=True)
    async with _lock:
        with open(settings.log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def with_retry(
    attempts: int = 3,
    delays: tuple[float, ...] = (5, 15, 45),
    label: str = "api call",
):
    """Retry an async API call with backoff. Logs and re-raises after final attempt."""

    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            last_exc: Exception | None = None
            for attempt in range(1, attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001 - deliberately broad, this is a network boundary
                    last_exc = exc
                    if attempt < attempts:
                        delay = delays[min(attempt - 1, len(delays) - 1)]
                        await log(f"{label} failed (attempt {attempt}/{attempts}): {exc} — retrying in {delay}s")
                        await asyncio.sleep(delay)
                    else:
                        await log(f"{label} failed (attempt {attempt}/{attempts}): {exc} — giving up")
            assert last_exc is not None
            raise last_exc

        return wrapper

    return decorator
