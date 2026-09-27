"""Glue between the Plugin class and the selection page inside Dispatcharr.

Nothing here imports Django at module level, so plugin.py (and the unit
tests) can import it anywhere.
"""
import logging
import os
import sys
import threading
import time

from . import server
from .apply import root_for, root_label, root_problem

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PORT = 9192

logger = logging.getLogger("vod2mlib.selection")

_service = None
_service_lock = threading.Lock()


def plugin_key():
    """Dispatcharr's registry key: folder name, lowercased, spaces as '_'."""
    return os.path.basename(PLUGIN_DIR).lower().replace(" ", "_")


def data_dir():
    explicit = os.environ.get("VOD2MLIB_DATA_DIR")
    if explicit:
        return explicit
    if os.path.isdir("/data"):
        return "/data/vod2mlib"
    return os.path.join(PLUGIN_DIR, ".selection-data")


def settings_with_defaults(plugin, saved):
    merged = {f["id"]: f["default"] for f in plugin.fields if "default" in f}
    merged.update(saved or {})
    return merged


def _load_config(plugin):
    from apps.plugins.models import PluginConfig
    return (PluginConfig.objects.filter(key=plugin_key()).first()
            or PluginConfig.objects.filter(name=plugin.name).first())


def _current_settings(plugin):
    from django.db import close_old_connections
    close_old_connections()
    try:
        cfg = _load_config(plugin)
    finally:
        # Page threads end without Django's request cycle: without this the
        # connection never goes back to Dispatcharr's pool (8 per process),
        # and once it is empty every query fails with gevent's LoopExit.
        close_old_connections()
    return settings_with_defaults(plugin, cfg.settings if cfg else {}), cfg


def get_service(plugin):
    global _service
    with _service_lock:
        if _service is None:
            from .catalogue import DjangoCatalogue
            from .store import SelectionStore
            _service = server.SelectionService(
                plugin=plugin,
                catalogue=DjangoCatalogue(plugin),
                store=SelectionStore(os.path.join(data_dir(), "selection.db")),
                settings_provider=lambda: _current_settings(plugin)[0],
                logger=logger,
            )
        return _service


def live_settings(plugin):
    """The plugin's saved settings, read now (the cron's own copy can be old)."""
    return _current_settings(plugin)[0]


def run_scheduled_upkeep(plugin, settings, logger):
    """Scheduled run in selection mode, on the Celery worker (not the page
    process); the disk lease in selection.db keeps it apart from Apply."""
    from .catalogue import DjangoCatalogue
    from .store import SelectionStore
    from .upkeep import run_upkeep
    store = SelectionStore(os.path.join(data_dir(), "selection.db"))
    return run_upkeep(plugin, DjangoCatalogue(plugin), store, settings, logger)


def port_from(settings):
    try:
        return int(settings.get("selection_port") or DEFAULT_PORT)
    except (TypeError, ValueError):
        return DEFAULT_PORT


def tick(plugin):
    """One watchdog pass: serve if selection mode is ON, stop if OFF."""
    settings, cfg = _current_settings(plugin)
    wanted = (
        cfg is not None and cfg.enabled
        and settings.get("selection_mode")
        and (settings.get("selection_password") or "").strip()
    )
    if not wanted:
        if server.owns_server():
            server.stop_server()
            logger.info("Selection page stopped (selection mode off or plugin disabled)")
        return
    server.start_server(get_service(plugin), port_from(settings))


def is_page_host(argv=None):
    """True in Dispatcharr's daphne (ASGI) process, the only one that serves
    the page. It exists in every Dispatcharr layout, runs plugin discovery,
    and isn't gevent-patched, so the page's database and file work can't
    stall video streams (uWSGI workers). The dvr Celery worker was the first
    choice, but a task worker is no place for a long-lived server."""
    argv = sys.argv if argv is None else argv
    return bool(argv) and "daphne" in os.path.normpath(argv[0]).split(os.sep)


def boot(plugin):
    """Called from Plugin.__init__. Starts the watchdog only in the page host
    inside a configured Django (i.e. Dispatcharr), never in tests or plain
    imports.

    Discovery can run while Django is still starting (apps not ready yet);
    that's fine, because the watchdog's first tick is delayed and failed
    ticks are retried."""
    if not is_page_host():
        return
    try:
        from django.conf import settings as django_settings
        if not django_settings.configured:
            return
    except ImportError:
        return
    server.start_watchdog(lambda: tick(plugin))


def shutdown():
    server.stop_watchdog()
    server.stop_server()


def _uptime(seconds):
    minutes = int(seconds // 60)
    return f"{minutes // 60}h {minutes % 60}m" if minutes >= 60 else f"{minutes}m"


def status(plugin, settings, service=None):
    """Result for the '[SELECTION] Page status' action. The click is handled
    by whichever web worker Dispatcharr picks, so this only reports: the
    serving process describes itself through the owner record."""
    if not settings.get("selection_mode"):
        return {"status": "ok", "message": "Selection mode is OFF. Turn it on in Settings, Save, then click this again."}
    if not (settings.get("selection_password") or "").strip():
        return {"status": "error", "message": "Set a Selection page password in Settings (required), Save, then click this again."}
    service = service or get_service(plugin)
    port = port_from(settings)
    running = server.port_in_use(port)
    owner = service.store.owner() if running else None
    if owner and owner.get("port") != port:
        owner = None  # a record left behind by a server on an old port
    if running:
        who = (f"pid {owner['pid']}, up {_uptime(time.time() - owner['started_at'])}"
               if owner else "owner unknown")
        parts = [f"Selection page is running ({who}) on container port {port}. "
                 f"Open http://<dispatcharr-host>:<host-port>/ using the host port "
                 f"you publish for {port} in docker-compose."]
    else:
        parts = [f"Selection page is not running on container port {port}. It starts "
                 f"within about 30 seconds of saving settings, in Dispatcharr's daphne "
                 f"process. If it stays down, check the Dispatcharr logs for 'vod2mlib.selection'."]
    problems = []
    for kind in ("movie", "series"):
        root = root_for(settings, kind)
        problem = root_problem(root)
        if problem:
            problems.append(problem)
        parts.append(f"{root_label(kind)} root folder: {problem}" if problem
                     else f"{root_label(kind)} root folder {root} is writable.")
    counts = service.store.counts()
    parts.append(f"On disk: {counts['movies_on_disk']} movies, {counts['series_on_disk']} series. "
                 f"{counts['pending']} pending.")
    ok = running and not problems
    return {"status": "ok" if ok else "error", "message": " ".join(parts),
            "port": port, "running": running, "pid": owner["pid"] if owner else None,
            "root_writable": not problems, **counts}
