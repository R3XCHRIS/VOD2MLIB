"""Standalone HTTP server runner with Django initialization"""

import os
import sys
import logging

# Configure logging before Django setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s"
)

logger = logging.getLogger(__name__)


def setup_django():
    """Initialize Django in the child process"""
    # Must be set before django.setup()
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "dispatcharr.settings")

    try:
        import django
        django.setup()
        logger.info("Django initialized successfully in child process")

        _use_blocking_db_backend()

        # Verify model access — the import itself is the check (raises if unavailable)
        from apps.vod.models import Movie, Series  # noqa: F401
        logger.info("VOD models accessible: Movie, Series")
        return True
    except Exception as e:
        logger.error("Failed to initialize Django: %s", e)
        return False


def _use_blocking_db_backend():
    """Swap Dispatcharr's gevent connection-pool DB backend for the standard
    blocking psycopg3 backend in THIS process.

    Dispatcharr serves its own workers under gevent (django-db-geventpool), whose
    cooperative connections require a running gevent hub. Our standalone server is
    asyncio (uvicorn) + a thread pool with no gevent hub, so the pooled connections
    raise `gevent LoopExit: This operation would block forever` under concurrency.
    The standard backend uses ordinary blocking connections — one per worker thread.
    """
    from django.conf import settings
    from django.db import connections

    db = settings.DATABASES.get("default", {})
    engine = db.get("ENGINE", "")
    if "geventpool" in engine or "psycopg3" in engine or engine.startswith("dispatcharr"):
        db["ENGINE"] = "django.db.backends.postgresql"
        db["OPTIONS"] = {}
        db["CONN_MAX_AGE"] = 60
        try:
            del connections["default"]
        except Exception:
            pass
        logger.info("Standalone DB backend set to standard blocking psycopg3 "
                    "(was %s)", engine)


def run_server(port: int = 8888):
    """Run the HTTP filesystem server with Django context"""
    if not setup_django():
        logger.error("Cannot start server without Django")
        sys.exit(1)

    # Add the mountsrv directory (for `from server import ...`) AND the plugin root
    # (for `import vodlib`, the shared core) to the path.
    plugin_dir = os.path.dirname(os.path.abspath(__file__))
    plugin_root = os.path.dirname(plugin_dir)
    for p in (plugin_dir, plugin_root):
        if p not in sys.path:
            sys.path.insert(0, p)

    # server.run_server builds the VirtualTree and the FastAPI app itself.
    from server import run_server as _run_server

    logger.info("Starting HTTP filesystem server on port %d", port)
    _run_server(port)


def main():
    """Entry point for child process"""
    import argparse

    parser = argparse.ArgumentParser(description="VOD HTTP Filesystem Server")
    parser.add_argument("--port", type=int, default=8888, help="Port to listen on")
    args = parser.parse_args()

    run_server(args.port)


if __name__ == "__main__":
    main()