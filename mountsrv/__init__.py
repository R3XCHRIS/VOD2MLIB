"""HTTP server package exports.

Vendored from VODFS (https://github.com/OneHotTake/vodfs) — MIT, Copyright (c)
2026 OneHotTake. See mountsrv/LICENSE. Provides VOD2MLIB's optional Plex mount mode.

``create_app``/``run_server`` live in ``server`` which imports uvicorn/fastapi.
Those deps only exist inside the spawned child process, so we expose them lazily
(PEP 562) — importing this package must not require them. This lets Dispatcharr's
plugin loader import the package in-process before the child deps are installed.
"""

__all__ = ["create_app", "run_server"]


def __getattr__(name):
    if name in __all__:
        from . import server
        return getattr(server, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")