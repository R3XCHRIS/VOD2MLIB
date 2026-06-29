"""FastAPI HTTP server for VOD filesystem - live DB queries"""

import asyncio
import logging
import os
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import Response, JSONResponse
from starlette.middleware.gzip import GZipMiddleware

try:
    from .tree import VirtualTree
    from .httpfs import HTTPFilesystem, shutdown_executor, _directory_cache
    from .integration import DispatcharrIntegrator
except (ImportError, AttributeError):
    from tree import VirtualTree
    from httpfs import HTTPFilesystem, shutdown_executor, _directory_cache
    from integration import DispatcharrIntegrator

from vodlib.config import ENV_ENABLE_AUTH, ENV_BIND_HOST

logger = logging.getLogger(__name__)

_ENABLE_AUTH = os.environ.get(ENV_ENABLE_AUTH, "false").lower() == "true"
# Bind host. Defaults to 0.0.0.0: the server runs inside the Dispatcharr
# container and must be reachable through Docker's published port by rclone/Plex,
# which a 127.0.0.1-only listener prevents. Lock down with VOD2MLIB_BIND_HOST and/or
# enable_auth + Dispatcharr's STREAMS network policy when exposing it.
_BIND_HOST = os.environ.get(ENV_BIND_HOST, "0.0.0.0")


class _DjangoRequestShim:
    """Adapt a Starlette request to the ``.META`` interface Dispatcharr's
    ``network_access_allowed`` expects (it was written for Django requests and reads
    ``request.META["HTTP_X_REAL_IP"]``/``["REMOTE_ADDR"]``). Without this shim the
    policy hook raises ``AttributeError`` on every call — which the previous code
    swallowed, silently disabling the STREAMS check. Wiring it correctly makes the
    gate actually enforce."""
    def __init__(self, request: Request):
        meta = {"REMOTE_ADDR": (request.client.host if request.client else "127.0.0.1")}
        xri = request.headers.get("x-real-ip")
        if xri:
            meta["HTTP_X_REAL_IP"] = xri
        self.META = meta


def _refresh_db_connection():
    """Discard a stale/closed thread-local Django connection before an ORM call.

    FastAPI runs these sync dependencies in a threadpool. Django connections are
    thread-local and can be left closed/aborted between requests, so the first
    query on that thread raises 'connection already closed' — which the fail-closed
    gate below turns into a blanket 403 (rclone then sees the mount as I/O errors).
    The tree path already brackets its queries this way via _db_task; the auth and
    network gates need the same hygiene."""
    try:
        from django.db import close_old_connections
    except ImportError:
        return
    close_old_connections()


def check_network_access(request: Request):
    """Enforce Dispatcharr's STREAMS network-access policy. Fail CLOSED: deny if the
    hook can't be imported or errors — this is the primary protection when auth is
    off. (The STREAMS policy defaults to allow-all; operators restrict it in
    Dispatcharr settings, and only then does this gate narrow access.)"""
    try:
        from dispatcharr.utils import network_access_allowed
    except (ImportError, AttributeError):
        logger.error("network policy hook unavailable — denying request (fail-closed)")
        raise HTTPException(status_code=403, detail="Forbidden")
    _refresh_db_connection()
    try:
        allowed = network_access_allowed(_DjangoRequestShim(request), "STREAMS")
    except Exception as e:
        # Clear the poisoned connection so the next request recovers instead of
        # latching this thread into permanent denial.
        _refresh_db_connection()
        logger.error("network policy check errored (%s) — denying request (fail-closed)", e)
        raise HTTPException(status_code=403, detail="Forbidden")
    if not allowed:
        raise HTTPException(status_code=403, detail="Forbidden")


def check_api_key_auth(request: Request):
    """Validate Dispatcharr API key from Authorization or X-API-Key header"""
    if not _ENABLE_AUTH:
        return

    api_key = request.headers.get("x-api-key")

    if not api_key:
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("apikey "):
            api_key = auth_header[7:].strip()

    if not api_key:
        raise HTTPException(
            status_code=401,
            detail="API key required",
            headers={"WWW-Authenticate": "ApiKey"}
        )

    _refresh_db_connection()
    try:
        from django.contrib.auth import get_user_model
        User = get_user_model()
        user = User.objects.get(api_key=api_key, is_active=True)
        request.state.auth_user = user
    except User.DoesNotExist:
        raise HTTPException(
            status_code=401,
            detail="Invalid API key",
            headers={"WWW-Authenticate": "ApiKey"}
        )


def _check_django_available():
    """Log whether Django/VOD models are reachable at startup (no state kept)."""
    if DispatcharrIntegrator().is_available():
        logger.info("Django available - all queries will be live against DB")
    else:
        logger.warning("Django/VOD models not available - listings will be empty")


def _external_base_url(request) -> str:
    """Reconstruct the URL the client actually reached us on, honouring reverse-proxy
    headers. Behind a reverse proxy (e.g. Dispatcharr's nginx at location /vod2mlib/),
    the browser hits /vod2mlib/rclone_conf and these headers carry the real host +
    prefix — so the rclone config points at the right place with no IP to get wrong."""
    h = request.headers
    proto = h.get("x-forwarded-proto") or request.url.scheme or "http"
    host = h.get("x-forwarded-host") or h.get("host") or request.url.netloc
    prefix = (h.get("x-forwarded-prefix") or "").rstrip("/")
    return f"{proto}://{host}{prefix}"


def _build_rclone_config(base_url: str) -> str:
    """Build copy/paste-ready rclone config and mount notes."""
    base_url = base_url.rstrip("/") + "/"
    if _ENABLE_AUTH:
        auth_note = "# Secured installs: replace <your-dispatcharr-api-key> with an active Dispatcharr API key."
        auth_line = "headers = Authorization, ApiKey <your-dispatcharr-api-key>"
    else:
        auth_note = "# Secured installs: enable plugin auth, then uncomment the headers line and replace the placeholder."
        auth_line = "# headers = Authorization, ApiKey <your-dispatcharr-api-key>"

    return f"""# VOD2MLIB rclone remote (Plex mount mode)
# Paste the [vod2mlib] block into your rclone.conf file.
# Suggested mount point: /mnt/vod2mlib
# Mount command:
#   sudo mkdir -p /mnt/vod2mlib
#   rclone mount vod2mlib: /mnt/vod2mlib --allow-other --read-only \\
#     --vfs-cache-mode off --dir-cache-time 1h --poll-interval 0 --daemon
#
# CACHE MODE — start with OFF (the default above):
#   cache=off issues direct Range requests to the proxy and uses no local disk. This
#   is the safe default: a Plex library SCAN opens every file's header for analysis,
#   and with cache=full those reads (especially mp4 'moov' atoms at end-of-file) can
#   download tens of GB and fill your disk. With cache=off a scan costs nothing on disk.
#   Switch to cache=full ONLY if playback seeking thrashes your provider, and then
#   BOUND it: --vfs-cache-mode full --cache-dir /var/cache/vod2mlib
#            --vfs-cache-max-size 20G --vfs-cache-max-age 24h
#   (Pick a max-size that fits your free disk — an unbounded cache will fill it.)
# Plex library paths (prefer per-category dirs on large libraries; /All can be huge):
#   Movies: /mnt/vod2mlib/Movies/<Category>   (or /mnt/vod2mlib/Movies/All)
#   Series: /mnt/vod2mlib/Series/<Category>   (or /mnt/vod2mlib/Series/All)
#
# AVOID HAMMERING YOUR PROVIDER (important at scale):
#   - The mount serves stored/estimated sizes (no provider probe), so rclone's per-file
#     HEAD during a scan is cheap and only queries Dispatcharr. Keep 'no_head = false'.
#   - In Plex, DISABLE deep analysis during scan (Settings > Library): uncheck
#     "Analyze audio tracks"/"Perform extensive media analysis"; set thumbnail/preview
#     generation to never. Plex opening every file for analysis is what slams the provider.
#   - In Dispatcharr, set a per-provider MAX CONNECTIONS on each M3U account. The mount
#     only issues 302 redirects (never in the data path), so that cap is the throttle.
{auth_note}

[vod2mlib]
type = http
url = {base_url}
no_head = false
{auth_line}
"""


def _query_stats_sync() -> dict:
    """Synchronous ORM portion of /stats. Returns counts only."""
    try:
        from apps.vod.models import M3UMovieRelation, M3USeriesRelation
        from django.db.models import Count
    except ImportError:
        return {"available": False}

    try:
        from .tree import _enabled, _MOVIE_SIZED, _SERIES_HAS_SIZED_EP
    except ImportError:
        from tree import _enabled, _MOVIE_SIZED, _SERIES_HAS_SIZED_EP
    enabled = _enabled()
    # Same predicate but for *inactive* accounts: content that would be listed if
    # the provider were re-activated. Surfacing it explains a sudden drop in counts.
    orphaned = dict(enabled, **{"m3u_account__is_active": False})

    def per_category(model):
        rows = (
            model.objects.filter(**enabled)
            .values("category__name")
            .annotate(n=Count("id", distinct=True))
            .order_by("-n")
        )
        return {r["category__name"]: r["n"] for r in rows if r["category__name"]}

    def total(model):
        return model.objects.filter(**enabled).distinct().count()

    def orphaned_total(model):
        return model.objects.filter(**orphaned).distinct().count()

    # "sized" = how many are actually visible under the size gate (have a known
    # size). The gap to total is the movie backfill's remaining work.
    movies_sized = M3UMovieRelation.objects.filter(**enabled, **_MOVIE_SIZED).distinct().count()
    series_sized = M3USeriesRelation.objects.filter(**enabled, **_SERIES_HAS_SIZED_EP).distinct().count()

    return {
        "available": True,
        "movies": {
            "total": total(M3UMovieRelation),
            "sized": movies_sized,
            "by_category": per_category(M3UMovieRelation),
        },
        "series": {
            "total": total(M3USeriesRelation),
            "sized": series_sized,
            "by_category": per_category(M3USeriesRelation),
        },
        "orphaned": {
            "movies": orphaned_total(M3UMovieRelation),
            "series": orphaned_total(M3USeriesRelation),
        },
    }


async def _collect_stats() -> dict:
    """Run stats query off the event loop and merge in cache info."""
    loop = asyncio.get_event_loop()
    library = await loop.run_in_executor(None, _query_stats_sync)
    return {
        "library": library,
        "cache": _directory_cache.stats(),
        "auth_enabled": _ENABLE_AUTH,
    }


def create_app(tree: VirtualTree) -> FastAPI:
    """Create FastAPI application with HTTP filesystem handlers"""
    try:
        from .hydrator import Hydrator
    except ImportError:
        from hydrator import Hydrator
    hydrator = Hydrator()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _check_django_available()
        hydrator.start()
        try:
            yield
        finally:
            hydrator.stop()
            shutdown_executor()
            logger.info("Shutdown complete")

    app = FastAPI(title="VOD HTTP Filesystem", lifespan=lifespan)
    # The directory tree is large, highly repetitive HTML — the /Movies/All and
    # /Series/All listings can be megabytes of <a> rows. gzip cuts that ~10x on the
    # wire. Starlette compresses streaming responses chunk-by-chunk (it never buffers
    # the whole body), so the row-streamed listings stay O(chunk) in memory. Empty
    # bodies (302 file redirects, healthz) fall under minimum_size and are untouched,
    # so the playback path carries no overhead. Level 6 balances ratio vs CPU on text.
    app.add_middleware(GZipMiddleware, minimum_size=1000, compresslevel=6)
    httpfs = HTTPFilesystem(tree)

    @app.post("/hydrate/run")
    async def hydrate_run(
        _network=Depends(check_network_access),
        _auth=Depends(check_api_key_auth),
    ):
        """Kick an immediate hydration pass (the 'Hydrate Now' action). Mutating +
        triggers provider fetches, so it carries the same network/auth gate as the
        file endpoints — never leave it open to any caller on a published port."""
        ok = hydrator.trigger_now()
        return JSONResponse(content={"triggered": ok, "status": hydrator.status()})

    @app.get("/hydrate/status")
    async def hydrate_status(
        _network=Depends(check_network_access),
        _auth=Depends(check_api_key_auth),
    ):
        return JSONResponse(content=hydrator.status())

    @app.get("/healthz")
    async def healthz():
        """Basic liveness check (no library/state disclosure)."""
        return Response(status_code=200, content="OK")

    @app.get("/rclone_conf")
    async def rclone_conf(
        request: Request,
        _network=Depends(check_network_access),
        _auth=Depends(check_api_key_auth),
    ):
        """Return copy/paste-ready rclone configuration, with the remote URL derived
        from the request (reverse-proxy aware) — no configured IP to get wrong."""
        return Response(
            content=_build_rclone_config(_external_base_url(request)),
            media_type="text/plain; charset=utf-8",
        )

    @app.get("/stats")
    async def stats(
        _network=Depends(check_network_access),
        _auth=Depends(check_api_key_auth),
    ):
        """Return library visibility counts.

        Answers the operator question: 'is the plugin actually seeing
        my library?' Counts only — no titles, URLs, or credentials.
        Uses the same enabled-category/same-account predicate the
        directory listings use, so the numbers reflect what rclone
        and Plex will see.
        """
        return JSONResponse(content=await _collect_stats())

    @app.api_route("/{path:path}", methods=["GET", "HEAD"])
    async def handle_request(
        path: str,
        request: Request,
        _network=Depends(check_network_access),
        _auth=Depends(check_api_key_auth),
    ):
        """Handle all filesystem requests"""
        if not path.startswith("/"):
            path = "/" + path
        return await httpfs.handle_request(path, request)

    @app.get("/")
    async def root(
        request: Request,
        _network=Depends(check_network_access),
        _auth=Depends(check_api_key_auth),
    ):
        """Root endpoint"""
        return await httpfs.handle_request("/", request)

    return app


def run_server(port: int, log_level: str = "info"):
    """Run the FastAPI server using uvicorn"""
    app = create_app(VirtualTree())

    logger.info("Uvicorn starting on %s:%d (auth: %s)", _BIND_HOST, port,
                "enabled" if _ENABLE_AUTH else "disabled")
    uvicorn.run(
        app,
        host=_BIND_HOST,
        port=port,
        log_level=log_level,
        # Off: per-request access logging appended to mountsrv/server.log unbounded.
        # Plex/rclone hammer the mount, so this file grew without limit. Startup
        # lines and tracebacks (tiny, bounded) still land in the log.
        access_log=False,
    )
