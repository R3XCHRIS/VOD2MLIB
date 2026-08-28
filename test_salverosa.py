"""Generate version-grouped .strm for SalveRosa + 15h17 (fast ffprobe).
Run inside Dispatcharr: manage.py shell -c "exec(open('/data/plugins/vod2mlib/test_salverosa.py').read())"
"""
import logging, sys, os
logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger("salv")
import importlib.util, subprocess, json as _json
spec = importlib.util.spec_from_file_location("v", "/data/plugins/vod2mlib/plugin.py")
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
p = mod.Plugin()
from apps.vod.models import M3UMovieRelation, Movie

TITLES = ["SalveRosa", "15h17"]
root = "/VODS/Movies/_exemplos_pr1"
os.makedirs(root, exist_ok=True)

def fast_res(url, logger, fp="ffprobe"):
    try:
        r = subprocess.run([fp, "-v", "error", "-user_agent", "Mozilla/5.0",
            "-analyzeduration", "2000000", "-probesize", "1000000", "-timeout", "10000000",
            "-i", url, "-show_entries", "stream=width,codec_type", "-of", "json"],
            capture_output=True, text=True, timeout=10)
        j = _json.loads(r.stdout)
        v = [s for s in j.get("streams", []) if s.get("codec_type") == "video"]
        if not v: return "SD"
        w = v[0].get("width") or 0
        return "4K" if w>=3840 else "1080p" if w>=1920 else "720p" if w>=1280 else f"{w}p" if w else "SD"
    except Exception:
        return "SD"
p._detect_resolution = fast_res

total = 0
for TITLE in TITLES:
    print("=== %s ===" % TITLE)
    movies = Movie.objects.filter(name__icontains=TITLE)
    uuids = [m.uuid for m in movies]
    print("Movies:", [(m.id, m.name) for m in movies])
    rels = list(M3UMovieRelation.objects.filter(movie__uuid__in=uuids, m3u_account__is_active=True))
    print("Active relations:", len(rels))
    pending = {}
    for r in rels:
        name = r.movie.name or ""
        clean_base = p._clean_variant_name(name)
        variant = p._detect_variant(name, "", logger, "ffprobe")
        proxy_url = p._build_proxy_url("http://casa-dispatcharr:9191", "movie", r.movie.uuid, r.stream_id, False)
        res = p._detect_resolution(proxy_url, logger, "ffprobe")
        print("  stream=%s name=%r -> %s %s" % (r.stream_id, name, variant, res))
        grp = pending.setdefault(clean_base, {"movie": r.movie, "cat_name": "", "clean_base": clean_base, "groups": {}})
        key = (variant, res)
        if key not in grp["groups"]:
            grp["groups"][key] = (r, proxy_url)
    fmt = " - {variant} {res}"
    for cb, pend in pending.items():
        c, r, u, s, e, mt = p._flush_pending_movie(pend, root, False, False, "plex", False, False, fmt, False, False, logger)
        total += c
        print("Flushed %s: created=%s" % (cb, c))
print("DONE total created:", total)
