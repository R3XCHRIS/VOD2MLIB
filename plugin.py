"""
VOD to Media Library — Dispatcharr VOD .strm Generator Plugin
(slug: vod2mlib)
v1.18.0 — cleaner NFO titles (provider tags/quality tokens stripped)
          plus an option to omit <title> entirely so Jellyfin uses TMDB;
          collapse duplicate episode relations to one .strm; bigger
          series batch sizes; warn when a TMDB ID is missing.

MIT License
Copyright (c) 2025-2026 shedunraid (original author)
Copyright (c) 2026 R3XCHRIS (downstream maintainer, fork)
Upstream:   https://github.com/shedunraid/VOD2MLIB
This fork:  https://github.com/R3XCHRIS/VOD2MLIB
"""
import os
import re
import json
from typing import Dict, Any
from concurrent.futures import ThreadPoolExecutor, as_completed


class Plugin:
    """Generate .strm files for VOD movies from Dispatcharr."""
    
    name = "VOD to Media Library"
    version = "1.18.0"
    help_url = "https://github.com/R3XCHRIS/VOD2MLIB#readme"
    description = (
        "Convert Dispatcharr VODs into media-server-friendly .strm files, with "
        "optional NFO metadata, batch processing, and a cron-driven auto-rescan."
    )

    # Tunables
    MAX_WORKERS = 3
    LOG_EVERY = 50
    LOG_FIRST_N = 10
    MAX_FILENAME_LEN = 200
    # Cap the category list printed by Scan — catalogues can have hundreds.
    SCAN_CATEGORY_LIMIT = 40

    # Schedule task identity (django-celery-beat row name + Celery task name)
    SCHEDULE_TASK_NAME = "vod2mlib.auto_rescan"
    SCHEDULED_TASK_CELERY_NAME = "vod2mlib.scheduled_rescan"

    # The legacy default Dispatcharr URL — a placeholder that must NOT be
    # shipped into .strm files. We reject it explicitly to catch users who
    # forgot to click Save after editing the URL field.
    PLACEHOLDER_DISPATCHARR_URL = "http://192.168.99.11:9191"

    # File suffixes the plugin writes (used by cleanup and skip logic)
    _PLUGIN_FILE_SUFFIXES = ('.strm', '.nfo')

    # Language / provider tag prefixes stripped from titles and category names.
    # Handles the formats providers actually use, each guarded against eating
    # real titles (see issue #3):
    #   * "EN - Title"  — dash, any 2-3 letter code. Requires whitespace BEFORE
    #     the dash so "AC-130" / "MI-5" are preserved.
    #   * "EN| Title"   — pipe, any 2-3 letter code (a leading pipe is never a
    #     real title, so any code is safe here).
    #   * "EN Title"    — bare space, restricted to "EN" ONLY so real titles
    #     like "IT Chapter Two", "UP (2009)", "ED TV" survive.
    #   * "▪NL▪ Title"  — bullet-wrapped code, e.g. ▪NL▪ / ▪MULTIG▪ (any 2-8
    #     letters between marker symbols).
    _BULLET_CHARS = r'▪▫■□●○•·◦‣⁃︎️'
    # Provider quality/edition tags that lead a title, e.g. '4K-A+ Title',
    # 'A+ Title'. Only tokens containing '+' are stripped here: a leading '+'
    # token is never part of a real title, whereas letter-hyphen-letter forms
    # very much are ('X-Men', 'AC-130', 'MI-5'), so those are left alone.
    # Reported by @matrix26 (provider tags like 4K-A+, EN-TOP, AMZ).
    # Provider plus-tags: "A+ ", "4K-A+ ", "VIP+ ". Uppercase-only and a
    # MANDATORY trailing space, so real titles that merely contain a "+"
    # survive — "Love+War" and "Genera+ion" are both real films.
    _PROVIDER_PLUS_TAG_RE = re.compile(r'^[A-Z0-9]{1,6}(?:[-_][A-Z0-9]{1,6})*\+\s+')
    _LEADING_DELIM_TAG_RE = re.compile(r'^(?:[\[\(\|]\s*[A-Za-z0-9][A-Za-z0-9 +\-]{0,9}\s*[\]\)\|]\s*)+')
    _EMPTY_DELIM_RE = re.compile(r'[\[\(]\s*[\]\)]')
    _LANGUAGE_PREFIX_RE = re.compile(
        r'^(?:'
        r'[A-Z]{2,3}\s+-\s*'                              # EN - Title
        r'|[A-Z]{2,3}\s*\|\s*'                            # EN| Title
        r'|EN\s+'                                         # EN Title (EN only)
        r'|[' + _BULLET_CHARS + r']+\s*[A-Za-z]{2,8}\s*[' + _BULLET_CHARS + r']+\s*'  # ▪NL▪ Title
        r')'
    )
    _TRAILING_YEAR_RE = re.compile(r'\s*\((\d{4})\)\s*$')

    # First-(YYYY) detector for v1.15.0+ folder-name cleanup. Some providers ship
    # titles like "Cool Hand Luke 4K (1967) PAUL NEWMAN (1967)" — ChannelsDVR
    # scrapes off the folder name and fails to match those because of the
    # trailing junk. Truncating at the first (YYYY) yields "Cool Hand Luke 4K"
    # which then has quality tokens stripped to give "Cool Hand Luke".
    _FIRST_YEAR_RE = re.compile(r'\((\d{4})\)')

    # Bare trailing year (no parens) at the very end of a title, e.g.
    # "Wicked: For Good - 2025". The negative lookbehind stops it matching the
    # tail of a longer digit run ("12345"). Used by
    # _strip_redundant_trailing_year to de-duplicate the year a provider stuffs
    # into the title against the (YYYY) suffix the plugin adds.
    _BARE_TRAILING_YEAR_RE = re.compile(r'(?<!\d)(\d{4})\s*$')

    # Quality / encoding tokens commonly stuffed into provider VOD titles.
    # Stripped from folder names so media-server scrapers see a clean title.
    # Word-boundary anchored so legitimate substrings ("Whiplash" etc.) survive.
    _QUALITY_TOKEN_ALT = (
        r'4K|UHD|FHD|HD|SD|HDR(?:10\+?)?|HEVC|H\.?26[45]|x26[45]|'
        r'1080p|720p|2160p|480p|BluRay|BDRip|DVDRip|WEB-?DL|HDTV|REMUX'
    )
    _QUALITY_TOKEN_RE = re.compile(r'\b(' + _QUALITY_TOKEN_ALT + r')\b', re.IGNORECASE)

    # Edge-anchored variants for NFO titles. Folder names can afford to strip
    # these anywhere; a <title> cannot — "NTSF:SD:SUV::" is a real show, and
    # removing its interior "SD" corrupts the name.
    _LEADING_QUALITY_RE = re.compile(
        r'^(?:(?:' + _QUALITY_TOKEN_ALT + r')\b[\s\-_:|]*)+', re.IGNORECASE)
    _TRAILING_QUALITY_RE = re.compile(
        r'(?:[\s\-_:|]*\b(?:' + _QUALITY_TOKEN_ALT + r'))+\s*$', re.IGNORECASE)
    # If removing a trailing quality token leaves the title dangling on a
    # connector, the token was part of the name: "WWII in HD" is a real
    # series, and "WWII in" is not a title anyone meant to write.
    _DANGLING_TAIL_RE = re.compile(
        r'\b(?:a|an|the|in|on|at|of|to|for|and|or|with|from|is|my)$', re.IGNORECASE)

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
            "description": "Workflow:\n  1. Configure paths below.\n  2. Actions → Scan → see catalogue totals.\n  3. Actions → Generate Movies / Generate Series (start with Batch Size 10).\n  4. (Optional) Turn ON Refresh Existing Series, set cron, click Apply Schedule for nightly auto-rescan.\n\nDocs: https://github.com/R3XCHRIS/VOD2MLIB",
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
            "id": "nfo_omit_title",
            "label": "Omit <title> from NFO files",
            "type": "boolean",
            "default": False,
            "help_text": "Leave the `<title>` element OUT of generated movie and tvshow NFO files. Jellyfin (and Emby) treat a `<title>` in the NFO as authoritative and will NOT override it from TMDB — so if your provider prefixes titles with tags like `4K-A+`, `EN-TOP` or `AMZ`, that junk becomes the displayed name. With this ON the plugin still writes the NFO (IDs, plot, genres, rating, poster) but omits the title, letting your media server take the clean title from TMDB via the `<tmdbid>` we already emit. OFF by default (unchanged behaviour). Note v1.18.0 also cleans provider junk out of the title, so try that first — this is the belt-and-braces option. Episode NFOs always keep their title (media servers match episodes by season/episode number)."
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
            "help_text": "Append a TMDB id tag to every Movies and Series folder name when a TMDB ID is known — e.g. `Cool Hand Luke (1967) {tmdb-378}/`. Media servers honour this as a forced exact match, which is the safest defence against name collisions and bad metadata scrapes. Pick the convention your server expects with `TMDB Folder Tag Format` below. ⚠ MIGRATION: the plugin does NOT rename existing folders in place — turning this on (or off) for an already-generated library writes the new folder names ALONGSIDE the old ones, creating duplicates. To switch cleanly, run `[⚠ DANGER] Clean up Movies` / `Series` first, then re-generate; or accept the duplicates until the old folders age out."
        },
        {
            "id": "tmdb_tag_format",
            "label": "TMDB Folder Tag Format",
            "type": "select",
            "default": "plex",
            "options": [
                {"value": "plex", "label": "Plex / ChannelsDVR — {tmdb-123}"},
                {"value": "jellyfin", "label": "Jellyfin / Emby — [tmdbid-123]"}
            ],
            "help_text": "Which convention to use for the TMDB folder tag — media servers disagree, and each ignores the other's format. `Plex / ChannelsDVR` writes `Cool Hand Luke (1967) {tmdb-378}`; `Jellyfin / Emby` writes `Cool Hand Luke (1967) [tmdbid-378]`. Only has an effect when `Append TMDB ID to folder names` is ON. Defaults to Plex for backwards compatibility with libraries generated before v1.16.1 — if you use Jellyfin or Emby, switch this to `jellyfin` or the tag is silently ignored by your server. ⚠ MIGRATION: changing the format renames every folder, and the plugin does NOT rename in place — the new names are written ALONGSIDE the old ones. Run `[⚠ DANGER] Clean up Movies` / `Series` first, then re-generate."
        },
        {
            "id": "omit_stream_id",
            "label": "Don't pin .strm files to a specific provider stream",
            "type": "boolean",
            "default": False,
            "help_text": "When ON, .strm URLs omit ?stream_id=, so Dispatcharr's VOD proxy resolves and fails over across every account carrying the title instead of being locked to the one relation this plugin happened to pick. Requires a patched Dispatcharr with VOD failover support (PR #1398). When OFF (default), the .strm is pinned to this plugin's selected relation, matching original behavior."
        },
        {
            "id": "category_filter",
            "label": "Category Filter (include only)",
            "type": "string",
            "default": "",
            "help_text": "Only generate content whose M3U CATEGORY name STARTS WITH one of these comma-separated prefixes — e.g. `[EN],[FR]` or `EN`. Case-insensitive. Leave empty to generate all (active) content. Ideal for large multi-language catalogues where you only want one or two languages: it filters at the database-query level, so unwanted folders are never created (no generate-then-clean-up waste). Applies to BOTH Movies and Series. When a filter is set, content with no category — or a category that doesn't match — is skipped. ⚠ This matches the CATEGORY name, NOT the movie/series title — many providers put a language tag in the title (`|EN| The Matrix`) while the category is something else entirely (`FOR ADULTS`). Run `[LIBRARY] Catalogue snapshot` to print your real category names and see exactly which ones your filter matches."
        },
        {
            "id": "category_exclude",
            "label": "Category Exclude (block list)",
            "type": "string",
            "default": "",
            "help_text": "Skip content whose M3U CATEGORY name starts with any of these comma-separated prefixes — e.g. `FOR ADULTS,XXX`. Case-insensitive. Applied AFTER the include filter, so you can use both together. This is usually what you want for 'everything EXCEPT adult content': leave `Category Filter` empty and put the unwanted categories here, rather than trying to list every category you do want. Content with no category is never excluded. Run `[LIBRARY] Catalogue snapshot` to see your exact category names. ⚠ Already-generated folders are not removed when you add an exclude — run `[⚠ DANGER] Clean up` once, then re-generate."
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
                {"value": "50", "label": "50 series"},
                {"value": "100", "label": "100 series"},
                {"value": "250", "label": "250 series"},
                {"value": "all", "label": "All series (may time out — use the schedule)"}
            ],
            "help_text": "Series to process per click (episodes are auto-fetched for each, so series are much slower than movies). ⚠ This button runs synchronously and your reverse proxy will usually cut it off after ~60s with a 504 — the run keeps going server-side, but you lose the result. For a big catalogue don't use 'All' here: set the cron to 'Full rescan' and click [SCHEDULE] Apply / Update. Scheduled runs execute on the Celery worker with no HTTP timeout."
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

        return {"status": "error", "message": f"Unknown action: {action}"}
    
    def _scan_all_vods(self, settings: Dict[str, Any], logger):
        """Scan and show total movies and series available."""
        logger.info("Scanning VODs in Dispatcharr...")
        logger.info("")
        
        try:
            from apps.vod.models import Movie, Series, M3UMovieRelation, M3USeriesRelation
            from django.db.models import Count
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

            # Category breakdown — the exact names to type into Category Filter
            # / Category Exclude. People guess at names their provider shows in
            # titles rather than the category the DB actually stores (#8), so
            # print them, and mark which ones the current filter matches.
            cat_filter_prefixes = self._parse_category_filter(settings.get("category_filter"))
            cat_exclude_prefixes = self._parse_category_filter(settings.get("category_exclude"))
            counts = {}
            for model, idx in ((M3UMovieRelation, 0), (M3USeriesRelation, 1)):
                for row in (model.objects
                            .filter(m3u_account__is_active=True)
                            .values("category__name")
                            .annotate(n=Count("id"))):
                    entry = counts.setdefault(row["category__name"], [0, 0])
                    entry[idx] += row["n"]

            if counts:
                logger.info("")
                logger.info("CATEGORIES (%d) — use these names in Category Filter / Exclude:", len(counts))
                if cat_filter_prefixes or cat_exclude_prefixes:
                    logger.info("  ✓ = included by your filter   ✗ = dropped by your exclude")
                ordered = sorted(counts.items(), key=lambda kv: -(kv[1][0] + kv[1][1]))
                shown = 0
                matched = 0
                for name, (mv, sr) in ordered:
                    included = (
                        self._matches_category_prefixes(name, cat_filter_prefixes)
                        if cat_filter_prefixes else True
                    )
                    if included and self._matches_category_prefixes(name, cat_exclude_prefixes):
                        included = False
                    if included:
                        matched += 1
                    if shown < self.SCAN_CATEGORY_LIMIT:
                        mark = " " if not (cat_filter_prefixes or cat_exclude_prefixes) else ("✓" if included else "✗")
                        logger.info("  %s %6d  %r", mark, mv + sr, name if name else "(no category)")
                        shown += 1
                if len(ordered) > shown:
                    logger.info("     ... and %d more", len(ordered) - shown)
                if cat_filter_prefixes or cat_exclude_prefixes:
                    logger.info("")
                    logger.info("  → %d of %d categories will generate.", matched, len(ordered))
                    if matched == 0:
                        logger.warning("  ⚠ NOTHING matches — Generate will produce no files.")
                        logger.warning("    Your filter %s doesn't match any category name above.", cat_filter_prefixes)
                        logger.warning("    Note the filter matches the CATEGORY name, not the movie title.")
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
    
    def _category_subfolder(self, category_name: str, nest: bool) -> str:
        """Return the category subfolder segment to insert into a path.

        Returns "" when nest is False (caller should not insert a layer).
        Returns the sanitised raw category name when nest is True and a
        category is provided. Returns "Unassigned" when nest is True but
        no category is available.
        """
        if not nest:
            return ""
        cat = (category_name or "").strip()
        if not cat:
            return "Unassigned"
        return self._sanitize_filename(cat)

    def _movie_target_paths(self, movie, root_folder: str, category_name: str = "", nest: bool = False, append_tmdb_id: bool = False, tmdb_tag_format: str = "plex"):
        """Compute the (folder_path, strm_filename, clean_name, year) for a movie.

        When nest=True the folder is wrapped in a category subfolder named
        by the raw M3U category (or 'Unassigned' if none).

        When append_tmdb_id=True AND the movie has a tmdb_id, the folder name
        gets a Plex/ChannelsDVR-friendly `{tmdb-NNN}` suffix for exact
        metadata matching. The strm filename inside the folder is NOT
        affected — only the folder name, since that's what scrapers read.
        """
        raw_name = movie.name or f"Unknown Movie {movie.id}"
        clean_name, title_year = self._extract_clean_name_and_year(raw_name)
        year = movie.year or title_year
        clean_name, year = self._strip_redundant_trailing_year(clean_name, year)
        safe = self._sanitize_filename(clean_name)
        if year:
            base_name = f"{safe} ({year})"
            strm_filename = f"{safe} ({year}).strm"
        else:
            base_name = safe
            strm_filename = f"{safe}.strm"
        folder_name = self._apply_tmdb_suffix(base_name, movie, append_tmdb_id, tmdb_tag_format)
        cat_segment = self._category_subfolder(category_name, nest)
        if cat_segment:
            folder_path = os.path.join(root_folder, cat_segment, folder_name)
        else:
            folder_path = os.path.join(root_folder, folder_name)
        return folder_path, strm_filename, clean_name, year

    def _strip_redundant_trailing_year(self, name, year):
        """Remove a bare trailing year a provider stuffed into the title, so it
        doesn't get doubled against the `(YYYY)` suffix the plugin adds.

        Two modes:
          * If `year` is known and the title ends with that exact year
            (optionally after a separator), strip it — `Wicked: For Good - 2025`
            + year 2025 -> `Wicked: For Good`.
          * If `year` is None and the title ends with a plausible bare year
            (1900–2100), ADOPT it as the year and strip it — so
            `Wicked: For Good - 2025` with no DB year still yields a clean
            `Wicked: For Good (2025)/` folder.

        Guards:
          * `Blade Runner 2049` (DB year 2017) — trailing 2049 ≠ 2017, kept.
          * `Room 1408` (DB year 2007) — 1408 ≠ 2007 and < 1900, kept.
          * `1984` / `2012` where the year IS the whole title — never stripped
            to empty.

        Returns `(name, year)` — `year` may be newly adopted in mode two.
        """
        if not name:
            return name, year
        m = self._BARE_TRAILING_YEAR_RE.search(name)
        if not m:
            return name, year
        trailing = int(m.group(1))
        if year is None:
            if not (1900 <= trailing <= 2100):
                return name, year
            adopted = trailing
        elif trailing == year:
            adopted = year
        else:
            return name, year
        stripped = name[: m.start()].rstrip(" -–—_:.,").strip()
        if not stripped:
            # The year is the entire title (e.g. "1984", "2012") — keep it.
            return name, year
        return stripped, adopted

    def _parse_category_filter(self, category_filter):
        """Split the comma-separated category-filter setting into a clean list
        of non-empty prefixes. Pure/testable."""
        return [pfx.strip() for pfx in (category_filter or "").split(",") if pfx.strip()]

    def _matches_category_prefixes(self, category_name, prefixes) -> bool:
        """True when `category_name` starts with any of `prefixes`
        (case-insensitive). Mirrors the DB-side `category__name__istartswith`
        so the Scan diagnostic and the actual query agree.

        Content with no category never matches — which is why an include-only
        filter also drops uncategorised content.
        """
        if not prefixes:
            return False
        name = (category_name or "").strip().lower()
        if not name:
            return False
        return any(name.startswith(p.strip().lower()) for p in prefixes if p.strip())

    def _apply_category_exclude(self, query, category_exclude):
        """Drop relations whose category name starts with any of the
        comma-separated prefixes (case-insensitive).

        Applied *after* the include filter. Content with no category is NOT
        excluded — only categories that explicitly match are dropped — so
        `category_exclude` is a safe way to say "everything except X" without
        silently losing uncategorised titles (issue #8).
        """
        prefixes = self._parse_category_filter(category_exclude)
        if not prefixes:
            return query
        from django.db.models import Q
        q = Q()
        for pfx in prefixes:
            q |= Q(category__name__istartswith=pfx)
        return query.exclude(q)

    def _apply_category_filter(self, query, category_filter):
        """Restrict an M3U relation queryset to categories whose name starts
        with any of the comma-separated prefixes (case-insensitive).

        Include-only: when a filter is set, relations with no category — or a
        category that doesn't match any prefix — are excluded. Empty filter is
        a no-op (generate everything). Composes with the active-account filter
        and the dedup ordering.
        """
        prefixes = self._parse_category_filter(category_filter)
        if not prefixes:
            return query
        from django.db.models import Q
        q = Q()
        for pfx in prefixes:
            q |= Q(category__name__istartswith=pfx)
        return query.filter(q)

    def _build_proxy_url(self, dispatcharr_url, content_type, uuid, stream_id, omit_stream_id=False):
        """Build a Dispatcharr VOD proxy URL for a .strm file.

        Omits the `?stream_id=` query parameter when `omit_stream_id` is set
        (or when no stream_id is available), letting Dispatcharr's VOD proxy
        pick / fail over across accounts by priority instead of being pinned
        to one relation — see #5 and Dispatcharr#1398. Included by default so
        existing behaviour is unchanged.
        """
        base = f"{dispatcharr_url}/proxy/vod/{content_type}/{uuid}"
        if omit_stream_id or not stream_id:
            return base
        return f"{base}?stream_id={stream_id}"

    def _apply_tmdb_suffix(self, base_name: str, obj, append_tmdb_id: bool, tag_format: str = "plex") -> str:
        """Append a TMDB id tag to a folder base name when the toggle is on and
        the object exposes a tmdb_id. Returns unchanged otherwise.

        The two media-server ecosystems use different conventions, and each
        ignores the other's (issue #9):

          * `plex`     -> `Title (Year) {tmdb-123}`   — Plex's Personal Media
            agent and ChannelsDVR's local-media scraper.
          * `jellyfin` -> `Title (Year) [tmdbid-123]` — Jellyfin and Emby.

        Defaults to `plex` because that's what pre-v1.16.1 releases emitted;
        changing an existing library's tag format renames every folder, so the
        switch has to be opt-in (see the setting's migration note).
        """
        if not append_tmdb_id:
            return base_name
        tmdb_id = (getattr(obj, "tmdb_id", "") or "").strip()
        if not tmdb_id:
            return base_name
        if (tag_format or "plex").strip().lower() == "jellyfin":
            return f"{base_name} [tmdbid-{tmdb_id}]"
        return f"{base_name} {{tmdb-{tmdb_id}}}"

    # ------------------------------------------------------------------
    # Version grouping (DUB/LEG + resolution -> Jellyfin/Emby "Versions")
    # ------------------------------------------------------------------
    # Title normalization for version grouping — reuses the norm2 rule VALIDATED
    # 28/08 against the live 83k-movie catalogue: 15.7% dedupe, ZERO false-merge.
    # norm2 strips year (anywhere), [TAG]/(x) brackets, and EVERY non-alphanumeric
    # char, so format variants ("15h17 - Trem Para Paris 4K" vs "15h17 Trem para
    # Paris") and audio tags ([DUB]/[LEG]/dublado/legendado) collapse to one key.
    # Only formatting differs between merged titles — never different words.
    #
    # For the plugin we ALSO strip quality/edition tokens (4K/1080p/HDR/...) from
    # the grouping key, because IPTV providers stuff resolution into the title
    # (e.g. "#SalveRosa 4K [HDR]") and those must collapse into the same title.
    _NORM2_YEAR_RE = re.compile(r'\([0-9]{4}\)')
    _NORM2_BRACKET_RE = re.compile(r'\[[A-Za-z0-9]+\]')
    _NORM2_PAREN_RE = re.compile(r'\([a-z]\)')
    _NORM2_DUBLEG_RE = re.compile(
        r'-\s*(dub|leg|dual).*$|'          # "- dub ..." / "- leg ..." (hifen)
        r'\s+(dub|leg|dual)\s*$|'          # " Title LEG" / " Title DUB" (tag solta no fim)
        r'\([0-9]{4}\)\s*(dub|leg|dual)\s*$',  # " Title (2018) LEG" (após ano)
        re.IGNORECASE)
    _QUALITY_TOKEN_RE = re.compile(
        r'#|\b(4K|2160P|1080P|720P|480P|FHD|UHD|HD|HDR10\+|HDR|DV|DOLBY|REMUX|BLURAY|BDRIP|WEBDL|WEB-DL|HEVC|H265|X264|X265)\b',
        re.IGNORECASE)
    _AUDIO_TAG_RE = re.compile(r'\[(?:DUB|LEG|[LD])\]|\((?:DUB|LEG|[LD])\)|\b(dublado|dub|legendado|leg)\b', re.IGNORECASE)
    _BULLET_TAG_RE = re.compile(r'▪[A-Za-z]{1,8}▪', re.IGNORECASE)

    # --- Detection cache ---------------------------------------------------
    # ffprobe is the slow part (3-15s per relation). We cache each relation's
    # detected (variant, res) keyed by (movie_uuid, stream_id) so a re-run (or a
    # partial run that was stopped mid-way) reuses prior probes instead of
    # re-probing 570k relations. Cache lives on disk next to the plugin and is
    # written atomically.
    _DETECT_CACHE_FILE = "/data/plugins/vod2mlib/.detect_cache.json"

    def _load_detect_cache(self):
        try:
            with open(self._DETECT_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, ValueError):
            return {}

    def _save_detect_cache(self, cache):
        tmp = self._DETECT_CACHE_FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cache, f)
            os.replace(tmp, self._DETECT_CACHE_FILE)
        except OSError as e:
            # Non-fatal: a missed cache write just means a future re-probe.
            pass

    def _detect_variant_cached(self, cache, rel_id, name, proxy_url, logger, ffprobe_path):
        key = str(rel_id)
        if key in cache and "variant" in cache[key]:
            return cache[key]["variant"]
        v = self._detect_variant(name, proxy_url, logger, ffprobe_path)
        cache.setdefault(key, {})["variant"] = v
        return v

    def _detect_resolution_cached(self, cache, rel_id, proxy_url, logger, ffprobe_path):
        key = str(rel_id)
        if key in cache and "res" in cache[key]:
            return cache[key]["res"]
        r = self._detect_resolution(proxy_url, logger, ffprobe_path)
        cache.setdefault(key, {})["res"] = r
        return r

    def _clean_variant_name(self, name):
        """Grouping KEY for version grouping (norm2 + quality tokens stripped).

        Collapses every formatting/resolution/audio variant of a title to one
        alnum string so a DUB 1080p movie and its LEG 4K twin (separate
        Dispatcharr Movies with differently-formatted names) land in ONE folder.
        Used ONLY as the group key — NOT as the on-disk folder name (see
        _display_title for that).
        """
        if not name:
            return name
        n = name.lower()
        n = self._NORM2_YEAR_RE.sub("", n)
        n = self._NORM2_BRACKET_RE.sub("", n)
        n = self._NORM2_PAREN_RE.sub("", n)
        n = self._NORM2_DUBLEG_RE.sub("", n)
        n = self._QUALITY_TOKEN_RE.sub("", n)
        return re.sub(r'[^a-z0-9]', '', n)

    def _display_title(self, name):
        """Human-readable folder/title name: tags + quality tokens stripped but
        spacing and (YYYY) kept, so the Jellyfin library shows a clean title like
        'SalveRosa (2025)' / '15h17 Trem Para Paris (2018)' instead of the norm2
        collage 'salverosa' / '15h17trempaparis'.
        """
        if not name:
            return name
        cleaned = self._AUDIO_TAG_RE.sub("", name)
        cleaned = self._BULLET_TAG_RE.sub("", cleaned)
        cleaned = self._QUALITY_TOKEN_RE.sub("", cleaned)
        # Drop brackets/parens left empty after tag removal ("SalveRosa [] (2025)"
        # -> "SalveRosa (2025)"), so the on-disk folder keeps a clean title.
        cleaned = re.sub(r'\[\s*\]', '', cleaned)
        cleaned = re.sub(r'\(\s*\)', '', cleaned)
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()
        cleaned = cleaned.rstrip(' -').strip()
        return cleaned

    _AUDIO_TAG_RE = re.compile(r'\[(?:d|l)\]|\((?:d|l)\)|\b(dublado|dub|legendado|leg)\b', re.IGNORECASE)
    _BULLET_VARIANT_RE = re.compile(r'▪[A-Za-z]{1,8}▪', re.IGNORECASE)

    def _detect_variant(self, name, proxy_url, logger, ffprobe_path="ffprobe"):
        """DUB vs LEG for a movie relation.

        Priority: explicit title tag ([DUB]/[LEG]/dublado/legendado/▪XX▪) wins.
        Else ffprobe the proxy and read the audio stream language
        (por -> DUB; eng/und/jpn/spa -> LEG). Fallback: und -> DUB.
        """
        low = (name or "").lower()
        tag = self._AUDIO_TAG_RE.search(low)
        if tag:
            tok = tag.group(0).lower()
            if 'leg' in tok or tok in ('[l]', '(l)', 'l'):
                return "LEG"
            if 'dub' in tok or tok in ('[d]', '(d)', 'd'):
                return "DUB"
        bul = self._BULLET_VARIANT_RE.search(name or "")
        if bul:
            b = bul.group(0).lower().strip('▪').strip()
            if 'leg' in b:
                return "LEG"
            if 'dub' in b:
                return "DUB"
        try:
            import subprocess, json as _json
            r = subprocess.run(
                [ffprobe_path, "-v", "error", "-user_agent", "Mozilla/5.0",
                 "-analyzeduration", "4000000", "-probesize", "2000000", "-timeout", "15000000",
                 "-i", proxy_url,
                 "-show_entries", "stream=codec_type,language:stream_tags=language", "-of", "json"],
                capture_output=True, text=True, timeout=40)
            j = _json.loads(r.stdout)
            langs = [(s.get('tags', {}).get('language') or s.get('language') or 'und')
                     for s in j.get('streams', []) if s.get('codec_type') == 'audio']
            if any(l == 'por' for l in langs):
                return "DUB"
            if langs and all(l in ('eng', 'und', 'jpn', 'spa') for l in langs):
                return "LEG"
        except Exception as e:
            logger.debug("ffprobe variant detect failed for %s: %s", name, e)
        return "DUB"

    def _detect_resolution(self, proxy_url, logger, ffprobe_path="ffprobe"):
        """Resolution label from ffprobe width: 4K / 1080p / 720p / <w>p."""
        try:
            import subprocess, json as _json
            r = subprocess.run(
                [ffprobe_path, "-v", "error", "-user_agent", "Mozilla/5.0",
                 "-analyzeduration", "4000000", "-probesize", "2000000", "-timeout", "15000000",
                 "-i", proxy_url, "-show_entries", "stream=width,codec_type", "-of", "json"],
                capture_output=True, text=True, timeout=40)
            j = _json.loads(r.stdout)
            v = [s for s in j.get('streams', []) if s.get('codec_type') == 'video']
            if not v:
                return "SD"
            w = v[0].get('width') or 0
            if w >= 3840:
                return "4K"
            if w >= 1920:
                return "1080p"
            if w >= 1280:
                return "720p"
            return f"{w}p" if w else "SD"
        except Exception as e:
            logger.debug("ffprobe resolution detect failed: %s", e)
            return "SD"

    def _apply_version_format(self, strm_filename, version_format, variant, res):
        """Append the version suffix template to a .strm filename.

        version_format may use {variant} and {res}. Empty => unchanged.
        """
        if not version_format:
            return strm_filename
        suffix = version_format.replace("{variant}", variant).replace("{res}", res)
        if ".strm" in strm_filename:
            base, _ = strm_filename.rsplit(".strm", 1)
            return f"{base}{suffix}.strm"
        return f"{strm_filename}{suffix}"

    def _accum_movie_counts(self, lc, created, refreshed, unchanged, skipped, errors, missing_tmdb):
        lc["created"] += created
        lc["refreshed"] += refreshed
        lc["unchanged"] += unchanged
        lc["skipped"] += skipped
        lc["errors"] += errors
        lc["missing_tmdb"] += missing_tmdb

    def _flush_pending_movie(self, pend, root_folder, nest_by_cat, append_tmdb_id,
                             tmdb_tag_format, generate_nfo, nfo_omit_title,
                             version_format, omit_stream_id, refresh_existing, logger):
        """Write .strm (and .nfo) for one grouped movie.

        pend = {"movie":, "cat_name":, "clean_base":, "groups": {(variant,res): (relation, proxy_url)}}
        Returns tuple of counters (created, refreshed, unchanged, skipped, errors, missing_tmdb).

        The folder is named from `clean_base` (variant tags stripped) so a DUB
        movie and its LEG twin land in the SAME folder. The version suffix is
        applied ONLY when the group has >= 2 variants (decision A): a single-
        variant title keeps its clean .strm name, so the rest of the library is
        unaffected. When version_format is empty the original behaviour is kept.
        """
        movie = pend["movie"]
        cat_name = pend["cat_name"]
        clean_base = pend.get("clean_base") or (movie.name or "")
        groups = pend["groups"]
        created = refreshed = unchanged = skipped = errors = missing_tmdb = 0

        if append_tmdb_id and not (getattr(movie, "tmdb_id", "") or "").strip():
            missing_tmdb += 1

        # Build a lightweight stand-in so _movie_target_paths names the folder
        # from clean_base (tags stripped) while still carrying tmdb_id/year.
        class _Name:
            def __init__(self, name, tmdb_id, year):
                self.name = name
                self.tmdb_id = tmdb_id
                self.year = year
        # Folder name uses the human-readable display title (tags/quality stripped
        # but spacing + (YYYY) kept), NOT the norm2 collage used as the group key.
        display_name = self._display_title(movie.name or clean_base)
        stub = _Name(display_name, getattr(movie, "tmdb_id", ""), getattr(movie, "year", ""))

        movie_folder, strm_filename, movie_name, year = self._movie_target_paths(
            stub, root_folder, cat_name, nest_by_cat, append_tmdb_id, tmdb_tag_format,
        )
        try:
            os.makedirs(movie_folder, exist_ok=True)
        except OSError as e:
            logger.error("  \u2717 %s: %s", movie_name, e)
            return 0, 0, 0, 0, 1, missing_tmdb

        # Only stamp the version suffix when there is more than one variant in
        # this group (so single-variant titles stay clean). Empty template =>
        # no suffix at all (backwards compatible).
        effective_fmt = version_format if (version_format and len(groups) >= 2) else ""

        for (variant, res), (relation, proxy_url) in groups.items():
            fname = self._apply_version_format(strm_filename, effective_fmt, variant or "", res or "")
            strm_path = os.path.join(movie_folder, fname)
            is_existing = os.path.exists(strm_path)
            if is_existing and not refresh_existing:
                skipped += 1
                continue
            try:
                changed = self._write_if_different_preserve_times(strm_path, proxy_url)
                if not changed:
                    unchanged += 1
                elif is_existing:
                    refreshed += 1
                else:
                    created += 1
                if generate_nfo:
                    nfo_filename = fname.replace('.strm', '.nfo')
                    nfo_path = os.path.join(movie_folder, nfo_filename)
                    if not os.path.exists(nfo_path):
                        category_name = relation.category.name if relation.category else ""
                        with open(nfo_path, 'w', encoding='utf-8') as f:
                            f.write(self._generate_nfo(movie, category_name, nfo_omit_title))
            except OSError as e:
                logger.error("  \u2717 %s: %s", movie_name, e)
                errors += 1
        return created, refreshed, unchanged, skipped, errors, missing_tmdb

    def _generate_movies_from_relations(self, relations_iter, settings, logger,
                                       dispatcharr_url, root_folder, batch_size,
                                       target_batch, version_format, ffprobe_path,
                                       nest_by_cat, append_tmdb_id, tmdb_tag_format,
                                       generate_nfo, nfo_omit_title, omit_stream_id,
                                       refresh_existing, dedupe_across_cats,
                                       detect_cache):
        """Django-free core of the movie generation loop.

        Walks `relations_iter` (an iterator of M3UMovieRelation-like objects),
        groups relations by clean title + (variant, res), and flushes one or
        more .strm files per group. Returns a dict with the merged counters and
        the (possibly updated) detection cache.

        Kept free of any Django import so it can be unit-tested without a live
        Dispatcharr database. All DB/wiring (query construction, active-account
        filtering, category filter, the `apps.vod.models` import) stays in
        `_generate_movies`, which calls this with `query.iterator()`.

        The grouping is lazy: pending groups accumulate in `pending_movies`
        until either (a) the batch_size limit is reached (then ALL pending
        groups are flushed before breaking) or (b) the iterator is exhausted
        (then ALL pending groups are flushed). The exhaustion branch MUST flush
        every group — a previous bug only flushed the last one, silently
        dropping every other movie on a full run.
        """
        local_counts = {"created": 0, "refreshed": 0, "unchanged": 0,
                        "skipped": 0, "errors": 0, "missing_tmdb": 0}
        pending_movies = {}
        scanned = 0
        deduped = 0

        # seen-set is only used when dedupe is on; kept as None otherwise so the
        # membership check short-circuits cheaply for everyone else.
        seen_movie_uuids = set() if dedupe_across_cats else None

        def _flush_all():
            flushed = 0
            for _cb in list(pending_movies.keys()):
                c, r, u, s, e, mt = self._flush_pending_movie(
                    pending_movies.pop(_cb), root_folder, nest_by_cat, append_tmdb_id,
                    tmdb_tag_format, generate_nfo, nfo_omit_title, version_format,
                    omit_stream_id, refresh_existing, logger)
                self._accum_movie_counts(local_counts, c, r, u, s, e, mt)
                flushed += 1
            return flushed

        logger.info("Processing movies:")
        logger.info("-" * 60)

        for relation in relations_iter:
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
            proxy_url = self._build_proxy_url(
                dispatcharr_url, "movie", movie.uuid, relation.stream_id, omit_stream_id,
            )
            # version grouping: detect variant/res per relation.
            # Group KEY is the clean base name (tag [DUB]/[LEG]/dublado/legendado
            # stripped) so a DUB movie and its LEG twin (separate Dispatcharr Movies)
            # land in the SAME folder and collapse into Jellyfin/Emby Versions.
            clean_base = self._clean_variant_name(movie.name or "")
            # Detect ONLY what the template asks for (conditional ffprobe):
            # {variant} => audio DUB/LEG; {res} => resolution. A user who only
            # wants resolution grouping (e.g. non-BR, no DUB/LEG) pays no audio
            # probe; a BR user who wants DUB/LEG pays both. Keeps the two
            # features independent while sharing one template.
            if version_format:
                variant = self._detect_variant_cached(detect_cache, relation.id, movie.name or "", proxy_url, logger, ffprobe_path) if "{variant}" in version_format else None
                res = self._detect_resolution_cached(detect_cache, relation.id, proxy_url, logger, ffprobe_path) if "{res}" in version_format else None
            else:
                variant, res = None, None

            # group by clean base name, then (variant,res)
            grp = pending_movies.setdefault(clean_base, {
                "movie": movie, "cat_name": cat_name, "clean_base": clean_base, "groups": {}})
            grp["cat_name"] = cat_name
            grp["clean_base"] = clean_base
            key = (variant, res)
            # keep first relation per (variant,res) — provider priority could be added later
            if key not in grp["groups"]:
                grp["groups"][key] = (relation, proxy_url)

            # Stop after target_batch distinct (grouped) movies have been collected,
            # so a small batch_size yields a small, fast test run instead of scanning
            # hundreds of relations. Flush EVERY pending group before breaking.
            if batch_size != "all" and len(pending_movies) >= target_batch:
                logger.info("Batch limit reached (%d movies); flushing %d pending.", target_batch, len(pending_movies))
                _flush_all()
                break
        else:
            # Iterator exhausted: flush EVERY pending group (the bug was flushing
            # only the last one here, dropping all but one movie on a full run).
            if pending_movies:
                logger.info("Flushing %d remaining pending movie group(s).", len(pending_movies))
                _flush_all()

        return {
            "counts": local_counts,
            "detect_cache": detect_cache,
            "scanned": scanned,
            "deduped": deduped,
        }

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
        tmdb_tag_format = (settings.get("tmdb_tag_format") or "plex").strip().lower()
        omit_stream_id = bool(settings.get("omit_stream_id", False))
        category_filter = (settings.get("category_filter") or "").strip()
        category_exclude = (settings.get("category_exclude") or "").strip()
        nfo_omit_title = bool(settings.get("nfo_omit_title", False))

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
            "Append TMDB ID": ("Yes (%s)" % tmdb_tag_format) if append_tmdb_id else "No",
            "Category filter": category_filter or "(all)",
            "Category exclude": category_exclude or "(none)",
        })

        version_format = (settings.get("version_format") or "").strip()
        ffprobe_path = (settings.get("ffprobe_path") or "ffprobe").strip() or "ffprobe"

        # Load the detection cache so re-runs / interrupted runs reuse prior
        # ffprobe results instead of re-probing every relation.
        detect_cache = self._load_detect_cache()

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
            query = self._apply_category_filter(query, category_filter)
            query = self._apply_category_exclude(query, category_exclude)
            if dedupe_across_cats:
                # Deterministic "first category wins" requires a stable sort.
                # Alphabetical by category name, then relation id as a tiebreaker.
                # Only applied when the toggle is ON so we don't penalise normal
                # iteration with an unnecessary ORDER BY on the relation table.
                query = query.order_by('category__name', 'id')
            total_count = query.count()
            if total_count == 0:
                # Don't just say "nothing found" — if a filter is set it's
                # almost certainly the cause, and users mistake the category
                # name for the title prefix their provider shows (#8).
                if category_filter or category_exclude:
                    msg = (
                        "No movies matched your Category Filter/Exclude — nothing generated. "
                        "Run '[LIBRARY] Catalogue snapshot' to see the real category names "
                        "(the filter matches the CATEGORY name, not the movie title)."
                    )
                    logger.warning(msg)
                    return {"status": "ok", "message": msg, "processed": 0, "filtered_out": True}
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
        unchanged_strm = 0
        missing_tmdb_id = 0
        created_nfo = 0
        skipped = 0
        deduped = 0
        errors = 0
        scanned = 0

        # Delegate the grouping + flush loop to the Django-free core so it can
        # be unit-tested without a live Dispatcharr DB. It returns the merged
        # counters and (as a side effect) saves the detection cache.
        loop_result = self._generate_movies_from_relations(
            query.iterator(), settings, logger,
            dispatcharr_url=dispatcharr_url,
            root_folder=root_folder,
            batch_size=batch_size,
            target_batch=target_batch,
            version_format=version_format,
            ffprobe_path=ffprobe_path,
            nest_by_cat=nest_by_cat,
            append_tmdb_id=append_tmdb_id,
            tmdb_tag_format=tmdb_tag_format,
            generate_nfo=generate_nfo,
            nfo_omit_title=nfo_omit_title,
            omit_stream_id=omit_stream_id,
            refresh_existing=refresh_existing,
            dedupe_across_cats=dedupe_across_cats,
            detect_cache=detect_cache,
        )
        local_counts = loop_result["counts"]
        saved_cache = loop_result.get("detect_cache", detect_cache)
        scanned = loop_result.get("scanned", 0)

        # Persist the detection cache so the next run (or a resumed partial run)
        # reuses these ffprobe results instead of re-probing every relation.
        try:
            self._save_detect_cache(saved_cache)
            logger.info("Detection cache: %d entries saved.", len(saved_cache))
        except Exception as e:
            logger.warning("Failed to save detection cache: %s", e)

        # publish grouped counters into the SUMMARY variables
        created_strm = local_counts["created"]
        refreshed_strm = local_counts["refreshed"]
        unchanged_strm = local_counts["unchanged"]
        skipped = local_counts["skipped"]
        errors = local_counts["errors"]
        missing_tmdb_id = local_counts["missing_tmdb"]
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
            logger.info("  .strm refreshed: %d  (URL changed)", refreshed_strm)
            logger.info("  .strm unchanged: %d  (skipped, mtime preserved)", unchanged_strm)
        if generate_nfo:
            logger.info("  .nfo created:    %d", created_nfo)
        logger.info("  Errors:          %d", errors)
        if append_tmdb_id and missing_tmdb_id:
            logger.warning(
                "  ⚠ %d title(s) had no TMDB ID, so no {tmdb-…} tag was added to those "
                "folders — your provider didn't supply one. This is not a plugin error.",
                missing_tmdb_id,
            )
        logger.info("=" * 60)

        summary_msg = f"Wrote {created_strm} new .strm files"
        if refresh_existing and refreshed_strm:
            summary_msg += f", refreshed {refreshed_strm}"
        if refresh_existing and unchanged_strm:
            summary_msg += f", {unchanged_strm} already current"
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
            "unchanged_strm": unchanged_strm,
            "missing_tmdb_id": missing_tmdb_id,
            "created_nfo": created_nfo if generate_nfo else 0,
            "skipped": skipped,
            "deduped": deduped,
            "errors": errors,
        }
    
    def _series_target_folder(self, series, series_root: str, category_name: str = "", nest: bool = False, append_tmdb_id: bool = False, tmdb_tag_format: str = "plex"):
        """Compute the target folder for a series. Returns (folder_path, clean_name, year).

        When nest=True the folder is wrapped in a category subfolder named
        by the raw M3U category (or 'Unassigned' if none).

        When append_tmdb_id=True AND the series has a tmdb_id, the folder name
        gets a `{tmdb-NNN}` suffix for Plex/ChannelsDVR exact matching. See
        `_apply_tmdb_suffix` for caveats around flipping the toggle on an
        existing library.
        """
        raw_name = series.name or f"Unknown Series {series.id}"
        clean_name, title_year = self._extract_clean_name_and_year(raw_name)
        year = series.year or title_year
        clean_name, year = self._strip_redundant_trailing_year(clean_name, year)
        safe = self._sanitize_filename(clean_name)
        base_name = f"{safe} ({year})" if year else safe
        folder_name = self._apply_tmdb_suffix(base_name, series, append_tmdb_id, tmdb_tag_format)
        cat_segment = self._category_subfolder(category_name, nest)
        if cat_segment:
            return os.path.join(series_root, cat_segment, folder_name), clean_name, year
        return os.path.join(series_root, folder_name), clean_name, year

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
        tmdb_tag_format = (settings.get("tmdb_tag_format") or "plex").strip().lower()
        omit_stream_id = bool(settings.get("omit_stream_id", False))
        category_filter = (settings.get("category_filter") or "").strip()
        category_exclude = (settings.get("category_exclude") or "").strip()
        nfo_omit_title = bool(settings.get("nfo_omit_title", False))

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
            "Category filter": category_filter or "(all)",
            "Category exclude": category_exclude or "(none)",
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
            query = self._apply_category_filter(query, category_filter)
            query = self._apply_category_exclude(query, category_exclude)
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
                if category_filter or category_exclude:
                    msg = (
                        "No series matched your Category Filter/Exclude — nothing generated. "
                        "Run '[LIBRARY] Catalogue snapshot' to see the real category names "
                        "(the filter matches the CATEGORY name, not the series title)."
                    )
                    logger.warning(msg)
                    return {"status": "ok", "message": msg, "filtered_out": True}
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
                    series_rel.series, series_root, cat_name, nest_by_cat, append_tmdb_id, tmdb_tag_format,
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
                    omit_stream_id,
                    tmdb_tag_format,
                    nfo_omit_title,
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

    def _process_single_series(self, series_rel, dispatcharr_url, generate_nfo, series_root, logger, refresh_existing=False, nest_by_cat=False, append_tmdb_id=False, omit_stream_id=False, tmdb_tag_format="plex", nfo_omit_title=False):
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
            series, series_root, cat_name, nest_by_cat, append_tmdb_id, tmdb_tag_format,
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

            # `id` is a deterministic tiebreaker: without it the winner among
            # duplicate relations for one episode varies run to run, so the
            # same .strm would flip between provider URLs on every rescan.
            episode_rels = list(
                M3UEpisodeRelation.objects.filter(
                    m3u_account=series_rel.m3u_account,
                    episode__series=series,
                )
                .select_related('episode')
                .order_by('episode__season_number', 'episode__episode_number', 'id')
            )

            # One Episode can be reached by several relations. Every relation
            # resolves to the same filename (the name comes from the Episode),
            # so writing each one just rewrites the same path — and when
            # `omit_stream_id` is on the URLs are byte-identical too, since the
            # URL then depends only on the episode UUID. Keep the first
            # relation per episode. Reported by @rammboslice.
            episodes = []
            seen_episode_uuids = set()
            duplicate_rels = 0
            for rel in episode_rels:
                uuid = getattr(rel.episode, "uuid", None)
                if uuid is not None:
                    if uuid in seen_episode_uuids:
                        duplicate_rels += 1
                        continue
                    seen_episode_uuids.add(uuid)
                episodes.append(rel)
            if duplicate_rels:
                logger.info(
                    "%s - collapsed %d duplicate episode relation(s) to one file each",
                    series_name, duplicate_rels,
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
            unchanged_episodes = 0
            new_nfo = 0

            if generate_nfo:
                tvshow_nfo_path = os.path.join(series_folder, "tvshow.nfo")
                if not os.path.isfile(tvshow_nfo_path):
                    category_name = series_rel.category.name if series_rel.category else ""
                    tvshow_content = self._generate_tvshow_nfo(series, category_name, nfo_omit_title)
                    with open(tvshow_nfo_path, 'w', encoding='utf-8') as f:
                        f.write(tvshow_content)
                    new_nfo += 1

            for episode_rel in episodes:
                episode = episode_rel.episode
                season_num = episode.season_number or 0
                episode_num = episode.episode_number or 0

                season_folder_name = f"Season {season_num:02d}"
                season_folder = os.path.join(series_folder, season_folder_name)

                episode_title = episode.name or ""
                if episode_title:
                    clean_title = self._clean_title(episode_title)
                    filename = f"{series_name} - S{season_num:02d}E{episode_num:02d} - {clean_title}"
                else:
                    filename = f"{series_name} - S{season_num:02d}E{episode_num:02d}"
                filename = self._sanitize_filename(filename)

                strm_path = os.path.join(season_folder, f"{filename}.strm")
                is_existing = os.path.isfile(strm_path)
                if is_existing and not refresh_existing:
                    continue

                os.makedirs(season_folder, exist_ok=True)
                proxy_url = self._build_proxy_url(
                    dispatcharr_url, "episode", episode.uuid, episode_rel.stream_id, omit_stream_id,
                )
                # Only write when the URL actually changed, preserving mtime so
                # media servers don't re-index the whole library (#11).
                changed = self._write_if_different_preserve_times(strm_path, proxy_url)
                if not changed:
                    unchanged_episodes += 1
                elif is_existing:
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
                    "unchanged": unchanged_episodes,
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
                "unchanged": unchanged_episodes,
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
        if not url_clean:
            return False, (
                "Dispatcharr URL is empty. Set it in the plugin Settings "
                "(and click Save) before running this action."
            )
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
    
    def _clean_title(self, title: str) -> str:
        """Remove language prefixes like 'EN - ', 'FR - ' from titles.

        Requires whitespace before the dash so real titles like 'AC-130' or
        'MI-5' are not stripped.
        """
        if not title:
            return title
        return self._LANGUAGE_PREFIX_RE.sub('', title).strip()

    def _strip_trailing_year(self, title: str):
        """Strip a trailing ' (YYYY)' from a title.

        Returns (cleaned_title, year) where year is an int if found, else None.
        Used to avoid double-year folder names when the source title already
        contains the year.
        """
        if not title:
            return title or "", None
        match = self._TRAILING_YEAR_RE.search(title)
        if not match:
            return title, None
        return self._TRAILING_YEAR_RE.sub('', title).rstrip(), int(match.group(1))

    def _extract_clean_name_and_year(self, raw_name: str):
        """Aggressive folder-name cleanup for movies & series.

        Strips language prefix, truncates at the FIRST (YYYY) so trailing
        provider junk (cast names, duplicate years, etc.) is discarded, then
        strips quality / encoding tokens from the surviving prefix. Returns
        (clean_name, year) where year is an int if a (YYYY) was found.

        Examples:
            "Cool Hand Luke 4K (1967) PAUL NEWMAN (1967)" → ("Cool Hand Luke", 1967)
            "EN - The Matrix (1999)"                     → ("The Matrix", 1999)
            "Whiplash 1080p HEVC (2014)"                 → ("Whiplash", 2014)
            "Avatar"                                     → ("Avatar", None)

        Used by `_movie_target_paths` and `_series_target_folder`. The simpler
        `_clean_title` / `_strip_trailing_year` helpers stay as-is for NFO
        generation, which wants gentler handling.
        """
        if not raw_name:
            # Preserve the falsy type contract used by _clean_title:
            # "" stays "", None stays None. Callers always pre-coalesce
            # the upstream name field so None never reaches us in practice.
            return raw_name, None
        # Language prefix first (same regex as _clean_title).
        title = self._LANGUAGE_PREFIX_RE.sub('', raw_name).strip()
        # Truncate at the first (YYYY) — everything after is provider noise.
        match = self._FIRST_YEAR_RE.search(title)
        year = None
        if match:
            year = int(match.group(1))
            title = title[:match.start()]
        # Strip quality tokens and collapse repeated whitespace.
        title = self._QUALITY_TOKEN_RE.sub('', title)
        title = re.sub(r'\s+', ' ', title).strip()
        # Trim trailing punctuation left behind by token removal (e.g. "Title -").
        title = title.rstrip(' -_.,;:').strip()
        return title, year
    
    def _extract_genres(self, category_name: str) -> list:
        """Extract genre names from category name."""
        if not category_name:
            return []

        # Strip language prefix using the same regex as _clean_title to avoid
        # the AC-130-becomes-130 over-strip bug.
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

    def _clean_nfo_title(self, raw_title: str, year=None) -> str:
        """Title for an NFO `<title>` element.

        NFO titles used to get only the language-prefix strip, so provider
        junk that folder names had cleaned away ("4K-A+ ...", quality tokens,
        a trailing bare year) survived into the element. Jellyfin treats a
        `<title>` in the NFO as authoritative and will not override it from
        TMDB, so that junk became the displayed title (reported by @matrix26).

        Deliberately gentler than `_extract_clean_name_and_year`, which is
        tuned for folder names: every step here is anchored to the start or
        end of the string. Interior surgery corrupts real names — checked
        against a live 3.4k-title catalogue, where the aggressive pass ate
        the "SD" out of "NTSF:SD:SUV::" and the trailing dot off "Chicago
        P.D." and "Marvel's Agents of S.H.I.E.L.D.".
        """
        if not raw_title:
            return raw_title
        title = self._LANGUAGE_PREFIX_RE.sub("", raw_title).strip()
        title = self._PROVIDER_PLUS_TAG_RE.sub("", title).strip()
        # Leading delimiter-wrapped tags: "|EN| ", "[4K] ", "(MULTI) ".
        # Guarded so titles that ARE a bracketed tag survive — "[REC] 2"
        # would otherwise be reduced to "2".
        without_tags = self._LEADING_DELIM_TAG_RE.sub("", title).strip()
        if any(ch.isalpha() for ch in without_tags):
            title = without_tags
        title = self._LEADING_QUALITY_RE.sub("", title)
        detailed = self._TRAILING_QUALITY_RE.sub("", title).strip()
        if detailed and not self._DANGLING_TAIL_RE.search(detailed):
            title = detailed
        # Trailing "(YYYY)", possibly repeated: "A Costa Rican Wedding (2025) (2025)".
        prev = None
        while prev != title:
            prev = title
            title, _ = self._strip_trailing_year(title)
            title = title.strip()
        # A bare trailing year is only stripped when it matches the known
        # year — otherwise "Blade Runner 2049" and "Bali 2002" lose theirs.
        if year:
            title, _ = self._strip_redundant_trailing_year(title, year)
        # A stripped token can leave "[] Title" behind.
        title = self._EMPTY_DELIM_RE.sub(" ", title)
        title = re.sub(r"\s{2,}", " ", title).strip(" -_")
        return title or self._clean_title(raw_title)

    def _generate_tvshow_nfo(self, series, category_name: str, omit_title: bool = False) -> str:
        """Generate tvshow.nfo XML content for a series."""
        raw_title = series.name or "Unknown"
        title = self._clean_nfo_title(raw_title, getattr(series, "year", None))
        title, title_year = self._strip_trailing_year(title)
        year = series.year or title_year or ""
        plot = series.description or ""
        rating = (getattr(series, "rating", "") or "").strip()
        tmdb_id = (getattr(series, "tmdb_id", "") or "").strip()
        imdb_id = (getattr(series, "imdb_id", "") or "").strip()

        genres = self._resolve_genres(getattr(series, "genre", ""), category_name)

        xml_lines = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>']
        xml_lines.append('<tvshow>')
        if not omit_title:
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
        title = self._clean_nfo_title(raw_title) if raw_title else "Episode"
        title, _ = self._strip_trailing_year(title)
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
    
    def _generate_nfo(self, movie, category_name: str, omit_title: bool = False) -> str:
        """Generate NFO XML content for a movie."""
        raw_title = movie.name or "Unknown"
        title = self._clean_nfo_title(raw_title, getattr(movie, "year", None))
        title, title_year = self._strip_trailing_year(title)
        year = movie.year or title_year or ""
        plot = movie.description or ""
        rating = (movie.rating or "").strip()
        tmdb_id = (movie.tmdb_id or "").strip()
        imdb_id = (movie.imdb_id or "").strip()

        genres = self._resolve_genres(getattr(movie, "genre", ""), category_name)
        
        # Build XML
        xml_lines = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>']
        xml_lines.append('<movie>')
        if not omit_title:
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
    
    def _sanitize_filename(self, name: str) -> str:
        """Sanitize filename by removing invalid characters."""
        if not name:
            return "Unknown"
        
        # Remove invalid characters for Windows/Linux filesystems
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', name)
        
        # Replace multiple spaces with single space
        name = re.sub(r'\s+', ' ', name)
        
        # Trim and limit length
        name = name.strip()[:self.MAX_FILENAME_LEN]
        
        # Remove trailing dots/spaces (Windows issue)
        name = name.rstrip('. ')

        return name or "Unknown"

    def _write_if_different_preserve_times(self, path: str, new_contents: str) -> bool:
        """Write a .strm only when its contents actually change, preserving the
        original mtime when updating an existing file.

        Returns True if the file was created or updated, False if it was left
        untouched.

        Why: since v1.13.0 the refresh paths rewrite every .strm so a changed
        Dispatcharr URL propagates. Rewriting bumps the mtime, and media
        servers key their "has this file changed?" check on mtime — so every
        nightly rescan made Emby/Jellyfin re-index the entire library even
        though not a byte differed. Skipping no-op writes fixes that (and makes
        a no-change rescan dramatically cheaper); restoring the original mtime
        covers the genuine-URL-change case, where the new URL still needs to be
        picked up but the media server has no reason to re-scan — players read
        the .strm at playback time, not from the index.

        Reported with a patch by @bruor (issue #11).
        """
        dir_path = os.path.dirname(path)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)

        if not os.path.exists(path):
            # Genuinely new file — a fresh mtime is correct here.
            with open(path, "w", encoding="utf-8") as f:
                f.write(new_contents)
            return True

        orig_mtime = os.stat(path).st_mtime

        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                current = f.read()
        except OSError:
            current = None

        # Tolerate trailing-whitespace differences so files written by older
        # versions (or hand-edited) don't count as changed.
        if current is not None and current.strip() == new_contents.strip():
            return False

        with open(path, "w", encoding="utf-8") as f:
            f.write(new_contents)
            f.flush()
            os.fsync(f.fileno())

        # Restore the original mtime so the media server doesn't re-index.
        os.utime(path, (os.stat(path).st_atime, orig_mtime))
        return True

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
