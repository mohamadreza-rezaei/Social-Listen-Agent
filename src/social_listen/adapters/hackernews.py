"""Hacker News via the public Algolia search API. No key."""

from __future__ import annotations

from social_listen.adapters.base import Phrase, SearchAdapter, SearchContext
from social_listen.http import HttpError
from social_listen.models import Mention
from social_listen.normalize import engagement_int, html_to_text, is_public_http_url, parse_time
from social_listen.textmatch import contains_term

_ENDPOINT = "https://hn.algolia.com/api/v1/search_by_date"


class HackerNewsAdapter(SearchAdapter):
    source = "hackernews"

    def search_phrase(self, ctx: SearchContext, phrase: Phrase) -> tuple[list[Mention], int]:
        mentions: list[Mention] = []
        fetched = 0
        failures: list[HttpError] = []
        for tags in ("story", "comment"):
            try:
                found, raw_count = _search_tag(ctx, phrase, tags)
            except HttpError as exc:
                failures.append(exc)
                continue
            fetched += raw_count
            mentions.extend(found)
        if failures:
            raise HttpError(
                failures[0].status,
                "; ".join(item.message for item in failures),
                partial_mentions=mentions,
                partial_fetched=fetched,
            )
        return mentions, fetched


def _search_tag(
    ctx: SearchContext, phrase: Phrase, tags: str
) -> tuple[list[Mention], int]:
    status, payload = ctx.client.get_json(
        _ENDPOINT,
        params={
            "query": phrase.text,
            "tags": tags,
            "numericFilters": f"created_at_i>={ctx.since_epoch}",
            "hitsPerPage": str(ctx.max_hits),
        },
    )
    if status >= 400:
        raise HttpError(status, f"HN Algolia HTTP {status} for {tags}")
    if not isinstance(payload, dict):
        raise HttpError(status, "HN Algolia returned an unexpected payload")
    hits = payload.get("hits")
    if not isinstance(hits, list):
        return [], 0
    mentions: list[Mention] = []
    fetched = 0
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        mention = _hit_to_mention(hit, tags)
        if mention is None:
            continue
        fetched += 1
        blob = f"{mention.title or ''}\n{mention.text}"
        if contains_term(blob, phrase.text):
            mentions.append(mention)
    return mentions, fetched


def _hit_to_mention(hit: dict[str, object], tags: str) -> Mention | None:
    object_id = hit.get("objectID")
    if not isinstance(object_id, str) or not object_id.isdigit():
        return None
    created = parse_time(hit.get("created_at") or hit.get("created_at_i"))
    author = hit.get("author")
    author_text = author if isinstance(author, str) else None
    points = engagement_int(hit.get("points"))
    comments = engagement_int(hit.get("num_comments"))
    engagement: dict[str, int] = {}
    if points is not None:
        engagement["points"] = points
    if comments is not None:
        engagement["comments"] = comments
    external = hit.get("url")
    external_url = external if isinstance(external, str) and is_public_http_url(external) else None
    if tags == "comment":
        body = hit.get("comment_text")
        if not isinstance(body, str) or not body.strip():
            return None
        title = hit.get("story_title")
        title_text = html_to_text(title) if isinstance(title, str) else ""
        return Mention(
            source="hackernews",
            url=f"https://news.ycombinator.com/item?id={object_id}",
            text=html_to_text(body),
            title=title_text or None,
            author=author_text,
            handle=author_text,
            published_at=created,
            engagement=engagement,
            external_url=external_url,
        )
    title = hit.get("title")
    story = hit.get("story_text")
    title_text = html_to_text(title) if isinstance(title, str) else ""
    story_text = html_to_text(story) if isinstance(story, str) else ""
    if not title_text and not story_text:
        return None
    text = story_text or title_text
    return Mention(
        source="hackernews",
        url=f"https://news.ycombinator.com/item?id={object_id}",
        text=text,
        title=title_text or None,
        author=author_text,
        handle=author_text,
        published_at=created,
        engagement=engagement,
        external_url=external_url,
    )
