"""The single configuration path for VOD2MLIB's mount.

One schema, defined once, derived from the plugin settings the user edits in
Dispatcharr. The parent process builds a `MountConfig` from settings and exports
it to the environment (`to_env`); the mount child reads those same VOD2MLIB_*
names. There is no second, divergent config contract — the env-var names are
defined here and consumed verbatim by the mount modules.

Settings-derived knobs live on `MountConfig`. A few internal defaults that are not
user settings (size gate, bind host, probe) are read straight from VOD2MLIB_* env
by the modules that need them; their names are listed in `INTERNAL_ENV` so there is
a single inventory of the whole env contract.
"""

from dataclasses import dataclass

# Settings-derived env contract (parent -> child).
ENV_DISPATCHARR_URL = "VOD2MLIB_DISPATCHARR_URL"
ENV_ENABLE_AUTH = "VOD2MLIB_ENABLE_AUTH"
ENV_HYDRATE_CONCURRENCY = "VOD2MLIB_HYDRATE_CONCURRENCY"
ENV_HYDRATE_ON_LOAD = "VOD2MLIB_HYDRATE_ON_LOAD"
ENV_HYDRATE_TIMES = "VOD2MLIB_HYDRATE_TIMES"
ENV_HYDRATE_TZ = "VOD2MLIB_HYDRATE_TZ"

# Internal (non-settings) knobs with sane defaults, for the full env inventory.
INTERNAL_ENV = (
    "VOD2MLIB_BIND_HOST",          # listen host (default 0.0.0.0)
    "VOD2MLIB_REQUIRE_SIZE",       # size gate (default true)
    "VOD2MLIB_PROBE_SIZE",         # probe provider for size on open (default false)
    "VOD2MLIB_PROBE_CONCURRENCY",  # parallel probes (default 4)
    "VOD2MLIB_HYDRATE_MAX_MINUTES",
)


def _b(v) -> str:
    return "true" if (v is True or str(v).lower() in ("1", "true", "yes", "on")) else "false"


@dataclass
class MountConfig:
    """The mount's configuration, built from plugin settings."""
    dispatcharr_url: str = ""
    port: int = 8889
    enable_auth: bool = False
    hydrate_concurrency: int = 8
    hydrate_on_load: bool = True
    hydrate_times: str = "0300"
    hydrate_tz: str = "UTC"

    @classmethod
    def from_settings(cls, settings: dict) -> "MountConfig":
        s = settings or {}
        try:
            port = int(s.get("httpfs_port") or 8889)
        except (TypeError, ValueError):
            port = 8889
        try:
            conc = int(s.get("httpfs_hydrate_concurrency", 8))
        except (TypeError, ValueError):
            conc = 8
        return cls(
            dispatcharr_url=(s.get("dispatcharr_url") or "").strip().rstrip("/"),
            port=port,
            enable_auth=bool(s.get("httpfs_enable_auth", False)),
            hydrate_concurrency=conc,
            hydrate_on_load=bool(s.get("httpfs_hydrate_on_load", True)),
            hydrate_times=str(s.get("httpfs_scheduled_times", "0300") or ""),
            hydrate_tz=str(s.get("httpfs_timezone", "") or "UTC"),
        )

    def to_env(self) -> dict:
        """The exact VOD2MLIB_* environment the mount child reads."""
        return {
            ENV_DISPATCHARR_URL: self.dispatcharr_url,
            ENV_ENABLE_AUTH: _b(self.enable_auth),
            ENV_HYDRATE_CONCURRENCY: str(self.hydrate_concurrency),
            ENV_HYDRATE_ON_LOAD: _b(self.hydrate_on_load),
            ENV_HYDRATE_TIMES: self.hydrate_times,
            ENV_HYDRATE_TZ: self.hydrate_tz,
        }
