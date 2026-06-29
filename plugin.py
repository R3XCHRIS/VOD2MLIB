"""
VOD to Media Library — Dispatcharr VOD .strm Generator Plugin
(slug: vod2mlib)
v1.15.2 — filter Scan + Generate to active M3U accounts only; strip
          bare trailing year from titles (Wicked: For Good - 2025);
          warn on Show Status when settings drift from the cron
          snapshot; clearer folder-migration help text.

MIT License
Copyright (c) 2025-2026 shedunraid (original author)
Copyright (c) 2026 R3XCHRIS (downstream maintainer, fork)
Upstream:   https://github.com/shedunraid/VOD2MLIB
This fork:  https://github.com/R3XCHRIS/VOD2MLIB
"""
import os
import re
import sys
from typing import Dict, Any
from concurrent.futures import ThreadPoolExecutor, as_completed

# The optional rclone/Plex mount mode lives in httpfs_control.py (vendored child
# server in httpfs/). Ensure the plugin dir is importable regardless of how
# Dispatcharr loads this module, then pull in the mixin. If it can't be imported
# the classic .strm generator must still work, so we degrade to a no-op base.
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

# The single shared core: one naming path, one playback path, one config path.
# Both the .strm generator (this file) and the HTTP mount (mountsrv/) build on it.
from vodlib import naming as _naming
from vodlib.playback import proxy_url as _proxy_url, validate_base_url as _validate_base_url

try:
    from httpfs_control import HttpfsControlMixin
except Exception as _httpfs_import_err:  # pragma: no cover
    print(f"[vod2mlib] httpfs mount mode unavailable: {_httpfs_import_err}",
          file=sys.stderr)

    class HttpfsControlMixin:  # type: ignore
        pass


class Plugin(HttpfsControlMixin):
    """Generate .strm files for VOD movies from Dispatcharr."""
    
    name = "VOD to Media Library"
    version = "1.16.0"
    help_url = "https://github.com/R3XCHRIS/VOD2MLIB#readme"
    description = (
        "Convert Dispatcharr VODs into media-server-friendly .strm files, with "
        "optional NFO metadata, batch processing, and a cron-driven auto-rescan."
    )

    # Tunables
    MAX_WORKERS = 3
    LOG_EVERY = 50
    LOG_FIRST_N = 10

    # Schedule task identity (django-celery-beat row name + Celery task name)
    SCHEDULE_TASK_NAME = "vod2mlib.auto_rescan"
    SCHEDULED_TASK_CELERY_NAME = "vod2mlib.scheduled_rescan"

    # The legacy default Dispatcharr URL — a placeholder that must NOT be
    # shipped into .strm files. We reject it explicitly to catch users who
    # forgot to click Save after editing the URL field.
    PLACEHOLDER_DISPATCHARR_URL = "http://192.168.99.11:9191"

    # File suffixes the plugin writes (used by cleanup and skip logic)
    _PLUGIN_FILE_SUFFIXES = ('.strm', '.nfo')

    # Title/year/quality cleaning now lives in the single naming path
    # (vodlib.naming) and is shared with the mount. The only regex kept here is
    # the language-prefix matcher used by category→genre extraction below
    # (genres are a .strm-NFO concern, not a naming concern).
    # Whitespace required before the dash so 'AC-130' is preserved.
    _LANGUAGE_PREFIX_RE = re.compile(r'^[A-Z]{2,3}\s+-\s*')

    # Year-bucket category names like "2026 Movies", "1990s Series",
    # "2020 TV Shows" — these are navigation buckets from the IPTV provider's
    # category list, not real genres. Suppressed when the genre would
    # otherwise be one of these.
    _YEAR_BUCKET_GENRE_RE = re.compile(
        r'^\d{2,4}s?\s+(movies?|series|tv\s*shows?)$',
        re.IGNORECASE,
    )

    fields = [
        {
            "id": "_about",
            "label": "About",
            "type": "info",
            "description": "Turns your Dispatcharr VOD catalogue into a correctly-named media library, delivered two ways from one engine:\n  • .strm files on disk — for Jellyfin, Emby, Kodi, ChannelsDVR.\n  • a live HTTP mount (rclone) — for Plex, which can't play .strm.\nBoth share the same title cleaning, folder naming, and playback URLs, so a title lands the same name whichever output you use.\n\nWorkflow:\n  1. Set the Dispatcharr URL and (for .strm) the root folders below.\n  2. Actions → Scan → see catalogue totals.\n  3. .strm: Generate Movies / Generate Series (start with Batch Size 10).\n  4. Plex: [MOUNT] Enable, then mount with rclone (see [MOUNT] rclone config).\n\nDocs: https://github.com/R3XCHRIS/VOD2MLIB",
        },
        {
            "id": "_section_paths",
            "label": "[PATHS & HOSTS]",
            "type": "info",
            "description": "Where to write .strm files and how media servers reach Dispatcharr.",
        },
        {
            "id": "root_folder",
            "label": "Root Folder for Movies",
            "type": "string",
            "default": "/VODS/Movies",
            "help_text": "Path inside the Dispatcharr container where movie folders will be created."
        },
        {
            "id": "series_root_folder",
            "label": "Root Folder for Series",
            "type": "string",
            "default": "/VODS/Series",
            "help_text": "Path inside the Dispatcharr container where series folders will be created."
        },
        {
            "id": "dispatcharr_url",
            "label": "Dispatcharr URL (REQUIRED)",
            "type": "string",
            "default": "",
            "placeholder": "http://192.168.1.10:9191",
            "help_text": "Required. The externally-reachable URL of your Dispatcharr instance — this gets baked into every .strm file, so it must resolve from wherever your media server runs. localhost works ONLY if the media server is on the same host with shared network namespace; otherwise use a routable LAN IP/hostname. Don't forget to click Save."
        },
        {
            "id": "_section_movies",
            "label": "[MOVIES]",
            "type": "info",
            "description": "Settings for the Generate Movies action.",
        },
        {
            "id": "batch_size",
            "label": "Batch Size (Movies)",
            "type": "select",
            "default": "250",
            "options": [
                {"value": "10", "label": "10 movies"},
                {"value": "100", "label": "100 movies"},
                {"value": "200", "label": "200 movies"},
                {"value": "500", "label": "500 movies"},
                {"value": "1000", "label": "1000 movies"},
                {"value": "all", "label": "All movies"}
            ],
            "help_text": "Number of movies to process in this run"
        },
        {
            "id": "generate_nfo",
            "label": "Generate Movie NFO Files",
            "type": "boolean",
            "default": True,
            "help_text": "Create .nfo metadata files for movies"
        },
        {
            "id": "nest_movies_by_category",
            "label": "Nest Movies by Category",
            "type": "boolean",
            "default": False,
            "help_text": "Wrap each movie's folder inside a subfolder named by its M3U category. Useful when your provider organises movies by genre. Movies without a category go into a folder named 'Unassigned'. Same content with different categories (e.g. 4K vs HD) gets separate folders intentionally — turn ON Dedupe Movies Across Categories below to suppress this for genre-overlap cases."
        },
        {
            "id": "dedupe_movies_across_categories",
            "label": "Dedupe Movies Across Categories",
            "type": "boolean",
            "default": False,
            "help_text": "When `Nest Movies by Category` is ON and a movie is tagged with multiple categories upstream (e.g. 'Action' AND 'Sci-Fi'), write the `.strm` under the first category only (alphabetical by category name) instead of duplicating across all of them. No effect when `Nest Movies by Category` is OFF — in that case multi-category movies already resolve to the same folder. Use this when you want one folder per movie regardless of provider tagging; your media server's genre tags still reflect every category via the NFO. ⚠ MIGRATION: changing this on an already-generated library does NOT remove the old duplicate folders — it just stops creating new ones. To clean up existing duplicates, run `[⚠ DANGER] Clean up Movies` once, then re-generate."
        },
        {
            "id": "append_tmdb_id_to_folder",
            "label": "Append TMDB ID to folder names",
            "type": "boolean",
            "default": False,
            "help_text": "Append `{tmdb-NNN}` to every Movies and Series folder name when a TMDB ID is known — e.g. `Cool Hand Luke (1967) {tmdb-378}/`. Plex's Personal Media agent and ChannelsDVR's local-media scraper both honour this convention for forced exact matches, which is the safest defence against name collisions and bad metadata scrapes. ⚠ MIGRATION: the plugin does NOT rename existing folders in place — turning this on (or off) for an already-generated library writes the new folder names ALONGSIDE the old ones, creating duplicates. To switch cleanly, run `[⚠ DANGER] Clean up Movies` / `Series` first, then re-generate; or accept the duplicates until the old folders age out."
        },
        {
            "id": "_section_series",
            "label": "[SERIES]",
            "type": "info",
            "description": "Settings for the Generate Series action.",
        },
        {
            "id": "series_batch_size",
            "label": "Batch Size (Series)",
            "type": "select",
            "default": "10",
            "options": [
                {"value": "1", "label": "1 series (testing)"},
                {"value": "5", "label": "5 series"},
                {"value": "10", "label": "10 series"},
                {"value": "25", "label": "25 series"},
                {"value": "all", "label": "All series (slow!)"}
            ],
            "help_text": "Series to process (episodes auto-fetched for each)"
        },
        {
            "id": "generate_series_nfo",
            "label": "Generate Series NFO Files",
            "type": "boolean",
            "default": True,
            "help_text": "Create .nfo metadata files for series and episodes"
        },
        {
            "id": "refresh_existing",
            "label": "Refresh Existing Series (rescan-friendly)",
            "type": "boolean",
            "default": False,
            "help_text": "Re-evaluate series that already have folders, picking up new episodes added upstream AND rewriting existing episode .strm files so they pick up the current Dispatcharr URL. .nfo files (including tvshow.nfo) are only written when missing, so your edits are preserved. Turn ON for cron rescans."
        },
        {
            "id": "nest_series_by_category",
            "label": "Nest Series by Category",
            "type": "boolean",
            "default": False,
            "help_text": "Wrap each series' folder inside a subfolder named by its M3U category. Useful when your provider organises series by genre. Series without a category go into a folder named 'Unassigned'. Same content with different categories gets separate folders intentionally — turn ON Dedupe Series Across Categories below to suppress this for genre-overlap cases."
        },
        {
            "id": "dedupe_series_across_categories",
            "label": "Dedupe Series Across Categories",
            "type": "boolean",
            "default": False,
            "help_text": "When `Nest Series by Category` is ON and a series is tagged with multiple categories upstream, write the series folder + episodes under the first category only (alphabetical by category name) instead of duplicating across all of them. No effect when `Nest Series by Category` is OFF. ⚠ MIGRATION: changing this on an already-generated library does NOT remove the old duplicate folders — run `[⚠ DANGER] Clean up Series` once, then re-generate, to clean them up."
        },
        {
            "id": "_section_schedule",
            "label": "[AUTO-RESCAN SCHEDULE]",
            "type": "info",
            "description": "Configure the cron job. Click Apply in the Actions tab to register or update.",
        },
        {
            "id": "schedule_cron",
            "label": "Auto-Rescan Schedule (cron)",
            "type": "string",
            "default": "0 3 * * *",
            "help_text": "Standard 5-field cron: 'minute hour day-of-month month day-of-week'. Default '0 3 * * *' = every day at 03:00. Used by 'Apply Schedule'."
        },
        {
            "id": "schedule_timezone",
            "label": "Schedule Timezone",
            "type": "string",
            "default": "",
            "placeholder": "Europe/London",
            "help_text": "IANA timezone name the cron expression is interpreted in (e.g. 'Europe/London', 'America/New_York', 'Australia/Sydney'). Leave empty to use UTC. Affects when the cron fires — '0 3 * * *' in 'Europe/London' means 03:00 London time year-round (handling BST automatically), not 03:00 UTC."
        },
        {
            "id": "schedule_target",
            "label": "Scheduled Action",
            "type": "select",
            "default": "rescan_all",
            "options": [
                {"value": "scan_all_vods", "label": "Scan only (totals)"},
                {"value": "generate_movies", "label": "Movies only"},
                {"value": "generate_series", "label": "Series only"},
                {"value": "rescan_all", "label": "Full rescan (movies + series)"}
            ],
            "help_text": "Which action the scheduler should run on each tick."
        },
        {
            "id": "_section_mount",
            "label": "[PLEX]",
            "type": "info",
            "description": "Plex delivery of the same library. Plex can't play .strm, so instead the plugin serves your VOD catalogue as a live read-only HTTP filesystem you mount with rclone; Plex reads real, correctly-named files (identical naming to the .strm output) that stream via Dispatcharr's VOD proxy. Click [MOUNT] Enable to start it; the mount runs as a small server inside Dispatcharr and installs uvicorn/fastapi/jinja2 on first enable (the .strm output needs no extra packages). Uses the same Dispatcharr URL as above.",
        },
        {
            "id": "httpfs_port",
            "label": "Mount HTTP Port",
            "type": "number",
            "default": 8889,
            "help_text": "Port the mount server listens on (inside the container; publish it so rclone can reach it). rclone connects here; the Dispatcharr URL above is the playback redirect target. Default 8889."
        },
        {
            "id": "httpfs_enable_auth",
            "label": "Mount Authentication (Token-based)",
            "type": "boolean",
            "default": False,
            "help_text": "Require a Dispatcharr API key on every mount request. Enable if the port is exposed beyond a trusted network. When ON, add the API key to the rclone config's headers line."
        },
        {
            "id": "httpfs_hydrate_concurrency",
            "label": "Mount Size-Hydration Concurrency",
            "type": "number",
            "default": 8,
            "help_text": "Parallel fetches the mount uses to backfill real file sizes from your provider (Plex needs true sizes to play). 0 disables hydration (only already-sized titles show). Default 8."
        },
        {
            "id": "httpfs_hydrate_on_load",
            "label": "Mount: Full size-sync on enable",
            "type": "boolean",
            "default": True,
            "help_text": "On [MOUNT] Enable, kick off a one-time size hydration pass so titles become visible to Plex sooner."
        },
        {
            "id": "httpfs_scheduled_times",
            "label": "Mount Hydration Times (24h)",
            "type": "string",
            "default": "0300",
            "help_text": "Comma-separated HHMM times to run a daily size-hydration pass (e.g. '0300,1500'). Empty disables the schedule."
        },
        {
            "id": "httpfs_timezone",
            "label": "Mount Hydration Timezone",
            "type": "string",
            "default": "",
            "placeholder": "America/New_York",
            "help_text": "IANA timezone for the hydration times above. Empty = UTC."
        }
    ]

    actions = [
        {
            "id": "scan_all_vods",
            "label": "[LIBRARY] Catalogue snapshot",
            "description": "Count unique Movies and Series in the Dispatcharr database. Read-only.",
            "button_label": "Scan",
            "button_variant": "outline",
            "button_color": "blue",
        },
        {
            "id": "generate_movies",
            "label": "[GENERATE] Movies",
            "description": "Process movies per Batch Size. Existing .strm files are skipped.",
            "button_label": "Generate",
            "button_variant": "filled",
            "button_color": "green",
        },
        {
            "id": "generate_series",
            "label": "[GENERATE] Series",
            "description": "Create episode .strm files. See 'Refresh Existing Series' setting.",
            "button_label": "Generate",
            "button_variant": "filled",
            "button_color": "green",
        },
        {
            "id": "rescan_all",
            "label": "[GENERATE] Full rescan",
            "description": "Rescan then force regenerate Movies + Series.",
            "button_label": "Rescan all",
            "button_variant": "filled",
            "button_color": "teal",
            "confirm": {
                "required": True,
                "title": "Run full rescan now?",
                "message": "Full rescan walks every Movie and every Series, re-fetching episode lists from the M3U source and writing any missing files. On large catalogues this can take many minutes. The cron schedule already runs this action nightly — only click here for an immediate refresh.",
            },
        },
        {
            "id": "schedule_status",
            "label": "[SCHEDULE] Show status",
            "description": "Show registered cron, last run, and total runs.",
            "button_label": "Status",
            "button_variant": "outline",
            "button_color": "blue",
        },
        {
            "id": "schedule_test_fire",
            "label": "[SCHEDULE] Test fire now",
            "description": "Fire the scheduled task immediately. Verifies the cron pipeline.",
            "button_label": "Test fire",
            "button_variant": "outline",
            "button_color": "blue",
            "confirm": {
                "required": True,
                "title": "Fire scheduled task now?",
                "message": "Runs the same action the cron will fire (with the snapshotted settings) right now. Useful to verify the pipeline works. May take many minutes depending on the action.",
            },
        },
        {
            "id": "apply_schedule",
            "label": "[SCHEDULE] Apply / Update",
            "description": "Register or update the cron task. Re-click after changing any setting.",
            "button_label": "Apply",
            "button_variant": "outline",
            "button_color": "blue",
        },
        {
            "id": "remove_schedule",
            "label": "[SCHEDULE] Unschedule",
            "description": "Remove the periodic auto-rescan task.",
            "button_label": "Remove",
            "button_variant": "outline",
            "button_color": "orange",
            "confirm": {
                "required": True,
                "title": "Remove auto-rescan schedule?",
                "message": "This unregisters the periodic task. You can re-create it any time with Apply.",
            },
        },
        {
            "id": "cleanup_movies",
            "label": "[⚠ DANGER] Clean up Movies",
            "description": "Delete plugin .strm/.nfo from Movies root. User files preserved.",
            "button_label": "Clean up",
            "button_variant": "filled",
            "button_color": "red",
            "confirm": {
                "required": True,
                "title": "Delete generated movie files?",
                "message": "This deletes every .strm and .nfo file this plugin created under your Movies root. User-added files (subtitles, posters, custom .nfo) in those folders are preserved. Continue?",
            },
        },
        {
            "id": "cleanup_series",
            "label": "[⚠ DANGER] Clean up Series",
            "description": "Delete plugin .strm/.nfo from Series root. User files preserved.",
            "button_label": "Clean up",
            "button_variant": "filled",
            "button_color": "red",
            "confirm": {
                "required": True,
                "title": "Delete generated series files?",
                "message": "This deletes every .strm and .nfo file this plugin created under your Series root. User-added files in those folders are preserved. Continue?",
            },
        },
        {
            "id": "httpfs_enable",
            "label": "[MOUNT] Enable Plex mount",
            "description": "Start the HTTP filesystem server for rclone/Plex. Installs uvicorn/fastapi/jinja2 on first run.",
            "button_label": "Enable",
            "button_variant": "filled",
            "button_color": "grape",
        },
        {
            "id": "httpfs_status",
            "label": "[MOUNT] Status",
            "description": "Show mount server state, visible title counts, and hydration progress.",
            "button_label": "Status",
            "button_variant": "outline",
            "button_color": "blue",
        },
        {
            "id": "httpfs_rclone_config",
            "label": "[MOUNT] rclone config",
            "description": "Where to get the paste-ready rclone remote for the mount.",
            "button_label": "Show",
            "button_variant": "outline",
            "button_color": "blue",
        },
        {
            "id": "httpfs_hydrate_now",
            "label": "[MOUNT] Hydrate sizes now",
            "description": "Trigger an immediate size-hydration pass so more titles become visible to Plex.",
            "button_label": "Hydrate",
            "button_variant": "outline",
            "button_color": "grape",
        },
        {
            "id": "httpfs_disable",
            "label": "[MOUNT] Disable Plex mount",
            "description": "Stop the HTTP filesystem server. The .strm generator is unaffected.",
            "button_label": "Disable",
            "button_variant": "outline",
            "button_color": "orange",
        },
    ]

    def run(self, action: str, params: dict, context: dict):
        """Execute plugin action."""
        logger = context.get("logger")
        settings = context.get("settings", {})
        
        logger.info("=" * 60)
        logger.info("VOD .strm Generator v%s", self.version)
        logger.info("Action: %s", action)
        logger.info("=" * 60)
        
        if action == "scan_all_vods":
            return self._scan_all_vods(settings, logger)
        elif action == "generate_movies":
            return self._generate_movies(settings, logger)
        elif action == "generate_series":
            return self._generate_series(settings, logger)
        elif action == "cleanup_movies":
            return self._cleanup_movies(settings, logger)
        elif action == "cleanup_series":
            return self._cleanup_series(settings, logger)
        elif action == "rescan_all":
            return self._rescan_all(settings, logger)
        elif action == "apply_schedule":
            return self._apply_schedule(settings, logger)
        elif action == "remove_schedule":
            return self._remove_schedule(settings, logger)
        elif action == "schedule_status":
            return self._schedule_status(settings, logger)
        elif action == "schedule_test_fire":
            return self._schedule_test_fire(settings, logger)
        elif action == "httpfs_enable":
            return self.httpfs_enable(settings, logger)
        elif action == "httpfs_disable":
            return self.httpfs_disable(logger)
        elif action == "httpfs_status":
            return self.httpfs_status(settings, logger)
        elif action == "httpfs_rclone_config":
            return self.httpfs_rclone_config(settings, logger)
        elif action == "httpfs_hydrate_now":
            return self.httpfs_hydrate_now(settings, logger)

        return {"status": "error", "message": f"Unknown action: {action}"}

    def stop(self, context: dict):
        """Called when the plugin is disabled/deleted/reloaded — kill the mount child."""
        import logging
        logger = context.get("logger") or logging.getLogger("vod2mlib")
        try:
            self.httpfs_stop_on_unload(logger)
        except Exception as e:
            logger.warning("httpfs stop_on_unload failed: %s", e)
    
    def _scan_all_vods(self, settings: Dict[str, Any], logger):
        """Scan and show total movies and series available."""
        logger.info("Scanning VODs in Dispatcharr...")
        logger.info("")
        
        try:
            from apps.vod.models import Movie, Series, M3UMovieRelation, M3USeriesRelation
        except ImportError as e:
            logger.error("Failed to import models: %s", e)
            return {"status": "error", "message": f"Import error: {e}"}

        try:
            # Counts are filtered to content that has at least one relation on an
            # ACTIVE M3U account — same definition Dispatcharr's own VODs UI and
            # the proxy use. Generate only writes active content, so the scan
            # totals now match what will actually be produced. Orphaned content
            # (no active provider) is surfaced separately so the gap is visible.
            active_movie = (
                Movie.objects
                .filter(m3u_relations__m3u_account__is_active=True)
                .distinct().count()
            )
            active_series = (
                Series.objects
                .filter(m3u_relations__m3u_account__is_active=True)
                .distinct().count()
            )
            total_movie = Movie.objects.count()
            total_series = Series.objects.count()
            orphan_movie = total_movie - active_movie
            orphan_series = total_series - active_series
            movie_relations = M3UMovieRelation.objects.filter(m3u_account__is_active=True).count()
            series_relations = M3USeriesRelation.objects.filter(m3u_account__is_active=True).count()

            logger.info("=" * 60)
            logger.info("MOVIES: %d active  (%d M3U relations)", active_movie, movie_relations)
            if orphan_movie:
                logger.info("        %d orphaned — no active provider (won't generate)", orphan_movie)
            logger.info("SERIES: %d active  (%d M3U relations)", active_series, series_relations)
            if orphan_series:
                logger.info("        %d orphaned — no active provider (won't generate)", orphan_series)
            logger.info("=" * 60)
            logger.info("")
            logger.info("Use 'Generate Movie .strm Files' for movies")
            logger.info("Use 'Generate Series .strm Files' for series")

            message = f"Found {active_movie} movies and {active_series} series"
            if orphan_movie or orphan_series:
                message += f" ({orphan_movie + orphan_series} orphaned — no active provider)"

            return {
                "status": "ok",
                "message": message,
                "movies": active_movie,
                "series": active_series,
                "movies_orphaned": orphan_movie,
                "series_orphaned": orphan_series,
            }
        except Exception as e:
            logger.error("Scan failed: %s", e)
            return {"status": "error", "message": f"Scan error: {e}"}
    
    def _movie_target_paths(self, movie, root_folder: str, category_name: str = "", nest: bool = False, append_tmdb_id: bool = False):
        """Compute the (folder_path, strm_filename, clean_name, year) for a movie.

        When nest=True the folder is wrapped in a category subfolder named
        by the raw M3U category (or 'Unassigned' if none).

        When append_tmdb_id=True AND the movie has a tmdb_id, the folder name
        gets a Plex/ChannelsDVR-friendly `{tmdb-NNN}` suffix for exact
        metadata matching. The strm filename inside the folder is NOT
        affected — only the folder name, since that's what scrapers read.
        """
        raw_name = movie.name or f"Unknown Movie {movie.id}"
        parsed = _naming.parse_title(raw_name, getattr(movie, "year", None))
        # The canonical folder name from the shared core — same engine, same result
        # the mount would produce for this movie. Always carries {tmdb-}/{imdb-} when
        # known (that is what makes scraper matching exact); `append_tmdb_id` is now
        # implicit in the one naming path and the parameter is retained only for
        # call-site compatibility.
        folder_name = _naming.folder_name(
            raw_name, getattr(movie, "year", None),
            getattr(movie, "tmdb_id", None), getattr(movie, "imdb_id", None))
        strm_filename = _naming.strm_filename(folder_name)
        cat_segment = _naming.category_segment(category_name, nest)
        if cat_segment:
            folder_path = os.path.join(root_folder, cat_segment, folder_name)
        else:
            folder_path = os.path.join(root_folder, folder_name)
        return folder_path, strm_filename, parsed["title"], parsed["year"]

    def _generate_movies(self, settings: Dict[str, Any], logger, refresh_urls: bool = False):
        """Generate movie .strm files according to batch size.

        Lazily walks M3UMovieRelation via iterator() so the batch limit is
        honoured even when most candidates are already-done. Stops scanning
        as soon as target_batch new files have been written.

        refresh_urls is an internal flag set by _rescan_all (and not a
        user-visible setting). When True, existing .strm files are rewritten
        with the current Dispatcharr URL; .nfo files are still preserved.
        """
        root_folder = settings.get("root_folder", "/VODS/Movies")
        dispatcharr_url = (settings.get("dispatcharr_url") or "").rstrip("/")
        batch_size = settings.get("batch_size") or "250"
        generate_nfo = settings.get("generate_nfo", True)
        refresh_existing = bool(refresh_urls)
        nest_by_cat = bool(settings.get("nest_movies_by_category", False))
        dedupe_across_cats = bool(settings.get("dedupe_movies_across_categories", False))
        append_tmdb_id = bool(settings.get("append_tmdb_id_to_folder", False))

        ok, err = self._validate_dispatcharr_url(dispatcharr_url, logger)
        if not ok:
            logger.error(err)
            return {"status": "error", "message": err}

        self._log_config(logger, {
            "Root Folder": root_folder,
            "Dispatcharr URL": self._mask_url(dispatcharr_url),
            "Batch Size": batch_size,
            "Generate NFO": "Yes" if generate_nfo else "No",
            "Refresh Existing": "Yes" if refresh_existing else "No",
            "Nest by category": "Yes" if nest_by_cat else "No",
            "Dedupe across cats": "Yes" if dedupe_across_cats else "No",
            "Append TMDB ID": "Yes" if append_tmdb_id else "No",
        })

        try:
            from apps.vod.models import M3UMovieRelation
        except ImportError as e:
            logger.error("Failed to import models: %s", e)
            return {"status": "error", "message": f"Import error: {e}"}

        try:
            # Only generate for relations on an ACTIVE M3U account — content
            # whose provider was deactivated upstream must not produce .strm
            # files (they'd point at a dead provider). Matches the scan totals.
            query = (
                M3UMovieRelation.objects
                .select_related('movie', 'm3u_account', 'category')
                .filter(m3u_account__is_active=True)
            )
            if dedupe_across_cats:
                # Deterministic "first category wins" requires a stable sort.
                # Alphabetical by category name, then relation id as a tiebreaker.
                # Only applied when the toggle is ON so we don't penalise normal
                # iteration with an unnecessary ORDER BY on the relation table.
                query = query.order_by('category__name', 'id')
            total_count = query.count()
            if total_count == 0:
                return {"status": "ok", "message": "No movies found to process", "processed": 0}
            target_batch = total_count if batch_size == "all" else int(batch_size)
            logger.info("Total relations: %d. Target batch: %s", total_count, "all" if batch_size == "all" else target_batch)
        except Exception as e:
            logger.error("Database query failed: %s", e)
            return {"status": "error", "message": f"Database error: {e}"}

        try:
            os.makedirs(root_folder, exist_ok=True)
        except OSError as e:
            return {"status": "error", "message": f"Folder creation error: {e}"}

        created_strm = 0
        refreshed_strm = 0
        created_nfo = 0
        skipped = 0
        deduped = 0
        errors = 0
        scanned = 0

        # seen-set is only used when dedupe is on; kept as None otherwise so the
        # membership check short-circuits cheaply for everyone else.
        seen_movie_uuids = set() if dedupe_across_cats else None

        logger.info("Processing movies:")
        logger.info("-" * 60)

        for relation in query.iterator():
            scanned += 1
            movie = relation.movie
            if seen_movie_uuids is not None:
                if movie.uuid in seen_movie_uuids:
                    # Same movie already written under an earlier-alphabetical
                    # category. Skip — counts under `deduped` not `skipped`.
                    deduped += 1
                    continue
                seen_movie_uuids.add(movie.uuid)
            cat_name = relation.category.name if relation.category else ""
            movie_folder, strm_filename, movie_name, year = self._movie_target_paths(
                movie, root_folder, cat_name, nest_by_cat, append_tmdb_id,
            )
            strm_path = os.path.join(movie_folder, strm_filename)
            is_existing = os.path.exists(strm_path)

            if is_existing and not refresh_existing:
                skipped += 1
                continue

            proxy_url = _proxy_url(dispatcharr_url, "movie", movie.uuid, relation.stream_id)

            written = created_strm + refreshed_strm
            log_this = (written + 1) % self.LOG_EVERY == 1 or written < self.LOG_FIRST_N
            verb = "refreshed" if is_existing else "created"
            if log_this:
                logger.info("")
                logger.info("[%d %s / %d scanned] %s (%s)", written + 1, verb, scanned, movie_name, year or "—")

            try:
                os.makedirs(movie_folder, exist_ok=True)
                with open(strm_path, 'w', encoding='utf-8') as f:
                    f.write(proxy_url)
                if is_existing:
                    refreshed_strm += 1
                else:
                    created_strm += 1

                wrote_nfo = False
                if generate_nfo:
                    nfo_filename = strm_filename.replace('.strm', '.nfo')
                    nfo_path = os.path.join(movie_folder, nfo_filename)
                    if not os.path.exists(nfo_path):
                        category_name = relation.category.name if relation.category else ""
                        with open(nfo_path, 'w', encoding='utf-8') as f:
                            f.write(self._generate_nfo(movie, category_name))
                        created_nfo += 1
                        wrote_nfo = True

                if log_this:
                    logger.info("  ✓ wrote .strm%s", " + .nfo" if wrote_nfo else "")
            except OSError as e:
                logger.error("  ✗ %s: %s", movie_name, e)
                errors += 1

            if batch_size != "all":
                limit_hit = (
                    (refreshed_strm + created_strm) >= target_batch
                    if refresh_existing
                    else created_strm >= target_batch
                )
                if limit_hit:
                    logger.info("")
                    if refresh_existing:
                        logger.info("Batch complete: %d new + %d refreshed .strm (scanned %d).", created_strm, refreshed_strm, scanned)
                    else:
                        logger.info("Batch complete: %d new .strm written (scanned %d, %d already done).", created_strm, scanned, skipped)
                    break

        logger.info("")
        logger.info("=" * 60)
        logger.info("SUMMARY:")
        logger.info("  Total relations: %d", total_count)
        logger.info("  Scanned:         %d", scanned)
        logger.info("  Already on disk: %d", skipped)
        if dedupe_across_cats:
            logger.info("  Deduped (multi-cat): %d", deduped)
        logger.info("  .strm created:   %d", created_strm)
        if refresh_existing:
            logger.info("  .strm refreshed: %d", refreshed_strm)
        if generate_nfo:
            logger.info("  .nfo created:    %d", created_nfo)
        logger.info("  Errors:          %d", errors)
        logger.info("=" * 60)

        summary_msg = f"Wrote {created_strm} new .strm files"
        if refresh_existing and refreshed_strm:
            summary_msg += f", refreshed {refreshed_strm}"
        if generate_nfo and created_nfo:
            summary_msg += f" + {created_nfo} .nfo"
        if skipped:
            summary_msg += f" ({skipped} already on disk)"
        if dedupe_across_cats and deduped:
            summary_msg += f", deduped {deduped} multi-category duplicates"

        return {
            "status": "ok",
            "message": summary_msg,
            "total_in_db": total_count,
            "scanned": scanned,
            "created_strm": created_strm,
            "refreshed_strm": refreshed_strm,
            "created_nfo": created_nfo if generate_nfo else 0,
            "skipped": skipped,
            "deduped": deduped,
            "errors": errors,
        }
    
    def _series_target_folder(self, series, series_root: str, category_name: str = "", nest: bool = False, append_tmdb_id: bool = False):
        """Compute the target folder for a series. Returns (folder_path, clean_name, year).

        The folder name comes from the single naming path (vodlib.naming) — the
        same canonical name the mount produces — always carrying {tmdb-}/{imdb-}
        when known. When nest=True the folder is wrapped in a category subfolder
        named by the raw M3U category (or 'Unassigned' if none). `append_tmdb_id`
        is retained for call-site compatibility; ids are always included now.
        """
        raw_name = series.name or f"Unknown Series {series.id}"
        parsed = _naming.parse_title(raw_name, getattr(series, "year", None))
        folder_name = _naming.folder_name(
            raw_name, getattr(series, "year", None),
            getattr(series, "tmdb_id", None), getattr(series, "imdb_id", None))
        cat_segment = _naming.category_segment(category_name, nest)
        if cat_segment:
            return os.path.join(series_root, cat_segment, folder_name), parsed["title"], parsed["year"]
        return os.path.join(series_root, folder_name), parsed["title"], parsed["year"]

    def _series_already_processed(self, series_folder: str) -> bool:
        """A series is considered processed if its folder contains any 'Season ...' subdir."""
        if not os.path.isdir(series_folder):
            return False
        try:
            return any(
                item.startswith("Season") and os.path.isdir(os.path.join(series_folder, item))
                for item in os.listdir(series_folder)
            )
        except OSError:
            return False

    def _generate_series(self, settings: Dict[str, Any], logger):
        """Generate series .strm files with episodes using parallel processing."""
        series_root = settings.get("series_root_folder", "/VODS/Series")
        dispatcharr_url = (settings.get("dispatcharr_url") or "").rstrip("/")
        batch_size = settings.get("series_batch_size") or "10"
        generate_nfo = settings.get("generate_series_nfo", True)
        refresh_existing = bool(settings.get("refresh_existing", False))
        nest_by_cat = bool(settings.get("nest_series_by_category", False))
        dedupe_across_cats = bool(settings.get("dedupe_series_across_categories", False))
        append_tmdb_id = bool(settings.get("append_tmdb_id_to_folder", False))

        ok, err = self._validate_dispatcharr_url(dispatcharr_url, logger)
        if not ok:
            logger.error(err)
            return {"status": "error", "message": err}

        self._log_config(logger, {
            "Series Root": series_root,
            "Dispatcharr URL": self._mask_url(dispatcharr_url),
            "Batch Size": batch_size,
            "Generate NFO": "Yes" if generate_nfo else "No",
            "Refresh Existing": "Yes" if refresh_existing else "No",
            "Nest by category": "Yes" if nest_by_cat else "No",
            "Dedupe across cats": "Yes" if dedupe_across_cats else "No",
            "Workers": self.MAX_WORKERS,
        })

        try:
            from apps.vod.models import M3USeriesRelation
        except ImportError as e:
            logger.error("Failed to import models: %s", e)
            return {"status": "error", "message": f"Import error: {e}"}

        try:
            # Active-account filter — see _generate_movies. Deactivated providers
            # must not produce episode .strm files.
            query = (
                M3USeriesRelation.objects
                .select_related('series', 'm3u_account', 'category')
                .filter(m3u_account__is_active=True)
            )
            if dedupe_across_cats:
                # See _generate_movies for rationale — deterministic
                # alphabetical-by-category-name ordering so "first category wins"
                # is repeatable across runs.
                query = query.order_by('category__name', 'id')
            total_count = query.count()

            if batch_size == "all":
                target_batch = total_count
                logger.info("Mode: process ALL %d series", total_count)
            else:
                target_batch = int(batch_size)
                logger.info("Target batch size: %d (of %d total)", target_batch, total_count)

            if total_count == 0:
                return {"status": "ok", "message": "No series found"}
        except Exception as e:
            logger.error("Query failed: %s", e)
            return {"status": "error", "message": f"Database error: {e}"}

        try:
            os.makedirs(series_root, exist_ok=True)
        except OSError as e:
            return {"status": "error", "message": f"Folder creation error: {e}"}

        if refresh_existing:
            logger.info("Refresh-existing mode: scanning all series for new episodes...")
        else:
            logger.info("Filtering already-processed series...")
        to_process = []
        scanned = 0
        deduped = 0
        seen_series_uuids = set() if dedupe_across_cats else None
        for series_rel in query.iterator():
            scanned += 1
            if seen_series_uuids is not None:
                if series_rel.series.uuid in seen_series_uuids:
                    # Same series tagged under multiple categories — already
                    # going to be processed under the alphabetically-first one.
                    deduped += 1
                    continue
                seen_series_uuids.add(series_rel.series.uuid)
            if not refresh_existing:
                cat_name = series_rel.category.name if series_rel.category else ""
                folder, _, _ = self._series_target_folder(
                    series_rel.series, series_root, cat_name, nest_by_cat, append_tmdb_id,
                )
                if self._series_already_processed(folder):
                    continue
            to_process.append(series_rel)
            if batch_size != "all" and len(to_process) >= target_batch:
                break

        skipped_pre = scanned - len(to_process) - deduped
        if refresh_existing:
            logger.info("Scanned %d series; %d to evaluate this run", scanned, len(to_process))
        else:
            logger.info("Scanned %d series; %d already processed (skipped); %d to process this run", scanned, skipped_pre, len(to_process))
        if dedupe_across_cats and deduped:
            logger.info("Deduped %d multi-category series (only the first-encountered category survives).", deduped)
        logger.info("")

        if not to_process:
            logger.info("Nothing to process.")
            return {
                "status": "ok",
                "message": f"Nothing to process; {skipped_pre} series already done.",
                "series_processed": 0,
                "episodes_created": 0,
                "nfo_created": 0,
                "deduped": deduped,
                "errors": 0,
            }

        created_strm = 0
        refreshed_strm = 0
        created_nfo = 0
        errors = 0
        series_created = 0
        series_uptodate = 0
        failures = []

        logger.info("Processing %d series with %d parallel workers:", len(to_process), self.MAX_WORKERS)
        logger.info("-" * 60)

        with ThreadPoolExecutor(max_workers=self.MAX_WORKERS) as executor:
            futures = {
                executor.submit(
                    self._process_single_series,
                    series_rel,
                    dispatcharr_url,
                    generate_nfo,
                    series_root,
                    logger,
                    refresh_existing,
                    nest_by_cat,
                    append_tmdb_id,
                ): series_rel
                for series_rel in to_process
            }

            for idx, future in enumerate(as_completed(futures), 1):
                series_rel = futures[future]
                try:
                    result = future.result()
                except Exception as e:
                    name = getattr(getattr(series_rel, "series", None), "name", "?")
                    logger.error("[%d/%d] Worker raised for '%s': %s", idx, len(futures), name, e)
                    errors += 1
                    failures.append(f"{name}: {e}")
                    continue

                if result.get("uptodate"):
                    series_uptodate += 1
                elif result.get("created"):
                    series_created += 1
                    created_strm += result["episodes"]
                    refreshed_strm += result.get("refreshed", 0)
                    created_nfo += result["nfo_files"]
                if "error" in result:
                    errors += 1
                    failures.append(f"{result.get('series_name', '?')}: {result['error']}")
                logger.info("[%d/%d] %s", idx, len(futures), result["message"])
        
        logger.info("")
        logger.info("=" * 60)
        logger.info("SUMMARY:")
        logger.info("  Series with new content: %d", series_created)
        logger.info("  Series up-to-date:       %d", series_uptodate)
        logger.info("  New episode .strm files: %d", created_strm)
        if refresh_existing:
            logger.info("  Refreshed episode URLs:  %d", refreshed_strm)
        if generate_nfo:
            logger.info("  New NFO files:           %d", created_nfo)
        logger.info("  Errors:                  %d", errors)
        logger.info("=" * 60)

        if series_created == 0 and series_uptodate > 0:
            summary_msg = f"All {series_uptodate} evaluated series already up-to-date — no new episodes."
        else:
            summary_msg = f"Wrote {created_strm} new episodes across {series_created} series"
            if refresh_existing and refreshed_strm:
                summary_msg += f", refreshed {refreshed_strm} episode URL{'s' if refreshed_strm != 1 else ''}"
            if series_uptodate:
                summary_msg += f" ({series_uptodate} already up-to-date)"
            if generate_nfo and created_nfo:
                summary_msg += f" + {created_nfo} NFO"

        if failures:
            logger.info("")
            logger.info("Failed series:")
            for f in failures[:20]:
                logger.info("  - %s", f)
            if len(failures) > 20:
                logger.info("  ... and %d more", len(failures) - 20)

        return {
            "status": "ok",
            "message": summary_msg,
            "series_processed": series_created,
            "series_uptodate": series_uptodate,
            "episodes_created": created_strm,
            "episodes_refreshed": refreshed_strm,
            "nfo_created": created_nfo if generate_nfo else 0,
            "deduped": deduped,
            "errors": errors,
            "failures": failures,
        }

    def _process_single_series(self, series_rel, dispatcharr_url, generate_nfo, series_root, logger, refresh_existing=False, nest_by_cat=False, append_tmdb_id=False):
        """Process a single series. Idempotent: writes only missing episode files.

        With refresh_existing=False, callers should pre-filter already-done
        series for performance. With refresh_existing=True, every series is
        re-evaluated and the M3U source is re-fetched so newly-aired episodes
        are picked up.

        When nest_by_cat=True the series folder is wrapped in a subfolder
        named by the M3U category (raw, sanitised) or 'Unassigned'.
        """
        from apps.vod.models import M3UEpisodeRelation
        from apps.vod.tasks import refresh_series_episodes

        series = series_rel.series
        cat_name = series_rel.category.name if series_rel.category else ""
        series_folder, series_name, _year = self._series_target_folder(
            series, series_root, cat_name, nest_by_cat, append_tmdb_id,
        )

        try:
            custom_props = series_rel.custom_properties or {}
            should_refetch = refresh_existing or not custom_props.get('episodes_fetched', False)
            if should_refetch:
                try:
                    refresh_series_episodes(
                        account=series_rel.m3u_account,
                        series=series_rel.series,
                        external_series_id=series_rel.external_series_id,
                    )
                except Exception as fetch_err:
                    logger.warning("refresh_series_episodes failed for %s: %s", series_name, fetch_err)

            episodes = list(
                M3UEpisodeRelation.objects.filter(
                    m3u_account=series_rel.m3u_account,
                    episode__series=series,
                )
                .select_related('episode')
                .order_by('episode__season_number', 'episode__episode_number')
            )
            episode_count = len(episodes)
            
            if episode_count == 0:
                return {
                    "created": False,
                    "uptodate": False,
                    "series_name": series_name,
                    "episodes": 0,
                    "nfo_files": 0,
                    "message": f"{series_name} - No episodes found",
                }

            os.makedirs(series_folder, exist_ok=True)

            new_episodes = 0
            refreshed_episodes = 0
            new_nfo = 0

            if generate_nfo:
                tvshow_nfo_path = os.path.join(series_folder, "tvshow.nfo")
                if not os.path.isfile(tvshow_nfo_path):
                    category_name = series_rel.category.name if series_rel.category else ""
                    tvshow_content = self._generate_tvshow_nfo(series, category_name)
                    with open(tvshow_nfo_path, 'w', encoding='utf-8') as f:
                        f.write(tvshow_content)
                    new_nfo += 1

            for episode_rel in episodes:
                episode = episode_rel.episode
                season_num = episode.season_number or 0
                episode_num = episode.episode_number or 0

                season_folder_name = _naming.season_dir_name(season_num)
                season_folder = os.path.join(series_folder, season_folder_name)

                # One naming path: same SxxEyy base the mount produces for this episode.
                filename = _naming.episode_basename(
                    series.name, getattr(series, "year", None),
                    season_num, episode_num, episode.name or "")

                strm_path = os.path.join(season_folder, f"{filename}.strm")
                is_existing = os.path.isfile(strm_path)
                if is_existing and not refresh_existing:
                    continue

                os.makedirs(season_folder, exist_ok=True)
                proxy_url = _proxy_url(dispatcharr_url, "episode", episode.uuid, episode_rel.stream_id)
                with open(strm_path, 'w', encoding='utf-8') as f:
                    f.write(proxy_url)
                if is_existing:
                    refreshed_episodes += 1
                else:
                    new_episodes += 1

                if generate_nfo:
                    nfo_path = os.path.join(season_folder, f"{filename}.nfo")
                    if not os.path.isfile(nfo_path):
                        with open(nfo_path, 'w', encoding='utf-8') as f:
                            f.write(self._generate_episode_nfo(episode))
                        new_nfo += 1

            if new_episodes == 0 and refreshed_episodes == 0:
                return {
                    "created": False,
                    "uptodate": True,
                    "series_name": series_name,
                    "episodes": 0,
                    "refreshed": 0,
                    "nfo_files": new_nfo,
                    "message": f"{series_name} - up-to-date ({episode_count} episodes on disk)",
                }

            if new_episodes > 0:
                msg = f"{series_name} - +{new_episodes} new episode{'s' if new_episodes != 1 else ''}"
                if refreshed_episodes:
                    msg += f", {refreshed_episodes} refreshed"
            else:
                msg = f"{series_name} - refreshed {refreshed_episodes} episode URL{'s' if refreshed_episodes != 1 else ''}"

            return {
                "created": True,
                "uptodate": False,
                "series_name": series_name,
                "episodes": new_episodes,
                "refreshed": refreshed_episodes,
                "nfo_files": new_nfo,
                "message": msg,
            }

        except Exception as e:
            return {
                "created": False,
                "uptodate": False,
                "series_name": series_name,
                "episodes": 0,
                "nfo_files": 0,
                "error": str(e),
                "message": f"{series_name} - ✗ Error: {e}",
            }
    
    def _delete_plugin_files_in_dir(self, dir_path: str, logger):
        """Delete only .strm and .nfo files in dir_path. Returns (strm_deleted, nfo_deleted, errors)."""
        strm = nfo = errors = 0
        try:
            entries = os.listdir(dir_path)
        except OSError as e:
            logger.error("Cannot list %s: %s", dir_path, e)
            return 0, 0, 1

        for name in entries:
            if not name.endswith(self._PLUGIN_FILE_SUFFIXES):
                continue
            path = os.path.join(dir_path, name)
            if not os.path.isfile(path):
                continue
            try:
                os.remove(path)
                if name.endswith('.strm'):
                    strm += 1
                else:
                    nfo += 1
            except OSError as e:
                logger.error("Failed to delete %s: %s", path, e)
                errors += 1
        return strm, nfo, errors

    def _try_rmdir(self, path: str) -> bool:
        """Remove path if it's an empty directory. Returns True if removed."""
        try:
            os.rmdir(path)
            return True
        except OSError:
            return False

    def _walk_and_cleanup_plugin_files(self, root: str, logger):
        """Recursively delete .strm/.nfo files under root, then bottom-up
        remove any directory that ends up empty (preserves root and any
        directory that still contains user-added files).

        Works for both flat (Movies/X/...) and nested (Movies/Cat/X/...)
        layouts because we walk the whole tree.
        """
        result = {
            "deleted_strm": 0,
            "deleted_nfo": 0,
            "removed_dirs": 0,
            "preserved_dirs": 0,
            "errors": 0,
            "scanned_dirs": 0,
        }
        if not os.path.isdir(root):
            return result

        # Pass 1: top-down — remove plugin files
        for dirpath, _, filenames in os.walk(root):
            result["scanned_dirs"] += 1
            for name in filenames:
                if not name.endswith(self._PLUGIN_FILE_SUFFIXES):
                    continue
                path = os.path.join(dirpath, name)
                try:
                    os.remove(path)
                    if name.endswith('.strm'):
                        result["deleted_strm"] += 1
                    else:
                        result["deleted_nfo"] += 1
                except OSError as e:
                    logger.error("Failed to delete %s: %s", path, e)
                    result["errors"] += 1

        # Pass 2: bottom-up — rmdir any directory that is now empty.
        # We never remove the root itself.
        root_real = os.path.realpath(root)
        for dirpath, _, _ in os.walk(root, topdown=False):
            if os.path.realpath(dirpath) == root_real:
                continue
            if self._try_rmdir(dirpath):
                result["removed_dirs"] += 1
            else:
                result["preserved_dirs"] += 1
        return result

    def _log_config(self, logger, items: Dict[str, Any]) -> None:
        """Log a 'Configuration:' block with key/value pairs."""
        logger.info("")
        logger.info("Configuration:")
        for k, v in items.items():
            logger.info("  %s: %s", k, v)
        logger.info("")

    def _validate_dispatcharr_url(self, url: str, logger):
        """Validate the configured Dispatcharr URL before writing .strm files.

        Returns (ok, error_message). On ok=True a non-fatal warning may have
        been logged for localhost-style URLs (which work in narrow setups
        but break the typical case). On ok=False the caller should abort
        the action and surface error_message to the user.
        """
        url_clean = (url or "").strip()
        # Structural validity (empty / scheme / host) comes from the one shared
        # playback validator; the .strm-specific placeholder + localhost checks
        # below are layered on top.
        structural = _validate_base_url(url_clean)
        if structural is not None:
            if not url_clean:
                return False, ("Dispatcharr URL is empty. Set it in the plugin Settings "
                               "(and click Save) before running this action.")
            return False, structural
        if url_clean == self.PLACEHOLDER_DISPATCHARR_URL:
            return False, (
                f"Dispatcharr URL is still the placeholder example "
                f"({self.PLACEHOLDER_DISPATCHARR_URL}). Update it to your "
                "actual Dispatcharr URL in Settings and click Save."
            )
        if "localhost" in url_clean.lower() or "127.0.0.1" in url_clean:
            logger.warning(
                "Dispatcharr URL contains localhost/127.0.0.1. This works "
                "only when your media server runs on the same host as "
                "Dispatcharr with shared network namespace (e.g. Docker "
                "host networking). Most setups need a routable LAN IP/"
                "hostname for the .strm files to play from another machine. "
                "Continuing anyway — verify playback after generation."
            )
        return True, None

    def _mask_url(self, url: str) -> str:
        """Mask the host portion of a URL for log output (keeps scheme + path)."""
        if not url:
            return url
        match = re.match(r'^(https?://)([^/]+)(/.*)?$', url)
        if not match:
            return url
        scheme, host, path = match.group(1), match.group(2), match.group(3) or ''
        if ':' in host:
            host_only, port = host.rsplit(':', 1)
            host_masked = '<host>' + ':' + port
        else:
            host_masked = '<host>'
        return scheme + host_masked + path

    def _cleanup_movies(self, settings: Dict[str, Any], logger):
        """Delete plugin-generated .strm and .nfo files under the movies root.

        Walks recursively so this works for both flat (Movies/X/...) and
        nested (Movies/Category/X/...) layouts. Empty folders are removed
        bottom-up; folders with user-added files are preserved.
        """
        root_folder = settings.get("root_folder", "/VODS/Movies")

        logger.info("=" * 60)
        logger.info("VOD2MLIB v%s — cleanup_movies", self.version)
        logger.info("Root: %s", root_folder)
        logger.info("=" * 60)
        logger.info("")

        if not os.path.exists(root_folder):
            logger.info("Root folder doesn't exist. Nothing to clean up.")
            return {"status": "ok", "message": "Root folder doesn't exist", "deleted_strm": 0, "deleted_nfo": 0, "removed_dirs": 0, "preserved_dirs": 0, "errors": 0}

        r = self._walk_and_cleanup_plugin_files(root_folder, logger)

        logger.info("")
        logger.info("=" * 60)
        logger.info("CLEANUP SUMMARY")
        logger.info("  Dirs scanned:    %d", r["scanned_dirs"])
        logger.info("  Dirs removed:    %d", r["removed_dirs"])
        logger.info("  Dirs preserved:  %d  (user-added files inside)", r["preserved_dirs"])
        logger.info("  .strm deleted:   %d", r["deleted_strm"])
        logger.info("  .nfo deleted:    %d", r["deleted_nfo"])
        logger.info("  Errors:          %d", r["errors"])
        logger.info("=" * 60)

        msg = f"Deleted {r['deleted_strm']} .strm + {r['deleted_nfo']} .nfo, removed {r['removed_dirs']} folders"
        if r["preserved_dirs"]:
            msg += f", preserved {r['preserved_dirs']} (user files)"
        return {"status": "ok", "message": msg, **r}

    def _cleanup_series(self, settings: Dict[str, Any], logger):
        """Delete plugin-generated .strm and .nfo files under the series root.

        Walks recursively so this works for both flat (Series/X/Season..) and
        nested (Series/Category/X/Season..) layouts. Empty folders (Season,
        series, category) are removed bottom-up. Folders with user-added
        files are preserved.
        """
        series_root = settings.get("series_root_folder", "/VODS/Series")

        logger.info("=" * 60)
        logger.info("VOD2MLIB v%s — cleanup_series", self.version)
        logger.info("Root: %s", series_root)
        logger.info("=" * 60)
        logger.info("")

        if not os.path.exists(series_root):
            logger.info("Series root doesn't exist. Nothing to clean up.")
            return {"status": "ok", "message": "Series root doesn't exist", "deleted_strm": 0, "deleted_nfo": 0, "removed_dirs": 0, "preserved_dirs": 0, "errors": 0}

        r = self._walk_and_cleanup_plugin_files(series_root, logger)

        logger.info("")
        logger.info("=" * 60)
        logger.info("CLEANUP SUMMARY")
        logger.info("  Dirs scanned:    %d", r["scanned_dirs"])
        logger.info("  Dirs removed:    %d", r["removed_dirs"])
        logger.info("  Dirs preserved:  %d  (user-added files inside)", r["preserved_dirs"])
        logger.info("  .strm deleted:   %d", r["deleted_strm"])
        logger.info("  .nfo deleted:    %d", r["deleted_nfo"])
        logger.info("  Errors:          %d", r["errors"])
        logger.info("=" * 60)

        msg = f"Deleted {r['deleted_strm']} .strm + {r['deleted_nfo']} .nfo, removed {r['removed_dirs']} folders"
        if r["preserved_dirs"]:
            msg += f", preserved {r['preserved_dirs']} (user files)"
        return {"status": "ok", "message": msg, **r}
    
    def _extract_genres(self, category_name: str) -> list:
        """Extract genre names from category name."""
        if not category_name:
            return []

        # Strip a leading language prefix ('EN - ', 'FR - ') from the category.
        # Whitespace-before-dash anchoring avoids the AC-130-becomes-130 over-strip.
        genre_text = self._LANGUAGE_PREFIX_RE.sub('', category_name)

        # Remove (movie) or (series) suffix
        genre_text = re.sub(r'\s*\((movie|series)\)\s*$', '', genre_text, flags=re.IGNORECASE)
        
        # Split on common separators
        genres = re.split(r'[/&,]', genre_text)
        
        # Clean up each genre
        cleaned_genres = []
        for genre in genres:
            genre = genre.strip()
            # Capitalize first letter of each word
            genre = ' '.join(word.capitalize() for word in genre.split())
            if genre:
                cleaned_genres.append(genre)
        
        return cleaned_genres or ["Unknown"]

    def _split_genres_clean(self, s: str) -> list:
        """Split an already-clean genre string (e.g. from Series.genre / Movie.genre)
        on /&, and trim whitespace.

        Unlike _extract_genres, this preserves case — TMDB-grade values come
        in as 'Sci-Fi & Fantasy' / 'Action & Adventure', and re-capitalising
        would produce 'Sci-fi' which is wrong.
        """
        if not s:
            return []
        out = []
        for part in re.split(r'[/&,]', s):
            part = part.strip()
            if part:
                out.append(part)
        return out

    def _is_year_bucket_genre(self, g: str) -> bool:
        """Return True if g looks like a year-bucket category name
        ('2026 Movies', '1990s Series') rather than a real genre.

        Used by _resolve_genres to suppress useless category-derived genres
        when Movie.genre / Series.genre is empty. Real categorical genres
        like 'Action', 'Drama, Crime' are unaffected.
        """
        return bool(self._YEAR_BUCKET_GENRE_RE.match((g or "").strip()))

    def _resolve_genres(self, db_genre: str, category_name: str) -> list:
        """Prefer the DB genre (TMDB-grade) when populated; fall back to the
        M3U category-derived genre, with year-bucket noise filtered out.

        If the only category-derived genre would be a year-bucket like
        '2026 Movies', return an empty list — better to emit no <genre> tag
        than a misleading one. The TMDB id in the NFO lets media servers
        fetch a real genre from TMDB themselves.
        """
        db_clean = (db_genre or "").strip()
        if db_clean:
            return self._split_genres_clean(db_clean)
        candidates = self._extract_genres(category_name)
        return [g for g in candidates if not self._is_year_bucket_genre(g)]

    def _generate_tvshow_nfo(self, series, category_name: str) -> str:
        """Generate tvshow.nfo XML content for a series."""
        raw_title = series.name or "Unknown"
        parsed = _naming.parse_title(raw_title, getattr(series, "year", None))
        title = parsed["title"] or "Unknown"
        year = series.year or parsed["year"] or ""
        plot = series.description or ""
        rating = (getattr(series, "rating", "") or "").strip()
        tmdb_id = (getattr(series, "tmdb_id", "") or "").strip()
        imdb_id = (getattr(series, "imdb_id", "") or "").strip()

        genres = self._resolve_genres(getattr(series, "genre", ""), category_name)

        xml_lines = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>']
        xml_lines.append('<tvshow>')
        xml_lines.append(f'    <title>{self._xml_escape(title)}</title>')

        if year:
            xml_lines.append(f'    <year>{year}</year>')

        for genre in genres:
            xml_lines.append(f'    <genre>{self._xml_escape(genre)}</genre>')

        if plot:
            xml_lines.append(f'    <plot>{self._xml_escape(plot)}</plot>')

        if rating:
            xml_lines.append(f'    <rating>{self._xml_escape(rating)}</rating>')

        if tmdb_id:
            xml_lines.append(f'    <tmdbid>{self._xml_escape(tmdb_id)}</tmdbid>')
            xml_lines.append(f'    <uniqueid type="tmdb" default="true">{self._xml_escape(tmdb_id)}</uniqueid>')

        if imdb_id:
            xml_lines.append(f'    <imdbid>{self._xml_escape(imdb_id)}</imdbid>')
            xml_lines.append(f'    <uniqueid type="imdb">{self._xml_escape(imdb_id)}</uniqueid>')

        # Emit poster URL when available so media servers can render artwork
        # without scraping TMDB themselves. Dispatcharr exposes the provider's
        # TMDB image via series.logo.url (typically image.tmdb.org/...).
        poster_url = self._logo_url(series)
        if poster_url:
            xml_lines.append(f'    <thumb aspect="poster">{self._xml_escape(poster_url)}</thumb>')

        xml_lines.append('</tvshow>')

        return '\n'.join(xml_lines)
    
    def _generate_episode_nfo(self, episode) -> str:
        """Generate episode.nfo XML content for an episode."""
        raw_title = episode.name or ""
        title = (_naming.episode_display_title(raw_title) or "Episode") if raw_title else "Episode"
        season_num = episode.season_number or 0
        episode_num = episode.episode_number or 0
        plot = episode.description or ""
        rating = (getattr(episode, "rating", "") or "").strip()
        tmdb_id = (getattr(episode, "tmdb_id", "") or "").strip()
        imdb_id = (getattr(episode, "imdb_id", "") or "").strip()
        air_date = getattr(episode, "air_date", None)
        duration_secs = getattr(episode, "duration_secs", 0) or 0
        runtime_min = duration_secs // 60 if duration_secs > 0 else 0

        xml_lines = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>']
        xml_lines.append('<episodedetails>')
        xml_lines.append(f'    <title>{self._xml_escape(title)}</title>')
        xml_lines.append(f'    <season>{season_num}</season>')
        xml_lines.append(f'    <episode>{episode_num}</episode>')

        if plot:
            xml_lines.append(f'    <plot>{self._xml_escape(plot)}</plot>')

        if air_date:
            xml_lines.append(f'    <aired>{air_date}</aired>')

        if runtime_min:
            xml_lines.append(f'    <runtime>{runtime_min}</runtime>')

        if rating:
            xml_lines.append(f'    <rating>{self._xml_escape(rating)}</rating>')

        if tmdb_id:
            xml_lines.append(f'    <tmdbid>{self._xml_escape(tmdb_id)}</tmdbid>')
            xml_lines.append(f'    <uniqueid type="tmdb" default="true">{self._xml_escape(tmdb_id)}</uniqueid>')

        if imdb_id:
            xml_lines.append(f'    <imdbid>{self._xml_escape(imdb_id)}</imdbid>')
            xml_lines.append(f'    <uniqueid type="imdb">{self._xml_escape(imdb_id)}</uniqueid>')

        xml_lines.append('</episodedetails>')

        return '\n'.join(xml_lines)
    
    def _generate_nfo(self, movie, category_name: str) -> str:
        """Generate NFO XML content for a movie."""
        raw_title = movie.name or "Unknown"
        parsed = _naming.parse_title(raw_title, getattr(movie, "year", None))
        title = parsed["title"] or "Unknown"
        year = movie.year or parsed["year"] or ""
        plot = movie.description or ""
        rating = (movie.rating or "").strip()
        tmdb_id = (movie.tmdb_id or "").strip()
        imdb_id = (movie.imdb_id or "").strip()

        genres = self._resolve_genres(getattr(movie, "genre", ""), category_name)
        
        # Build XML
        xml_lines = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>']
        xml_lines.append('<movie>')
        xml_lines.append(f'    <title>{self._xml_escape(title)}</title>')
        
        if year:
            xml_lines.append(f'    <year>{year}</year>')
        
        for genre in genres:
            xml_lines.append(f'    <genre>{self._xml_escape(genre)}</genre>')
        
        if plot:
            xml_lines.append(f'    <plot>{self._xml_escape(plot)}</plot>')
        
        if rating:
            xml_lines.append(f'    <rating>{self._xml_escape(rating)}</rating>')

        if tmdb_id:
            xml_lines.append(f'    <tmdbid>{self._xml_escape(tmdb_id)}</tmdbid>')
            xml_lines.append(f'    <uniqueid type="tmdb" default="true">{self._xml_escape(tmdb_id)}</uniqueid>')

        if imdb_id:
            xml_lines.append(f'    <imdbid>{self._xml_escape(imdb_id)}</imdbid>')
            xml_lines.append(f'    <uniqueid type="imdb">{self._xml_escape(imdb_id)}</uniqueid>')

        # Emit poster URL when available (Dispatcharr's movie.logo.url is
        # typically a TMDB image URL). Saves the media server from doing a
        # second roundtrip to TMDB just for artwork.
        poster_url = self._logo_url(movie)
        if poster_url:
            xml_lines.append(f'    <thumb aspect="poster">{self._xml_escape(poster_url)}</thumb>')

        xml_lines.append('</movie>')

        return '\n'.join(xml_lines)

    def _logo_url(self, obj) -> str:
        """Best-effort extraction of an artwork URL from a Dispatcharr Movie /
        Series object. Returns '' if no usable URL is present.

        Dispatcharr's VOD models expose artwork via a `logo` FK to VODLogo,
        whose `.url` is typically a TMDB image URL like
        `https://image.tmdb.org/t/p/w600_and_h900_bestv2/<hash>.jpg`. We use
        getattr defensively so the helper survives schema changes (e.g. a
        future flat `logo_url` string field) and missing relations.
        """
        try:
            logo = getattr(obj, "logo", None)
            if logo is None:
                return ""
            url = getattr(logo, "url", None) or (logo if isinstance(logo, str) else "")
            return (url or "").strip()
        except Exception:
            return ""

    def _xml_escape(self, text: str) -> str:
        """Escape special XML characters."""
        if not text:
            return ""
        text = str(text)
        text = text.replace('&', '&amp;')
        text = text.replace('<', '&lt;')
        text = text.replace('>', '&gt;')
        text = text.replace('"', '&quot;')
        text = text.replace("'", '&apos;')
        return text

    def _rescan_all(self, settings: Dict[str, Any], logger):
        """Combined scan + generate movies + generate series. Used by the cron schedule.

        Forces refresh-existing semantics ON for both movies and series so cron
        rescans (and manual Rescan All clicks) reliably pick up new content AND
        rewrite existing .strm files so URL changes propagate. Movies use an
        internal kwarg on _generate_movies; series uses the user-visible
        refresh_existing setting (which also enables new-episode discovery).
        Existing .nfo files are preserved either way.
        """
        logger.info("Combined rescan: scan + movies + series (refresh URLs forced ON)")
        logger.info("")

        scan = self._scan_all_vods(settings, logger)
        if scan.get("status") != "ok":
            return scan

        logger.info("")
        logger.info("=" * 60)
        logger.info("Rescan: movies  (refresh_urls=True)")
        logger.info("=" * 60)
        movies = self._generate_movies(settings, logger, refresh_urls=True)

        logger.info("")
        logger.info("=" * 60)
        logger.info("Rescan: series  (refresh_existing=True)")
        logger.info("=" * 60)
        series_settings = {**settings, "refresh_existing": True}
        series = self._generate_series(series_settings, logger)

        m = movies if isinstance(movies, dict) else {}
        s = series if isinstance(series, dict) else {}

        movie_strm = m.get("created_strm", 0)
        movie_refreshed = m.get("refreshed_strm", 0)
        movie_skipped = m.get("skipped", 0)
        ep_new = s.get("episodes_created", 0)
        ep_refreshed = s.get("episodes_refreshed", 0)
        sc_new = s.get("series_processed", 0)
        sc_uptodate = s.get("series_uptodate", 0)
        total_errors = m.get("errors", 0) + s.get("errors", 0)

        movie_extra = ""
        if movie_refreshed:
            movie_extra = f", {movie_refreshed} refreshed"
        elif movie_skipped:
            movie_extra = f" ({movie_skipped} on disk)"

        series_extra = ""
        if ep_refreshed:
            series_extra = f", {ep_refreshed} refreshed"
        if sc_uptodate:
            series_extra += f" ({sc_uptodate} up-to-date)"

        message = (
            f"Rescan complete. Movies: {movie_strm} new{movie_extra}. "
            f"Series: {ep_new} new episodes across {sc_new} series{series_extra}."
        )
        if total_errors:
            message += f" {total_errors} errors — see logs."

        return {
            "status": "ok",
            "message": message,
            "scan": scan,
            "movies": movies,
            "series": series,
        }

    def _validate_timezone(self, tz_str: str):
        """Validate an IANA timezone name.

        Returns (ok, error_message). Empty string is treated as 'use UTC'
        and is considered valid.
        """
        clean = (tz_str or "").strip()
        if not clean:
            return True, None
        try:
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        except ImportError:
            return True, None  # pre-3.9 Python — trust the user
        try:
            ZoneInfo(clean)
            return True, None
        except (ZoneInfoNotFoundError, ValueError):
            return False, (
                f"Invalid timezone {clean!r}. Use an IANA name like "
                "'Europe/London', 'America/New_York', or 'UTC'. "
                "See https://en.wikipedia.org/wiki/List_of_tz_database_time_zones"
            )

    def _parse_cron(self, cron_expr: str):
        """Validate and split a 5-field cron expression. Returns tuple or raises ValueError."""
        if not cron_expr:
            raise ValueError("Cron expression is empty")
        parts = cron_expr.strip().split()
        if len(parts) != 5:
            raise ValueError(
                f"Cron expression must have 5 fields (minute hour dom month dow), got {len(parts)}: {cron_expr!r}"
            )
        return tuple(parts)

    def _valid_schedule_targets(self) -> set:
        """The action ids that are valid as scheduled targets.

        Derived from the schedule_target field's options so the source of
        truth is the manifest, not a hardcoded set.
        """
        for f in self.fields:
            if f.get("id") == "schedule_target":
                return {opt["value"] for opt in f.get("options", []) if opt.get("value")}
        return set()

    def _apply_schedule(self, settings: Dict[str, Any], logger):
        """Register or update a periodic auto-rescan task via django-celery-beat."""
        cron_expr = settings.get("schedule_cron") or "0 3 * * *"
        target = settings.get("schedule_target") or "rescan_all"
        tz_str = (settings.get("schedule_timezone") or "").strip() or "UTC"

        valid_targets = self._valid_schedule_targets()
        if target not in valid_targets:
            return {"status": "error", "message": f"Invalid schedule_target: {target}"}

        try:
            minute, hour, dom, month, dow = self._parse_cron(cron_expr)
        except ValueError as e:
            logger.error("Invalid cron expression: %s", e)
            return {"status": "error", "message": str(e)}

        ok_tz, tz_err = self._validate_timezone(tz_str)
        if not ok_tz:
            logger.error(tz_err)
            return {"status": "error", "message": tz_err}

        try:
            from django_celery_beat.models import PeriodicTask, CrontabSchedule
        except ImportError as e:
            logger.error("django-celery-beat is not installed: %s", e)
            logger.error("")
            logger.error("Fallback: add a host-side cron entry that POSTs to Dispatcharr's plugin")
            logger.error("action endpoint to trigger '%s' on plugin '%s'.", target, self.name)
            return {
                "status": "error",
                "message": "django-celery-beat not available. Use host cron to call the plugin action instead.",
            }

        import json
        schedule, _ = CrontabSchedule.objects.get_or_create(
            minute=minute,
            hour=hour,
            day_of_month=dom,
            month_of_year=month,
            day_of_week=dow,
            timezone=tz_str,
        )

        snapshot = {k: v for k, v in (settings or {}).items() if not k.startswith("schedule_")}

        task, created = PeriodicTask.objects.update_or_create(
            name=self.SCHEDULE_TASK_NAME,
            defaults={
                "crontab": schedule,
                "task": self.SCHEDULED_TASK_CELERY_NAME,
                "queue": "dvr",
                "kwargs": json.dumps({"action": target, "settings": snapshot}),
                "enabled": True,
                "description": f"Auto-rescan for {self.name} v{self.version}",
            },
        )

        verb = "Created" if created else "Updated"
        logger.info("%s schedule: %s @ '%s' (%s) → action '%s'", verb, self.SCHEDULE_TASK_NAME, cron_expr, tz_str, target)
        logger.info("Settings snapshot keys: %s", sorted(snapshot.keys()))
        logger.info("")
        logger.info("Note: re-run 'Apply Schedule' after changing settings to refresh the snapshot.")

        warning = ""
        refresh_on = bool(snapshot.get("refresh_existing", False))
        if target == "generate_series" and not refresh_on:
            warning = (
                " ⚠️ 'Refresh Existing Series' is OFF — cron will only ADD new series, "
                "not pick up new episodes for already-processed series. "
                "Turn it ON and re-Apply for true auto-rescans, or use target 'rescan_all' which forces it ON."
            )
            logger.warning(warning.strip())

        return {
            "status": "ok",
            "message": f"{verb} periodic task for cron '{cron_expr}' ({tz_str}) → {target}.{warning}",
            "created": created,
            "cron": cron_expr,
            "timezone": tz_str,
            "target": target,
            "refresh_existing_in_snapshot": refresh_on,
        }

    def _remove_schedule(self, settings: Dict[str, Any], logger):
        """Unregister the periodic auto-rescan task."""
        try:
            from django_celery_beat.models import PeriodicTask
        except ImportError:
            return {"status": "ok", "message": "django-celery-beat not installed; nothing to remove."}

        deleted, _ = PeriodicTask.objects.filter(name=self.SCHEDULE_TASK_NAME).delete()
        if deleted:
            logger.info("Removed periodic task '%s'", self.SCHEDULE_TASK_NAME)
        else:
            logger.info("No periodic task named '%s' was registered.", self.SCHEDULE_TASK_NAME)
        return {"status": "ok", "message": f"Removed {deleted} scheduled task(s).", "deleted": deleted}

    def _schedule_status(self, settings: Dict[str, Any], logger):
        """Show current schedule registration."""
        try:
            from django_celery_beat.models import PeriodicTask
        except ImportError:
            msg = "django-celery-beat is not installed — scheduling disabled."
            logger.info(msg)
            return {"status": "ok", "message": msg, "scheduled": False, "reason": "django-celery-beat not installed"}

        task = PeriodicTask.objects.filter(name=self.SCHEDULE_TASK_NAME).first()
        if not task:
            msg = "No schedule registered. Click 'Apply Schedule' to enable auto-rescan."
            logger.info(msg)
            return {"status": "ok", "message": msg, "scheduled": False}

        cron = task.crontab
        if cron:
            cron_str = f"{cron.minute} {cron.hour} {cron.day_of_month} {cron.month_of_year} {cron.day_of_week}"
            tz_str = str(cron.timezone) if cron.timezone else "UTC"
        else:
            cron_str = "<none>"
            tz_str = "<none>"
        last_run = str(task.last_run_at) if task.last_run_at else "never"
        state = "enabled" if task.enabled else "disabled"

        drifted = self._settings_drift_keys(task, settings)

        logger.info("Schedule: %s", task.name)
        logger.info("  Enabled:    %s", task.enabled)
        logger.info("  Cron:       %s", cron_str)
        logger.info("  Timezone:   %s", tz_str)
        logger.info("  Task:       %s", task.task)
        logger.info("  Kwargs:     %s", task.kwargs)
        logger.info("  Last run:   %s", last_run)
        logger.info("  Total runs: %s", task.total_run_count)
        if drifted:
            logger.warning(
                "  ⚠ Settings changed since last Apply Schedule: %s", ", ".join(drifted)
            )
            logger.warning(
                "    Cron is still running the OLD snapshot — click "
                "'[SCHEDULE] Apply / Update' to refresh it."
            )

        message = (
            f"Schedule {state} — cron '{cron_str}' ({tz_str}), "
            f"last run {last_run}, total runs {task.total_run_count}"
        )
        if drifted:
            message = (
                f"⚠ Settings changed since last Apply ({', '.join(drifted)}) — "
                f"re-click Apply Schedule to refresh the cron snapshot. " + message
            )
        return {
            "status": "ok",
            "message": message,
            "scheduled": True,
            "enabled": task.enabled,
            "cron": cron_str,
            "timezone": tz_str,
            "task": task.task,
            "last_run_at": str(task.last_run_at) if task.last_run_at else None,
            "total_run_count": task.total_run_count,
            "settings_drifted": drifted,
        }

    def _settings_drift_keys(self, task, current_settings):
        """Return the list of setting keys whose live value differs from the
        snapshot stored in the PeriodicTask at the last Apply Schedule.

        Compared over the SNAPSHOT's keys only (intersection), so newly-added
        settings introduced by a plugin upgrade don't raise a false "changed"
        flag for users who never touched them. `schedule_`-prefixed keys are
        excluded — they're cron config, not part of the rescan snapshot.
        """
        import json
        try:
            stored = (json.loads(task.kwargs or "{}") or {}).get("settings") or {}
        except (ValueError, TypeError):
            return []
        current = {
            k: v for k, v in (current_settings or {}).items()
            if not k.startswith("schedule_")
        }
        return sorted(k for k in stored if stored.get(k) != current.get(k))

    def _schedule_test_fire(self, settings: Dict[str, Any], logger):
        """Enqueue the registered schedule's task on Celery, returning immediately.

        Mirrors what django-celery-beat does on a cron tick: send the task to
        the worker pool and let it run there. The HTTP request returns at once
        so nginx doesn't time out for long rescans. Verify completion via
        [SCHEDULE] Show status (last_run_at updates when the worker finishes).
        """
        try:
            from django_celery_beat.models import PeriodicTask
        except ImportError:
            return {"status": "error", "message": "django-celery-beat not installed."}

        task = PeriodicTask.objects.filter(name=self.SCHEDULE_TASK_NAME).first()
        if not task:
            return {"status": "error", "message": "No schedule registered. Click Apply first."}

        import json
        try:
            kwargs = json.loads(task.kwargs or "{}")
        except json.JSONDecodeError as e:
            return {"status": "error", "message": f"Stored task kwargs invalid JSON: {e}"}

        action = kwargs.get("action") or "rescan_all"
        snapshot_settings = kwargs.get("settings") or {}

        if action not in self._valid_schedule_targets():
            return {"status": "error", "message": f"Stored action '{action}' is not a valid target."}

        try:
            from celery import current_app
            async_result = current_app.send_task(
                self.SCHEDULED_TASK_CELERY_NAME,
                kwargs={"action": action, "settings": snapshot_settings},
                queue="dvr",
            )
        except Exception as e:
            logger.error("Failed to enqueue test fire: %s", e)
            return {"status": "error", "message": f"Failed to enqueue task on Celery: {e}"}

        logger.info("Test fire enqueued: action=%s task_id=%s", action, async_result.id)
        return {
            "status": "ok",
            "message": f"Test fire enqueued ({action}); task id {async_result.id}. [SCHEDULE] Show status updates when the worker finishes (timestamp = completion time).",
            "fired_action": action,
            "task_id": async_result.id,
        }


try:
    from celery import shared_task as _vod2mlib_shared_task

    @_vod2mlib_shared_task(name=Plugin.SCHEDULED_TASK_CELERY_NAME)
    def _vod2mlib_scheduled_rescan(action="rescan_all", settings=None):
        """Celery entry point invoked by the periodic task registered via _apply_schedule.

        On completion, bumps PeriodicTask.last_run_at so Show Status reflects
        manual Test fire runs (which bypass beat) and updates beat-dispatched
        runs at *completion* time rather than dispatch-start. Without this the
        UI would show stale timestamps for Test fire clicks and silently mask
        ticks that beat dispatched but the worker rejected/failed.
        """
        import logging
        logger = logging.getLogger("vod2mlib.schedule")
        result = Plugin().run(action, {}, {"logger": logger, "settings": settings or {}})
        try:
            from django.utils import timezone
            from django_celery_beat.models import PeriodicTask
            PeriodicTask.objects.filter(name=Plugin.SCHEDULE_TASK_NAME).update(
                last_run_at=timezone.now(),
            )
        except Exception as e:
            logger.warning("Failed to bump PeriodicTask.last_run_at: %s", e)
        return result
except Exception as _celery_register_err:
    # Celery may not be importable in some environments. Log to stderr so the
    # cause is visible if the user wonders why scheduled rescans never run.
    import sys as _sys
    print(f"[vod2mlib] Celery task registration failed: {_celery_register_err}", file=_sys.stderr)
