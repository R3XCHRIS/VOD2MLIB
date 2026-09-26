<p align="center">
  <img src="logo.png" alt="VOD to Media Library" width="200">
</p>

<h1 align="center">VOD to Media Library</h1>

<p align="center">A Dispatcharr plugin that turns your VOD catalogue into a folder of <code>.strm</code> files (with optional NFO metadata) that media servers — Jellyfin, Emby, Kodi, ChannelsDVR — can index and play.</p>

<p align="center">
  <i>v1.18.0 — slug <code>vod2mlib</code></i>
</p>

> **Note on scheduled rescans.** The cron task routes via Dispatcharr's `dvr` Celery worker as a workaround for an upstream plugin-task-registration issue affecting the default prefork worker pool ([Dispatcharr#1244](https://github.com/Dispatcharr/Dispatcharr/issues/1244)). The routing is transparent — no user action required for new installs. If you originally set up your schedule on **v1.14.1 or earlier**, click `[SCHEDULE] Apply / Update` once after upgrading so the stored task picks up the new routing.

> **Plex users:** Plex does *not* play `.strm` files. Jellyfin and ChannelsDVR do. See [Plex compatibility](#plex-compatibility) below.

## Credits

- **Original author:** [shedunraid](https://github.com/shedunraid) — created v0.x–v1.3 ([upstream repo](https://github.com/shedunraid/VOD2MLIB)).
- **Fork maintainer:** [R3XCHRIS](https://github.com/R3XCHRIS) — v1.4+ adds scheduling and bug fixes. Listed in the [official Dispatcharr Plugins catalogue](https://github.com/Dispatcharr/Plugins/tree/main/plugins/vod2mlib) since v1.14.3. Upstream has been dormant since early 2026; this fork continues maintenance.
- MIT License.

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

The Settings tab is grouped into five sections:

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
|  | Category Filter (include only) | Comma-separated **category-name** prefixes (e.g. `[EN],[FR]`, case-insensitive). Only generate content whose category starts with one of them — filters at query level so unwanted folders are never created. Applies to Movies *and* Series. Empty = all. ⚠ Matches the *category* name, not the title. |
|  | Category Exclude (block list) | Comma-separated category-name prefixes to **skip** (e.g. `FOR ADULTS,XXX`). Applied after the include filter. Usually the right tool for "everything except adult content" — leave the filter empty and list what you don't want here. Content with no category is never excluded. |
| **Series** | Batch Size (Series) | How many series to process per click |
|  | Generate Series NFO Files | Toggle `tvshow.nfo` and per-episode `.nfo` |
|  | Refresh Existing Series | Re-evaluate already-processed series for new episodes AND rewrite existing episode `.strm` URLs (cron-friendly). Preserves `tvshow.nfo` and episode `.nfo` edits. |
|  | Nest Series by Category | Wrap each series folder inside a subfolder named by its M3U category (off by default; series without a category go to `Unassigned/`) |
|  | Dedupe Series Across Categories | When nesting is ON and a series is tagged with multiple categories upstream, write under the first category only (alphabetical) instead of duplicating. No effect when nesting is OFF. Off by default. ⚠ Doesn't remove existing duplicate folders — `[⚠ DANGER] Clean up` + re-generate to migrate. |
| **Selection mode** | Selection mode | Off by default. When ON, nothing is generated unless you pick it on the selection page and press Apply; the Generate / Full rescan buttons are paused and the schedule maintains your selection instead. See [Selection mode](#selection-mode-opt-in). |
|  | Selection page port | Port the page listens on inside the container (default `9192`). Publish it in docker-compose. |
|  | Selection page password | Required: the page can create and delete library files, and the server won't start without one. |
| **Auto-rescan schedule** | Schedule (cron) | Standard 5-field expression. Default `0 3 * * *` (daily 03:00) |
|  | Schedule Timezone | IANA timezone the cron is interpreted in (e.g. `Europe/London`). Empty = UTC. Handles DST automatically. |
|  | Scheduled Action | What the cron fires (full rescan recommended) |

## Workflow

**First run.** Configure paths → click `[LIBRARY] Catalogue snapshot` to verify the plugin can see your VODs → click `[GENERATE] Movies` with Batch Size 10 → spot-check the output → scale up.

**Scaling up.** Increase Batch Size, click again. Existing files are skipped, so each click only processes new ones. (If you need to refresh URLs in already-generated files — typically after changing the `Dispatcharr URL` setting — use `[GENERATE] Full rescan` instead; it rewrites all existing `.strm` while preserving your `.nfo` edits.)

**Auto-rescan.**
1. Turn ON **Refresh Existing Series**.
2. Set **Scheduled Action** to **Full rescan**.
3. Click `[SCHEDULE] Apply / Update`.
4. Verify with `[SCHEDULE] Show status` — last run / total runs populate after the first cron tick.
5. Optional: click `[SCHEDULE] Test fire now` to immediately replay the scheduled action without waiting for the next cron tick.

The cron snapshots your settings at click-time. **Re-click Apply after changing any setting** to refresh the snapshot.

## Selection mode (opt-in)

For catalogues too big to generate wholesale. With **Selection mode** ON, everything is ignored by default: you tick the movies and series you want on a page served by the plugin, choose which provider **copy** of each (copies differ in quality, audio languages and subtitles), and press **Apply** to write or delete their files. With it OFF (the default) the plugin behaves exactly as described above.

### Setup

1. **Publish the page's port** in your Dispatcharr container (default `9192`):

   ```yaml
   services:
     dispatcharr:
       ports:
         - "9192:9192"
   ```

2. In the plugin settings, set a **Selection page password**, turn **Selection mode** ON and save. The page starts within ~30 s at `http://<dispatcharr-host>:9192/`. `[SELECTION] Page status` shows whether it's running, the port to publish, and whether the Movies folder is writable.
3. **Restart Dispatcharr after installing or updating the plugin.** The page runs in Dispatcharr's `daphne` process, which only loads plugins at startup.

The page has its own login (the password above, 7-day sessions) and isn't behind Dispatcharr's authentication, so keep the port on your LAN.

### Using the page

- **Movies | Series**, a **Table** (copies, languages, seasons) or a **Grid** of posters (click to select with the best copy).
- Tabs: **All**, **New** (added since you last pressed *Mark all seen*), **Selected**, **Ignored**, **Pending** (changes not applied yet), **Flagged** (see upkeep below). Filters: search, provider (M3U account), category and decade.
- **Copies** are ranked by your **Preferences** (audio languages, subtitle languages, 4K or 1080p first) and labelled with what Dispatcharr knows, e.g. `DE 1080p H264 4.8 Mb/s`. Provider data rarely says which languages a stream really has, so **Probe** (per title) and **Probe all selected** run `ffprobe` through Dispatcharr's own VOD proxy (one connection at a time, so account limits hold) and record every audio and subtitle track. Titles you already picked keep their copy; a better one shows as a *better copy: switch* hint.
- **Audio override** per title (e.g. a French film in French with your usual subtitle languages).
- **Seasons**: untick seasons you don't want. New seasons are included automatically.
- **Review & Apply** lists every change before anything is written.

**Existing library.** If you generated files before turning selection mode on, a banner offers **Scan library**: it reads your `.strm` files, shows which titles and copies they play, and **Adopt** records them as selected without touching the files. Files it can't match are left alone.

### Scheduled upkeep

In selection mode the `[SCHEDULE]` cron (and the page's **Refresh selected now**) maintains what you applied instead of generating the whole catalogue: it refreshes `.strm` URLs, adds new episodes, and handles copies that disappear:

- **Another copy exists:** switches to the best remaining one (flag `fallback`).
- **No copy left:** deletes the title's files but keeps it selected (flag `no_copy`); it comes back when a copy reappears.
- **Dispatcharr re-created the titles** (new ids after a provider refresh): selections are relinked by copy id, then TMDB id.
- **Mass-loss guard:** if more than 20% (and at least 5) of your applied titles lose every copy in one run, nothing is deleted and the run reports it, since that usually means a provider outage.

Changes you haven't applied are never touched by upkeep.

### Safety

- The plugin only deletes files it recorded writing. Other files in your library folders, and `.nfo` files it didn't write, are never modified or deleted.
- Selection state lives in `/data/vod2mlib/selection.db` inside the container. Uninstalling the plugin wipes its settings but not this file.
- Turning selection mode OFF leaves your files as they are and restores the classic buttons.

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

**Schedule fires but no new files appear.** Most likely: `Refresh Existing Series` is OFF and your existing series already have folders, so the cron only adds *new* series. Toggle Refresh Existing ON, click Apply Schedule again to update the snapshot.

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

**Want to browse and hand-pick VOD into Emby rather than import everything?** [VodLink](https://github.com/jdfrey1/vodlink) reads this plugin's `.strm` + `.nfo` output and lets you browse/search your VOD catalogue and link individual movies and series into an Emby library directory, instead of pointing Emby at the whole generated tree. It also runs a stream proxy that converts `HEAD` to `GET` and caches Dispatcharr session URLs, so seeking and resume behave. Emby-specific, Docker-based. Keep `Generate NFO Files` ON if you use it, since it reads those `.nfo` files. For coarser filtering at generation time, use `Category Filter` / `Category Exclude` (above).

**"All profiles at capacity" error when playing on TiviMate / Android.** Not a `.strm` issue — this is a known Dispatcharr connection-counting bug ([Dispatcharr #451](https://github.com/Dispatcharr/Dispatcharr/issues/451)). TiviMate (and similar Android players) makes multiple simultaneous Range requests to probe a file before playback; Dispatcharr counts each request as a separate provider connection, blowing through `max_streams=1` before playback even starts. The community plugin [`dispatcharr_vod_fix`](https://github.com/cedric-marcoux/dispatcharr_vod_fix) patches Dispatcharr's request handling to track slots by (client IP + content UUID) so multiple Range requests share one slot. Install it alongside this plugin if your Android clients can't play VOD content.

**Folders named `Aladdin (2026) (2026)` (duplicate year).** This was a bug in v1.4 and earlier. Fixed in v1.5+ but pre-existing duplicate-year folders aren't auto-renamed. Run `[⚠ DANGER] Clean up Movies` once to remove them, then re-run `[GENERATE] Movies` to regenerate cleanly. (Cleanup deletes only `.strm`/`.nfo` — user-added subtitles/posters survive.)

**Generate Series fails for some series.** The summary lists the failed series names with their errors. Common causes: M3U upstream timeout, malformed episode metadata. The plugin continues with the rest of the batch.

**`localhost`/`127.0.0.1` in Dispatcharr URL.** The plugin refuses to write `.strm` with a localhost URL — your media server can't resolve it. Use the container's reachable IP/hostname.

## Development

Pure-helper unit tests live in `tests/`. From the repo root:

```bash
python3 -m pytest tests/ -v
```

The tests don't need Django or a running Dispatcharr — they exercise `_clean_title`, `_strip_trailing_year`, `_sanitize_filename`, `_parse_cron`, `_extract_genres`, `_mask_url`, and the path-building helpers in isolation. 45 tests, ~50ms.

Selection mode has its own suite, `tests/test_selection.py`, which runs against an in-memory fake catalogue (no Django needed). To try the page without Dispatcharr:

```bash
python3 -m selection.devserver --no-login   # http://127.0.0.1:9192, fake data
```

The bundled logo is reproducible — replace `tools/source_logo.png` and run `python3 tools/build_logo.py` to regenerate `logo.png` at 512×512 with NEAREST resampling (preserves pixel-art crispness).

## Architecture (for contributors)

- The plugin is a single `plugin.py` declaring a `Plugin` class with `fields`, `actions`, and `run()` per Dispatcharr's plugin contract.
- `plugin.json` is the manifest the [Dispatcharr/Plugins catalogue](https://github.com/Dispatcharr/Plugins) reads. Dispatcharr's runtime reads action metadata from the Python class — the JSON is for the catalogue and pre-enable preview.
- Schedule registration uses `django-celery-beat`'s `PeriodicTask` + `CrontabSchedule`. The cron-fired task is a module-level `@shared_task` named `vod2mlib.scheduled_rescan` that constructs a fresh `Plugin()` and dispatches.
- Settings are snapshotted into the PeriodicTask's `kwargs` at Apply-time so the cron runs with deterministic config. Re-click Apply to refresh. The selection page password is left out of the snapshot, and in selection mode the task reads live settings instead.
- Selection mode lives in the `selection/` subpackage (stdlib only), so `plugin.py` only gains its settings, one action and a few hooks:
  - `server.py`: the page's HTTP server (`http.server`) and JSON API. It runs in Dispatcharr's `daphne` process (present in every layout and not gevent-patched, so page work can't stall streams); a 30-second watchdog there starts and stops it as the setting changes, and binding the port doubles as the lock against a second copy.
  - `store.py`: SQLite state. Each title has a *desired* state (edited on the page) and an *applied* state (on disk); `applied_file` records every file written, and only those are ever deleted.
  - `catalogue.py`: read-only ORM queries over Dispatcharr's VOD models, plus a fake catalogue for tests and the dev server.
  - `apply.py`, `upkeep.py`, `relink.py`, `adopt.py`: Apply, scheduled upkeep, relinking re-created titles, adopting an existing library. They reuse `Plugin`'s naming and writing helpers, so files are named exactly as in classic mode.
  - `copyinfo.py`, `probe.py`: copy labels and ranking; ffprobe through Dispatcharr's VOD proxy.
  - `static/index.html`: the page (plain JS, no build step).
- ORM calls from the page's threads must close their DB connection afterwards (`close_old_connections()`): they run outside Django's request cycle, and Dispatcharr's pool has 8 connections per process.

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for the full release history.
