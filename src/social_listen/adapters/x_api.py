"""X recent search via the official API. Read-only. Requires X_BEARER_TOKEN."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

from social_listen.adapters.base import Phrase, SearchAdapter, SearchContext, with_negatives
from social_listen.http import HttpError
from social_listen.models import AdapterBatch, Mention, SourceReport
from social_listen.normalize import engagement_int, parse_time
from social_listen.textmatch import contains_term

_ENDPOINT = "https://api.x.com/2/tweets/search/recent"


class XAdapter(SearchAdapter):
    source = "x"

    def collect(self, ctx: SearchContext) -> AdapterBatch:
        token = os.environ.get("X_BEARER_TOKEN", "").strip()
        if not token:
            return AdapterBatch(
                [],
                SourceReport(
                    self.source,
                    "unconfigured",
                    "Set X_BEARER_TOKEN to query the official recent-search API. No token was set, so X was not called.",
                    0,
                    0,
                ),
            )
        self._token = token
        if ctx.config.window_days > 7:
            self.extra_detail = (
                "X recent search covers about the last 7 days; "
                "older days in this window were not queried."
            )
        return super().collect(ctx)

    def search_phrase(self, ctx: SearchContext, phrase: Phrase) -> tuple[list[Mention], int]:
        handles = _exclude_handles(ctx, phrase.target_id)
        query = _fit_x_query(with_negatives(phrase.text, phrase.negatives), handles)
        status, payload = ctx.client.get_json(
            _ENDPOINT,
            params={
                "query": query,
                "max_results": str(max(10, min(ctx.max_hits, 100))),
                "start_time": _recent_start(ctx.since_epoch).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "tweet.fields": "created_at,lang,public_metrics,author_id",
                "expansions": "author_id",
                "user.fields": "username,name",
            },
            headers={"Authorization": f"Bearer {self._token}"},
        )
        if status in {401, 402, 403, 429}:
            raise HttpError(status, f"X API HTTP {status}")
        if status >= 400 or not isinstance(payload, dict):
            raise HttpError(status, f"X API HTTP {status}")
        return _parse_payload(payload, phrase.text)


def _fit_x_query(base: str, handles: list[str]) -> str:
    """Keep the query inside the 512-character recent-search cap without cutting a token."""
    if len(base) > 512:
        raise HttpError(None, "X query exceeds 512 characters before exclusions")
    query = base
    for handle in handles:
        cleaned = handle.strip().lstrip("@")
        if not cleaned:
            continue
        extra = f" -from:{cleaned}"
        if len(query) + len(extra) > 512:
            break
        query = f"{query}{extra}"
    return query


def _recent_start(since_epoch: int) -> datetime:
    """Clamp start_time to the recent-search window, which is about seven days."""
    now = datetime.now(UTC)
    earliest = now - timedelta(days=7) + timedelta(minutes=10)
    requested = datetime.fromtimestamp(since_epoch, tz=UTC)
    return max(requested, earliest)


def _exclude_handles(ctx: SearchContext, target_id: str) -> list[str]:
    for target in ctx.config.targets:
        if target.id == target_id:
            return list(target.handles)
    return []


def _parse_payload(payload: dict[str, object], phrase: str) -> tuple[list[Mention], int]:
    data = payload.get("data")
    if data is None:
        return [], 0
    if not isinstance(data, list):
        raise HttpError(200, "X API returned an unexpected data field")
    users: dict[str, str] = {}
    includes = payload.get("includes")
    if isinstance(includes, dict):
        raw_users = includes.get("users")
        if isinstance(raw_users, list):
            for user in raw_users:
                if not isinstance(user, dict):
                    continue
                user_id = user.get("id")
                username = user.get("username")
                if isinstance(user_id, str) and isinstance(username, str):
                    users[user_id] = username
    mentions: list[Mention] = []
    fetched = 0
    for tweet in data:
        if not isinstance(tweet, dict):
            continue
        mention = _tweet_to_mention(tweet, users)
        if mention is None:
            continue
        fetched += 1
        if contains_term(mention.text, phrase):
            mentions.append(mention)
    return mentions, fetched


def _tweet_to_mention(tweet: dict[str, object], users: dict[str, str]) -> Mention | None:
    tweet_id = tweet.get("id")
    text = tweet.get("text")
    if not isinstance(tweet_id, str) or not isinstance(text, str) or not text.strip():
        return None
    author_id = tweet.get("author_id")
    username = users.get(author_id) if isinstance(author_id, str) else None
    if username:
        url = f"https://x.com/{username}/status/{tweet_id}"
    else:
        url = f"https://x.com/i/web/status/{tweet_id}"
    metrics = tweet.get("public_metrics")
    engagement: dict[str, int] = {}
    if isinstance(metrics, dict):
        for key, out_key in (
            ("like_count", "likes"),
            ("retweet_count", "reposts"),
            ("reply_count", "replies"),
            ("quote_count", "quotes"),
        ):
            value = engagement_int(metrics.get(key))
            if value is not None:
                engagement[out_key] = value
    lang = tweet.get("lang")
    return Mention(
        source="x",
        url=url,
        text=text.strip(),
        author=username,
        handle=username,
        published_at=parse_time(tweet.get("created_at")),
        language=lang if isinstance(lang, str) else None,
        engagement=engagement,
    )
