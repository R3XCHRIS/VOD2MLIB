# mwongj fork installation and distribution

This file documents the separately packaged mwongj build. The shared [README](README.md) describes plugin behavior, configuration, storage mappings, and development using the upstream project identity.

## Install and upgrade

1. Map persistent output storage into Dispatcharr and make the same files visible to your media server. Defaults are `/VODS/Movies` and `/VODS/Series`; see [Sharing the VODs folder](#sharing-the-vods-folder-with-media-servers).
2. In Dispatcharr's plugin repositories, add **mwongj Plugin Forks** using this manifest URL:

   ```text
   https://raw.githubusercontent.com/mwongj/Dispatcharr-Plugins/releases/manifest.json
   ```

3. Select **VOD to Media Library (mwongj fork)** from that repository and install it. Alternatively, import `vod2mlib-1.20.2.zip` from the [fork distribution releases](https://github.com/mwongj/Dispatcharr-Plugins/releases).
4. Enable the plugin, configure reachable paths and the Dispatcharr URL, and click **Save**.

The source repository is [mwongj/VOD2MLIB](https://github.com/mwongj/VOD2MLIB); ZIPs and update manifests are published by [mwongj/Dispatcharr-Plugins](https://github.com/mwongj/Dispatcharr-Plugins). The official catalogue's upstream plugin is a separate distribution. Use **mwongj Plugin Forks** for this fork's updates. The identifier remains `vod2mlib`, so installing the fork over an existing managed installation keeps the same settings and schedule identity; the two distributions are not intended to run as separate plugins.

Requires Dispatcharr **v0.24.0 or later**. Scheduling uses Django, Celery, and django-celery-beat supplied by Dispatcharr. Keep the `/data` volume persistent: plugin state lives outside the installation at `/data/vod2mlib`. Existing schedules retain their enabled state on upgrade; fresh installations default to scheduling disabled. After an upgrade, reload the plugin and ensure idle Celery workers load the current plugin task code before using the schedule. This fork does not modify Dispatcharr source files.


## Contributing upstream

Keep functional changes and shared documentation compatible with R3XCHRIS/VOD2MLIB. Preserve the original authors, copyright notices, MIT license, plugin identifier, and task identities. Fork-specific installation instructions belong here or in the distribution repository; exclude this file from upstream contribution commits.

Create short-lived contribution branches from upstream for each coherent change. Include the relevant feature commits and shared documentation, then retire the branch after the upstream PR is resolved.

## Packaging this fork

[mwongj/Dispatcharr-Plugins](https://github.com/mwongj/Dispatcharr-Plugins) pins a source commit and builds the ZIP. Its definition supplies the fork display name and release version; the publisher applies the fork author, repository/help links, and Settings → About documentation link without changing the shared source. Copyright notices, Credits, and LICENSE remain intact.

Use a new release version when advancing the pinned source or changing package contents. Existing release ZIPs and checksums remain immutable. The already-published stable 1.20.2 package is unchanged by this documentation split.
