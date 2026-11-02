"""URL, handle, language, and timestamp normalization."""

from __future__ import annotations

import html
import ipaddress
import re
import socket
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

_TRACKING_PREFIXES = ("utm_",)
_TRACKING_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid", "igshid", "ref_src", "ref_url"}
_PERSIAN_LETTERS = re.compile(r"[\u067e\u0686\u0698\u06af]")
_ARABIC_SCRIPT = re.compile(r"[\u0600-\u06ff]")
_LATIN = re.compile(r"[A-Za-z]")
_IPV4_LITERAL = re.compile(
    r"^(?:0x[0-9a-f]+|\d+)(?:\.(?:0x[0-9a-f]+|\d+)){0,3}$",
    re.IGNORECASE,
)
_UNDETERMINED_LANGS = {"und", "zxx", "mul", "mis", "art", "qme", "qht", "qam", "qaa"}
_YOUTUBE_HOSTS = {"youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
_REDDIT_HOSTS = {"reddit.com", "old.reddit.com", "m.reddit.com", "np.reddit.com", "www.reddit.com"}


def is_public_http_url(url: str) -> bool:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    return _is_public_host(parsed.hostname)


def _is_public_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def _is_public_host(host: str) -> bool:
    lowered = host.strip("[]").lower().rstrip(".")
    if lowered in {"localhost", "0.0.0.0"} or lowered.endswith(".local"):
        return False
    if ":" in lowered:
        try:
            address = ipaddress.ip_address(lowered)
        except ValueError:
            return False
        return _is_public_ip(address)
    if _IPV4_LITERAL.match(lowered):
        try:
            packed = socket.inet_aton(lowered)
        except OSError:
            return False
        return _is_public_ip(ipaddress.IPv4Address(packed))
    try:
        address = ipaddress.ip_address(lowered)
    except ValueError:
        return True
    return _is_public_ip(address)


def _bare_host(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def _youtube_id(host: str, path: str, query: list[tuple[str, str]]) -> str | None:
    bare = _bare_host(host)
    if bare not in _YOUTUBE_HOSTS and host not in _YOUTUBE_HOSTS:
        return None
    if bare == "youtu.be":
        parts = [part for part in path.split("/") if part]
        return parts[0] if parts else None
    found = dict(query).get("v")
    if found:
        return found
    parts = [part for part in path.split("/") if part]
    if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live", "v"}:
        return parts[1]
    return None


def canonical_url(url: str) -> str:
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    path = parsed.path or "/"
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=False)
        if key not in _TRACKING_KEYS and not key.startswith(_TRACKING_PREFIXES)
    ]
    video_id = _youtube_id(host, path, query)
    if video_id:
        host = "youtube.com"
        path = "/watch"
        query = [("v", video_id)]
    else:
        bare = _bare_host(host)
        if host in {"twitter.com", "mobile.twitter.com", "www.twitter.com", "mobile.x.com"} or bare == "twitter.com":
            host = "x.com"
        elif host in _REDDIT_HOSTS or bare == "reddit.com":
            host = "reddit.com"
        elif host.startswith("www."):
            host = host[4:]
    query.sort()
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    port = parsed.port
    netloc = host
    if port and port not in {80, 443}:
        netloc = f"{host}:{port}"
    scheme = "https" if parsed.scheme in {"http", "https"} else parsed.scheme
    return urlunparse((scheme, netloc, path, "", urlencode(query), ""))


def clean_handle(value: str | None) -> str | None:
    if value is None:
        return None
    handle = value.strip().lstrip("@").strip()
    if not handle or handle.lower() in {"[deleted]", "[removed]", "none"}:
        return None
    return handle.lower()


def handle_from_url(url: str) -> str | None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    parts = [part for part in parsed.path.split("/") if part]
    if host in {"x.com", "twitter.com"} and parts:
        if parts[0].lower() not in {"search", "home", "intent", "share", "i", "hashtag"}:
            return clean_handle(parts[0])
    if host.endswith("reddit.com") and len(parts) >= 2 and parts[0].lower() in {"user", "u"}:
        return clean_handle(parts[1])
    return None


def host_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def host_matches_domain(host: str, domain: str) -> bool:
    domain = domain.lower().strip().lstrip(".")
    if domain.startswith("www."):
        domain = domain[4:]
    return host == domain or host.endswith("." + domain)


def parse_time(value: object) -> datetime | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if abs(number) > 10_000_000_000:
            number = number / 1000.0
        try:
            return datetime.fromtimestamp(number, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return parse_time(int(value.strip()))
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def guess_language(text: str, provided: str | None) -> str | None:
    code = None
    if provided:
        raw = provided.strip().lower().replace("_", "-")
        code = raw.split("-", 1)[0] if raw else None
        if not code or code in _UNDETERMINED_LANGS:
            code = None
    if code:
        return code
    if _PERSIAN_LETTERS.search(text):
        return "fa"
    if _ARABIC_SCRIPT.search(text):
        return None
    if _LATIN.search(text):
        return "en"
    return None


def html_to_text(value: str) -> str:
    """Turn escaped HTML from a source API into the plain text a person wrote.

    Only real tags are removed. A comparison such as ``a < b > c`` stays intact.
    """
    text = html.unescape(value)
    text = re.sub(r"(?is)<a\s+[^>]*href=[\"']([^\"']+)[\"'][^>]*>", r" \1 ", text)
    text = re.sub(r"(?is)<br\s*/?>", " ", text)
    text = re.sub(r"(?is)</p\s*>", " ", text)
    text = re.sub(r"(?i)</?[a-z][^>]*>", " ", text)
    return " ".join(text.split())


def engagement_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None
