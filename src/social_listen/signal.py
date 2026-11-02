"""Signal strength and the 7-day quiet/active verdict.

STRONG requires dated, in-window evidence from at least two sources, one of
them a community source, and at least three dated mentions. Undated web
snippets never promote a run to STRONG and never flip the 7-day verdict.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta

from social_listen.filters import dated_in_last
from social_listen.models import Mention, SignalLabel, Verdict

_COMMUNITY = {
    "hackernews",
    "reddit",
    "x",
    "youtube",
    "bilibili",
    "xiaohongshu",
    "v2ex",
    "github",
    "twitter",
}


def source_family(source: str) -> str:
    if ":" in source:
        return source.split(":", 1)[1]
    return source


def _is_community(source: str) -> bool:
    """Community means a social discussion source, not web, news, or an aggregate CLI."""
    family = source_family(source)
    return family in _COMMUNITY or source in _COMMUNITY


def dated_in_window(mentions: list[Mention], now: datetime, days: int) -> list[Mention]:
    return [mention for mention in mentions if dated_in_last(mention, now, days)]


def signal_label(mentions: list[Mention], now: datetime, window_days: int) -> SignalLabel:
    if not mentions:
        return "NONE"
    dated = dated_in_window(mentions, now, window_days)
    families = {source_family(mention.source) for mention in dated}
    community = any(_is_community(mention.source) for mention in dated)
    if len(dated) >= 3 and len(families) >= 2 and community:
        return "STRONG"
    return "THIN"


def verdict_7d(mentions: list[Mention], now: datetime) -> tuple[Verdict, int]:
    count = len(dated_in_window(mentions, now, 7))
    if count:
        return "active", count
    return "quiet", 0


def per_day_counts(mentions: list[Mention], now: datetime, days: int) -> list[tuple[str, int]]:
    """Calendar days touched by the rolling window, counting only in-window hits.

    A 7-day window that starts late on the first day spans eight dates. A hit
    still inside the window on that first date is included.
    """
    start = (now - timedelta(days=days)).date()
    end = now.date()
    counts: Counter[str] = Counter()
    for mention in mentions:
        if mention.published_at is None or not dated_in_last(mention, now, days):
            continue
        counts[mention.published_at.date().isoformat()] += 1
    labels: list[tuple[str, int]] = []
    span = (end - start).days
    for offset in range(span + 1):
        day = start + timedelta(days=offset)
        iso = day.isoformat()
        labels.append((iso, counts.get(iso, 0)))
    return labels


def age_label(mention: Mention, now: datetime) -> str:
    if mention.published_at is None:
        return "date not provided"
    delta = now - mention.published_at
    if delta.total_seconds() < 0:
        return "date is in the future"
    days = delta.days
    if days == 0:
        return "today"
    if days == 1:
        return "1 day ago"
    return f"{days} days ago"
