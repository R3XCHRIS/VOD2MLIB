"""Archive filter/ownership-excluded NFO-only output, including legacy metadata.

NFO contents never supply filter metadata. Complete native relation projections
establish exact output paths and protect every path with a passing source.
"""

import errno
import json
import os
import shutil
import stat
import uuid
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

try:
    from .inventory import LOOKUP_BATCH_SIZE, contained, file_hash
    from .media_library import Identity
except ImportError:
    from inventory import LOOKUP_BATCH_SIZE, contained, file_hash
    from media_library import Identity


def plan(rec, kinds, rules=None):
    from apps.vod.models import M3UMovieRelation, M3USeriesRelation

    paths = defaultdict(list)
    roots = [Path(root).resolve() for root in rec.roots]
    overlap = roots[0] == roots[1] or roots[0] in roots[1].parents or roots[1] in roots[0].parents
    read_kinds = ('movie', 'series') if overlap else kinds
    count = 0
    prefix = 'ownership' if rules is None else 'filter'
    phase = f'{prefix}_nfo_metadata_read'
    with rec.measure(phase):
        for kind in read_kinds:
            model = M3UMovieRelation if kind == 'movie' else M3USeriesRelation
            fields = ['id', 'name', 'year', 'tmdb_id', 'imdb_id']
            if rules is not None:
                fields.append('rating')
            if kind == 'series' and rules is not None:
                fields.append('genre')
            query = model.objects.values_list(
                *(f'{kind}__{field}' for field in fields), 'category__name')
            # Include disabled accounts/categories: a passing source still protects
            # shared output. Finish all lookups before any filter removal occurs.
            for row in query.iterator(chunk_size=LOOKUP_BATCH_SIZE):
                obj = SimpleNamespace(**dict(zip(fields, row[:-1])))
                if rules is not None:
                    passed, rejected, _ = rules[kind].evaluate(
                        rating=obj.rating, release_year=obj.year,
                        genre=getattr(obj, 'genre', None), title=obj.name)
                else:
                    title, title_year = rec.plugin._extract_clean_name_and_year(obj.name or '')
                    title, year = rec.plugin._strip_redundant_trailing_year(title, obj.year or title_year)
                    identity = Identity(kind, title, year, str(obj.tmdb_id or '').strip().lower(),
                                        str(obj.imdb_id or '').strip().lower())
                    # Episode mode may retain missing VOD episodes; whole-folder
                    # metadata cannot establish ownership of those positions.
                    owned = (kind == 'movie' or rec.settings.get('media_tv_mode', 'show') == 'show') and rec.snapshot.owns(identity)
                    passed, rejected = not owned, ['media_library'] if owned else []
                root = rec.roots[0 if kind == 'movie' else 1]
                settings = rec.settings
                args = (obj, root, row[-1] or '',
                        bool(settings.get(f'nest_{"movies" if kind == "movie" else "series"}_by_category', False)),
                        bool(settings.get('append_tmdb_id_to_folder', False)),
                        settings.get('tmdb_tag_format') or 'plex')
                folder = (rec.plugin._movie_target_paths(*args)[0] if kind == 'movie'
                          else rec.plugin._series_target_folder(*args)[0])
                paths[(kind, root, folder)].append(
                    {'id': obj.id, 'passed': passed, 'rejected': list(rejected)})
                count += 1
    rec.report['timings'][phase]['items'] += count
    # Also protect cross-kind collisions when output roots overlap.
    passing = {folder for (_, _, folder), sources in paths.items()
               if any(source['passed'] for source in sources)}
    return [(kind, root, folder, sources) for (kind, root, folder), sources in paths.items()
            if kind in kinds and folder not in passing]


def snapshot(folder, ignored=()):
    """Only regular NFO files and real directories may be archived."""
    files = {}
    pending = [folder]
    while pending:
        parent = pending.pop()
        with os.scandir(parent) as entries:
            for entry in entries:
                path = Path(entry.path)
                info = path.lstat()
                if stat.S_ISREG(info.st_mode) and str(path) in ignored:
                    continue
                if stat.S_ISDIR(info.st_mode):
                    pending.append(path)
                elif stat.S_ISREG(info.st_mode) and path.suffix.lower() == '.nfo':
                    files[str(path.relative_to(folder))] = (
                        info.st_ino, info.st_size, info.st_mtime_ns)
                else:
                    raise ValueError('contains a STRM, symlink, or unrelated file')
    if not files:
        raise ValueError('contains no NFO files')
    return files


def move(folder, target, before):
    """Rename when possible; verified copies precede cross-device removal."""
    if snapshot(folder) != before:
        raise ValueError('metadata changed during archive preparation')
    try:
        os.rename(folder, target)
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise
    else:
        try:
            if snapshot(target) != before:
                raise ValueError('folder changed during rename; archive cancelled')
        except (ValueError, OSError):
            os.rename(target, folder)
            raise
        return
    # An interrupted copy leaves metadata in the library; backups are outside it.
    shutil.copytree(folder, target, symlinks=True)
    copied = snapshot(target)
    if copied.keys() != before.keys() or snapshot(folder) != before:
        raise ValueError('metadata changed during archive copy; originals retained')
    hashes = {name: file_hash(target / name) for name in before}
    if any(file_hash(folder / name) != digest for name, digest in hashes.items()):
        raise ValueError('archive verification failed; originals retained')
    # Leave tvshow.nfo until last, so partial failures remain discoverable on retry.
    for name in sorted(before, key=lambda name: Path(name).name == 'tvshow.nfo'):
        source = folder / name
        info = source.lstat()
        if (not stat.S_ISREG(info.st_mode)
                or (info.st_ino, info.st_size, info.st_mtime_ns) != before[name]
                or file_hash(source) != hashes[name]):
            raise ValueError('metadata changed during removal; changed originals retained')
        source.unlink()
    for parent, dirs, files in os.walk(folder, topdown=False, followlinks=False):
        Path(parent).rmdir()  # Never recursively delete newly appeared files.


def archive(rec, candidates, dry_run=False, prefix='filter'):
    if prefix not in ('filter', 'ownership'):
        raise ValueError('Invalid NFO cleanup policy')
    archive_root = Path(rec.store.db_path).parent / 'filtered-nfo'
    run = archive_root / uuid.uuid4().hex
    manifest = None
    removals = dict(getattr(rec, 'filter_preview_removals', {})) if dry_run else {}
    if dry_run and prefix == 'ownership':
        removals.update(getattr(rec, 'cleanup_preview_removals', {}))
    ignored = set(removals)
    shared = set()
    for nfos in removals.values():
        for name, digest in nfos.items():
            if contained(name, rec.roots) and not os.path.islink(name) and file_hash(name) == digest:
                if Path(name).name == 'tvshow.nfo':
                    shared.add(name)
                else:
                    ignored.add(name)
    for name in shared:
        if not any(str(path) not in ignored for path in Path(name).parent.rglob('*.strm')):
            ignored.add(name)
    # Failed inventory finalization must remain retryable; do not move its NFOs.
    tracked = {str(parent) for (path,) in rec.store.db.execute('SELECT path FROM files')
               if path not in removals for parent in Path(path).parents}
    inspected_files = 0
    preview_folders = getattr(rec, 'nfo_preview_archives', set())
    rec.nfo_preview_archives = preview_folders
    rec.progress(f'Checking {prefix} exclusions for NFO-only folders')
    try:
        with rec.measure(f'{prefix}_nfo_archive'):
            for kind, root, name, sources in candidates:
                folder = Path(name)
                if dry_run and name in preview_folders:
                    continue
                if not os.path.lexists(folder):
                    continue
                rec.report[f'{prefix}_nfo_folders_checked'] += 1
                if rec.report[f'{prefix}_nfo_folders_checked'] % 100 == 0:
                    rec.progress(f"{prefix.title()} NFO-only cleanup: {rec.report[f'{prefix}_nfo_folders_checked']:,} folders checked, "
                                 f"{rec.report[f'{prefix}_nfo_folders_archived']:,} archived")
                try:
                    if (not contained(folder, [root])
                            or any(part.is_symlink() for part in [folder, *folder.parents])
                            or any(Path(other).resolve() == folder.resolve()
                                   or folder.resolve() in Path(other).resolve().parents
                                   for other in rec.roots)):
                        raise ValueError('unsafe or overlapping output folder')
                    before = snapshot(folder, ignored)
                    inspected_files += len(before)
                    if str(folder) in tracked:
                        raise ValueError('recorded output remains; retry cleanup first')
                    if (any(run.resolve() == Path(other).resolve()
                            or Path(other).resolve() in run.resolve().parents for other in rec.roots)
                            or any(part.is_symlink() for part in [archive_root, *archive_root.parents])):
                        raise ValueError('archive must be outside output roots without symlinks')
                    if dry_run:
                        rec.report[f'{prefix}_nfo_folders_candidates'] += 1
                        rec.report[f'{prefix}_nfo_candidates'] += len(before)
                        preview_folders.add(name)
                        continue
                    target = run / kind / folder.relative_to(root)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if manifest is None:
                        fd = os.open(run / 'manifest.jsonl', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        manifest = os.fdopen(fd, 'w', encoding='utf8')
                    record = {'source': str(folder), 'archive': str(target),
                              'kind': kind, 'reason': prefix, 'sources': sources, 'nfo_files': len(before)}
                    manifest.write(json.dumps({'event': 'planned', **record}) + '\n')
                    manifest.flush()
                    os.fsync(manifest.fileno())
                    move(folder, target, before)
                    manifest.write(json.dumps({'event': 'archived', **record}) + '\n')
                    manifest.flush()
                    os.fsync(manifest.fileno())
                    rec.report[f'{prefix}_nfo_folders_archived'] += 1
                    rec.report[f'{prefix}_nfo_archived'] += len(before)
                    rec.report[f'{prefix}_nfo_archive_root'] = str(archive_root)
                    rec.logger.info('Archived %s-excluded NFO-only folder: %s -> %s', prefix, folder, target)
                except ValueError as error:
                    rec.report[f'{prefix}_nfo_folders_preserved'] += 1
                    rec.logger.info('Protected NFO folder %s: %s', folder, error)
                except OSError as error:
                    rec.report[f'{prefix}_nfo_errors'] += 1
                    rec.report['errors'] += 1
                    rec.warning(f'Could not archive {prefix}-excluded NFO folder {folder}: {error}')
    finally:
        if manifest is not None:
            manifest.close()
    rec.report['timings'][f'{prefix}_nfo_archive']['items'] += inspected_files
