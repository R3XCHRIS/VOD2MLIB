"""vod2mlib_core — the single shared core for VOD2MLIB.

Every concern that used to be duplicated across the .strm generator and the HTTP
mount lives here exactly once:

- naming   : the one title-cleaning + folder/file naming path
- playback : the one proxy-URL resolution path
- config   : the one configuration schema

Both delivery formats — the .strm writer (in plugin.py) and the HTTP mount (in
mountsrv/) — are thin adapters over this core. There is no second naming engine,
no second URL builder, no second config contract.
"""
