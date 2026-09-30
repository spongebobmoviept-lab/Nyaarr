import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

from . import auth_store, connections_store, curation, decisions_store, discord, franchise, prefs_store, radarr, settings_store, sonarr
from .auth import check_credentials, require_login, security
from .config import settings
from .logger import log

_background_tasks: list[asyncio.Task] = []

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
_NO_CACHE_HEADERS = {"Cache-Control": "no-cache, no-store, must-revalidate"}


@asynccontextmanager
async def lifespan(_: FastAPI):
    os.makedirs(settings.data_dir, exist_ok=True)
    connections_store.load_overrides()
    settings_store.load_overrides()
    prefs_store.load()
    await decisions_store.store.load()

    await log(f"nyaarr: starting (dry_run={settings.dry_run})")
    _background_tasks.append(asyncio.create_task(curation.scan_loop()))
    asyncio.create_task(discord.startup_online())

    yield
    for task in _background_tasks:
        task.cancel()


app = FastAPI(title="Nyaarr", version="1.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/favicon.svg")
async def favicon() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "favicon.svg"), media_type="image/svg+xml")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> Response:
    if not auth_store.is_setup_complete():
        return RedirectResponse(url="/setup")
    credentials = await security(request)
    check_credentials(request, credentials)
    return FileResponse(os.path.join(STATIC_DIR, "index.html"), headers=_NO_CACHE_HEADERS)


@app.get("/setup", response_class=HTMLResponse)
async def setup_page() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "setup.html"), headers=_NO_CACHE_HEADERS)


@app.get("/style.css")
async def style(_: str = Depends(require_login)) -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "style.css"), media_type="text/css", headers=_NO_CACHE_HEADERS)


@app.get("/app.js")
async def app_js(_: str = Depends(require_login)) -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "app.js"), media_type="application/javascript", headers=_NO_CACHE_HEADERS)


# ---------------------------------------------------------------------------
# Setup wizard
# ---------------------------------------------------------------------------


@app.get("/api/setup/status")
async def api_setup_status() -> JSONResponse:
    return JSONResponse(
        {
            "setup_complete": auth_store.is_setup_complete(),
            "admin_configured": auth_store.is_admin_configured(),
            "sonarr_configured": bool(settings.sonarr_url and settings.sonarr_api_key),
            "radarr_configured": bool(settings.radarr_url and settings.radarr_api_key),
        }
    )


@app.post("/api/setup/admin")
async def api_setup_admin(body: dict) -> JSONResponse:
    if auth_store.is_admin_configured():
        raise HTTPException(status_code=403, detail="An admin login already exists for this instance")
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not username or len(password) < 8:
        raise HTTPException(status_code=400, detail="Username is required and password must be at least 8 characters")
    auth_store.set_admin(username, password)
    await log(f"setup: admin login created for '{username}'")
    return JSONResponse({"ok": True})


@app.post("/api/setup/test-sonarr")
async def api_setup_test_sonarr(body: dict, _: str = Depends(require_login)) -> JSONResponse:
    try:
        result = await sonarr.test_connection(body.get("url", ""), body.get("api_key", ""))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Couldn't connect: {exc}")
    return JSONResponse(result)


@app.post("/api/setup/test-radarr")
async def api_setup_test_radarr(body: dict, _: str = Depends(require_login)) -> JSONResponse:
    try:
        result = await radarr.test_connection(body.get("url", ""), body.get("api_key", ""))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Couldn't connect: {exc}")
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# Connections / settings
# ---------------------------------------------------------------------------


@app.get("/api/connections")
async def api_get_connections(_: str = Depends(require_login)) -> JSONResponse:
    return JSONResponse(connections_store.current_display())


@app.post("/api/connections")
async def api_save_connections(update: dict, _: str = Depends(require_login)) -> JSONResponse:
    unknown = [k for k in update if k not in connections_store.ALL_KEYS]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown connection field(s): {unknown}")
    result = connections_store.save_overrides(update)
    await log("connections: settings updated by user")
    return JSONResponse(result)


@app.get("/api/settings")
async def api_get_settings(_: str = Depends(require_login)) -> JSONResponse:
    return JSONResponse(settings_store.current_editable())


@app.post("/api/settings")
async def api_save_settings(update: dict, _: str = Depends(require_login)) -> JSONResponse:
    unknown = [k for k in update if k not in settings_store.EDITABLE_KEYS]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown/non-editable setting(s): {unknown}")
    return JSONResponse(settings_store.save_overrides(update))


@app.get("/api/show-prefs")
async def api_get_show_prefs(_: str = Depends(require_login)) -> JSONResponse:
    return JSONResponse(prefs_store.all_overrides())


@app.post("/api/series/{series_id}/preference")
async def api_set_show_pref(series_id: int, body: dict, _: str = Depends(require_login)) -> JSONResponse:
    preference = body.get("preference", "default")
    try:
        prefs_store.set_preference(series_id, preference)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Episodes (Sonarr)
# ---------------------------------------------------------------------------


@app.get("/api/series")
async def api_list_series(_: str = Depends(require_login)) -> JSONResponse:
    tag_labels = await sonarr.list_tags()
    all_series = await sonarr.list_series()
    anime = [s for s in all_series if sonarr.is_anime_series(s, tag_labels)]
    return JSONResponse(
        [
            {
                "id": s.id,
                "title": s.title,
                "tvdb_id": s.tvdb_id,
                "monitored": s.monitored,
                "preference": prefs_store.get_preference(s.id),
            }
            for s in anime
        ]
    )


@app.get("/api/series/{series_id}/episodes")
async def api_series_episodes(series_id: int, _: str = Depends(require_login)) -> JSONResponse:
    all_series = await sonarr.list_series()
    series = next((s for s in all_series if s.id == series_id), None)
    if series is None:
        raise HTTPException(status_code=404, detail="Series not found")

    episodes = await sonarr.list_episodes(series_id)
    results = []
    for ep in episodes:
        if not ep.monitored:
            continue
        outcome = curation.get_episode_status(ep)  # cheap, cache-backed — see curation.py module docstring
        results.append(
            {
                "episode_id": ep.id,
                "season": ep.season_number,
                "episode": ep.episode_number,
                "title": ep.title,
                "air_date_utc": ep.air_date_utc,
                **outcome,
            }
        )
    return JSONResponse(results)


@app.post("/api/episodes/{episode_id}/check")
async def api_check_episode(episode_id: int, body: dict, _: str = Depends(require_login)) -> JSONResponse:
    """On-demand live release search for exactly one episode — the fast,
    targeted alternative to waiting for the next background scan cycle.
    """
    series_title = body.get("series_title", "")
    series_id = body.get("series_id")
    if series_id is None:
        raise HTTPException(status_code=400, detail="series_id is required")
    episodes = await sonarr.list_episodes(series_id)
    episode = next((e for e in episodes if e.id == episode_id), None)
    if episode is None:
        raise HTTPException(status_code=404, detail="Episode not found")
    series = await sonarr.get_series(series_id)
    poster_url = series.poster_url if series else None
    outcome = await curation.scan_episode(episode, series_title, poster_url=poster_url)
    return JSONResponse(outcome)


@app.post("/api/episodes/{episode_id}/grab")
async def api_grab_episode(episode_id: int, body: dict, _: str = Depends(require_login)) -> JSONResponse:
    guid = body.get("guid")
    indexer_id = body.get("indexer_id")
    series_title = body.get("series_title", "")
    episode_label = body.get("episode_label", "")
    series_id = body.get("series_id")
    if not guid or indexer_id is None:
        raise HTTPException(status_code=400, detail="guid and indexer_id are required")
    poster_url = None
    if series_id is not None:
        series = await sonarr.get_series(series_id)
        poster_url = series.poster_url if series else None
    await curation.grab_episode(episode_id, series_title, guid, indexer_id, episode_label=episode_label, poster_url=poster_url)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Movies (Radarr) + franchise discovery
# ---------------------------------------------------------------------------


@app.post("/api/series/{series_id}/find-movies")
async def api_find_movies(series_id: int, _: str = Depends(require_login)) -> JSONResponse:
    if not settings.tmdb_api_key:
        raise HTTPException(status_code=400, detail="TMDB API key not configured — add it in Settings to use movie discovery")
    all_series = await sonarr.list_series()
    series = next((s for s in all_series if s.id == series_id), None)
    if series is None:
        raise HTTPException(status_code=404, detail="Series not found")
    candidates = await franchise.find_movies_for_series(series.title)
    return JSONResponse(candidates)


@app.get("/api/radarr/library-options")
async def api_radarr_library_options(_: str = Depends(require_login)) -> JSONResponse:
    try:
        result = await radarr.list_library_options()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Couldn't reach Radarr: {exc}")
    return JSONResponse(result)


@app.post("/api/movies/add")
async def api_add_movie(body: dict, _: str = Depends(require_login)) -> JSONResponse:
    tmdb_id = body.get("tmdb_id")
    source_series_id = body.get("source_series_id")
    quality_profile_id = body.get("quality_profile_id") or settings.default_movie_quality_profile_id
    root_folder_path = body.get("root_folder_path") or settings.default_movie_root_folder
    if not all([tmdb_id, source_series_id, quality_profile_id, root_folder_path]):
        raise HTTPException(
            status_code=400,
            detail="tmdb_id and source_series_id are required, and a default quality profile + root folder must be set in Settings (or passed explicitly)",
        )
    try:
        movie = await franchise.add_confirmed_movie(tmdb_id, source_series_id, quality_profile_id, root_folder_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse({"ok": True, "movie_id": movie.id, "title": movie.title})


@app.get("/api/movies")
async def api_list_movies(_: str = Depends(require_login)) -> JSONResponse:
    movies = await radarr.list_movies()
    linked = [m for m in movies if prefs_store.is_movie_linked(m.id)]  # only movies Nyaarr itself added via franchise discovery
    results = []
    for m in linked:
        outcome = curation.get_movie_status(m)  # cheap, cache-backed
        results.append({"movie_id": m.id, "title": m.title, "tmdb_id": m.tmdb_id, **outcome})
    return JSONResponse(results)


@app.post("/api/movies/{movie_id}/check")
async def api_check_movie(movie_id: int, _: str = Depends(require_login)) -> JSONResponse:
    movies = await radarr.list_movies()
    movie = next((m for m in movies if m.id == movie_id), None)
    if movie is None:
        raise HTTPException(status_code=404, detail="Movie not found")
    outcome = await curation.scan_movie(movie)
    return JSONResponse(outcome)


@app.post("/api/movies/{movie_id}/grab")
async def api_grab_movie(movie_id: int, body: dict, _: str = Depends(require_login)) -> JSONResponse:
    guid = body.get("guid")
    indexer_id = body.get("indexer_id")
    title = body.get("title", "")
    if not guid or indexer_id is None:
        raise HTTPException(status_code=400, detail="guid and indexer_id are required")
    movies = await radarr.list_movies()
    poster_url = next((m.poster_url for m in movies if m.id == movie_id), None)
    await curation.grab_movie(movie_id, title, guid, indexer_id, poster_url=poster_url)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# History / scan control
# ---------------------------------------------------------------------------


@app.get("/api/history")
async def api_history(_: str = Depends(require_login)) -> JSONResponse:
    return JSONResponse(decisions_store.store.history)


@app.post("/api/debug/run-scan-now")
async def api_run_scan_now(_: str = Depends(require_login)) -> JSONResponse:
    asyncio.create_task(curation.run_full_scan())
    return JSONResponse({"ok": True, "note": "scan started in background"})


@app.get("/api/log")
async def api_log(lines: int = 200, _: str = Depends(require_login)) -> JSONResponse:
    if not os.path.exists(settings.log_file):
        return JSONResponse([])
    with open(settings.log_file, "r", encoding="utf-8") as f:
        all_lines = f.readlines()
    return JSONResponse([line.rstrip("\n") for line in all_lines[-lines:]])
