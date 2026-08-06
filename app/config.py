import os


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _env_bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes")


class Settings:
    def __init__(self) -> None:
        # Blank by default (not required at startup) so a fresh, unconfigured
        # copy of the app can boot and serve the first-run setup wizard
        # instead of crashing before FastAPI even starts — same reasoning as
        # Reclaimarr's config.py.
        self.sonarr_url = os.environ.get("SONARR_URL", "")
        self.sonarr_api_key = os.environ.get("SONARR_API_KEY", "")

        self.radarr_url = os.environ.get("RADARR_URL", "")
        self.radarr_api_key = os.environ.get("RADARR_API_KEY", "")

        # Optional — franchise movie discovery (Phase 4) doesn't work without
        # it, but everything else (episode curation) does.
        self.tmdb_api_key = os.environ.get("TMDB_API_KEY", "")

        self.auth_username = os.environ.get("AUTH_USERNAME", "admin")
        self.auth_password = os.environ.get("AUTH_PASSWORD", "")

        self.dry_run = _env_bool("DRY_RUN", True)

        # How the tool decides a series is "anime" — seriesType is Sonarr's
        # own structured field (primary signal); anime_tag_name is a
        # fallback for shows tagged manually instead of set via seriesType.
        self.anime_tag_name = os.environ.get("ANIME_TAG_NAME", "anime")

        # A near-dead torrent otherwise looks identical to a healthy one
        # until you check seeders — confirmed live (Bleach) that a naive
        # "best quality first" pick landed a 0-seeder dead release twice.
        # This is a hard filter, not just a tiebreaker.
        self.min_seeders = _env_int("MIN_SEEDERS", 1)

        # Comma-separated fansub groups whose releases are trusted as a
        # HIGH-confidence sub signal even with no explicit "Multi-Subs"-style
        # marker in the title, since their naming convention is well known
        # in the community. User-editable via Settings.
        self.known_fansub_groups = [
            g.strip() for g in os.environ.get(
                "KNOWN_FANSUB_GROUPS", "SubsPlease,Erai-raws,ASW,EMBER,ToonsHub,NanDesuKa"
            ).split(",") if g.strip()
        ]

        # Global default; per-show overrides live in prefs_store.py.
        # One of: "prefer_sub", "prefer_dub", "no_preference".
        self.default_language_preference = os.environ.get("DEFAULT_LANGUAGE_PREFERENCE", "prefer_sub")

        self.scan_interval_minutes = _env_int("SCAN_INTERVAL_MINUTES", 45)

        # A library with hundreds of anime series each potentially having
        # many monitored-but-missing episodes (long-running shows especially
        # — confirmed live against a 500-series real library) makes "search
        # every missing episode every cycle" genuinely too expensive to be a
        # background task, let alone a page load. Caps how many get a fresh
        # live release search per scan cycle; already-fresh entries (younger
        # than the scan interval) are skipped rather than re-searched.
        self.max_episodes_per_scan = _env_int("MAX_EPISODES_PER_SCAN", 50)

        # Phase 6 — off by default. Even when true, UNKNOWN-confidence
        # releases are never auto-grabbed, only HIGH-confidence ones.
        self.auto_grab_high_confidence = _env_bool("AUTO_GRAB_HIGH_CONFIDENCE", False)

        # Franchise movie discovery noise filters (see franchise.py) — keeps
        # "the main ones" (real theatrical releases) out of a sea of shorts
        # and unrelated same-named titles.
        self.movie_min_runtime_minutes = _env_int("MOVIE_MIN_RUNTIME_MINUTES", 40)
        self.movie_min_vote_count = _env_int("MOVIE_MIN_VOTE_COUNT", 20)

        # Which Radarr quality profile / root folder a confirmed franchise
        # movie gets added under — set once in Settings (populated from
        # Radarr's own live list) rather than re-prompted on every add.
        self.default_movie_quality_profile_id = _env_int("DEFAULT_MOVIE_QUALITY_PROFILE_ID", 0)
        self.default_movie_root_folder = os.environ.get("DEFAULT_MOVIE_ROOT_FOLDER", "")

        # Optional — same reasoning as Reclaimarr's: empty means Discord
        # notifications are silently skipped everywhere they're called.
        self.discord_webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

        self.app_port = _env_int("APP_PORT", 8686)

        self.data_dir = os.environ.get("DATA_DIR", "/data")
        self.log_file = os.path.join(self.data_dir, "log.txt")


settings = Settings()
