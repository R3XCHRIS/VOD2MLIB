<p align="center">
  <img src="logo.png" alt="VOD to Media Library" width="200">
</p>

<h1 align="center">VOD to Media Library</h1>

<p align="center">A Dispatcharr plugin that turns your VOD catalogue into a folder of <code>.strm</code> files (with optional NFO metadata) that media servers — Jellyfin, Emby, Kodi, ChannelsDVR — can index and play.</p>

<p align="center">
  <i>v1.18.0 — slug <code>vod2mlib</code></i>
</p>

> **Note on scheduled rescans.** The cron task routes via Dispatcharr's `dvr` Celery worker as a workaround for an upstream plugin-task-registration issue affecting the default prefork worker pool ([Dispatcharr#1244](https://github.com/Dispatcharr/Dispatcharr/issues/1244)). The routing is transparent — no user action required for new installs. Existing schedules retain their enabled state and automatically adopt the current routing after upgrading.

> **Plex users:** Plex does *not* play `.strm` files. Jellyfin and ChannelsDVR do. See [Plex compatibility](#plex-compatibility) below.

## Credits

- **Original author:** [shedunraid](https://github.com/shedunraid) — created v0.x–v1.3 ([upstream repo](https://github.com/shedunraid/VOD2MLIB)).
- **Fork maintainer:** [R3XCHRIS](https://github.com/R3XCHRIS) — v1.4+ adds scheduling and bug fixes. Listed in the [official Dispatcharr Plugins catalogue](https://github.com/Dispatcharr/Plugins/tree/main/plugins/vod2mlib) since v1.14.3. Upstream has been dormant since early 2026; this fork continues maintenance.
- MIT License.

---

## Media-library reconciliation (1.19.0-rc.13)

Optional integration with one Emby server prevents generated STRMs from duplicating real media. Integration is disabled by default. Configure the server URL and API key, enable integration, then use **List media libraries** to find names/IDs and enter the real-media libraries you want checked. Explicit library names or IDs are required; there is no all-libraries option. Leave generated VOD/STRM libraries out to avoid fetching and discarding their contents. Empty, missing or ambiguous selections stop actions even with the continue-on-server-failure policy. Existing installations must save explicit library names or IDs. Names are resolved on every run (case-insensitive exact matching), so a recreated library with the same name uses its new ID. Duplicate names require an ID. STRM-only, remote and virtual Emby entries do not count as owned; a movie with both a real file and a STRM does.

Movies and shows match separately, first by TMDB or IMDb ID. When comparable IDs are unavailable, an exact cleaned title and known matching year can match. Conflicting IDs, unknown years and fuzzy titles are retained. A show counts as owned only when Emby has actual non-STRM episodes. **Skip entire owned show** is the default; **Fill missing episodes** compares season/episode positions, including specials and multi-episode files. Uncertain positions are retained in missing-episode mode.

Existing duplicate cleanup defaults to every generation. It runs before generation across the tracked output, independently of batch limits; full rescans share one complete paginated Emby snapshot across both generators. Cleanup can instead run only on full rescans or be disabled. Matching still prevents generation of owned content when automatic cleanup is disabled. When real media disappears from Emby, normal generation can restore eligible STRMs; use a full rescan or Refresh Existing Series to revisit already-processed series. Missing-episode mode automatically revisits existing series folders.

**Preview selective cleanup** logs candidates without deleting output files. **Run selective cleanup** applies the configured server and M3U checks immediately, overriding their automatic timing. Preview refreshes the Dispatcharr episode catalogue when M3U cleanup is enabled and may initialize/adopt inventory records.

M3U-removal cleanup is separately disabled by default. When enabled, it uses the complete Dispatcharr catalogue, including content outside current generation batches and disabled categories. It uses account/provider stream IDs rather than UUIDs alone. Full rescans are the default timing; manual-only timing is available. Confirmed removals are deleted on the first complete check, with no grace period. Episode refresh responses and completion are verified; failed or incomplete queries/refreshes never establish absence. Native Dispatcharr category selections still determine generation eligibility.

The default deletion scope is **STRMs only**. The optional **STRMs and unedited generated NFOs** scope also deletes sidecars whose recorded generated hashes still match. STRM ownership uses the recorded URL text, not a hash. Edited STRMs/NFOs, unverified legacy NFOs, artwork, subtitles and unrelated files are preserved. Empty directories can be removed; roots are retained. Recognizable legacy Dispatcharr STRMs are adopted using their proxy URLs and complete catalogue metadata; ambiguous files are preserved and counted. The existing Movies/Series cleanup actions also use these ownership checks.

On an Emby failure, **Continue with warning** is the default: generation proceeds without server exclusions or server deletion, and the warning appears in the action result. **Stop before file changes** aborts the action before changing output files. M3U cleanup remains independent under the continue policy. Results and logs report exclusions, deletions, preserved files and errors.

A plugin-owned SQLite inventory lives at `/data/vod2mlib/inventory.sqlite3`, beside `/data/plugins`, outside plugin installations and media roots. Keep Dispatcharr's `/data` volume persistent across upgrades. An operator can override the state directory with `VOD2MLIB_STATE_DIR`; use persistent local storage outside both media roots and the plugin installation. The inventory uses schema versioning, indexed source/metadata/path lookups, bulk `executemany()` writes in transactions of up to 1,000 records, bounded queues and streamed catalogue reads. Batched lookups are split to respect SQLite parameter limits. Parent-thread writes and a process lock serialize generation and cleanup. Only generated NFOs are hashed; media files are never hashed. No new runtime dependencies are required.

Save integration or cleanup changes in Settings; manual actions and scheduled jobs read the same current saved configuration at run start. Jellyfin and Plex adapters are not included in this prerelease.

---

## Install

1. **Map a host folder to `/VODS` in your Dispatcharr container** (see [Sharing the VODs folder](#sharing-the-vods-folder-with-media-servers) for *why* this matters and how to share with other apps).

   ```yaml
   # docker-compose.yml
   services:
     dispatcharr:
       volumes:
         - /opt/dispatcharr-vods:/VODS
   ```

2. **Install the plugin** — two options:

   - **From the official catalogue (recommended):** Dispatcharr → Plugins → **Find Plugins** → search "VOD to Media Library" → Install. Updates also surface here.
   - **Manual:** download `plugin-vod2mlib-v<version>.zip` from a [GitHub release](https://github.com/R3XCHRIS/VOD2MLIB/releases), then Dispatcharr → Plugins → **Import** → upload the zip.

3. Enable the plugin from the Plugins tab.

Requires Dispatcharr **v0.24.0** or later. The auto-rescan feature additionally needs `django-celery-beat` (Dispatcharr ships with it).

---

## Sharing the VODs folder with media servers

This is the part most people get wrong on first try.

The plugin runs **inside the Dispatcharr container**. When it writes `/VODS/Movies/Aladdin (1992)/Aladdin (1992).strm`, that path exists inside the container's filesystem. For Jellyfin / ChannelsDVR / Kodi to find that file, **the same data has to be visible to them too** — either as a bind-mounted volume on the same host, or via a network share.

**Three common patterns**, pick whichever matches your setup:

### 1. Same host, both apps in Docker (recommended)

Bind-mount the same host directory into both containers. The plugin writes; the media server reads.

```yaml
services:
  dispatcharr:
    volumes:
      - /opt/dispatcharr-vods:/VODS    # plugin writes here

  jellyfin:
    volumes:
      - /opt/dispatcharr-vods:/data/vods:ro    # read-only mount
    # then in Jellyfin: Add Library → Movies → /data/vods/Movies
    #                                  Shows  → /data/vods/Series
```

`:ro` (read-only) is good practice for the consumer — guarantees Jellyfin can't accidentally modify the plugin's output.

### 2. Media server on the same host, *not* in Docker

Just point the media server at the host path directly:

```
/opt/dispatcharr-vods/Movies   # for Movies library
/opt/dispatcharr-vods/Series   # for Series library
```

Watch out for **file permissions** — the Dispatcharr container writes as its own UID (often `1000`/`dispatch`). If your media server runs under a different user, it may not be able to read the `.strm` files. Easiest fix: align UIDs, or `chmod -R a+r /opt/dispatcharr-vods`.

### 3. Media server on a different host

Export the directory over NFS/SMB from the host running Dispatcharr, mount it on the host running the media server.

```bash
# On the Dispatcharr host (Linux + NFS):
echo "/opt/dispatcharr-vods 192.168.1.0/24(ro,sync,no_subtree_check)" >> /etc/exports
sudo exportfs -ra

# On the media server host:
sudo mount -t nfs dispatcharr-host:/opt/dispatcharr-vods /mnt/vods
# ... then point Jellyfin/Plex/Emby at /mnt/vods/{Movies,Series}
```

SMB works equally well; pick whatever your stack already uses.

### One critical setting either way

The `Dispatcharr URL` in plugin settings is **baked into every `.strm` file** — it's the URL the media server's player follows when you press Play. It MUST be reachable from wherever your media server runs:

- Same host: a LAN IP works (e.g. `http://192.168.1.10:9191`).
- Different host on same LAN: still a LAN IP, just make sure routing/firewall allows it.
- Different network: a routable hostname/IP, possibly via Tailscale, VPN, or reverse proxy.

`localhost` / `127.0.0.1` will not work — your media server is a different process, possibly on a different machine. The plugin actively rejects this.

---

## Settings

The Settings tab is grouped into four sections:

| Section | Field | What it does |
|---|---|---|
| **Paths & hosts** | Root Folder for Movies / Series | Paths inside the container (defaults `/VODS/Movies`, `/VODS/Series`) |
|  | Dispatcharr URL | Externally-reachable URL of Dispatcharr (NOT `localhost`). Baked into every `.strm`. |
| **Movies** | Batch Size | How many movies to process per click |
|  | Generate Movie NFO Files | Toggle Kodi/Jellyfin metadata generation |
|  | Omit `<title>` from NFO files | Leave the title out of movie/tvshow NFOs so Jellyfin/Emby take it from TMDB instead. Useful when your provider prefixes titles (`4K-A+`, `EN-TOP`, `AMZ`) — Jellyfin treats an NFO `<title>` as authoritative and won't override it. Off by default. |
|  | Nest Movies by Category | Wrap each movie folder inside a subfolder named by its M3U category (off by default; movies without a category go to `Unassigned/`) |
|  | Dedupe Movies Across Categories | When nesting is ON and a movie is tagged with multiple categories upstream, write under the first category only (alphabetical) instead of duplicating. No effect when nesting is OFF. Off by default (preserves 4K-vs-HD variant-stream behaviour). ⚠ Doesn't remove existing duplicate folders — `[⚠ DANGER] Clean up` + re-generate to migrate. |
|  | Append TMDB ID to folder names | Append a TMDB id tag to Movie *and* Series folder names when a TMDB ID is known — e.g. `Cool Hand Luke (1967) {tmdb-378}/`. Media servers honour this as a forced exact metadata match. Off by default. ⚠ Doesn't rename existing folders in place — writes new names alongside the old ones; `[⚠ DANGER] Clean up` + re-generate to migrate cleanly. |
|  | TMDB Folder Tag Format | Which tag convention to write: **Plex / ChannelsDVR** `{tmdb-123}` (default) or **Jellyfin / Emby** `[tmdbid-123]`. Each server ignores the other's format — Jellyfin/Emby users should switch this. Only applies when the setting above is ON. |
|  | Don't pin .strm to a specific stream | Omit `?stream_id=` from `.strm` URLs so Dispatcharr can fail over across providers. Off by default; only useful with a Dispatcharr build that has VOD failover ([#1398](https://github.com/Dispatcharr/Dispatcharr/pull/1398)). |
| **Series** | Batch Size (Series) | How many series to process per click |
|  | Generate Series NFO Files | Toggle `tvshow.nfo` and per-episode `.nfo` |
|  | Refresh Existing Series | Re-evaluate already-processed series using stored Dispatcharr episodes and refresh changed episode `.strm` URLs (cron-friendly), without provider requests. Preserves `tvshow.nfo` and episode `.nfo` edits. |
|  | Nest Series by Category | Wrap each series folder inside a subfolder named by its M3U category (off by default; series without a category go to `Unassigned/`) |
|  | Dedupe Series Across Categories | When nesting is ON and a series is tagged with multiple categories upstream, write under the first category only (alphabetical) instead of duplicating. No effect when nesting is OFF. Off by default. ⚠ Doesn't remove existing duplicate folders — `[⚠ DANGER] Clean up` + re-generate to migrate. |
| **Enable Auto-Rescan** | Schedule enabled | Off for new installs; enable and Save with a valid cron to schedule jobs |
| **Auto-rescan schedule** | Schedule (cron) | Standard 5-field expression. Default `0 3 * * *` (daily 03:00) |
|  | Schedule Timezone | IANA timezone the cron is interpreted in (e.g. `Europe/London`). Empty = UTC. Handles DST automatically. |
|  | Scheduled Action | What the cron fires (full rescan recommended) |

VOD category selection uses Dispatcharr’s native per-account category settings. Scan and generation require an active M3U account and an enabled category for that account and VOD type. The former plugin Category Filter and Category Exclude settings have been removed; saved values are ignored. Existing generated files remain in place when a category is disabled.

## Workflow

**First run.** Configure paths → click `[LIBRARY] Catalogue snapshot` to verify the plugin can see your VODs → click `[GENERATE] Movies` with Batch Size 10 → spot-check the output → scale up.

**Scaling up.** Increase Batch Size, click again. Existing files are skipped, so each click only processes new ones. (If you need to refresh URLs in already-generated files — typically after changing the `Dispatcharr URL` setting — use `[GENERATE] Full rescan` instead; it rewrites all existing `.strm` while preserving your `.nfo` edits.)

**Auto-rescan.**
1. Turn ON **Refresh Existing Series**.
2. Set **Scheduled Action** to **Full rescan**.
3. Turn **Enable Auto-Rescan** ON and click **Save**. A valid cron and timezone are required before scheduling can be enabled.
4. Verify with `[SCHEDULE] Show status` — last run / total runs populate after the first cron tick.
5. Optional: click `[SCHEDULE] Test fire now` to immediately replay the scheduled action without waiting for the next cron tick.

Manual actions, scheduled runs, and Test fire use one configuration: the current saved plugin settings, with the same defaults. Save changes to paths, batching, concurrency, integration, cleanup, filters, timeout, or the scheduled action; they apply to the next run. Save also registers or updates the cron time and timezone. Turn **Enable Auto-Rescan** OFF and Save to use manual actions only; queued ticks are skipped, and a running action keeps the configuration it started with. New installations default to scheduling disabled, while existing schedules retain their enabled state. Existing task snapshots are ignored and cleared. Invalid enabled-schedule settings are rejected before saving.

## Plex compatibility

Plex does **not** play `.strm` files (it can index them but the URL inside doesn't play). This is a long-standing Plex limitation — it's been an unfulfilled feature request for 5+ years.

Workable alternatives:

- **Jellyfin alongside Plex.** Jellyfin plays `.strm` natively. Run it in a container next to Plex, point both at the same library folder (see [Sharing the VODs folder](#sharing-the-vods-folder-with-media-servers) above).
- **ChannelsDVR's Personal Media** — works perfectly out of the box. Point CDVR at the Movies/Series root.
- **Kodi** — works.
- **Emby** — works.

## Troubleshooting

**"Unknown action" error in the toast.** Dispatcharr cached an old version of the plugin module. `docker restart dispatcharr` clears it. Toggling enable/disable on the plugin also forces a reload.

**The Run button drops below the action title instead of right-aligning.** That's Dispatcharr's UI flex-wrap when the description spans 2+ lines. We keep descriptions single-line to avoid this; if it happens again, the description is too long for your viewport.

**Cron task registered but didn't fire.** Check `[SCHEDULE] Show status` — `last_run` should populate after the first scheduled tick. If still `never` after the expected time:
- Verify Celery beat is running in your Dispatcharr deployment.
- Check container logs for `core.scheduling Updated periodic task 'vod2mlib.auto_rescan'`.
- Click `[SCHEDULE] Test fire now` to confirm the task itself works (proves it's a scheduling-layer issue, not a plugin issue).

**Schedule fires but no new files appear.** Most likely: `Refresh Existing Series` is OFF and your existing series already have folders, so the cron only adds *new* series. Toggle Refresh Existing ON and Save; the next run uses the change.

**Media server can't see the generated files at all.** The host path isn't shared with the media server's process. See [Sharing the VODs folder](#sharing-the-vods-folder-with-media-servers).

**Media server sees the files but playback fails immediately.** Open one of the `.strm` files in a text editor — it contains a single URL. Try fetching that URL from the machine running your media server (`curl -I <url>`). If that fails, the `Dispatcharr URL` setting isn't reachable from there. Fix the URL, then run `[GENERATE] Full rescan` — every existing `.strm` is rewritten with the new URL, and your `.nfo` edits are preserved. (Pre-v1.13.0 you had to `[⚠ DANGER] Clean up` then regenerate, which also wiped any user `.nfo` edits.)

**Playback worked initially but starts failing after a few days / after a Dispatcharr refresh.** (Symptom: Emby/Jellyfin reports "No compatible streams" on titles that previously played fine; CDVR reports 404s on files that worked yesterday.) Upstream Dispatcharr bug — VOD movie/episode UUIDs are regenerated on every M3U refresh, so the URLs your media server cached at library-scan time become orphaned ([Dispatcharr#961](https://github.com/Dispatcharr/Dispatcharr/issues/961)). The plugin can't fix this externally — rewriting `.strm` files doesn't help because Emby/Jellyfin only re-reads them at library-scan time, not on playback retry. The read-side fix [Dispatcharr#1315](https://github.com/Dispatcharr/Dispatcharr/pull/1315) is **merged to `dev`** (verified working in production): switch your Dispatcharr container from `:latest` to `:dev` and dead-UUID requests will resolve via the stable `stream_id` that every VOD2MLIB URL already carries.

```yaml
# docker-compose.yml
services:
  dispatcharr:
    image: ghcr.io/dispatcharr/dispatcharr:dev    # was :latest
    # ...rest of your config
```

Closed [Dispatcharr#973](https://github.com/Dispatcharr/Dispatcharr/pull/973) would be the complementary write-side root fix (preserves UUIDs across refresh instead of just tolerating the orphaning); it's stalled and needs reviving. This note will be removed once a tagged Dispatcharr release contains the fix.

**Jellyfin/Emby downloads tens of GB of images after adding a VOD library.** Not a plugin issue, but it bites hard: media servers fetch artwork for *every* item, and a large VOD library can pull 70 GB+ before you notice.

The important part is **turn off the library's *metadata downloaders*, not just its image fetchers.** Unticking image fetchers stops posters and backdrops, but **cast/crew ("people") images are fetched separately and there is currently no setting to disable them** in Jellyfin — it's a standing [feature request](https://features.jellyfin.org/posts/1646/disable-actors-metadata), and excluding a provider from the library's image fetchers [does not stop them](https://forum.jellyfin.org/t-exclude-tvdb-people-cast-crew-images). With thousands of titles, those people images are a large share of the total. Turning the *metadata downloaders* off means Jellyfin never builds a cast list for the item in the first place, so there are no people to fetch images for.

That works here because **this plugin's NFOs already carry the metadata**: title, year, genre(s), plot, rating, TMDB id, and a poster URL. So you can point Jellyfin at a VOD library with online metadata and image fetching fully off and still get a populated, artworked library — it reads what's in the `.nfo` instead of going to the internet per item. (VOD2MLIB never writes `<actor>` entries, so nothing here creates people records.)

In Jellyfin: Dashboard → Libraries → (your VOD library) → Manage Library, then untick the metadata downloaders and image fetchers. Do this **before** the first scan. If you've already been hit, delete the cached images and re-scan with them off — a routine scan won't re-fetch them, but note that a *metadata refresh* will (check the library's periodic-refresh cadence), and deleted cast images are re-fetched lazily when someone clicks a blank actor tile, [which happens even with online providers disabled](https://github.com/jellyfin/jellyfin/issues/8288). Turning the metadata downloaders off avoids that too, since no cast list is built in the first place.

**Want to browse and hand-pick VOD into Emby rather than import everything?** [VodLink](https://github.com/jdfrey1/vodlink) reads this plugin's `.strm` + `.nfo` output and lets you browse/search your VOD catalogue and link individual movies and series into an Emby library directory, instead of pointing Emby at the whole generated tree. It also runs a stream proxy that converts `HEAD` to `GET` and caches Dispatcharr session URLs, so seeking and resume behave. Emby-specific, Docker-based. Keep `Generate NFO Files` ON if you use it, since it reads those `.nfo` files. To choose which categories generate, enable or disable VOD categories for each M3U account in Dispatcharr.

**"All profiles at capacity" error when playing on TiviMate / Android.** Not a `.strm` issue — this is a known Dispatcharr connection-counting bug ([Dispatcharr #451](https://github.com/Dispatcharr/Dispatcharr/issues/451)). TiviMate (and similar Android players) makes multiple simultaneous Range requests to probe a file before playback; Dispatcharr counts each request as a separate provider connection, blowing through `max_streams=1` before playback even starts. The community plugin [`dispatcharr_vod_fix`](https://github.com/cedric-marcoux/dispatcharr_vod_fix) patches Dispatcharr's request handling to track slots by (client IP + content UUID) so multiple Range requests share one slot. Install it alongside this plugin if your Android clients can't play VOD content.

**Folders named `Aladdin (2026) (2026)` (duplicate year).** This was a bug in v1.4 and earlier. Fixed in v1.5+ but pre-existing duplicate-year folders aren't auto-renamed. Run `[⚠ DANGER] Clean up Movies` once to remove them, then re-run `[GENERATE] Movies` to regenerate cleanly. (Cleanup deletes only `.strm`/`.nfo` — user-added subtitles/posters survive.)

**Generate Series fails for some series.** The summary lists the failed series names with their errors. Common causes: database read errors or malformed stored episode metadata. The plugin continues with the rest of the batch.

**`localhost`/`127.0.0.1` in Dispatcharr URL.** The plugin refuses to write `.strm` with a localhost URL — your media server can't resolve it. Use the container's reachable IP/hostname.

## Development

Pure-helper unit tests live in `tests/`. From the repo root:

```bash
python3 -m pytest tests/ -v
```

The tests don't need Django or a running Dispatcharr — they exercise `_clean_title`, `_strip_trailing_year`, `_sanitize_filename`, `_parse_cron`, `_extract_genres`, `_mask_url`, and the path-building helpers in isolation. 45 tests, ~50ms.

The bundled logo is reproducible — replace `tools/source_logo.png` and run `python3 tools/build_logo.py` to regenerate `logo.png` at 512×512 with NEAREST resampling (preserves pixel-art crispness).

## Architecture (for contributors)

- The plugin is a single `plugin.py` declaring a `Plugin` class with `fields`, `actions`, and `run()` per Dispatcharr's plugin contract.
- `plugin.json` is the manifest the [Dispatcharr/Plugins catalogue](https://github.com/Dispatcharr/Plugins) reads. Dispatcharr's runtime reads action metadata from the Python class — the JSON is for the catalogue and pre-enable preview.
- Schedule registration uses `django-celery-beat`'s `PeriodicTask` + `CrontabSchedule`. The cron-fired task is a module-level `@shared_task` named `vod2mlib.scheduled_rescan` that constructs a fresh `Plugin()` and dispatches.
- PeriodicTask stores only the registered trigger. The worker reads current saved plugin settings and the selected scheduled action at run start; old task snapshots are ignored. Plugin-owned Django save signals validate and synchronize the cron trigger; Dispatcharr source code is unchanged.

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for the full release history.

### Background actions and cancellation

Generation, library listing, preview, and cleanup run in isolated background processes. The action button returns immediately; use **[ACTION] Status** to read the final result, exclusions, deletions, and warnings. **[ACTION] Stop running action** cancels the action and its worker group within a few seconds without restarting Dispatcharr or its stream workers.

**Maximum action runtime (minutes)** defaults to 30 and applies to manual and scheduled actions. A separate supervisor enforces the deadline even during blocked Emby HTTP/DNS and generation threads. Cancellation keeps completed file changes; the OS releases the SQLite/action locks and the next run reconciles inventory and missing files. Both entry points use the current saved limit. Settings passed to workers are stored temporarily with private permissions, removed when the action exits, and never included in status output or command arguments.

M3U cleanup checks tracked sources against Dispatcharr's complete, unfiltered database catalogue. It never contacts VOD providers or updates native metadata. Failed database checks disable M3U deletion for that run. A running action from an older plugin version cannot be cancelled by the new supervisor; its original process must finish or be recycled once.

Action status includes the current phase, Emby snapshot item counts, elapsed minutes, and remaining deadline. Source-presence lookup uses indexed, bounded database projections.

Emby snapshots fetch up to 20,000 items per page with a 30-second request timeout. Complete-pagination checks still discard interrupted, changed, or repeated snapshots; the action deadline bounds the entire operation.

Bulk listings request provider IDs and paths rather than playback MediaSources. The global `/Items` endpoint returns file versions as separate records; STRM versions are ignored by their paths. The first page and a final count-only request validate the total; intermediate pages disable repeated total counts. Each page must make progress, contain unique IDs, and stay within the initial count. The complete snapshot remains unusable until the final check succeeds.

The complete Dispatcharr catalogue census projects only identity, source, UUID, and episode-position columns. It joins media records without unrelated account metadata. Cleanup uses a complete database source census, and status reports processed row counts. Existing tracked paths skip legacy-adoption checks; deletion still verifies ownership and containment at the point of removal.

Catalogue reads stream projected tuples rather than constructing Django models for each row. Action-local caches hold at most 8,192 identities and show/account membership checks; repeated episodes reuse cleaned metadata without retaining the complete catalogue in memory. SQLite still writes in transactions of up to 1,000 rows.

Action results include local timing telemetry: wall seconds, worker-process CPU seconds, batch counts and row counts, with the worker PID. Action status shows a short phase summary after completion; detailed metrics are retained in the completed result and `/data/vod2mlib/timings.json` (latest run, private permissions). Timing summaries are also emitted through the action logger. No credentials, settings, URLs or file paths are recorded in timing telemetry, and nothing is sent externally. Phase totals contain their detailed child measurements, so do not add parent and child durations together. Worker CPU time includes its threads but excludes separate provider/Dispatcharr processes; total timing excludes subprocess startup.

Series actions publish live completed-series and evaluated-episode counters approximately every two seconds through Action status. Timings cover database episode loading, SQL execution (`episode_sql`), episode-cache reads, output processing, STRM/NFO work, and inventory record/checkpoint batches. `episode_query` includes query execution and Django model hydration; `episode_load` includes the complete episode read, so these nested timings must not be added. SQL execution excludes some result fetching/decoding work. Concurrent worker measurements sum durations and can exceed elapsed action time; worker CPU uses thread CPU time, while action totals use process CPU time. Live timings contain completed calls only. No provider-fetch or native metadata-write phase occurs.

Series generation prepares each season directory once per show and hashes the show NFO from the same bytes used to verify its generated contents. Ownership checks, containment validation, inventory STRM verification and refresh frequency remain in effect. Series concurrency defaults to three workers and is configurable as described below.

Catalogue lookup indices are built once after the complete streamed load, before adoption or cleanup. Temporary SQLite tables remain on disk with a bounded 32 MiB page cache; persistent inventory transactions and durability are unchanged. Generated-output discovery uses directory-entry type information and skips symlink entries. Preview avoids existence checks for non-candidates; actual deletion still verifies file ownership and containment. M3U provider refresh selection skips its series scan if no generated episodes are tracked, after legacy adoption. Live verification on 627,500 catalogue rows reduced a cleanup preview from 87 to about 31 seconds with zero errors/warnings.

**[LIBRARY] Rebuild / discover inventory** rescans configured output roots to adopt recognizable Dispatcharr STRMs copied or restored outside the plugin. It deletes no output files and preserves all existing ownership records and generated NFO hashes, including protection for edited files. New legacy NFOs stay unverified and are preserved. Unrecognized URLs or ambiguous catalogue matches remain unmanaged and are reported. This action checks the complete Dispatcharr catalogue but needs no Emby connection and performs no provider refresh or cleanup; use selective cleanup afterward if wanted. It runs with the same process lock, deadline, cancellation and timing controls as generation.

Output discovery now runs once per successfully scanned root and Dispatcharr connection context. Subsequent syncs, full rescans and cleanup actions reuse persistent inventory without traversing already discovered roots. New roots, changed connection contexts, and a missing/rebuilt inventory trigger discovery automatically. Files generated by the plugin are tracked immediately. Missing roots and incomplete scans are not marked complete; failed forced discovery invalidates its marker and retries on the next run. Manually added files in an already discovered root require the rebuild/discovery action. SQLite schema 2 adds discovery markers and migrates existing schema 1 inventories without changing ownership or NFO hashes. Root markers also migrate during plugin upgrades, so the first run after upgrading performs discovery once.

Routine M3U-removal checks now verify every tracked account/provider source using fresh indexed lookups across Dispatcharr's complete, unfiltered catalogue. They query only media kinds/accounts/source IDs represented in inventory, in groups of up to 900 keys. Unrelated catalogue entries cannot affect deletion of managed output and are not loaded or written into SQLite. Account-specific episode/show membership is verified for found episodes, so orphaned episodes do not falsely establish presence. Checks remain independent of account activity, native category selection and generation batch limits. All queries must finish; an incomplete query or malformed tracked source disables M3U deletion. Valid alternative JSON spellings of the same source establish equivalent presence.

Initial discovery and rebuild retain the complete metadata census. Sources adopted during discovery are included in the targeted database check. There is no cross-run source-presence cache or TTL: removals are detected on the first successful check even immediately after a sync. If M3U checks are not due and no root needs discovery, no census runs. Telemetry includes requested/present source counts and lookup query counts. Live parity validation compared all 16,127 managed sources and absence decisions against the 627,666-row census: identical results, with census time reduced from 7.04 seconds to 0.22 seconds. Presence checks use only database data; the plugin never refreshes provider episodes.


### Incremental generation

Routine generation treats SQLite as authoritative for plugin-managed output. Movies compare a complete, streamed projection of eligible account/provider identities and output-affecting fields against persistent generation decisions. Only new or changed candidates are loaded as full Django records and checked/written on disk. The comparison uses raw values, without hashing STRM URLs. Category eligibility and deterministic deduplication remain native; batch limits apply to pending work, and failed writes or unprocessed candidates are retried. Changes to roots, proxy URL, naming, NFO options or Emby ownership invalidate the relevant decisions. Plugin deletions invalidate decisions immediately, allowing eligible media to return when ownership/source availability changes.

Unchanged generation does not stat every tracked STRM or traverse output directories. Only verified cleanup candidates touch the filesystem. Files added, edited or removed externally require **Rebuild / discover inventory**, which preserves ownership/NFO protection and resets generation decisions. Run generation afterward to recreate missing eligible files. Selective cleanup also verifies missing tracked files. Missing entire output roots invalidate decisions automatically. SQLite schema 3 adds generation decisions and upgrades schema 1/2 in place; the first generation establishes the cache.

TV episode output skips unchanged cached STRMs after reading episode models from Dispatcharr. Refresh Existing Series and full rescan recheck database episodes and managed output; they never fetch provider data. New episodes appear only after Dispatcharr stores them. Missing episode data produces no episode output; refresh/fetch it in Dispatcharr first. Full rescans retain unfiltered database source-presence checks according to cleanup timing. Telemetry reports projected rows checked, unchanged decisions, hydrated candidates and database read time.


### Independent metadata filters

Movies and series have separate **Minimum Score**, **Earliest Year**, **Latest Year**, and **Missing Metadata** settings. Blank score/year bounds disable those rules; all new rules start inactive and missing metadata defaults to **Keep unknowns**. Movie years are release years; series years are debut years from Dispatcharr's model `year` field. Scores must be numeric within 0-10, years must be positive integers, and earliest year must not exceed latest year. Invalid settings stop the action before reconciliation or generation.

Enabled rules combine with AND, and score/year boundaries are inclusive. Missing, zero, invalid, nonfinite, negative, and above-10 model scores are unknown. Missing or invalid years are unknown. **Reject unknowns** applies only to enabled rules; it does not discard titles for unused metadata.

Series also support comma-separated **Genre Include** and **Genre Exclude** lists. Match complete names without case sensitivity: `Action & Adventure` stays one name, as does `Sci-Fi & Fantasy`. Any included genre qualifies; any excluded genre rejects, even if another genre qualifies. Only commas separate names, both in configuration and model metadata. Empty model genres follow the series missing-metadata policy when a genre rule is enabled. Movie genre filtering is unavailable.

Movies and series also have independent **Title Include Regex** and **Title Exclude Regex** settings. Each accepts one Python regular expression, searched case-insensitively in the original Dispatcharr model title before provider tags or years are stripped. Blank disables the rule. Include requires a match; exclude rejects a match and wins over include. Title rules combine with enabled score, year, and genre rules using AND. Invalid patterns stop the action before reconciliation or generation. Missing or blank titles follow that media type's missing-metadata policy only when a title rule is enabled.

For example, `^\s*(AF|AR)\s*[-:|]\s*` matches `AF - Yard Palava`, `AR: Title`, and `AR|Title`; `^\s*\[(AF|AR)\]\s*` matches bracketed tags. Combine alternatives with `|`, use `^` to anchor prefixes, and escape punctuation when it should be literal. Patterns retain their whitespace and commas, including quantifiers such as `{1,2}`. Genre include/exclude remain comma-separated complete names. Use **Catalogue snapshot** to see rejected-title counts before generating; existing files remain in place.

Filters read Dispatcharr model metadata directly, without NFOs, Emby enrichment, title-derived years, or category-derived genres. Provider age classifications such as `PG-13` are unknown numeric scores. Metadata completeness varies by provider; **Keep unknowns** can retain many titles.

**Catalogue snapshot** reports separate movie/series eligible and passing title counts, rejected counts for score/year/genre/title, and passing titles retained with unknown metadata. Counts use unique titles after native account/category eligibility and precede existing-library checks. A title failing multiple rules counts against each, so rejection counts can overlap.

Each generation run first applies current filters to tracked output for its media type, independently of the creation batch limit. Verified generated STRMs that fail are removed, along with matching generated NFOs; edited or unverified files are preserved. Full rescan covers movies and series. **Preview cleanup** reports filter removal candidates without changing files. If any tracked source passes, or a source cannot be resolved, shared output is retained. Missing model metadata follows the configured policy. All bounded metadata lookups must finish before filter deletion begins. Filter removal is separate from upstream absence: source-presence cleanup still checks the complete unfiltered catalogue. Existing Emby/M3U cleanup policies apply independently. Rejected candidates are excluded before deduplication, batch selection, and database episode loading. Incremental signatures include filter settings and metadata so relaxing filters can recreate eligible output.

Save settings and run **Catalogue snapshot** to inspect eligibility, or **Preview cleanup** to inspect existing-file removals. The next manual or scheduled run uses current saved filters. Filter cleanup telemetry reports checked, candidate, deleted, missing, preserved, and error counts; series deletion counts represent episode STRMs. Verified filter NFO removal is automatic and preserves edited or unverified NFOs, independently of the separate cleanup deletion-scope setting.


Parallel Series Workers (under SERIES) defaults to 3 and accepts 1-6 workers. It controls series database-read/generation concurrency; movie concurrency stays at 3. Worker-count changes do not invalidate output signatures. Save changes to use the new value for the next manual or scheduled run. Increase concurrency only after comparing the same workload and checking database and storage errors.

Containment validation checks a lexically matching root first while still resolving the candidate and root on every check, preserving alias and symlink-escape behavior. Series ownership matching is calculated once from stored series metadata and reused for episode positions against the immutable library snapshot. Telemetry separates `output_guard`, `episode_ownership`, `inventory_enqueue`, and `checkpoint_enqueue`; enqueue durations include queue blocking and overhead. Inventory record/checkpoint CPU timings now use the action thread CPU clock to exclude concurrent generation work.

Inventory draining frees both bounded queues before verifying and committing the captured records. Checkpoints are captured first (producers enqueue records before checkpoints), then records are committed before those checkpoints; a failed inventory write cancels the action and commits no captured checkpoints. This lets generation continue during inventory verification without weakening completion guarantees.

For generation equivalence checks, freeze episode/model data and the media-library snapshot, keep Dispatcharr database inputs fixed, and compare output filenames and bytes. Live creation benchmarks can observe changes made by Dispatcharr refreshes: identical settings and series counts do not guarantee identical episode inputs. Investigate changed output by episode identity and provider-added timestamps before attributing it to generation or performance changes.

Generation and cleanup use only movie/series metadata and episode relations already stored in Dispatcharr. The plugin does not instantiate provider clients, call provider APIs or native importers, or change native freshness timestamps/flags. Dispatcharr is responsible for populating and refreshing its catalogue; generation reflects the data available at run time. Emby integration remains an ownership check, not a metadata enrichment source.

Cleanup verifies each STRM and generated NFO before removal. Inventory removals commit in bounded batches with normal SQLite durability; interrupted batches remain retryable. Empty-directory pruning runs once per distinct parent in each batch. Action status reports checked/removed/protected counts and `filter_cleanup_batch` / `cleanup_batch` timings, including verification, filesystem changes, directory pruning and the durable inventory commit. Initial legacy discovery is retained per output root, so subsequent runs reuse the inventory.

Metadata-filter cleanup uses three bounded filesystem workers for STRM verification/removal. Shared NFO handling, SQLite changes and directory pruning stay serialized on the action thread. `cleanup_strm_io` records cumulative worker wall/thread CPU time separately from elapsed batch time.
