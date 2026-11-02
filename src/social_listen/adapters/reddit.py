"""Reddit public search JSON. Datacenter IPs are often answered with HTTP 403.

A 403 is reported as blocked. This adapter does not use a logged-in session
and does not post.
"""

from __future__ import annotations

from social_listen.adapters.base import Phrase, SearchAdapter, SearchContext, with_negatives
from social_listen.http import HttpError
from social_listen.models import Mention
from social_listen.normalize import engagement_int, host_of, is_public_http_url, parse_time
from social_listen.textmatch import contains_term

_HOSTS = ("https://www.reddit.com", "https://old.reddit.com")


class RedditAdapter(SearchAdapter):
    source = "reddit"

    def search_phrase(self, ctx: SearchContext, phrase: Phrase) -> tuple[list[Mention], int]:
        query = with_negatives(phrase.text, phrase.negatives)
        window = ctx.config.window_days
        if window <= 31:
            span = "month"
        elif window <= 365:
            span = "year"
        else:
            span = "all"
        last_error: HttpError | None = None
        for host in _HOSTS:
            status, payload = ctx.client.get_json(
                f"{host}/search.json",
                params={
                    "q": query,
                    "sort": "new",
                    "limit": str(min(ctx.max_hits, 100)),
                    "t": span,
                    "raw_json": "1",
                },
            )
            if status in {401, 403, 429}:
                last_error = HttpError(status, f"Reddit HTTP {status}")
                continue
            if status >= 400 or not isinstance(payload, dict):
                raise HttpError(status, f"Reddit HTTP {status}")
            return _parse_listing(payload, phrase.text)
        if last_error is not None:
            raise last_error
        raise HttpError(None, "Reddit returned no listing")


def _parse_listing(payload: dict[str, object], phrase: str) -> tuple[list[Mention], int]:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise HttpError(200, "Reddit listing missing data")
    children = data.get("children")
    if not isinstance(children, list):
        return [], 0
    mentions: list[Mention] = []
    fetched = 0
    for child in children:
        if not isinstance(child, dict):
            continue
        post = child.get("data")
        if not isinstance(post, dict):
            continue
        mention = _post_to_mention(post)
        if mention is None:
            continue
        fetched += 1
        blob = f"{mention.title or ''}\n{mention.text}"
        if contains_term(blob, phrase):
            mentions.append(mention)
    return mentions, fetched


def _post_to_mention(post: dict[str, object]) -> Mention | None:
    permalink = post.get("permalink")
    if not isinstance(permalink, str) or not permalink.startswith("/"):
        return None
    title = post.get("title")
    body = post.get("selftext")
    title_text = title.strip() if isinstance(title, str) else ""
    body_text = body.strip() if isinstance(body, str) else ""
    if body_text in {"[removed]", "[deleted]"}:
        body_text = ""
    text = body_text or title_text
    if not text:
        return None
    author = post.get("author")
    author_text = author if isinstance(author, str) else None
    ups = engagement_int(post.get("ups"))
    comments = engagement_int(post.get("num_comments"))
    engagement: dict[str, int] = {}
    if ups is not None:
        engagement["upvotes"] = ups
    if comments is not None:
        engagement["comments"] = comments
    external = post.get("url")
    external_url = None
    if isinstance(external, str) and is_public_http_url(external):
        host = host_of(external)
        if host != "reddit.com" and not host.endswith(".reddit.com"):
            external_url = external
    return Mention(
        source="reddit",
        url=f"https://www.reddit.com{permalink}",
        text=text,
        title=title_text or None,
        author=author_text,
        handle=author_text,
        published_at=parse_time(post.get("created_utc")),
        engagement=engagement,
        external_url=external_url,
    )
