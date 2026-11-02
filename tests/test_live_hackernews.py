"""One live call to the keyless Hacker News search API."""

from datetime import datetime, timedelta, timezone

import pytest

from social_listen.adapters.base import Phrase, SearchContext
from social_listen.adapters.hackernews import HackerNewsAdapter
from social_listen.config import Config, Target
from social_listen.http import HttpClient, HttpError


def test_hackernews_live_search_returns_real_item_urls() -> None:
    now = datetime.now(timezone.utc)
    config = Config(
        name="live",
        window_days=30,
        targets=[Target(id="sqlite", role="brand", names=["SQLite"])],
        max_hits=5,
    )
    ctx = SearchContext(
        config=config,
        since_epoch=int((now - timedelta(days=30)).timestamp()),
        client=HttpClient(timeout=20),
        phrases=[Phrase("SQLite", "sqlite", ())],
        max_hits=5,
    )
    try:
        batch = HackerNewsAdapter().collect(ctx)
    except HttpError as exc:
        pytest.skip(f"HN unreachable: {exc}")
    if batch.report.status != "ok":
        pytest.skip(batch.report.detail)
    assert batch.report.fetched >= 0
    for mention in batch.mentions:
        assert mention.url.startswith("https://news.ycombinator.com/item?id=")
        assert "sqlite" in f"{mention.title or ''} {mention.text}".lower()
        assert mention.text.strip()
