"""
VOD to Media Library — Dispatcharr VOD .strm Generator Plugin
(slug: vod2mlib)
v1.20.1 — independent movie and series metadata filters.

MIT License
Copyright (c) 2025-2026 shedunraid (original author)
Copyright (c) 2026 R3XCHRIS (downstream maintainer, fork)
Upstream:   https://github.com/shedunraid/VOD2MLIB
This fork:  https://github.com/mwongj/VOD2MLIB
"""
import os
import time
import hashlib
from contextlib import nullcontext
from pathlib import Path
import re
from enum import Enum
from typing import Dict, Any
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
try:
    from .inventory import action_lock, state_directory, file_hash, contained, BATCH_SIZE
    from .reconciliation import Reconciliation
    from . import action_runner
    from .media_library import create_adapter
    from .metadata_filters import FIELDS as FILTER_FIELDS, SECTION as FILTER_SECTION, configuration, passing_relations, catalogue_counts
except ImportError:
    from inventory import action_lock, state_directory, file_hash, contained, BATCH_SIZE
    from reconciliation import Reconciliation
    import action_runner
    from media_library import create_adapter
    from metadata_filters import FIELDS as FILTER_FIELDS, SECTION as FILTER_SECTION, configuration, passing_relations, catalogue_counts


class VODType(Enum):
    MOVIE = "movie"
    SERIES = "series"


class Plugin:
    """Generate .strm files for VOD movies from Dispatcharr."""

    name = "VOD to Media Library (mwongj fork)"
    version = "1.20.1"
    help_url = "https://github.com/mwongj/VOD2MLIB#readme"
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

    fields = [{'id': '_about',
      'label': 'About',
      'type': 'info',
      'description': 'Workflow:\n'
                     '  1. Configure paths below.\n'
                     '  2. Actions → Scan → see catalogue totals.\n'
                     '  3. Actions → Generate Movies / Generate Series (start with Batch Size 10).\n'
                     '  4. (Optional) Turn ON Refresh Existing Series, enable Auto-Rescan, set cron, and Save for '
                     'nightly auto-rescan.\n'
                     '\n'
                     'Docs: https://github.com/mwongj/VOD2MLIB'},
     {'id': '_section_paths',
      'label': '[PATHS & HOSTS]',
      'type': 'info',
      'description': 'Where to write .strm files and how media servers reach Dispatcharr.'},
     {'id': 'root_folder',
      'label': 'Root Folder for Movies',
      'type': 'string',
      'default': '/VODS/Movies',
      'help_text': 'Path inside the Dispatcharr container where movie folders will be created. Map a host folder '
                   'to /VODS in your container.'},
     {'id': 'series_root_folder',
      'label': 'Root Folder for Series',
      'type': 'string',
      'default': '/VODS/Series',
      'help_text': 'Path inside the Dispatcharr container where series folders will be created.'},
     {'id': 'dispatcharr_url',
      'label': 'Dispatcharr URL (REQUIRED)',
      'type': 'string',
      'default': '',
      'placeholder': 'http://192.168.1.10:9191',
      'help_text': 'Required. The externally-reachable URL of your Dispatcharr instance — this gets baked into '
                   'every .strm file, so it must resolve from wherever your media server runs. localhost works '
                   'ONLY if the media server is on the same host with shared network namespace; otherwise use a '
                   "routable LAN IP/hostname. Don't forget to click Save."},
     {'id': '_section_nfo',
      'label': '[NFO METADATA]',
      'type': 'info',
      'description': 'Choose who writes metadata: this plugin or Emby. Disable both generation toggles if Emby manages metadata and saves NFOs. Emby integration checks real-media ownership independently. Filters and enabled Emby ownership cleanup archive excluded NFO-only folders even when generation is off or Deletion scope is STRMs only.'},
     {'id': 'generate_nfo',
      'label': 'Generate Movie NFO Files',
      'type': 'boolean',
      'default': True,
      'help_text': 'Write Dispatcharr metadata when the movie NFO is absent. Turn this off if Emby manages metadata and saves its own NFOs. Emby integration only checks library ownership; it does not configure metadata saving. Turning this off preserves existing NFOs. Filters and enabled Emby ownership cleanup archive excluded NFO-only folders independently of this toggle and Deletion scope.'},
     {'id': 'generate_series_nfo',
      'label': 'Generate Series NFO Files',
      'type': 'boolean',
      'default': True,
      'help_text': 'Write Dispatcharr metadata when tvshow.nfo or an episode NFO is absent. Turn this off if Emby manages metadata and saves its own NFOs. Existing NFOs are preserved during generation. Filters and enabled Emby ownership cleanup archive excluded NFO-only folders independently of this toggle and Deletion scope.'},
     {'id': 'nfo_omit_title',
      'label': 'Omit <title> from NFO files',
      'type': 'boolean',
      'default': False,
      'help_text': 'Leave the `<title>` element OUT of generated movie and tvshow NFO files. Jellyfin (and Emby) '
                   'treat a `<title>` in the NFO as authoritative and will NOT override it from TMDB — so if '
                   'your provider prefixes titles with tags like `4K-A+`, `EN-TOP` or `AMZ`, that junk becomes '
                   'the displayed name. With this ON the plugin still writes the NFO (IDs, plot, genres, rating, '
                   'poster) but omits the title, letting your media server take the clean title from TMDB via '
                   'the `<tmdbid>` we already emit. OFF by default (unchanged behaviour). Note v1.18.0 also '
                   'cleans provider junk out of the title, so try that first — this is the belt-and-braces '
                   'option. Episode NFOs always keep their title (media servers match episodes by season/episode '
                   'number).'},
     {'id': '_section_movies',
      'label': '[MOVIES]',
      'type': 'info',
      'description': 'Settings for the Generate Movies action.'},
     {'id': 'batch_size',
      'label': 'Batch Size (Movies)',
      'type': 'select',
      'default': '250',
      'options': [{'value': '10', 'label': '10 movies'},
                  {'value': '100', 'label': '100 movies'},
                  {'value': '200', 'label': '200 movies'},
                  {'value': '250', 'label': '250 movies'},
                  {'value': '500', 'label': '500 movies'},
                  {'value': '1000', 'label': '1000 movies'},
                  {'value': 'all', 'label': 'All movies'}],
      'help_text': 'Number of movies to process in this run. Start small (10) to verify, then scale up.'},
     {'id': 'nest_movies_by_category',
      'label': 'Nest Movies by Category',
      'type': 'boolean',
      'default': False,
      'help_text': "Wrap each movie's folder inside a subfolder named by its M3U category. Useful when your "
                   'provider organises movies by genre. Movies without a category go into a folder named '
                   "'Unassigned'. Same content with different categories (e.g. 4K vs HD) gets separate folders "
                   'intentionally — turn ON Dedupe Movies Across Categories below to suppress this for '
                   'genre-overlap cases.'},
     {'id': 'dedupe_movies_across_categories',
      'label': 'Dedupe Movies Across Categories',
      'type': 'boolean',
      'default': False,
      'help_text': 'When `Nest Movies by Category` is ON and a movie is tagged with multiple categories upstream '
                   "(e.g. 'Action' AND 'Sci-Fi'), write the `.strm` under the first category only (alphabetical "
                   'by category name) instead of duplicating across all of them. No effect when `Nest Movies by '
                   'Category` is OFF — multi-category movies already resolve to the same folder in that case. '
                   'Use this when you want one folder per movie regardless of provider tagging. ⚠ MIGRATION: '
                   'changing this on an already-generated library does NOT remove the old duplicate folders — it '
                   'just stops creating new ones. To clean up existing duplicates, run `[⚠ DANGER] Clean up '
                   'Movies` once, then re-generate.'},
     {'id': 'append_tmdb_id_to_folder',
      'label': 'Append TMDB ID to folder names',
      'type': 'boolean',
      'default': False,
      'help_text': 'Append a TMDB id tag to every Movies and Series folder name when a TMDB ID is known — e.g. '
                   '`Cool Hand Luke (1967) {tmdb-378}/`. Media servers honour this as a forced exact match, '
                   'which is the safest defence against name collisions and bad metadata scrapes. Pick the '
                   'convention your server expects with `TMDB Folder Tag Format` below. ⚠ MIGRATION: the plugin '
                   'does NOT rename existing folders in place — turning this on (or off) for an '
                   'already-generated library writes the new folder names ALONGSIDE the old ones, creating '
                   'duplicates. To switch cleanly, run `[⚠ DANGER] Clean up Movies` / `Series` first, then '
                   're-generate; or accept the duplicates until the old folders age out.'},
     {'id': 'tmdb_tag_format',
      'label': 'TMDB Folder Tag Format',
      'type': 'select',
      'default': 'plex',
      'options': [{'value': 'plex', 'label': 'Plex / ChannelsDVR — {tmdb-123}'},
                  {'value': 'jellyfin', 'label': 'Jellyfin / Emby — [tmdbid-123]'}],
      'help_text': 'Which convention to use for the TMDB folder tag — media servers disagree, and each ignores '
                   "the other's format. `Plex / ChannelsDVR` writes `Cool Hand Luke (1967) {tmdb-378}`; "
                   '`Jellyfin / Emby` writes `Cool Hand Luke (1967) [tmdbid-378]`. Only has an effect when '
                   '`Append TMDB ID to folder names` is ON. Defaults to Plex for backwards compatibility with '
                   'libraries generated before v1.16.1 — if you use Jellyfin or Emby, switch this to `jellyfin` '
                   'or the tag is silently ignored by your server. ⚠ MIGRATION: changing the format renames '
                   'every folder, and the plugin does NOT rename in place — the new names are written ALONGSIDE '
                   'the old ones. Run `[⚠ DANGER] Clean up Movies` / `Series` first, then re-generate.'},
     {'id': 'omit_stream_id',
      'label': "Don't pin .strm files to a specific provider stream",
      'type': 'boolean',
      'default': False,
      'help_text': "When ON, .strm URLs omit ?stream_id=, so Dispatcharr's VOD proxy resolves and fails over "
                   'across every account carrying the title instead of being locked to the one relation this '
                   'plugin happened to pick. Requires a patched Dispatcharr with VOD failover support (PR '
                   "#1398). When OFF (default), the .strm is pinned to this plugin's selected relation, matching "
                   'original behavior.'},
     {'id': '_section_series',
      'label': '[SERIES]',
      'type': 'info',
      'description': 'Settings for the Generate Series action.'},
     {'id': 'series_batch_size',
      'label': 'Batch Size (Series)',
      'type': 'select',
      'default': '10',
      'options': [{'value': '1', 'label': '1 series (testing)'},
                  {'value': '5', 'label': '5 series'},
                  {'value': '10', 'label': '10 series'},
                  {'value': '25', 'label': '25 series'},
                  {'value': '50', 'label': '50 series'},
                  {'value': '100', 'label': '100 series'},
                  {'value': '250', 'label': '250 series'},
                  {'value': 'all', 'label': 'All series (may time out — use the schedule)'}],
      'help_text': 'Number of series to process per run using episodes stored in Dispatcharr. Actions run in the background. For automatic full rescans, select Full rescan, enable Auto-Rescan, and Save.'},
     {'id': 'series_workers', 'label': 'Parallel Series Workers', 'type': 'select', 'default': '3', 'options': [{'value': '1', 'label': '1'}, {'value': '2', 'label': '2'}, {'value': '3', 'label': '3'}, {'value': '4', 'label': '4'}, {'value': '5', 'label': '5'}, {'value': '6', 'label': '6'}], 'help_text': 'Concurrent series generation tasks using Dispatcharr database metadata. Default 3; increase after measuring database and storage performance. Movies continue using 3 workers.'},
     {'id': 'refresh_existing', 'label': 'Refresh Existing Series (rescan-friendly)', 'type': 'boolean', 'default': False, 'help_text': 'Re-evaluate existing series using only metadata and episodes already stored in Dispatcharr. Include newly stored episodes and refresh changed managed STRM/NFO output while preserving edited files. No provider requests or native metadata updates occur. Off skips already-generated series; On rechecks them. Full rescan forces this On. Refresh or fetch episode data in Dispatcharr before generating if its database is incomplete or stale.'},
     {'id': 'nest_series_by_category',
      'label': 'Nest Series by Category',
      'type': 'boolean',
      'default': False,
      'help_text': "Wrap each series' folder inside a subfolder named by its M3U category. Useful when your "
                   'provider organises series by genre. Series without a category go into a folder named '
                   "'Unassigned'. Same content with different categories gets separate folders intentionally — "
                   'turn ON Dedupe Series Across Categories below to suppress this for genre-overlap cases.'},
     {'id': 'dedupe_series_across_categories',
      'label': 'Dedupe Series Across Categories',
      'type': 'boolean',
      'default': False,
      'help_text': 'When `Nest Series by Category` is ON and a series is tagged with multiple categories '
                   'upstream, write the series folder + episodes under the first category only (alphabetical by '
                   'category name) instead of duplicating across all of them. No effect when `Nest Series by '
                   'Category` is OFF. ⚠ MIGRATION: changing this on an already-generated library does NOT remove '
                   'the old duplicate folders — run `[⚠ DANGER] Clean up Series` once, then re-generate, to '
                   'clean them up.'},
     {'id': '_section_schedule',
      'label': '[AUTO-RESCAN SCHEDULE]',
      'type': 'info',
      'description': 'Scheduled jobs use the same saved settings as manual actions. Enable auto-rescan and Save to register or update a valid cron schedule; disable it and Save for manual actions only.'},
     {'id': 'schedule_enabled',
      'label': 'Enable Auto-Rescan',
      'type': 'boolean',
      'default': False,
      'help_text': 'Enable scheduled runs with a valid cron and timezone. Save applies all settings. Turn off and Save for manual actions only.'},
     {'id': 'schedule_cron',
      'label': 'Auto-Rescan Schedule (cron)',
      'type': 'string',
      'default': '0 3 * * *',
      'help_text': "Standard 5-field cron: 'minute hour day-of-month month day-of-week'. Default '0 3 * * *' = "
                   'every day at 03:00. Validated when scheduling is enabled; Save applies changes.'},
     {'id': 'schedule_timezone',
      'label': 'Schedule Timezone',
      'type': 'string',
      'default': '',
      'placeholder': 'Europe/London',
      'help_text': "IANA timezone name the cron expression is interpreted in (e.g. 'Europe/London', "
                   "'America/New_York', 'Australia/Sydney'). Leave empty to use UTC. Affects when the cron fires "
                   "— '0 3 * * *' in 'Europe/London' means 03:00 London time year-round (handling BST "
                   'automatically), not 03:00 UTC.'},
     {'id': 'schedule_target',
      'label': 'Scheduled Action',
      'type': 'select',
      'default': 'rescan_all',
      'options': [{'value': 'scan_all_vods', 'label': 'Scan only (totals)'},
                  {'value': 'generate_movies', 'label': 'Movies only'},
                  {'value': 'generate_series', 'label': 'Series only'},
                  {'value': 'rescan_all', 'label': 'Full rescan (movies + series)'}],
      'help_text': "Which action the scheduler should run on each tick. 'Full rescan' is recommended."},
     {'id': '_section_media_library',
      'label': '[MEDIA LIBRARY & CLEANUP]',
      'type': 'info',
      'description': 'Skip content already available as real media and optionally remove generated files for '
                     'duplicates or M3U removals.'},
     {'id': 'media_library_enabled',
      'label': 'Enable media-library integration',
      'type': 'boolean',
      'default': False,
      'help_text': 'Check the configured media server before generation to skip content already available as '
                   'real media. Disabled by default.'},
     {'id': 'media_server',
      'label': 'Media server',
      'type': 'select',
      'default': 'emby',
      'options': [{'value': 'emby', 'label': 'Emby'}],
      'help_text': 'Choose the media server to check for existing real media. This release supports one Emby '
                   'server.'},
     {'id': 'media_server_url',
      'label': 'Emby server URL',
      'type': 'string',
      'default': '',
      'help_text': 'URL of your Emby server as reachable from the Dispatcharr container, for example '
                   'http://emby:8096. Include any configured base path.'},
     {'id': 'media_server_token',
      'label': 'Emby API key/token',
      'type': 'string',
      'default': '',
      'help_text': 'Emby API key used to read libraries and media metadata. Create one in the Emby dashboard '
                   'under API Keys.'},
     {'id': 'media_library_ids',
      'label': 'Library names or IDs',
      'type': 'string',
      'default': '',
      'help_text': 'Required comma-separated library names or IDs, for example Movies, TV Shows. Names are '
                   'resolved on every run, so recreated libraries keep working under the same name. Run List '
                   'media libraries to discover names/IDs; omit generated VOD/STRM libraries. Empty, missing '
                   'or ambiguous selections stop actions; use IDs for duplicate names.'},
     {'id': 'media_tv_mode',
      'label': 'TV handling',
      'type': 'select',
      'default': 'show',
      'options': [{'value': 'show', 'label': 'Skip entire owned show'},
                  {'value': 'episodes', 'label': 'Fill missing episodes'}],
      'help_text': 'Skip entire owned show excludes all episodes when Emby has any real episode of that show. '
                   'Fill missing episodes excludes only season/episode positions Emby owns and revisits existing '
                   'series folders to generate missing episodes. STRM-only episodes do not count.'},
     {'id': 'media_server_failure',
      'label': 'Server-check failure',
      'type': 'select',
      'default': 'continue',
      'options': [{'value': 'continue', 'label': 'Continue with warning'},
                  {'value': 'stop', 'label': 'Stop before file changes'}],
      'help_text': 'Continue with warning generates without media-server exclusions or duplicate cleanup if the '
                   'server check fails; enabled M3U cleanup can still run. Stop before file changes aborts the '
                   'action before changing output files.'},
     {'id': 'media_duplicate_cleanup',
      'label': 'Existing duplicate cleanup',
      'type': 'select',
      'default': 'every',
      'options': [{'value': 'every', 'label': 'Every generation'},
                  {'value': 'rescan', 'label': 'Full rescans only'},
                  {'value': 'disabled', 'label': 'Disabled'}],
      'help_text': 'Choose when generation removes verified STRMs that duplicate real media on the server. '
                   'Cleanup checks all tracked output, regardless of batch size. Disabled stops automatic '
                   'deletion but still skips owned content during generation. Run selective cleanup checks '
                   'immediately.'},
     {'id': 'm3u_cleanup_enabled',
      'label': 'Clean up M3U removals',
      'type': 'boolean',
      'default': False,
      'help_text': 'Remove verified generated STRMs when their account/provider source is confirmed missing from '
                   'the complete Dispatcharr catalogue, with no grace period. This ignores generation batches '
                   'and category selections. Failed queries or episode refreshes do not establish removal. '
                   'Disabled by default.'},
     {'id': 'm3u_cleanup_timing',
      'label': 'M3U cleanup timing',
      'type': 'select',
      'default': 'rescan',
      'options': [{'value': 'rescan', 'label': 'Full rescans'},
                  {'value': 'manual', 'label': 'Manual action only'}],
      'help_text': 'When M3U cleanup is enabled, Full rescans checks removals during each Full rescan, including '
                   'scheduled rescans. Manual action only waits for Run selective cleanup. Preview selective '
                   'cleanup shows candidates without deleting output files.'},
     {'id': 'deletion_scope',
      'label': 'Deletion scope',
      'type': 'select',
      'default': 'strm',
      'options': [{'value': 'strm', 'label': 'STRMs only'},
                  {'value': 'strm_nfo', 'label': 'STRMs and unedited generated NFOs'}],
      'help_text': 'For Emby/source-removal and explicit root cleanup, STRMs only preserves NFO metadata. Filters and enabled Emby ownership cleanup independently archive excluded NFO-only folders outside the library. STRMs and unedited generated NFOs also removes NFOs '
                   'whose recorded generated hashes still match. Edited or unverified files, artwork and '
                   'subtitles are preserved. Applies to automatic, selective and Movies/Series cleanup actions.'}]

    fields.append({'id': 'action_timeout_minutes', 'label': 'Maximum action runtime (minutes)',
                   'type': 'select', 'default': '30',
                   'options': [{'value': str(n), 'label': str(n)} for n in (5, 15, 30, 60, 120)],
                   'help_text': 'Stops a generation, library check or cleanup and all its workers after this deadline. Background actions continue after the browser closes. Use Action status for results or Stop running action to cancel sooner. Completed file changes remain; retry resumes normal processing.'})

    actions = [{'id': 'rebuild_inventory',
      'label': '[LIBRARY] Rebuild / discover inventory',
      'description': 'Rediscover STRMs; reset decisions. Deletes no files.',
      'button_label': 'Rebuild',
      'button_variant': 'outline',
      'button_color': 'blue'},
     {'id': 'list_media_libraries',
      'label': 'List media libraries',
      'description': 'List Emby library names and IDs.',
      'button_label': 'List',
      'button_variant': 'outline',
      'button_color': 'blue'},
     {'id': 'preview_cleanup',
      'label': 'Preview selective cleanup',
      'description': 'Preview removals and NFO archives; changes no files.',
      'button_label': 'Preview',
      'button_variant': 'outline',
      'button_color': 'blue'},
     {'id': 'selective_cleanup',
      'label': 'Run selective cleanup',
      'description': 'Apply filters and enabled ownership/source cleanup.',
      'button_label': 'Clean up',
      'button_variant': 'filled',
      'button_color': 'orange'},
     {'id': 'scan_all_vods',
      'label': '[LIBRARY] Catalogue snapshot',
      'description': 'Count native movie/series eligibility. Read-only.',
      'button_label': 'Scan',
      'button_variant': 'outline',
      'button_color': 'blue'},
     {'id': 'generate_movies',
      'label': '[GENERATE] Movies',
      'description': 'Apply movie filters, then generate up to Batch Size.',
      'button_label': 'Generate',
      'button_variant': 'filled',
      'button_color': 'green'},
     {'id': 'generate_series',
      'label': '[GENERATE] Series',
      'description': 'Apply series filters, then generate episode files.',
      'button_label': 'Generate',
      'button_variant': 'filled',
      'button_color': 'green'},
     {'id': 'rescan_all',
      'label': '[GENERATE] Full rescan',
      'description': 'Apply filters, then rescan movies and series.',
      'button_label': 'Rescan all',
      'button_variant': 'filled',
      'button_color': 'teal',
      'confirm': {'required': True,
                  'title': 'Run full rescan now?',
                  'message': 'Apply current filters and configured cleanup, then process movies and series '
                             'from Dispatcharr database metadata using saved batch limits. Rejected managed '
                             'STRMs can be removed and rejected NFO-only folders archived. Only episodes '
                             'already stored by Dispatcharr are available. This can take several minutes.'}},
     {'id': 'schedule_status',
      'label': '[SCHEDULE] Show status',
      'description': 'Show registered cron, last run, and total runs.',
      'button_label': 'Status',
      'button_variant': 'outline',
      'button_color': 'blue'},
     {'id': 'schedule_test_fire',
      'label': '[SCHEDULE] Test fire now',
      'description': 'Run the saved scheduled action now.',
      'button_label': 'Test fire',
      'button_variant': 'outline',
      'button_color': 'blue',
      'confirm': {'required': True,
                  'title': 'Fire scheduled task now?',
                  'message': 'Runs the current saved scheduled action with the current saved settings right now. '
                             'Useful to verify the pipeline works. May take many minutes depending on the '
                             'action.'}},
     {'id': 'cleanup_movies',
      'label': '[⚠ DANGER] Clean up Movies',
      'description': 'Delete verified movie output per Deletion scope.',
      'button_label': 'Clean up',
      'button_variant': 'filled',
      'button_color': 'red',
      'confirm': {'required': True,
                  'title': 'Delete generated movie files?',
                  'message': 'Delete verified generated STRMs in this root? NFOs are deleted only if Deletion '
                             'scope includes NFOs and their generated hashes match. Unverified and edited files '
                             'are preserved.'}},
     {'id': 'cleanup_series',
      'label': '[⚠ DANGER] Clean up Series',
      'description': 'Delete verified series output per Deletion scope.',
      'button_label': 'Clean up',
      'button_variant': 'filled',
      'button_color': 'red',
      'confirm': {'required': True,
                  'title': 'Delete generated series files?',
                  'message': 'Delete verified generated STRMs in this root? NFOs are deleted only if Deletion '
                             'scope includes NFOs and their generated hashes match. Unverified and edited files '
                             'are preserved.'}}]

    fields.extend([FILTER_SECTION, *FILTER_FIELDS])

    actions.extend([{'id': 'action_status', 'label': '[ACTION] Status',
                     'description': 'Show the running action or its final result.',
      'button_label': 'Status',
      'button_variant': 'outline',
      'button_color': 'blue'},
                    {'id': 'stop_action', 'label': '[ACTION] Stop running action',
                     'description': 'Stop workers; keep completed file changes.',
      'button_label': 'Stop',
      'button_variant': 'filled',
      'button_color': 'red'}])

    def run(self, action: str, params: dict, context: dict):
        settings = context.get("settings", {})
        if action == "action_status":
            return action_runner.status()
        if action == "stop_action":
            return action_runner.stop()
        try:
            configuration(settings)
            self._series_worker_count(settings)
        except ValueError as error:
            return {"status": "error", "message": str(error)}
        if action in {"generate_movies", "generate_series", "rescan_all", "cleanup_movies", "cleanup_series", "preview_cleanup", "selective_cleanup", "list_media_libraries", "rebuild_inventory"}:
            return action_runner.start(action, params, settings)
        return self._run_action(action, params, context)

    def _run_locked_action(self, action: str, params: dict, context: dict):
        logger, settings = context.get("logger"), context.get("settings", {})
        if action == "list_media_libraries":
            try:
                libraries = create_adapter(settings).list_libraries()
                return {"status": "ok", "message": "; ".join(f"{x['Name']}: {x['Id']}" for x in libraries), "libraries": libraries}
            except Exception as error:
                return {"status": "error", "message": str(error)}
        mutating = {"generate_movies", "generate_series", "rescan_all", "cleanup_movies", "cleanup_series", "preview_cleanup", "selective_cleanup", "rebuild_inventory"}
        if action not in mutating:
            return self._run_action(action, params, context)
        reconciliation = None
        try:
            configuration(settings)
            self._series_worker_count(settings)
            with action_lock(state_directory()):
                reconciliation = Reconciliation(self, settings, logger, state_directory())
                self._reconciliation = reconciliation
                try:
                    reconciliation.prepare(action)
                    if action == "rebuild_inventory":
                        result = {"status": "ok", "message": f"Inventory discovery complete; adopted {reconciliation.report['adopted']}, preserved {reconciliation.report['preserved']} unverified files"}
                    elif action in ("preview_cleanup", "selective_cleanup"):
                        result = {"status": "ok", "message": "Cleanup preview complete" if action == "preview_cleanup" else "Selective cleanup complete"}
                    else:
                        with reconciliation.measure("action_processing"):
                            result = self._run_action(action, params, context)
                    with reconciliation.measure("inventory_drain"):
                        reconciliation.drain()
                    result['reconciliation'] = reconciliation.report
                    result['message'] += f"; excluded {reconciliation.report['excluded']}, deleted {reconciliation.report['deleted']} ({reconciliation.report['filter_deleted']} by filters), cleanup errors {reconciliation.report['errors']}"
                    if action == 'preview_cleanup':
                        result['message'] += f"; filter removal candidates {reconciliation.report['filter_candidates']}"
                        result['message'] += f"; NFO-only archive candidates {reconciliation.report['filter_nfo_folders_candidates']} folders ({reconciliation.report['filter_nfo_candidates']} NFOs)"
                    elif reconciliation.report['filter_nfo_folders_archived']:
                        result['message'] += f"; archived {reconciliation.report['filter_nfo_folders_archived']} NFO-only folders ({reconciliation.report['filter_nfo_archived']} NFOs) to {reconciliation.report['filter_nfo_archive_root']}"
                    if action == 'preview_cleanup':
                        result['message'] += f"; ownership NFO-only archive candidates {reconciliation.report['ownership_nfo_folders_candidates']} folders ({reconciliation.report['ownership_nfo_candidates']} NFOs)"
                    elif reconciliation.report['ownership_nfo_folders_archived']:
                        result['message'] += f"; archived {reconciliation.report['ownership_nfo_folders_archived']} owned NFO-only folders ({reconciliation.report['ownership_nfo_archived']} NFOs) to {reconciliation.report['ownership_nfo_archive_root']}"
                    if reconciliation.report['warnings']:
                        result['message'] += "; WARNING: " + "; ".join(reconciliation.report['warnings'])
                    return result
                finally:
                    try:
                        reconciliation.drain()
                    finally:
                        reconciliation.store.close()
                        reconciliation.finish()
                        self._reconciliation = None
        except Exception as error:
            logger.error("Action failed: %s", error)
            result = {"status": "error", "message": str(error)}
            if reconciliation:
                result['reconciliation'] = reconciliation.report
            return result

    def _owned(self, obj, kind, position=None):
        rec = getattr(self, '_reconciliation', None)
        return bool(rec and rec.owns(obj, kind, position))

    def _writable_strm(self, path, uuid, kind):
        rec = getattr(self, '_reconciliation', None)
        return not rec or rec.writable(path, uuid, kind)

    def _track(self, path, obj, kind, rel, position=None, nfos=None):
        rec = getattr(self, '_reconciliation', None)
        if rec: rec.record(path, obj, kind, rel, position, nfos)

    def _drain_inventory(self):
        rec = getattr(self, '_reconciliation', None)
        if rec and (rec.queue.qsize() >= BATCH_SIZE or rec.cache_queue.qsize() >= BATCH_SIZE): rec.drain()

    def _movie_complete(self, relation, path):
        rec = getattr(self, '_reconciliation', None)
        decision = getattr(relation, '_generation_signature', None)
        if rec and decision:
            rec.cache_complete('movie', *decision, path)

    def _timed_operation(self, name, function, *args, **kwargs):
        rec = getattr(self, "_reconciliation", None)
        if rec is None:
            return function(*args, **kwargs)
        with rec.measure(name):
            return function(*args, **kwargs)

    def _run_action(self, action: str, params: dict, context: dict):
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
            return self._timed_operation("generate_movies", self._generate_movies, settings, logger)
        elif action == "generate_series":
            return self._timed_operation("generate_series", self._generate_series, settings, logger)
        elif action == "cleanup_movies":
            return self._cleanup_movies(settings, logger)
        elif action == "cleanup_series":
            return self._cleanup_series(settings, logger)
        elif action == "rescan_all":
            return self._rescan_all(settings, logger)
        elif action == "schedule_status":
            return self._schedule_status(settings, logger)
        elif action == "schedule_test_fire":
            return self._schedule_test_fire(settings, logger)

        return {"status": "error", "message": f"Unknown action: {action}"}

    def _eligible_vod_relations(self, query, vod_type: VODType):
        """Require an active account and its enabled category for this VOD type.

        Match the category relation to the content relation's account so an
        enabled category on another account cannot admit disabled content.
        Match the type so identically named movie and series categories remain
        independent.
        """
        from django.db.models import F

        return query.filter(
            m3u_account__is_active=True,
            category__category_type=vod_type.value,
            category__m3u_relations__enabled=True,
            category__m3u_relations__m3u_account_id=F("m3u_account_id"),
        )

    def _scan_all_vods(self, settings: Dict[str, Any], logger):
        """Scan and show total movies and series available."""
        try:
            filter_rules = configuration(settings)
        except ValueError as error:
            return {"status": "error", "message": str(error)}
        logger.info("Scanning VODs in Dispatcharr...")
        logger.info("")

        try:
            from apps.vod.models import Movie, Series, M3UMovieRelation, M3USeriesRelation
            from django.db.models import Count
        except ImportError as e:
            logger.error("Failed to import models: %s", e)
            return {"status": "error", "message": f"Import error: {e}"}

        try:
            # Reuse generator eligibility for unique content, relation totals,
            # and categories. Content without an eligible provider is orphaned.
            eligible_movies = self._eligible_vod_relations(
                M3UMovieRelation.objects.all(), VODType.MOVIE,
            )
            eligible_series = self._eligible_vod_relations(
                M3USeriesRelation.objects.all(), VODType.SERIES,
            )
            filter_counts = {
                'movies': catalogue_counts(eligible_movies, 'movie', filter_rules['movie']),
                'series': catalogue_counts(eligible_series, 'series', filter_rules['series']),
            }
            for kind, counts in filter_counts.items():
                logger.info("Metadata filters %s (before library checks): %s", kind, counts)
            active_movie = eligible_movies.values("movie_id").distinct().count()
            active_series = eligible_series.values("series_id").distinct().count()
            total_movie = Movie.objects.count()
            total_series = Series.objects.count()
            orphan_movie = total_movie - active_movie
            orphan_series = total_series - active_series
            movie_relations = eligible_movies.count()
            series_relations = eligible_series.count()

            logger.info("=" * 60)
            logger.info("MOVIES: %d active  (%d M3U relations)", active_movie, movie_relations)
            if orphan_movie:
                logger.info("        %d orphaned — no active provider with an enabled category (won't generate)", orphan_movie)
            logger.info("SERIES: %d active  (%d M3U relations)", active_series, series_relations)
            if orphan_series:
                logger.info("        %d orphaned — no active provider with an enabled category (won't generate)", orphan_series)
            logger.info("=" * 60)

            # Category breakdown includes only enabled categories on active accounts.
            counts = {}
            for query, idx in ((eligible_movies, 0), (eligible_series, 1)):
                for row in (query
                            .values("category__name")
                            .annotate(n=Count("id"))):
                    entry = counts.setdefault(row["category__name"], [0, 0])
                    entry[idx] += row["n"]

            if counts:
                logger.info("")
                logger.info("CATEGORIES (%d) — enabled on active M3U accounts:", len(counts))
                ordered = sorted(counts.items(), key=lambda kv: -(kv[1][0] + kv[1][1]))
                for name, (mv, sr) in ordered[:self.SCAN_CATEGORY_LIMIT]:
                    logger.info("    %6d  %r", mv + sr, name)
                if len(ordered) > self.SCAN_CATEGORY_LIMIT:
                    logger.info("     ... and %d more", len(ordered) - self.SCAN_CATEGORY_LIMIT)
                logger.info("=" * 60)

            logger.info("")
            logger.info("Use 'Generate Movie .strm Files' for movies")
            logger.info("Use 'Generate Series .strm Files' for series")

            message = f"Found {active_movie} movies and {active_series} series; metadata filters pass {filter_counts['movies']['passing']} movies and {filter_counts['series']['passing']} series"
            if orphan_movie or orphan_series:
                message += f" ({orphan_movie + orphan_series} orphaned — no active provider with an enabled category)"

            return {
                "status": "ok",
                "message": message,
                "movies": active_movie,
                "series": active_series,
                "metadata_filters": filter_counts,
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

    def _generate_movies(self, settings: Dict[str, Any], logger, refresh_urls: bool = False):
        """Generate movie .strm files according to batch size.

        Lazily walks M3UMovieRelation via iterator() so the batch limit is
        honoured even when most candidates are already-done. Stops scanning
        as soon as target_batch new files have been written.

        refresh_urls is an internal flag set by _rescan_all (and not a
        user-visible setting). When True, existing .strm files are rewritten
        with the current Dispatcharr URL; .nfo files are still preserved.
        """
        try:
            filter_rules = configuration(settings)
        except ValueError as error:
            return {"status": "error", "message": str(error)}
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
        })

        try:
            from apps.vod.models import M3UMovieRelation
        except ImportError as e:
            logger.error("Failed to import models: %s", e)
            return {"status": "error", "message": f"Import error: {e}"}

        try:
            # Apply Dispatcharr account/category eligibility before deduplication and batching.
            query = self._eligible_vod_relations(
                M3UMovieRelation.objects
                .select_related('movie', 'm3u_account', 'category'),
                VODType.MOVIE,
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
        unchanged_strm = 0
        missing_tmdb_id = 0
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

        rec = getattr(self, '_reconciliation', None)
        relations = rec.movies(query) if rec else passing_relations(query, 'movie', filter_rules['movie'])
        for relation in relations:
            scanned += 1
            movie = relation.movie
            if self._owned(movie, "movie"):
                continue
            if seen_movie_uuids is not None:
                if movie.uuid in seen_movie_uuids:
                    # Same movie already written under an earlier-alphabetical
                    # category. Skip — counts under `deduped` not `skipped`.
                    deduped += 1
                    continue
                seen_movie_uuids.add(movie.uuid)
            cat_name = relation.category.name if relation.category else ""
            # Track titles the TMDB tag can't be applied to, so "I ticked the
            # box and nothing changed" isn't silent (reported by @drahmed86).
            if append_tmdb_id and not (getattr(movie, "tmdb_id", "") or "").strip():
                missing_tmdb_id += 1
            movie_folder, strm_filename, movie_name, year = self._movie_target_paths(
                movie, root_folder, cat_name, nest_by_cat, append_tmdb_id, tmdb_tag_format,
            )
            strm_path = os.path.join(movie_folder, strm_filename)
            is_existing = os.path.exists(strm_path)
            if is_existing and not refresh_existing and not getattr(relation, '_generation_signature', None):
                # Existing output was adopted before generation; no new write or
                # inventory update is needed for a skipped file.
                skipped += 1
                self._movie_complete(relation, strm_path)
                continue
            if not self._writable_strm(strm_path, movie.uuid, "movie"):
                logger.warning("Preserving unverified or edited STRM: %s", strm_path)
                skipped += 1
                continue

            proxy_url = self._build_proxy_url(
                dispatcharr_url, "movie", movie.uuid, relation.stream_id, omit_stream_id,
            )
            written = created_strm + refreshed_strm
            log_this = (written + 1) % self.LOG_EVERY == 1 or written < self.LOG_FIRST_N
            verb = "refreshed" if is_existing else "created"
            if log_this:
                logger.info("")
                logger.info("[%d %s / %d scanned] %s (%s)", written + 1, verb, scanned, movie_name, year or "—")

            try:
                os.makedirs(movie_folder, exist_ok=True)
                changed = self._write_if_different_preserve_times(strm_path, proxy_url)
                if not changed:
                    unchanged_strm += 1
                elif is_existing:
                    refreshed_strm += 1
                else:
                    created_strm += 1

                wrote_nfo = False
                generated_nfos = {}
                if generate_nfo:
                    nfo_filename = strm_filename.replace('.strm', '.nfo')
                    nfo_path = os.path.join(movie_folder, nfo_filename)
                    if not os.path.lexists(nfo_path):
                        category_name = relation.category.name if relation.category else ""
                        with open(nfo_path, 'w', encoding='utf-8') as f:
                            f.write(self._generate_nfo(movie, category_name, nfo_omit_title))
                        created_nfo += 1
                        wrote_nfo = True
                        generated_nfos[nfo_path] = file_hash(nfo_path)

                self._track(strm_path, movie, "movie", relation, nfos=generated_nfos)
                self._movie_complete(relation, strm_path)
                self._drain_inventory()
                if log_this:
                    if changed:
                        logger.info("  ✓ wrote .strm%s", " + .nfo" if wrote_nfo else "")
                    else:
                        logger.info("  · .strm already current (mtime preserved)%s", " + wrote .nfo" if wrote_nfo else "")
            except OSError as e:
                logger.error("  ✗ %s: %s", movie_name, e)
                errors += 1

            if batch_size != "all":
                # In refresh mode an already-current file still counts as
                # "processed" for pacing, so the batch limit behaves as it did
                # before no-op writes were skipped (#11).
                limit_hit = (
                    (refreshed_strm + created_strm + unchanged_strm) >= target_batch
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

        if rec:
            deduped += rec.report['generation_deduped']
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
        if rec and rec.report['generation_unchanged']:
            summary_msg += f", skipped {rec.report['generation_unchanged']} unchanged catalogue entries"
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
            "incremental": rec is not None,
            "unchanged_candidates": rec.report['generation_unchanged'] if rec else 0,
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

    @staticmethod
    def _series_worker_count(settings):
        value = str(settings.get('series_workers', '3'))
        if not value.isascii() or not value.isdigit() or not 1 <= int(value) <= 6:
            raise ValueError('Parallel series workers must be an integer from 1 to 6')
        return int(value)

    def _generate_series(self, settings: Dict[str, Any], logger):
        """Generate series .strm files with episodes using parallel processing."""
        try:
            filter_rules = configuration(settings)
            workers = self._series_worker_count(settings)
        except ValueError as error:
            return {"status": "error", "message": str(error)}
        series_root = settings.get("series_root_folder", "/VODS/Series")
        dispatcharr_url = (settings.get("dispatcharr_url") or "").rstrip("/")
        batch_size = settings.get("series_batch_size") or "10"
        generate_nfo = settings.get("generate_series_nfo", True)
        refresh_existing = bool(settings.get("refresh_existing", False)) or (settings.get("media_library_enabled", False) and settings.get("media_tv_mode", "show") == "episodes")
        nest_by_cat = bool(settings.get("nest_series_by_category", False))
        dedupe_across_cats = bool(settings.get("dedupe_series_across_categories", False))
        append_tmdb_id = bool(settings.get("append_tmdb_id_to_folder", False))
        tmdb_tag_format = (settings.get("tmdb_tag_format") or "plex").strip().lower()
        omit_stream_id = bool(settings.get("omit_stream_id", False))
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
            "Workers": workers,
        })

        try:
            from apps.vod.models import M3USeriesRelation
        except ImportError as e:
            logger.error("Failed to import models: %s", e)
            return {"status": "error", "message": f"Import error: {e}"}

        try:
            # Apply Dispatcharr account/category eligibility before deduplication and batching.
            query = self._eligible_vod_relations(
                M3USeriesRelation.objects
                .select_related('series', 'm3u_account', 'category'),
                VODType.SERIES,
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
        deduped = 0
        def candidates():
            nonlocal deduped
            submitted = 0
            seen = set() if dedupe_across_cats else None
            for rel in passing_relations(query, 'series', filter_rules['series']):
                if self._owned(rel.series, "series"):
                    continue
                if seen is not None:
                    if rel.series.uuid in seen:
                        deduped += 1
                        continue
                    seen.add(rel.series.uuid)
                if not refresh_existing:
                    cat_name = rel.category.name if rel.category else ""
                    folder, _, _ = self._series_target_folder(rel.series, series_root, cat_name, nest_by_cat, append_tmdb_id, tmdb_tag_format)
                    if self._series_already_processed(folder): continue
                yield rel
                submitted += 1
                if batch_size != "all" and submitted >= target_batch: break
        to_process = candidates()

        created_strm = 0
        refreshed_strm = 0
        created_nfo = 0
        errors = 0
        series_created = 0
        series_uptodate = 0
        episodes_evaluated = 0
        failures = []

        rec = getattr(self, '_reconciliation', None)
        last_progress = 0.0
        def progress(force=False):
            nonlocal last_progress
            now = time.monotonic()
            if rec and (force or now - last_progress >= 2):
                with rec.counter_lock:
                    rec.report['series_completed'] = idx
                    rec.report['episodes_evaluated'] = episodes_evaluated
                rec.progress(f"Generating series: {idx:,} completed, {len(futures)} active; "
                    f"{episodes_evaluated:,} episodes evaluated, {created_strm:,} new, "
                    f"{refreshed_strm:,} refreshed; {errors:,} errors")
                last_progress = now

        logger.info("Processing series with %d parallel workers", workers)
        with ThreadPoolExecutor(max_workers=workers) as executor:
            try:
                futures = {}
                exhausted = False
                idx = 0
                progress(force=True)
                while futures or not exhausted:
                    while not exhausted and len(futures) < workers:
                        try: rel = next(to_process)
                        except StopIteration:
                            exhausted = True
                            break
                        futures[executor.submit(self._process_single_series, rel, dispatcharr_url, generate_nfo,
                            series_root, logger, refresh_existing, nest_by_cat, append_tmdb_id,
                            omit_stream_id, tmdb_tag_format, nfo_omit_title)] = rel
                    if not futures: break
                    completed, _ = wait(futures, timeout=0.1, return_when=FIRST_COMPLETED)
                    self._drain_inventory()
                    progress()
                    for future in completed:
                        rel = futures.pop(future)
                        idx += 1
                        self._drain_inventory()
                        try:
                            result = future.result()
                        except Exception as error:
                            errors += 1
                            failures.append(str(error))
                            continue
                        if result.get("uptodate"): series_uptodate += 1
                        elif result.get("created"):
                            series_created += 1
                            created_strm += result["episodes"]
                            refreshed_strm += result.get("refreshed", 0)
                        created_nfo += result.get("nfo_files", 0)
                        episodes_evaluated += result.get('evaluated_episodes', 0)
                        if "error" in result:
                            errors += 1
                            failures.append(f"{result.get('series_name', '?')}: {result['error']}")
                        logger.info("[%d] %s", idx, result["message"])
                progress(force=True)
            except BaseException:
                rec = getattr(self, '_reconciliation', None)
                if rec: rec.cancelled.set()
                raise
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
        re-evaluated using the episodes and metadata already stored in Dispatcharr.

        When nest_by_cat=True the series folder is wrapped in a subfolder
        named by the M3U category (raw, sanitised) or 'Unassigned'.
        """
        from apps.vod.models import M3UEpisodeRelation

        rec = getattr(self, '_reconciliation', None)
        series = series_rel.series
        if self._owned(series, "series"):
            return {"created": False, "episodes": 0, "nfo_files": 0, "message": "Excluded owned series"}
        cat_name = series_rel.category.name if series_rel.category else ""
        series_folder, series_name, _year = self._series_target_folder(
            series, series_root, cat_name, nest_by_cat, append_tmdb_id, tmdb_tag_format,
        )

        try:
            # `id` is a deterministic tiebreaker: without it the winner among
            # duplicate relations for one episode varies run to run, so the
            # same .strm would flip between provider URLs on every rescan.
            with (rec.measure('episode_load', 1, worker=True) if rec else nullcontext()), \
                    (rec.episode_query() if rec else nullcontext()):
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
            rec = getattr(self, '_reconciliation', None)
            episode_cache_kind = f'episode:{series_rel.m3u_account_id}:{series.uuid}'
            with rec.measure('episode_cache_read', 1, worker=True) if rec else nullcontext():
                episode_cache = rec.episode_cache(episode_cache_kind) if rec else {}

            if episode_count == 0:
                return {
                    "created": False,
                    "uptodate": False,
                    "series_name": series_name,
                    "episodes": 0,
                    "nfo_files": 0,
                    "message": f"{series_name} - No episodes found",
                }

            with rec.measure('episode_ownership', 1, worker=True) if rec else nullcontext():
                episode_owned = rec.series_ownership(series) if rec else lambda position: False

            with rec.measure('series_files', episode_count, worker=True) if rec else nullcontext():
                if not contained(series_folder, [series_root]):
                    raise ValueError("Series folder resolves outside configured root")
                os.makedirs(series_folder, exist_ok=True)

                new_episodes = 0
                refreshed_episodes = 0
                unchanged_episodes = 0
                new_nfo = 0

                shared_nfos = {}
                prepared_seasons = set()
                if generate_nfo:
                    tvshow_nfo_path = os.path.join(series_folder, "tvshow.nfo")
                    tvshow_content = self._generate_tvshow_nfo(series, cat_name, nfo_omit_title)
                    if not os.path.lexists(tvshow_nfo_path):
                        with open(tvshow_nfo_path, 'w', encoding='utf-8') as f:
                            f.write(tvshow_content)
                        new_nfo += 1
                    try:
                        contents = Path(tvshow_nfo_path).read_bytes()
                        if contents.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n") == tvshow_content:
                            shared_nfos[tvshow_nfo_path] = hashlib.sha256(contents).hexdigest()
                    except (OSError, UnicodeError):
                        pass  # Unreadable or custom metadata is preserved.

                for episode_rel in episodes:
                    episode = episode_rel.episode
                    season_num = episode.season_number or 0
                    episode_num = episode.episode_number or 0
                    if episode_owned((season_num, episode_num)):
                        continue
                    generated_nfos = dict(shared_nfos)

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
                    decision = rec.episode_decision(episode_rel, strm_path, series) if rec else None
                    if decision and episode_cache.get(decision[0]) == decision[1]:
                        unchanged_episodes += 1
                        with rec.counter_lock:
                            rec.report['generation_unchanged'] += 1
                        continue
                    is_existing = os.path.isfile(strm_path)
                    if is_existing and not refresh_existing:
                        continue
                    if not self._writable_strm(strm_path, episode.uuid, "episode"):
                        logger.warning("Preserving unverified or edited STRM: %s", strm_path)
                        continue

                    if season_folder not in prepared_seasons:
                        os.makedirs(season_folder, exist_ok=True)
                        prepared_seasons.add(season_folder)
                    proxy_url = self._build_proxy_url(
                        dispatcharr_url, "episode", episode.uuid, episode_rel.stream_id, omit_stream_id,
                    )
                    # Only write when the URL actually changed, preserving mtime so
                    # media servers don't re-index the whole library (#11).
                    with rec.measure('strm_write', 1, worker=True) if rec else nullcontext():
                        changed = self._write_if_different_preserve_times(strm_path, proxy_url, directory_ready=True)
                    if not changed:
                        unchanged_episodes += 1
                    elif is_existing:
                        refreshed_episodes += 1
                    else:
                        new_episodes += 1

                    if generate_nfo:
                        nfo_path = os.path.join(season_folder, f"{filename}.nfo")
                        with rec.measure('episode_nfo', 1, worker=True) if rec else nullcontext():
                            if not os.path.lexists(nfo_path):
                                with open(nfo_path, 'w', encoding='utf-8') as f:
                                    f.write(self._generate_episode_nfo(episode))
                                new_nfo += 1
                                generated_nfos[nfo_path] = file_hash(nfo_path)
                    self._track(strm_path, series, "series", episode_rel, (season_num, episode_num), generated_nfos)
                    if decision:
                        rec.cache_complete(episode_cache_kind, *decision, strm_path)
            if new_episodes == 0 and refreshed_episodes == 0:
                return {
                    "created": False,
                    "uptodate": True,
                    "series_name": series_name,
                    "episodes": 0,
                    "refreshed": 0,
                    "unchanged": unchanged_episodes,
                    "evaluated_episodes": episode_count,
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
                "evaluated_episodes": episode_count,
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

    def _try_rmdir(self, path: str) -> bool:
        """Remove path if it's an empty directory. Returns True if removed."""
        try:
            os.rmdir(path)
            return True
        except OSError:
            return False

    def _walk_and_cleanup_plugin_files(self, root: str, logger):
        result = {"deleted_strm": 0, "deleted_nfo": 0, "removed_dirs": 0, "preserved_dirs": 0, "errors": 0, "scanned_dirs": 0}
        rec = getattr(self, '_reconciliation', None)
        if not rec:
            # Direct helper calls have no verified ownership inventory.
            return result
        for row in rec.store.rows():
            try:
                outcome = rec.store.delete(row, [root], rec.settings.get('deletion_scope', 'strm') == 'strm_nfo', stats=result)
                if outcome == 'deleted': result['deleted_strm'] += 1
                elif outcome == 'preserved': result['preserved_dirs'] += 1
            except OSError as error:
                result['errors'] += 1
                logger.error("Cleanup failed: %s", error)
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

    def _write_if_different_preserve_times(self, path: str, new_contents: str, directory_ready=False) -> bool:
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
        if dir_path and not directory_ready:
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
        refresh_existing setting (which rechecks episodes already stored in Dispatcharr).
        Existing .nfo files are preserved either way.
        """
        logger.info("Combined rescan: scan + movies + series (refresh URLs forced ON)")
        logger.info("")

        scan = self._timed_operation("scan_catalogue", self._scan_all_vods, settings, logger)
        if scan.get("status") != "ok":
            return scan

        logger.info("")
        logger.info("=" * 60)
        logger.info("Rescan: movies  (refresh_urls=True)")
        logger.info("=" * 60)
        movies = self._timed_operation("generate_movies", self._generate_movies, settings, logger, refresh_urls=True)

        logger.info("")
        logger.info("=" * 60)
        logger.info("Rescan: series  (refresh_existing=True)")
        logger.info("=" * 60)
        series_settings = {**settings, "refresh_existing": True}
        series = self._timed_operation("generate_series", self._generate_series, series_settings, logger)

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

    def _validate_saved_settings(self, settings):
        configuration(settings)
        self._series_worker_count(settings)
        if not settings.get("schedule_enabled", False):
            return
        target = settings.get("schedule_target") or "rescan_all"
        if target not in self._valid_schedule_targets():
            raise ValueError(f"Invalid schedule_target: {target}")
        minute, hour, dom, month, dow = self._parse_cron(settings.get("schedule_cron") or "0 3 * * *")
        from celery.schedules import crontab
        crontab(minute=minute, hour=hour, day_of_month=dom, month_of_year=month, day_of_week=dow)
        valid, message = self._validate_timezone(settings.get("schedule_timezone") or "UTC")
        if not valid:
            raise ValueError(message)

    def _sync_schedule(self, settings, logger, plugin_enabled=True):
        """Called after Save; only the trigger is persisted, never job settings."""
        from django_celery_beat.models import PeriodicTask, CrontabSchedule
        existing = PeriodicTask.objects.filter(name=self.SCHEDULE_TASK_NAME).first()
        enabled = plugin_enabled and bool(settings.get("schedule_enabled", False))
        if not enabled:
            if existing and (existing.enabled or existing.kwargs != "{}" or existing.args != "[]"):
                existing.enabled = False
                existing.kwargs = "{}"
                existing.args = "[]"
                existing.save()
            return {"status": "ok", "scheduled": False}
        self._validate_saved_settings(settings)
        minute, hour, dom, month, dow = self._parse_cron(settings.get("schedule_cron") or "0 3 * * *")
        schedule, _ = CrontabSchedule.objects.get_or_create(
            minute=minute, hour=hour, day_of_month=dom, month_of_year=month,
            day_of_week=dow, timezone=(settings.get("schedule_timezone") or "").strip() or "UTC")
        desired = {"crontab": schedule, "task": self.SCHEDULED_TASK_CELERY_NAME,
                   "queue": "dvr", "kwargs": "{}", "args": "[]", "enabled": True,
                   "description": f"Auto-rescan for {self.name} v{self.version}"}
        if existing is None or any(getattr(existing, key) != value for key, value in desired.items()):
            PeriodicTask.objects.update_or_create(name=self.SCHEDULE_TASK_NAME, defaults=desired)
            logger.info("Updated schedule from saved settings")
        return {"status": "ok", "scheduled": True, "settings_source": "current_saved"}

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
            msg = "No schedule registered. Enable Auto-Rescan in Settings and Save."
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


        logger.info("Schedule: %s", task.name)
        logger.info("  Enabled:    %s", task.enabled)
        logger.info("  Cron:       %s", cron_str)
        logger.info("  Timezone:   %s", tz_str)
        logger.info("  Task:       %s", task.task)
        logger.info("  Settings:   current saved plugin settings")
        logger.info("  Action:     %s", settings.get("schedule_target") or "rescan_all")
        logger.info("  Last run:   %s", last_run)
        logger.info("  Total runs: %s", task.total_run_count)
        message = (
            f"Schedule {state} — cron '{cron_str}' ({tz_str}), "
            f"last run {last_run}, total runs {task.total_run_count}"
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
            "settings_source": "current_saved",
            "target": settings.get("schedule_target") or "rescan_all",
        }

    @staticmethod
    def _scheduled_settings(snapshot=None, *, require_enabled=False):
        # Ignore legacy task snapshots: saved UI settings are the only source.
        from apps.plugins.models import PluginConfig
        cfg = PluginConfig.objects.get(key="vod2mlib")
        settings = dict(cfg.settings or {})
        for field in Plugin.fields:
            if "default" in field:
                settings.setdefault(field["id"], field["default"])
        if require_enabled and (not cfg.enabled or not settings.get("schedule_enabled", False)):
            return None
        Plugin()._validate_saved_settings(settings)
        return settings

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
            return {"status": "error", "message": "No schedule registered. Enable Auto-Rescan in Settings and Save."}

        current = self._scheduled_settings(require_enabled=True)
        if current is None or not task.enabled:
            return {"status": "error", "message": "Scheduling is disabled. Enable Auto-Rescan and Save first."}
        action = current.get("schedule_target") or "rescan_all"

        if action not in self._valid_schedule_targets():
            return {"status": "error", "message": f"Saved action '{action}' is not a valid target."}

        try:
            from celery import current_app
            async_result = current_app.send_task(
                self.SCHEDULED_TASK_CELERY_NAME,
                kwargs={},
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


if Path(__file__).resolve().parent == Path(os.environ.get("DISPATCHARR_PLUGINS_DIR", "/data/plugins")) / "vod2mlib":
    try:
        from .schedule_settings import install as _install_schedule_settings
        _install_schedule_settings(Plugin)
    except ImportError:
        import logging
        logging.getLogger("vod2mlib.schedule").warning("Scheduling requires Django and django-celery-beat")


try:
    from celery import shared_task as _vod2mlib_shared_task

    @_vod2mlib_shared_task(name=Plugin.SCHEDULED_TASK_CELERY_NAME)
    def _vod2mlib_scheduled_rescan(action="rescan_all", settings=None):
        """Celery entry point invoked by the periodic task maintained by the settings Save hook.

        On completion, bumps PeriodicTask.last_run_at so Show Status reflects
        manual Test fire runs (which bypass beat) and updates beat-dispatched
        runs at *completion* time rather than dispatch-start. Without this the
        UI would show stale timestamps for Test fire clicks and silently mask
        ticks that beat dispatched but the worker rejected/failed.
        """
        import logging
        logger = logging.getLogger("vod2mlib.schedule")
        current = Plugin._scheduled_settings(require_enabled=True)
        if current is None:
            return {"status": "ok", "message": "Scheduling is disabled; queued tick skipped."}
        action = current.get("schedule_target") or "rescan_all"
        if action not in Plugin()._valid_schedule_targets():
            raise ValueError(f"Invalid schedule_target: {action}")
        result = action_runner.run_and_wait(action, {}, current)
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
