"""The single playback path for VOD2MLIB.

There is exactly one place that knows how a playable VOD URL is formed: a `302`
target into Dispatcharr's native VOD proxy, carrying the provider `stream_id`.

- the .strm generator writes this URL as the contents of each .strm file;
- the HTTP mount returns this URL as the `Location` of its file-open redirect.

Same function, same URL, both outputs. Change the proxy contract here and both
delivery formats follow.
"""

from urllib.parse import urlparse

_VALID_CONTENT = ("movie", "episode")


def proxy_url(base_url: str, content_type: str, uuid: str, stream_id) -> str:
    """Dispatcharr VOD proxy URL for a single provider stream.

    The proxy streams the real container with HTTP Range support and a correct
    Content-Length, so media servers can seek, analyse, and direct-play.
    """
    if content_type not in _VALID_CONTENT:
        raise ValueError("Unknown content type: %s" % content_type)
    base = (base_url or "").rstrip("/")
    return "%s/proxy/vod/%s/%s?stream_id=%s" % (base, content_type, uuid, stream_id)


def validate_base_url(url: str):
    """Return an error string if the Dispatcharr base URL can never produce a working
    redirect (empty / no scheme / non-HTTP / no host), else None. Reachability-agnostic
    — 127.0.0.1 is valid for same-host setups."""
    value = (url or "").strip()
    if not value:
        return "Dispatcharr URL is empty — set it in the plugin settings."
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https"):
        return "Dispatcharr URL must start with http:// or https:// (got %r)." % value
    if not parsed.netloc:
        return "Dispatcharr URL has no host (got %r)." % value
    return None
