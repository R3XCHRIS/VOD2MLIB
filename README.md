<p align="center">
  <img src="logo.png" alt="VOD to Media Library" width="200">
</p>

<h1 align="center">VOD to Media Library (mwongj fork)</h1>

<p align="center">Generate <code>.strm</code> files and optional NFO metadata from Dispatcharr's stored VOD catalogue for a media server that supports stream-link files.</p>

<p align="center"><i>Stable v1.20.1 · plugin identifier <code>vod2mlib</code></i></p>

This fork adds native category eligibility, optional Emby ownership checks, safe managed-file cleanup, persistent inventory and incremental generation, independent movie/series filters, timing telemetry, and scheduling controlled entirely through Settings → Save.

**Dispatcharr owns catalogue fetching.** The plugin reads movie, series, and episode models already in Dispatcharr's database. It does not contact VOD provider APIs, run native importers, enrich metadata, or change Dispatcharr freshness timestamps or flags. New episodes can generate only after Dispatcharr stores them. Optional Emby requests check ownership of real media; they do not supply filter metadata.

## Install and upgrade

1. Map persistent output storage into Dispatcharr and make the same files visible to your media server. Defaults are `/VODS/Movies` and `/VODS/Series`; see [Sharing the VODs folder](#sharing-the-vods-folder-with-media-servers).
2. In Dispatcharr's plugin repositories, add **mwongj Plugin Forks** using this manifest URL:

   ```text
   https://raw.githubusercontent.com/mwongj/Dispatcharr-Plugins/releases/manifest.json
   ```

3. Select **VOD to Media Library (mwongj fork)** from that repository and install it. Alternatively, import `vod2mlib-1.20.1.zip` from the [fork distribution releases](https://github.com/mwongj/Dispatcharr-Plugins/releases).
4. Enable the plugin, configure reachable paths and the Dispatcharr URL, and click **Save**.

The source repository is [mwongj/VOD2MLIB](https://github.com/mwongj/VOD2MLIB); ZIPs and update manifests are published by [mwongj/Dispatcharr-Plugins](https://github.com/mwongj/Dispatcharr-Plugins). The official catalogue's upstream plugin is a separate distribution. Use **mwongj Plugin Forks** for this fork's updates. The identifier remains `vod2mlib`, so installing the fork over an existing managed installation keeps the same settings and schedule identity; the two distributions are not intended to run as separate plugins.

Requires Dispatcharr **v0.24.0 or later**. Scheduling uses Django, Celery, and django-celery-beat supplied by Dispatcharr. Keep the `/data` volume persistent: plugin state lives outside the installation at `/data/vod2mlib`. Existing schedules retain their enabled state on upgrade; fresh installations default to scheduling disabled. After an upgrade, reload the plugin and ensure idle Celery workers load the current plugin task code before using the schedule. This fork does not modify Dispatcharr source files.

## Quick start

1. Let Dispatcharr populate its VOD catalogue and episodes. Enable the wanted categories for each active M3U account and VOD type in Dispatcharr.
2. Save the plugin's movie root, series root, and reachable Dispatcharr URL. Configure optional filters and Emby integration before generating.
3. Run **Catalogue snapshot** to inspect native eligibility and filter counts. If output already exists, use **Preview selective cleanup** to review proposed removals.
4. Start with a small movie or series batch. Use **[ACTION] Status** to observe the background action and final result, then inspect the output in your media server.
5. Increase batch sizes when satisfied. For regular runs, choose a Scheduled Action, enter a valid cron and timezone, turn **Enable Auto-Rescan** on, and **Save**.

Saving settings does not run generation or remove media files immediately. Changes affect the next action; Save updates the scheduling trigger immediately. Enabled filters can remove verified generated output on the next applicable generation or selective cleanup run, including output created before the filters were enabled.

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
# ... then point Jellyfin/Emby/Kodi at /mnt/vods/{Movies,Series}
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

Every section in Settings has a heading and a description. Manual actions, cron runs, and Test fire read the same current saved configuration and defaults at execution start. A running job keeps that configuration until it finishes, fails, times out, or is stopped; later saves affect subsequent jobs. Persistent idle Celery workers wait for jobs and do not keep a separate settings snapshot.

### Paths and output

| Setting | Default | Behavior |
| --- | --- | --- |
| Root Folder for Movies / Series | `/VODS/Movies`, `/VODS/Series` | Output paths inside Dispatcharr; use persistent shared storage. |
| Dispatcharr URL | Required | Reachable base URL written into STRMs. Localhost addresses are rejected. |
| Batch Size (Movies) | 250 | Limit new/pending movie work; `all` removes the limit. |
| Batch Size (Series) | 10 | Limit series work; `all` removes the limit. Existing filter/ownership cleanup runs independently of creation limits. |
| Parallel Series Workers | 3 | Database-read and generation concurrency, from 1–6. Movies use 3 workers. |
| Generate Movie / Series NFO Files | On | Write movie NFOs, `tvshow.nfo`, and episode NFOs when absent; preserve existing NFO edits. |
| Omit `<title>` from NFO files | Off | Omit movie/show titles to let a media server identify them itself. Episode titles remain. |
| Refresh Existing Series | Off | Revisit processed shows using stored episodes and refresh changed STRM URLs. No provider requests. |
| Nest Movies / Series by Category | Off | Create category subfolders; uncategorized content uses `Unassigned/`. |
| Dedupe Movies / Series Across Categories | Off | With nesting enabled, use the first category alphabetically instead of duplicate category output. |
| Append TMDB ID to folder names | Off | Add a known TMDB ID using the selected format; unknown IDs are reported. |
| TMDB Folder Tag Format | Plex / ChannelsDVR | `{tmdb-123}` or Jellyfin / Emby `[tmdbid-123]`. |
| Don't pin STRMs to a specific provider stream | Off | Omit `?stream_id=`. Requires a Dispatcharr build that can resolve/fail over VOD without this parameter. |
| Maximum action runtime (minutes) | 30 | Deadline for manual and scheduled background actions. |

Changing roots, category nesting, deduplication, or TMDB tag format does not rename or migrate old folders. Review existing output and ownership-protected cleanup before changing its layout. NFO title cleanup removes provider/quality tags; NFO writing can use title/category fallbacks, but those fallbacks are **not** metadata-filter inputs. The plugin does not download artwork files; an NFO may contain a stored poster URL for the media server to retrieve.

Native category eligibility requires an active account and an enabled category for that same account and VOD type. The old plugin Category Filter/Exclude fields are removed and ignored. Disabling a native category alone does not treat its source as removed or delete existing output; metadata filters and optional Emby/M3U cleanup are separate policies.

### NFO metadata and Emby

The **NFO Metadata** settings group controls the plugin's movie/show/episode sidecar writing. If Emby manages metadata and its NFO saver is enabled, turn **Generate Movie NFO Files** and **Generate Series NFO Files** off to give Emby responsibility for writing metadata. Existing NFOs are not erased by changing these toggles. Defaults remain on for compatibility; saved choices are preserved.

Emby integration is an independent real-media ownership check. It does not enable Emby's NFO reader/saver or replace Emby's metadata/image providers. Plugin NFOs can seed identification with stored Dispatcharr metadata, but cannot guarantee that Emby avoids additional metadata or artwork requests. The plugin does not overwrite existing Emby or edited NFOs.

### Independent metadata and title filters

All filter rules are disabled by default. Movies and series have independent settings:

| Rule | Movies | Series |
| --- | --- | --- |
| Minimum Score | Stored numeric score, 0–10 setting | Separate stored numeric score |
| Earliest / Latest Year | Stored release year | Stored debut year |
| Missing Metadata | Keep unknowns by default; optionally reject | Independent policy |
| Title Include / Exclude Regex | Original stored title | Independent patterns |
| Genre Include / Exclude | Unavailable | Comma-separated complete genre names |

Blank score/year bounds and blank include/exclude values disable their rules. Enabled rules combine with AND; score/year boundaries are inclusive. Years must be positive integers and earliest must not exceed latest. Missing, zero, nonnumeric, nonfinite, negative, or above-10 model scores are unknown; missing or invalid model years are unknown. **Reject unknowns** applies only to enabled rules, including enabled genre/title rules.

Genre matching is case-insensitive and matches complete names. `Action & Adventure` and `Sci-Fi & Fantasy` each remain one genre. Only commas separate names in both configuration and stored genre metadata. Any included genre qualifies; any excluded genre rejects, even if another genre qualifies. Genres are plain text, not regex.

Title patterns are Python regular expressions searched case-insensitively against the original stored title, before provider tags or years are stripped. An include requires a match; an exclude rejects a match and wins over include. For example:

```text
Title Exclude Regex: ^\s*(AF|AR)\s*[-:|]\s*
```

This matches `AF - Yard Palava`, `AR: Title`, and `AR|Title`. For bracketed tags use `^\s*\[(AF|AR)\]\s*`. Commas and whitespace remain part of the pattern, including quantifiers such as `{1,2}`. A country/language prefix rule is a provider naming heuristic, not verification of a production's country of origin.

Filters use Dispatcharr model metadata directly, without NFOs, Emby enrichment, title-derived years, or category-derived genres. Age classifications such as `PG-13` are unknown numeric scores. Missing metadata varies by provider; **Keep unknowns** can retain many titles. Invalid scores, bounds, policies, or regex patterns are rejected before generation/reconciliation; settings Save also validates them while the plugin's save hooks are loaded.

**Catalogue snapshot** reports separate movie/series eligible, passing, per-rule rejection, and retained-unknown counts. Counts are unique titles after native category/account eligibility and before Emby ownership checks. Rejection counts may overlap because one title can fail several rules.

Each applicable run checks tracked output against current filters before creation batching. Movies generation removes failing movie output; series generation removes failing episode output; full rescan and selective cleanup cover both. Verified generated STRMs and matching generated NFOs can be removed. Edited/unverified STRMs, unresolved sources, and shared output with any passing or unresolved source remain protected. Legacy/edited NFOs are preserved or archived as described below. All bounded metadata lookups must finish before deletion starts. Filter NFO removal is automatic and independent of the separate Emby/M3U deletion-scope setting. Catalogue snapshot deletes nothing; Preview selective cleanup reports candidates without changing output files.

Filters also archive **NFO-only title folders**, including legacy NFOs without recorded ownership hashes and NFOs written or edited by Emby. This handles enabling filters after unfiltered generation or after STRM-only cleanup. Complete, bounded Dispatcharr relation projections identify exact folders using the current roots/naming settings; all matching sources must fail. NFO contents never supply filter metadata. Folders with a remaining STRM, symlink, unrelated file, or unresolved identity stay in place. Historical folders with a different naming layout are not guessed or automatically moved.

Archives retain the metadata under `/data/vod2mlib/filtered-nfo/<run>/<movie|series>/...`, with a `manifest.jsonl` mapping original paths to backups. `VOD2MLIB_STATE_DIR` changes that base. Keep state outside media-server library paths. Same-device moves are verified renames; cross-device copies are verified before originals are removed. Copy failures leave originals available for retry. Archives are retained until you remove them yourself; relaxing filters can regenerate eligible STRMs but does not automatically restore archived metadata. Rescan Emby after cleanup to remove cached empty-show entries.

This archive pass runs independently of both NFO-generation toggles and **Deletion scope**, and is limited to the media types selected by the action. Preview reports the folders/NFOs it would archive, including metadata that would remain after planned STRM removal. Results include archive counts, protected/error counts, and the archive location.

Enabled **Emby ownership cleanup** also archives NFO-only output for movies or whole shows already present in the selected real-media libraries, even if they pass all metadata filters. This prevents retained legacy/Emby NFOs from keeping an empty duplicate visible after its STRMs are removed. It uses the same complete Emby snapshot, conservative identity matching, both output roots, automatic timing and failure policy as existing ownership cleanup. Manual selective cleanup applies it immediately; a full-rescan-only or manual-only policy is respected during ordinary generation. Provider-ID conflicts and any unowned matching source protect shared paths.

Whole-show ownership mode can archive a series folder containing only episode NFOs, with no `tvshow.nfo`. Episode mode preserves series NFO-only folders because whole-folder metadata cannot establish ownership of missing episode positions. Metadata is retained in the same persistent archive location, with `reason: ownership` in its recovery manifest. A failed Emby snapshot or incomplete native ownership lookup cannot trigger archival. Real-media files and their metadata are outside plugin output roots and are not moved.

Rejected titles do not consume creation batch slots or trigger episode loading. Incremental signatures include filter settings and metadata, so changing stored metadata or relaxing rules causes affected candidates to be reconsidered and eligible output can return. Filter rejection never establishes upstream absence: M3U cleanup uses an unfiltered database source census.

### Media-library integration and cleanup

Optional integration supports **one Emby server** and is disabled by default. Jellyfin and Plex ownership adapters are not implemented.

| Setting | Default | Behavior |
| --- | --- | --- |
| Enable media-library integration | Off | Exclude media already owned as real files in the selected Emby libraries. |
| Media server / Emby URL / API token | Emby; empty URL/token | Configure the server connection. |
| Library names or IDs | Empty | Required when integration is enabled. Use **List media libraries** to find selections. |
| TV handling | Skip entire owned show | Alternatively fill missing season/episode positions, including specials. |
| Server-check failure | Continue with warning | Skip Emby exclusions/deletion for the run, or stop before output-file changes. |
| Existing duplicate cleanup | Every generation | Alternatively full rescans only or disabled. Ownership exclusions still apply when deletion is disabled. |
| Clean up M3U removals | Off | Remove verified output whose provider/account sources are absent from Dispatcharr. |
| M3U cleanup timing | Full rescans | Alternatively manual selective cleanup only. |
| Deletion scope | STRMs only | Optionally remove unedited generated NFOs for ownership/source cleanup. |

Library names match exactly, case-insensitively, and are resolved each run; duplicate names require an ID. There is no all-libraries selection. Empty, missing, and ambiguous selections stop the action even under Continue with warning. Select real-media libraries; leave generated VOD/STRM libraries out of the check. STRM-only, remote, and virtual entries do not establish real ownership.

Movies and shows match separately by TMDB/IMDb ID first. Where comparable IDs are unavailable, exact cleaned titles and known matching years can match. Conflicting IDs, unknown years, and fuzzy titles are retained. A show needs real non-STRM episodes to count as owned; missing-episode mode retains uncertain positions. Full rescans share one complete paginated Emby snapshot across the generators. Failed, interrupted, repeated, or inconsistent pagination never establishes ownership for deletion.

Automatic duplicate cleanup checks managed output before generation, independently of creation limits. **Run selective cleanup** runs enabled server/source checks immediately regardless of their automatic timing, and also applies current filters. **Preview selective cleanup** reports those candidates without deleting output files; it may initialize/adopt inventory records but does not refresh Dispatcharr metadata or contact VOD providers.

M3U cleanup uses account/provider stream identities, not UUIDs alone, and checks the complete **unfiltered** Dispatcharr database catalogue, including inactive accounts and disabled categories. Confirmed absence can delete output on the first successful complete check; there is no grace period. Failed/incomplete database checks disable source-removal deletion for that run. Dispatcharr must refresh its own catalogue before newly removed upstream items can be detected.

Removal verifies the recorded STRM URL text and path containment. Edited STRMs, symlinks, files outside configured roots, ambiguous/unrecognized legacy files, artwork, subtitles, and unrelated files remain protected. Optional NFO deletion requires matching recorded generated hashes. Legacy/edited NFOs are never deleted on that basis; filters can instead archive a confirmed rejected NFO-only title folder as described above. Shared `tvshow.nfo` survives while protected episode STRMs remain. Empty directories may be pruned; roots remain. Media files themselves are never hashed.

### Scheduling

Turn **Enable Auto-Rescan** on with a valid five-field cron and IANA timezone, select the scheduled action, and **Save**. The default cron is `0 3 * * *`; an empty timezone means UTC. Invalid enabled cron/timezone/target settings are rejected before persistence. New installs default off; upgrades preserve existing schedule enabled state.

Save immediately creates, updates, or disables the trigger through plugin-owned Django model signals. There is no Apply or Unschedule action. Turning the toggle off leaves manual actions available and makes queued cron/Test fire tasks skip execution; it does not cancel a job already running. Disabling the plugin also disables its trigger. Test fire requires scheduling enabled and reads current settings again when the worker executes it.

The beat task keeps its stable identity `vod2mlib.auto_rescan`, routes `vod2mlib.scheduled_rescan` to the `dvr` queue, and stores no settings or credentials in its payload. Settings Save does not generate files. **[SCHEDULE] Show status** reports enabled state, registered cron/timezone, current target, last run, and run count. Persistent idle workers are distinct from an action's execution lifecycle.

## Actions and observability

| Action | Purpose |
| --- | --- |
| Catalogue snapshot | Read-only database eligibility and filter counts; no output deletion. |
| Generate Movies / Series | Apply current filters and configured cleanup, then generate the selected media type. |
| Full rescan | Scan and run both generators with URL refresh/series revisit semantics forced on; configured batch sizes still apply. |
| List media libraries | List Emby names/IDs for explicit library selection. |
| Preview selective cleanup | Show proposed removals without deleting output files. |
| Run selective cleanup | Apply current filters and enabled ownership/source removal checks immediately. |
| Rebuild / discover inventory | Rediscover recognizable STRMs and reset generation decisions; deletes no output and requires no Emby connection. |
| Clean up Movies / Series | Remove verified managed output in that root; preserve edited/unverified files and follow deletion scope. |
| Schedule status / Test fire | Inspect the trigger or enqueue its selected action using saved settings. |
| Action status / Stop running action | Observe the background job or cancel its worker group. |

Generation, inventory rebuild, library listing, preview, and cleanup use isolated background processes. Buttons return immediately; read final counters, warnings, and errors through **[ACTION] Status**. The supervisor enforces the saved runtime limit, including blocked network calls. Stop keeps completed changes; an interrupted inventory batch remains retryable. One process lock serializes plugin generation/cleanup; overlapping work is rejected rather than run against the same inventory concurrently.

Changed STRM URLs are refreshed while preserving modification time; identical contents are left untouched. Existing NFOs are not overwritten. Full rescan does not fetch missing episodes from providers and does not bypass configured batch limits: choose `all` for both media types when you want all eligible pending work considered in one run.

Telemetry is local. Detailed completed timings are in the action result and `/data/vod2mlib/timings.json` (or the overridden state directory); Action status shows a short phase summary and live progress. Series counters update approximately every two seconds. No telemetry is sent externally, and timing records omit credentials, settings, URLs, and output paths.

Measurements include wall/CPU seconds, item/batch counts, and phases for Emby, database catalogue reads, discovery, filter checks, cleanup, movie/series generation, episode reads, output, and inventory/checkpoint work. `episode_sql` is SQL execution; `episode_query` includes query/hydration; `episode_load` covers the complete read. `cleanup_strm_io` is cumulative verification/removal time across filesystem workers. `filter_cleanup_batch` and `cleanup_batch` include serialized inventory finalization and pruning. Parent phases include child measurements, and concurrent worker durations can exceed elapsed action time; do not add them together. Worker phases use thread CPU time, while action totals use process CPU time and exclude supervisor startup. There is no provider-fetch phase.

`filter_nfo_metadata_read` measures native identity/filter projections for legacy folders; `filter_nfo_archive` measures folder inspection and archiving. `filter_nfo_folders_*`, `filter_nfo_candidates`, `filter_nfo_archived`, and `filter_nfo_errors` distinguish archived metadata from deleted STRMs/NFOs.

`ownership_nfo_metadata_read`, `ownership_nfo_archive` and corresponding `ownership_nfo_*` counters report Emby-excluded NFO-only metadata separately. Preview accounts for the configured STRM/NFO deletion scope and never counts the same folder twice when both filters and ownership exclude it.

## Inventory, incremental generation, and performance

State lives at `/data/vod2mlib/inventory.sqlite3`. `VOD2MLIB_STATE_DIR` can override the location; keep it on persistent storage outside plugin installations and both media roots. Inventory upgrades use schema versioning and preserve recorded ownership and generated-NFO hashes.

Initial discovery and explicit rebuild adopt recognizable Dispatcharr STRMs using proxy URLs and complete stored source metadata. Ambiguous or unrelated files stay unmanaged. Successful discovery markers are retained per root and Dispatcharr connection context, so routine runs avoid rescanning every directory. New roots, changed connection context, missing/rebuilt inventory, and incomplete discovery cause a new scan. A cold first run can therefore be much slower than a routine run.

Routine generation uses persistent decisions for managed output. Movies stream eligible identities/output-affecting fields before hydrating new or changed candidates. Episodes are loaded from Dispatcharr before unchanged output decisions are skipped. Settings, metadata, output-layout/URL changes, Emby ownership, and plugin deletions invalidate relevant decisions. Unchanged runs avoid statting every managed STRM. When files are added, changed, or removed externally, run **Rebuild / discover inventory**, then generation; missing entire roots invalidate decisions automatically.

Database/filter reads are bounded projections. Title regexes run in the shared Python evaluator over those projections, not through a SQLite REGEXP query. Inventory changes commit in batches of up to 1,000 records with normal SQLite durability. Generation queues are bounded and inventory records commit before their completion checkpoints. Metadata-filter deletion uses three bounded filesystem workers; shared NFO handling, SQLite finalization, and parent pruning remain serialized. Season directories are prepared once per show. Worker-count changes do not invalidate output signatures.

For comparisons, hold saved settings, Dispatcharr models/episodes, selected Emby data, inventory state, and filesystem layout constant. Record cold discovery separately from routine runs. Use telemetry to find the dominant phase; cumulative parallel timing is not end-to-end latency. No fixed runtime is promised for a catalogue or storage system.

## Troubleshooting

- **Fewer exclusions than expected:** inspect Catalogue snapshot and the actual model values. Keep unknowns retains missing years/scores/genres. Genre names are complete comma-delimited names; title prefixes do not prove country of origin. Emby exclusions are separate from snapshot filter counts.
- **Old filtered output remains:** run the applicable generator or selective cleanup. Preview never deletes output. Edited/unverified files and unresolved/shared sources are protected; check the removal counters and warnings. Disabling a native category alone is not a deletion policy.
- **No new episodes:** check that Dispatcharr has stored episode relations, then use Refresh Existing Series or Full rescan. The plugin does no provider refresh. Also check batches, filters, and real-media ownership.
- **A file removed outside the plugin is not recreated:** run Rebuild / discover inventory, then generation, to invalidate persistent decisions.
- **Schedule does not fire:** enable Auto-Rescan and Save, check Schedule status, ensure beat and the `dvr` worker are running with current plugin code, then use Test fire and Action status. Read Dispatcharr logs for validation or task errors.
- **Unknown action or old fields after upgrading:** reload the plugin; stale worker task code may require restarting idle workers. Apply and Unschedule are intentionally removed.
- **Files are invisible to the media server:** check shared storage mappings and read permissions. A container path may differ from the media server's path while referring to the same host files.
- **Playback fails:** inspect the STRM URL and test it from the media server's network. Fix the Dispatcharr URL and use Full rescan to update changed URLs. Catalogue/UUID changes, provider availability, and playback connection limits are Dispatcharr/player concerns; this plugin does not patch playback handling.
- **Unexpected artwork downloads:** artwork fetching is controlled by the media server. Configure its metadata/image providers before indexing a large VOD library. NFOs only contain available stored metadata and references; they do not guarantee complete offline metadata.
- **Naming/layout changes leave old folders:** generation does not migrate old paths. Preview and review ownership-protected cleanup before recreating output.

This plugin has no Plex playback adapter. The player must support STRM URLs; successful indexing alone does not establish playback compatibility.

## Development and architecture

The entry point is `plugin.py` (`Plugin.fields`, `Plugin.actions`, `Plugin.run`). `plugin.json` mirrors UI metadata/version and links to this fork. Ship **all runtime modules** in a release:

| Module | Responsibility |
| --- | --- |
| `action_runner.py` | Supervisor, isolated action process, deadlines, cancellation, status. |
| `metadata_filters.py` | Shared pure rules, projected eligibility, preview counts. |
| `filter_cleanup.py` | Bounded checks and removal of managed output failing current filters. |
| `orphan_nfo.py` | Native path matching, preview and verified archival of rejected NFO-only title folders. |
| `reconciliation.py` | Emby/source checks, discovery, action telemetry, inventory queues. |
| `media_library.py` | Emby adapter, complete snapshots, conservative identity/ownership matching. |
| `inventory.py` | SQLite state, ownership verification, containment, batched writes/removals. |
| `generation_cache.py` | Persistent incremental movie and episode decisions. |
| `schedule_settings.py` | Plugin-owned validation/save/delete signals and schedule-state upgrade. |

Scheduling uses Django model signals; Dispatcharr files are unchanged. Runtime dependencies come from Dispatcharr. Tests include pure helpers, model-backed generation/cleanup fixtures, process/deadline checks, and a real Django/beat Save integration test using an isolated SQLite database; a running Dispatcharr or live provider/Emby server is not required.

```bash
python -m pip install pytest django celery django-celery-beat
python -m pytest -q
python -m compileall -q plugin.py action_runner.py metadata_filters.py filter_cleanup.py orphan_nfo.py reconciliation.py media_library.py inventory.py generation_cache.py schedule_settings.py
```

GitHub CI tests Python 3.10 and 3.12. The v1.20.1 suite passes 417 tests with 4 platform-specific skips on Windows. Behavioral changes should update the README and [CHANGELOG.md](CHANGELOG.md), keep Python/manifest fields and versions aligned, and update the fork distribution's pinned source SHA and file list when publishing. ZIP publication verifies checksums before updating the catalogue feed; do not replace an existing version's package with different bytes.

The bundled logo is reproducible: replace `tools/source_logo.png` and run `python tools/build_logo.py`.

## Fork release history and credits

| Version | Fork changes |
| --- | --- |
| 1.20.1 | One saved configuration for actions/schedules; Save validates and applies cron/timezone; enable toggle; legacy schedule-state preservation; Apply/Unschedule removed. |
| 1.20.0 | Independent score/year/unknown/genre and title regex filters; next-run managed-output removal; database-only metadata/episodes; finer telemetry and faster output/cleanup handling. |
| 1.19.0 | Optional Emby reconciliation, safe source/ownership cleanup, persistent SQLite inventory/discovery, incremental decisions, isolated actions, deadlines/cancellation, and timing telemetry. |
| 1.18.1 | Native per-account/category eligibility; legacy category-prefix fields removed. |

Original implementation: [shedunraid](https://github.com/shedunraid/VOD2MLIB). Earlier scheduling, NFO, naming, and deduplication work: [R3XCHRIS](https://github.com/R3XCHRIS/VOD2MLIB). This fork is maintained by [mwongj](https://github.com/mwongj/VOD2MLIB) and distributed through mwongj Plugin Forks. License: [MIT](LICENSE). Earlier upstream release notes remain in [CHANGELOG.md](CHANGELOG.md).
