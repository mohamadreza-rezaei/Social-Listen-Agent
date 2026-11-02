"""YouTube Data API search. Read-only. Requires YOUTUBE_API_KEY.

Search results do not include view counts, so engagement is left empty
rather than filled with a guess. Keyless YouTube search belongs to the
optional Agent Reach adapter.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from social_listen.adapters.base import Phrase, SearchAdapter, SearchContext
from social_listen.http import HttpError
from social_listen.models import AdapterBatch, Mention, SourceReport
from social_listen.normalize import parse_time
from social_listen.textmatch import contains_term

_ENDPOINT = "https://www.googleapis.com/youtube/v3/search"


class YouTubeAdapter(SearchAdapter):
    source = "youtube"

    def collect(self, ctx: SearchContext) -> AdapterBatch:
        key = os.environ.get("YOUTUBE_API_KEY", "").strip()
        if not key:
            return AdapterBatch(
                [],
                SourceReport(
                    self.source,
                    "unconfigured",
                    "Set YOUTUBE_API_KEY for the Data API, or enable sources.agent_reach for the keyless youtube channel.",
                    0,
                    0,
                ),
            )
        self._key = key
        return super().collect(ctx)

    def search_phrase(self, ctx: SearchContext, phrase: Phrase) -> tuple[list[Mention], int]:
        published_after = datetime.fromtimestamp(ctx.since_epoch, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        params = {
            "part": "snippet",
            "q": phrase.text,
            "type": "video",
            "order": "date",
            "maxResults": str(min(ctx.max_hits, 50)),
            "publishedAfter": published_after,
            "key": self._key,
        }
        status, payload = ctx.client.get_json(_ENDPOINT, params=params)
        if status in {400, 401, 403, 429}:
            raise HttpError(status, f"YouTube API HTTP {status}")
        if status >= 400 or not isinstance(payload, dict):
            raise HttpError(status, f"YouTube API HTTP {status}")
        items = payload.get("items")
        if not isinstance(items, list):
            return [], 0
        mentions: list[Mention] = []
        fetched = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            mention = _video_to_mention(item)
            if mention is None:
                continue
            fetched += 1
            blob = f"{mention.title or ''}\n{mention.text}"
            if contains_term(blob, phrase.text):
                mentions.append(mention)
        return mentions, fetched


def _video_to_mention(item: dict[str, object]) -> Mention | None:
    identity = item.get("id")
    snippet = item.get("snippet")
    if not isinstance(identity, dict) or not isinstance(snippet, dict):
        return None
    video_id = identity.get("videoId")
    title = snippet.get("title")
    description = snippet.get("description")
    if not isinstance(video_id, str) or not video_id:
        return None
    title_text = title.strip() if isinstance(title, str) else ""
    description_text = description.strip() if isinstance(description, str) else ""
    text = description_text or title_text
    if not text:
        return None
    channel = snippet.get("channelTitle")
    return Mention(
        source="youtube",
        url=f"https://www.youtube.com/watch?v={video_id}",
        text=text,
        title=title_text or None,
        author=channel if isinstance(channel, str) else None,
        handle=None,
        published_at=parse_time(snippet.get("publishedAt")),
    )
