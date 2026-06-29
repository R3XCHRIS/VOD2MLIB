"""HTTP filesystem request handlers: directory listings + 302 file redirects."""

import asyncio
import html
import logging
from typing import List
from concurrent.futures import ThreadPoolExecutor

from fastapi import Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from jinja2 import Template
from urllib.parse import quote

try:
    from .tree import VirtualTree
    from .cache import LRUCache
except ImportError:
    from tree import VirtualTree
    from cache import LRUCache


logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=8)
_directory_cache = LRUCache(max_size=5000, ttl=300)


def shutdown_executor() -> None:
    _executor.shutdown(wait=False, cancel_futures=True)


_DIR_TEMPLATE = Template("""<!DOCTYPE html>
<html><head><title>Index of {{ path }}</title>
<style>body{font-family:monospace;padding:20px}table{border-collapse:collapse;width:100%}
th{text-align:left;padding:5px;border-bottom:1px solid #ccc}td{padding:5px}
a{text-decoration:none;color:#06c}a:hover{text-decoration:underline}</style></head>
<body><h1>Index of {{ path }}</h1><table>
<thead><tr><th>Name</th><th>Size</th></tr></thead><tbody>
{% for e in entries %}<tr><td><a href="{{ e.href }}">{{ e.name }}</a></td><td>{{ e.size }}</td></tr>
{% endfor %}</tbody></table></body></html>
""", autoescape=True)


# Streamed equivalent of _DIR_TEMPLATE for unbounded (movie/series) listings: same
# markup, emitted row-by-row so a 100k-entry directory never materialises in RAM.
_LISTING_HEAD = ("""<!DOCTYPE html>
<html><head><title>Index of {path}</title>
<style>body{{font-family:monospace;padding:20px}}table{{border-collapse:collapse;width:100%}}
th{{text-align:left;padding:5px;border-bottom:1px solid #ccc}}td{{padding:5px}}
a{{text-decoration:none;color:#06c}}a:hover{{text-decoration:underline}}</style></head>
<body><h1>Index of {path}</h1><table>
<thead><tr><th>Name</th><th>Size</th></tr></thead><tbody>
""")
_LISTING_TAIL = "</tbody></table></body></html>\n"


def _stream_row(name: str, href: str) -> str:
    return ('<tr><td><a href="%s">%s</a></td><td></td></tr>\n'
            % (html.escape(href, quote=True), html.escape(name)))


def _fmt_size(n: int) -> str:
    if not n:
        return ""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0:
            return f"{int(size)}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} PB"


def _dir_entry(name: str) -> dict:
    return {"name": name + "/", "href": quote(name) + "/", "size": ""}


def _file_entry(name: str, size: int) -> dict:
    return {"name": name, "href": quote(name), "size": _fmt_size(size)}


def _db_task(fn, *args):
    """Run a synchronous ORM call in a worker thread with connection hygiene.

    Django connections are thread-local. Under concurrent load a connection can be
    left in an aborted/unusable state; ``close_old_connections`` discards any such
    connection so the next query gets a fresh one (otherwise every subsequent
    request on that thread fails with a 500).
    """
    try:
        from django.db import close_old_connections
    except ImportError:
        return fn(*args)
    close_old_connections()
    try:
        return fn(*args)
    finally:
        close_old_connections()


class HTTPFilesystem:
    def __init__(self, tree: 'VirtualTree'):
        self.tree = tree

    async def handle_request(self, path: str, request: Request) -> Response:
        if request.method == "GET":
            return await self.handle_get(path, request)
        if request.method == "HEAD":
            return await self.handle_head(path, request)
        return Response(status_code=405, content="Method not allowed")

    async def _run(self, fn, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_executor, _db_task, fn, *args)

    async def handle_get(self, path: str, request: Request) -> Response:
        node = await self._run(self.tree.resolve_path_with_db, path)
        if node is None:
            return Response(status_code=404, content="Not found")
        if node.is_directory() and not path.endswith("/"):
            return RedirectResponse(url=quote(path) + "/", status_code=301)
        if node.is_directory():
            return await self.serve_directory(path)
        return RedirectResponse(url=node.metadata["stream_url"], status_code=302)

    async def handle_head(self, path: str, request: Request) -> Response:
        node = await self._run(self.tree.resolve_path_with_db, path)
        if node is None:
            return Response(status_code=404, content="Not found")
        if node.is_directory() and not path.endswith("/"):
            return RedirectResponse(url=quote(path) + "/", status_code=301)
        if node.is_directory():
            return Response(status_code=200,
                            headers={"content-type": "text/html; charset=utf-8"})
        return Response(status_code=200, headers={
            "Accept-Ranges": "bytes",
            "Content-Type": node.get_content_type(),
            "Content-Length": str(node.get_file_size()),
        })

    async def serve_directory(self, path: str) -> Response:
        comps = [c for c in path.split("/") if c]
        # The only unbounded listings are the movie/series folder lists
        # (/Movies/{All|cat}, /Series/{All|cat}) — stream those to bound memory.
        # Everything else (root, categories, a movie's files, a season's episodes)
        # is small, so keep the simple cached+rendered path.
        if len(comps) == 2 and comps[0] in ("Movies", "Series"):
            return self._stream_listing(path, comps)
        try:
            cached = _directory_cache.get(f"dir:{path}")
            if cached is None:
                cached = await self._build_listing(path)
                _directory_cache.set(f"dir:{path}", cached)
            html = _DIR_TEMPLATE.render(path=path, entries=cached)
            return HTMLResponse(content=html)
        except Exception:
            logger.exception("Failed to serve directory %s", path)
            return Response(status_code=500, content="Error listing directory")

    def _stream_listing(self, path: str, comps: List[str]) -> StreamingResponse:
        category = None if comps[1] == "All" else comps[1]
        source = (self.tree.movies_stream if comps[0] == "Movies"
                  else self.tree.series_stream)

        def rows():
            # Runs in Starlette's threadpool. Django connections are thread-local;
            # bracket the server-side cursor with connection hygiene.
            try:
                from django.db import close_old_connections
            except ImportError:
                close_old_connections = None
            if close_old_connections:
                close_old_connections()
            try:
                yield _LISTING_HEAD.format(path=html.escape(path))
                yield _stream_row("../", "../")
                for folder in source(category):
                    yield _stream_row(folder + "/", quote(folder) + "/")
                yield _LISTING_TAIL
            except Exception:
                logger.exception("Failed while streaming directory %s", path)
                # Response is already in flight; close the markup so clients don't hang.
                yield _LISTING_TAIL
            finally:
                if close_old_connections:
                    close_old_connections()

        return StreamingResponse(rows(), media_type="text/html; charset=utf-8")

    async def _build_listing(self, path: str) -> List[dict]:
        comps = [c for c in path.split("/") if c]
        top = comps[0] if comps else None
        parent = [{"name": "../", "href": "../", "size": ""}]

        if not comps:                                            # /
            return [_dir_entry("Movies"), _dir_entry("Series")]

        if top not in ("Movies", "Series"):
            return parent

        if len(comps) == 1:                                      # /Movies or /Series
            ct = 'movie' if top == "Movies" else 'series'
            cats = await self._run(self.tree.categories, ct)
            return parent + [_dir_entry("All")] + [_dir_entry(c) for c in cats]

        category = None if comps[1] == "All" else comps[1]

        if top == "Movies":
            if len(comps) == 2:                                  # /Movies/{All|cat}
                movies = await self._run(self.tree.movies, category)
                return parent + [_dir_entry(m['folder']) for m in movies]
            if len(comps) == 3:                                  # /Movies/{cat}/{folder}
                files = await self._run(self.tree.movie_files, comps[2], category)
                return parent + [_file_entry(f['filename'], f['size']) for f in files]
            return parent

        # Series
        if len(comps) == 2:                                      # /Series/{All|cat}
            shows = await self._run(self.tree.series, category)
            return parent + [_dir_entry(s['folder']) for s in shows]
        if len(comps) == 3:                                      # /Series/{cat}/{show}
            seasons = await self._run(self.tree.seasons, comps[2], category)
            return parent + [_dir_entry(s) for s in seasons]
        if len(comps) == 4:                                      # /Series/{cat}/{show}/Season NN
            eps = await self._run(self.tree.episodes, comps[2], comps[3], category)
            return parent + [_file_entry(e['filename'], e['size']) for e in eps]
        return parent
