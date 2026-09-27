"""Run the selection page locally against a fake catalogue. No Dispatcharr needed.

    python -m selection.devserver [--port 9192] [--out ./.dev-selection] [--no-login]

Password: dev (or pass --no-login to skip the login screen). Files are written under --out/Movies so you can inspect them.
"""
import argparse
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plugin import Plugin  # noqa: E402
from selection.catalogue import make_fake_catalogue  # noqa: E402
from selection.probe import ProbeError  # noqa: E402
from selection.server import SelectionService, start_server  # noqa: E402
from selection.store import SelectionStore  # noqa: E402

SAMPLE = {
    "accounts": {1: "Provider A", 2: "Provider B"},
    "movies": [
        {"name": "EN - The Matrix (1999)", "year": 1999, "tmdb_id": "603",
         "copies": [(1, 1001, "EN - Sci-Fi"), (2, 2001, "4K UHD Movies")]},
        {"name": "Cool Hand Luke 4K (1967) PAUL NEWMAN (1967)", "year": 1967, "tmdb_id": "378",
         "copies": [(1, 1002, "EN - Classics")]},
        {"name": "|FR| Amélie", "year": 2001, "copies": [(2, 2002, "FR - Films")]},
        {"name": "Whiplash 1080p HEVC (2014)", "year": 2014,
         "copies": [(1, 1003, "EN - Drama"), (2, 2003, "EN - Drama")]},
        {"name": "Blade Runner 2049", "year": 2017, "copies": [(1, 1004, "EN - Sci-Fi")]},
        {"name": "XXX Adult Title", "year": 2020, "copies": [(2, 2004, "FOR ADULTS")]},
    ] + [
        {"name": f"Filler Movie {i:03d}", "year": 1980 + i % 40,
         "copies": [(1 + i % 2, 3000 + i, f"EN - Genre {i % 5}")]}
        for i in range(1, 121)
    ],
    "series": [
        {"name": "EN - Breaking Bad (2008)", "year": 2008, "tmdb_id": "1396",
         "copies": [(1, "5001", "EN - Drama"), (2, "6001", "4K Series")],
         "episodes": {
             (1, "5001"): [(s, e, f"Episode {e}", 50000 + s * 100 + e) for s in (1, 2) for e in (1, 2, 3)],
             (2, "6001"): [(s, e, f"Episode {e}", 60000 + s * 100 + e) for s in (1, 2) for e in (1, 2, 3)],
         }},
        {"name": "P+ - 1883 (2021)", "year": 2021, "copies": [(2, "6002", "PARAMOUNT+")],
         "episodes": {(2, "6002"): [(1, e, f"Part {e}", 61000 + e) for e in range(1, 5)]}},
        {"name": "DE - Parallel Me (2025) (DE)", "year": 2025, "copies": [(2, "6003", "PARAMOUNT+")],
         "episodes": {}},
    ],
}


class _NoLoginService(SelectionService):
    def is_authenticated(self, token):
        return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9192)
    parser.add_argument("--out", default=os.path.join(os.getcwd(), ".dev-selection"))
    parser.add_argument("--no-login", action="store_true",
                        help="Skip the password screen (dev server only, bound to 127.0.0.1).")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logger = logging.getLogger("vod2mlib.selection.dev")

    plugin = Plugin()
    settings = {f["id"]: f["default"] for f in plugin.fields if "default" in f}
    settings.update(
        selection_mode=True,
        selection_password="dev",
        dispatcharr_url="http://192.168.1.10:9191",
        root_folder=os.path.join(args.out, "Movies"),
        series_root_folder=os.path.join(args.out, "Series"),
        category_exclude="FOR ADULTS",
    )
    def _fake_probe(url):
        time.sleep(1)
        if url.endswith("0"):
            raise ProbeError("the provider has no free connection (all in use); try again later")
        return {"quality": "1080", "height": 1080, "video_codec": "H264", "langs": ["EN", "ES", "CS"],
                "audio_tracks": 3, "subtitle_langs": ["EN", "NL", "DE", "FR"]}

    service_cls = _NoLoginService if args.no_login else SelectionService
    service = service_cls(
        plugin=plugin,
        catalogue=make_fake_catalogue(plugin, SAMPLE),
        store=SelectionStore(os.path.join(args.out, "selection.db")),
        settings_provider=lambda: settings,
        logger=logger,
    )
    service.probe_run = _fake_probe  # there is no Dispatcharr proxy to probe here
    if not start_server(service, args.port, host="127.0.0.1"):
        sys.exit(f"Port {args.port} is in use")
    print(f"Selection page: http://127.0.0.1:{args.port}/  (password: dev)")
    print(f"Output folders: {settings['root_folder']}, {settings['series_root_folder']}")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
