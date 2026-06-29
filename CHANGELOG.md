# Changelog

## v1.16.0 — Plex support via a unified library engine (integrates VODFS)

VOD2MLIB now delivers your VOD catalogue **two ways from one engine**: `.strm` files (Jellyfin/Emby/Kodi/ChannelsDVR) and a live HTTP **mount for Plex** — which can't play `.strm`. The Plex mount comes from [VODFS](https://github.com/OneHotTake/vodfs) (MIT, © 2026 OneHotTake), but this is not a bolt-on: the two outputs are built on a single shared core.

**One path for everything.** A new `vod2mlib_core/` core holds exactly one implementation of each concern, used by both outputs:

- **One naming path** (`vod2mlib_core/naming.py`) — title cleaning, year extraction, `Title (Year) {tmdb-…} {imdb-…}` folders, `Season NN`, `SxxEyy`. A movie lands at the *same folder name* whether written as `.strm` or served on the mount. This engine is strictly better than what either side had before: it strips provider noise (quality/language prefixes, bracket tags, dotted release names, list numbers) *and* preserves title-years like `Blade Runner 2049`, `Room 1408`, `1984`.
- **One playback path** (`vod2mlib_core/playback.py`) — a single function builds the Dispatcharr proxy URL. It's the contents of each `.strm` and the `302` target of every mount file-open.
- **One configuration path** (`vod2mlib_core/config.py`) — the mount's settings flow through one schema; the `Dispatcharr URL` is shared with the `.strm` output.

**New for Plex:**
- `[MOUNT]` actions: Enable / Disable / Status / rclone config / Hydrate sizes now.
- `[PLEX]` settings: mount HTTP port (default `8889`), token auth, and size-hydration (Plex needs true file sizes to play; the mount backfills them from your provider on a schedule).
- The mount runs as a small server inside Dispatcharr and installs `uvicorn`/`fastapi`/`jinja2` on first `[MOUNT] Enable` (the `.strm` output still needs no extra packages).

**⚠ Breaking for existing `.strm` libraries — folder names changed.** Because `.strm` now uses the unified naming, movie/series folders always carry `{tmdb-…}`/`{imdb-…}` when known (previously opt-in via *Append TMDB ID*) and use the more aggressive provider-noise cleanup. To migrate an existing library cleanly: run `[⚠ DANGER] Clean up`, then re-generate. New installs are unaffected. The *Append TMDB ID* toggle is now implicit (ids are always included) and retained only for compatibility.

## v1.15.2 — active-account filter, bare-year cleanup, schedule-drift warning

Four community-reported fixes, all backwards-compatible.

- **Scan & Generate now ignore inactive providers (thanks Motronic).** Previously the plugin counted every `Movie`/`Series` row and generated `.strm` files for *all* relations — including content whose M3U provider had been deactivated upstream. So disabling a provider in Dispatcharr left its content in the plugin's Scan totals and produced dead `.strm` files. Both `[LIBRARY] Catalogue snapshot` and `[GENERATE]` now filter to relations on an **active** M3U account (`m3u_account__is_active=True`), matching Dispatcharr's own VODs UI and proxy. Scan additionally reports an **orphaned** count (content with no active provider) so the gap is explicit. *If you have orphaned `.strm` files from a previous run on now-inactive content, run `[⚠ DANGER] Clean up` once, then re-generate.*

- **Bare trailing years are stripped from folder names (thanks Lid).** Providers that ship titles like `Wicked: For Good - 2025` (bare year, no parens) previously produced `Wicked: For Good - 2025 (2025)/` once the plugin added its own `(YYYY)` suffix. New `_strip_redundant_trailing_year` helper removes a bare trailing year when it matches the known year — and, when no year is otherwise known, **adopts** the bare trailing year as the folder year. Guards real titles: `Blade Runner 2049` (year 2017), `Room 1408` (year 2007), and `1984` / `2012` (where the year *is* the title) are all preserved.

- **Show Status warns when settings drift from the cron snapshot (thanks sjsteve).** The cron job runs against a snapshot of your settings taken at the last `[SCHEDULE] Apply / Update`. If you change a setting afterwards without re-applying, `[SCHEDULE] Show status` now flags exactly which keys differ and reminds you to re-click Apply. (Compared over the snapshot's keys only, so a plugin upgrade that adds new settings won't raise a false warning.)

- **Clearer folder-migration help text.** The `Append TMDB ID`, `Dedupe Movies`, and `Dedupe Series` settings now spell out that toggling them on an already-generated library does NOT rename folders in place — you get new names alongside the old ones until you `[⚠ DANGER] Clean up` and re-generate.

Defaults unchanged; no settings migration. 17 new unit tests (165 total, was 148).

## v1.15.1 — dedupe movies/series across categories

Closes the first GitHub issue against this repo ([#1](https://github.com/R3XCHRIS/VOD2MLIB/issues/1), thanks **FCSO-byte**).

When a provider tags one movie under multiple categories (e.g. `Action` AND `Sci-Fi`), Dispatcharr stores one `M3UMovieRelation` row per (movie × m3u_account × category). With `Nest Movies by Category` ON, each row was producing a different folder path — so the same `.strm` ended up duplicated across multiple category folders. The original intent (called out in the help text) was the 4K-vs-HD variant-stream case; the multi-genre case is a fair "shouldn't be the default" outcome.

Two new opt-in toggles:

- **`Dedupe Movies Across Categories`** (default OFF, in the `[MOVIES]` section). When ON *and* `Nest Movies by Category` is also ON, the plugin writes each movie's `.strm` under the *first* category only (alphabetical by category name, deterministic tiebreaker on relation `id`) and skips subsequent ones. No effect when nesting is OFF (multi-category rows already resolve to the same folder in that case, and the existence check skips them).
- **`Dedupe Series Across Categories`** (default OFF, in the `[SERIES]` section). Mirror behaviour for series.

Run summary surfaces a new `Deduped (multi-cat)` counter so the dedup activity is visible. The query gains an `ORDER BY category__name, id` only when the dedup toggle is on, so users not opting in pay no perf cost.

The defaults preserve the existing variant-stream-friendly behaviour exactly; this is a purely additive feature for users who want one folder per title regardless of upstream tagging.

## v1.15.0 — media-server-friendliness pass

Three asks from Discord community testers in May 2026, all landed together:

- **Folder names are now scraper-friendly.** Provider VOD names like `Cool Hand Luke 4K (1967) PAUL NEWMAN (1967)` previously became a folder of the same shape, which ChannelsDVR's personal-media scraper fails to match. New `_extract_clean_name_and_year` helper truncates at the *first* `(YYYY)` (discarding the trailing junk) and strips common quality / encoding tokens (`4K`, `UHD`, `FHD`, `HD`, `SD`, `HDR`, `HEVC`, `H.264/265`, `1080p`, `720p`, `2160p`, `BluRay`, `BDRip`, `DVDRip`, `WEB-DL`, `HDTV`, `REMUX`). Result for the example: `Cool Hand Luke (1967)/`. Thanks to **sjsteve** for the report.
- **New optional `{tmdb-NNN}` folder suffix.** New boolean setting `Append TMDB ID to folder names` (default OFF) appends Plex/ChannelsDVR-friendly `{tmdb-NNN}` to every Movies and Series folder when a TMDB ID is known — e.g. `Cool Hand Luke (1967) {tmdb-378}/`. Plex's Personal Media Movies agent and ChannelsDVR's local-media scraper both honour this convention for forced exact matches, the safest defence against name collisions and bad scrapes. **Caution:** flipping the toggle on an existing library creates the new folder names alongside the old ones — `[⚠ DANGER] Clean up` first or accept duplicates. Thanks again to **sjsteve**.
- **NFOs now emit a `<thumb aspect="poster">` URL.** When Dispatcharr knows the artwork URL (via `movie.logo.url` / `series.logo.url`, typically a TMDB image), the plugin emits `<thumb aspect="poster">URL</thumb>` in `tvshow.nfo` and movie NFOs so media servers can render artwork without a TMDB roundtrip. Thanks to **edison085** for the report.

No breaking changes. The simpler `_clean_title` / `_strip_trailing_year` helpers remain for NFO title generation (which wants gentler handling than folder naming).

## v1.14.3 — Show Status reflects Test fire + task completion

The Celery task now bumps `PeriodicTask.last_run_at` on completion. Previously, that field was updated only by django-celery-beat at dispatch time, which had two consequences:

- **Manual Test fire never showed up.** `[SCHEDULE] Test fire now` uses `send_task()` (bypasses beat), so beat never got the chance to update the field. Test fire returned an "enqueued" toast but `[SCHEDULE] Show status` stayed frozen even after the task succeeded.
- **Cron failures looked like successes.** Beat increments `last_run_at` and `total_run_count` when it *dispatches*, not when the worker *completes*. A task that beat fired but the worker rejected (the v1.14.1 and earlier `unregistered task` failure mode) still made Show Status look healthy.

Now `last_run_at` becomes "last time the task actually finished" — Test fire clicks show up after completion, and a failing tick leaves `last_run_at` stale, a real signal you can spot. `total_run_count` is still beat-owned (counts dispatches) so a divergence between `last_run_at` and a recent `total_run_count` increment now indicates task failure.

## v1.14.2 — fix silently-failing scheduled rescans (route to `dvr` queue)

Scheduled rescans have been silently failing since at least v1.4 — beat fires the task at 03:00, the default celery worker receives it, looks up `vod2mlib.scheduled_rescan` in its registry, doesn't find it (plugins live outside `INSTALLED_APPS`, the upstream hotfix in `dispatcharr/celery.py` crashes with `AppRegistryNotReady` at module-import time, and `apps.plugins.AppConfig.ready()` short-circuits via `should_skip_initialization()` when `'celery'` is in argv), and raises `KeyError: 'vod2mlib.scheduled_rescan'`. The `total_run_count` on the periodic task still increments because that field counts beat dispatches, not successful worker executions — so the failure looks invisible from `[SCHEDULE] Show status`.

Empirically, Dispatcharr's `dvr` celery worker DOES end up with plugin tasks registered (verified via `celery inspect registered`). v1.14.2 routes the scheduled task and `[SCHEDULE] Test fire now` to the `dvr` queue so the worker that actually has the task registered picks it up. The `dvr` worker is configured with a 20-thread pool, so this has no practical impact on DVR recording capacity.

**If you set up your schedule on v1.14.1 or earlier**, click `[SCHEDULE] Apply / Update` once after upgrading. That rewrites the stored PeriodicTask to set `queue='dvr'`. Without that re-click, the existing record keeps routing to the default queue (where the failure happens).

This is a workaround for an upstream Dispatcharr bug ([#1244](https://github.com/Dispatcharr/Dispatcharr/issues/1244) / [#1245](https://github.com/Dispatcharr/Dispatcharr/pull/1245)). Once Dispatcharr properly registers plugin tasks in the default worker, the `queue='dvr'` routing can be removed.

## v1.14.1 — Test fire is async; shorter Movies action description

Two small fixes:

- **`[SCHEDULE] Test fire now` no longer times out.** Previously the handler ran the scheduled action synchronously inside the HTTP request, which now (since the v1.13.0 URL-refresh work made `rescan_all` heavier) routinely exceeded nginx's 60s proxy timeout — users saw a `504 Gateway Time-out` toast. The new implementation enqueues the same Celery task that beat dispatches on a cron tick, returning the task id immediately. Verify completion via `[SCHEDULE] Show status` (the `last_run_at` field updates when the worker finishes).
- **`[GENERATE] Movies` action description shortened** back to the v1.12.0 wording. The v1.14.0 description ran over two lines in the UI, which trips Dispatcharr's flex-wrap behaviour and pushes the Run button below the title. The "use Full rescan to refresh URLs" tip now lives only in the README/CHANGELOG.

## v1.14.0 — drop the `Refresh Existing Movies` setting

Removes the user-visible `Refresh Existing Movies` toggle added in v1.13.0. The URL-refresh capability for movies stays — but it's now only triggered by `[GENERATE] Full rescan` (and the cron schedule's `rescan_all` target), via an internal kwarg on `_generate_movies` rather than a saved setting. The toggle was almost always redundant: anyone who wanted to refresh URLs would run Full rescan anyway, and that path already forces the refresh internally.

Net behaviour for users:
- `[GENERATE] Movies` (button or cron target) — same as v1.12.0 and earlier: always skips existing `.strm`.
- `[GENERATE] Full rescan` (button or cron target `rescan_all`) — same as v1.13.0: rewrites all existing `.strm` with the current Dispatcharr URL, preserves `.nfo` edits.
- The `Refresh Existing Series` setting is unchanged (still does what it did in v1.13.0 — picks up new episodes AND refreshes existing episode URLs).

If you previously saved `refresh_existing_movies: true` in plugin settings, it's silently ignored after upgrading — no breakage. Just rely on Full rescan when you need a URL refresh.

## v1.13.0 — URL refresh & user-edit preservation

Fix stale `.strm` URLs after changing `Dispatcharr URL`. Previously, once a movie or episode `.strm` was on disk, the plugin would never rewrite it — so editing the URL setting left every existing file pointing at the old address. The only workaround was `[⚠ DANGER] Clean up` followed by regenerate, which also wiped user-added `.nfo` edits.

New `Refresh Existing Movies (URL refresh)` setting (default OFF, mirrors the existing `Refresh Existing Series` toggle) — when ON, `[GENERATE] Movies` rewrites existing `.strm` files with the current Dispatcharr URL while preserving `.nfo` edits (only writes `.nfo` when missing). In refresh mode, `Batch Size` caps total writes (new + refreshed) so large URL-refresh runs can be paced over multiple ticks.

`Refresh Existing Series` now also rewrites existing episode `.strm` URLs (previously it only picked up *new* episodes — existing episodes silently kept the old URL). `tvshow.nfo` is no longer overwritten in refresh mode, preserving user metadata edits. Episode `.nfo` was already preserved.

`[GENERATE] Full rescan` and the cron schedule (`rescan_all`) force both refresh flags ON, so URL changes propagate automatically on the next scheduled tick — no manual intervention or cleanup-then-regenerate dance required.

## v1.12.0

New `Schedule Timezone` setting (IANA name like `Europe/London`, `America/New_York`; default empty = UTC). The cron expression is now interpreted in that timezone, so `0 3 * * *` in `Europe/London` fires at 03:00 local time year-round — DST handled automatically. `[SCHEDULE] Show status` now reports the timezone alongside the cron. New `help_url` manifest field pointing at the README — Dispatcharr's plugin tile renders this as a link next to the author name, so users can find the docs without leaving the UI. Validator (`_validate_timezone`) rejects invalid IANA names at Apply time with a helpful error pointing at the timezone list. 7 new unit tests (113 total).

## v1.11.0

Optional category-nested folder layout. Two new boolean settings (both default OFF): `Nest Movies by Category` and `Nest Series by Category`. When ON, each item's folder is wrapped in a subfolder named by its raw M3U category — useful when your provider organises content by genre. Items without a category land under `Unassigned/`. Items present under multiple categories (e.g. 4K vs HD) get separate folders intentionally. Cleanup actions refactored to walk recursively (`os.walk`) so they handle both flat and nested layouts in one pass — empty Season / series / category folders are removed bottom-up, user-added files (subtitles, posters, extras) are still preserved. **Layout-change warning:** flipping a `Nest by Category` setting after generation does NOT migrate existing folders — the new layout coexists alongside the old. Run `[⚠ DANGER] Clean up Movies` / `Series` followed by `[GENERATE]` to fully switch layouts. 17 new unit tests (106 total).

## v1.10.1

Year-bucket category names like `2026 Movies` / `1990s Series` / `2026 TV Shows` are no longer emitted as fake genres. These come from IPTV providers that organize their VOD catalogue by year rather than by genre — passing them through to media servers actively confuses genre browsing in Plex/Jellyfin/Kodi. Now: when the only category-derived genre would be a year-bucket, no `<genre>` tag is emitted at all. Plex/Jellyfin/CDVR will fetch real genres from TMDB themselves via the `<tmdbid>` we already emit. Real categorical genres (`Action`, `Drama, Crime`, `Action / Adventure`) pass through unchanged. Mixed cases (`Action / 2026 Movies`) keep the real part and drop the bucket. 13 new unit tests (89 total).

## v1.10.0

NFO files now emit external IDs and richer metadata, dramatically improving identification by ChannelsDVR / Jellyfin / Plex / Kodi / Emby. `tvshow.nfo` and `episode.nfo` get `<tmdbid>`, `<imdbid>`, and Kodi-style `<uniqueid type="tmdb"|"imdb">` (movie NFO already had IDs; gets `<uniqueid>` now too). Series and episode NFOs additionally get `<rating>`. Episode NFO gets `<aired>` (from `air_date`) and `<runtime>` (from `duration_secs`). Genre selection now prefers Dispatcharr's DB-stored `Series.genre` / `Movie.genre` (TMDB-grade values like "Sci-Fi & Fantasy") over the M3U-category-derived genre (which often produced unhelpful values like "Australian Tv"). Falls back to the category when the DB field is empty. 22 new unit tests (76 total). Existing folders need a regenerate to pick up the richer NFOs — `[⚠ DANGER] Clean up Movies` / `Series` then `[GENERATE]` will refresh them.

## v1.9.4

Closes a real footgun reported on the Dispatcharr Discord against the legacy v1.3 plugin: a user edited the Dispatcharr URL field but never clicked Save, so every `.strm` file silently shipped the placeholder URL `http://192.168.99.11:9191` and nothing played. Now: the Python class default is empty, the placeholder `http://192.168.99.11:9191` is rejected on action with a clear error, and a fresh installer is forced to set the URL before anything generates. As a separate concession to host-network setups, the localhost reject is downgraded to a warning — a setup with Dispatcharr and the consumer on the same host with shared network namespace can legitimately use `localhost`/`127.0.0.1`. Validation is centralised in `_validate_dispatcharr_url`, with 9 new unit tests (54 total).

## v1.9.3

Replaced the auto-generated V-on-gradient `logo.png` with custom pixel-art artwork (a CRT showing "VOD" with a download arrow into a `.STRM` file). Source kept at `tools/source_logo.png`; `tools/build_logo.py` resizes to 512×512 with NEAREST resampling.

## v1.9.2

Display name changed from `VOD2MLIB` to `VOD to Media Library`. Slug, repo URL, install folder name, and Celery task identifiers all unchanged.

## v1.9.1

Trimmed `[GENERATE] Full rescan` and `[SCHEDULE] Test fire now` descriptions to keep their Run buttons right-aligned.

## v1.9.0

NFO titles no longer include the year (Kodi/Jellyfin scrapers prefer just the title). Shared language-prefix regex between `_clean_title` and `_extract_genres`. `_generate_movies` now uses `query.iterator()` so the batch limit is honoured even when most candidates are already-done. Magic numbers promoted to class constants. New `[SCHEDULE] Test fire now` action. Failed series names surface in the rescan summary. New `tests/` directory with 45 unit tests.

## v1.8.x

Section dividers on Settings tab, action labels match the design's renaming map, full-rescan confirm dialog, `[BRACKET]` style headers.

## v1.7.x

UI clarity: button colors, confirm dialogs in Python class, accurate descriptions, `Rescan all` forces refresh-existing.

## v1.6

Rescan-friendly: per-episode skip, optional M3U re-fetch, `Refresh Existing Series` toggle. Schedule rescans now actually pick up new episodes.

## v1.5

Submission-ready: `plugin.json` manifest, MIT/attribution, `__init__.py`. Bug fixes: duplicate-year folders, AC-130 over-strip, batch-limit unreachable for series, episode query at DB level, `checkbox` → `boolean`, scan counts unique not relations. Cleanup is now non-destructive.

## v1.4

Cron-driven auto-rescan via `django-celery-beat`. New `Rescan All` action.

## v1.3 and earlier

See [shedunraid's upstream](https://github.com/shedunraid/VOD2MLIB) for the original v0.x–v1.3 history.
