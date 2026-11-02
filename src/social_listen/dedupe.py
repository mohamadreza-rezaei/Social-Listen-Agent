"""Merge the same page found through more than one source."""

from __future__ import annotations

import hashlib

from social_listen.models import Mention
from social_listen.normalize import canonical_url


def _prefer(existing: Mention, incoming: Mention) -> Mention:
    primary = existing if len(existing.text) >= len(incoming.text) else incoming
    secondary = incoming if primary is existing else existing
    targets = list(dict.fromkeys([*primary.target_ids, *secondary.target_ids]))
    engagement = dict(secondary.engagement)
    engagement.update(primary.engagement)
    themes = list(dict.fromkeys([*primary.themes, *secondary.themes]))
    published = primary.published_at or secondary.published_at
    return Mention(
        source=primary.source,
        url=primary.url,
        text=primary.text,
        title=primary.title or secondary.title,
        author=primary.author or secondary.author,
        handle=primary.handle or secondary.handle,
        published_at=published,
        language=primary.language or secondary.language,
        engagement=engagement,
        target_ids=targets,
        sentiment=primary.sentiment,
        themes=themes,
        external_url=primary.external_url or secondary.external_url,
    )


def dedupe(mentions: list[Mention]) -> list[Mention]:
    """Merge only when the canonical URL is the same. Distinct posts stay distinct."""
    by_url: dict[str, Mention] = {}
    order: list[str] = []
    for mention in mentions:
        key = canonical_url(mention.url)
        if key not in by_url:
            by_url[key] = mention
            order.append(key)
            continue
        by_url[key] = _prefer(by_url[key], mention)

    result: list[Mention] = []
    for key in order:
        mention = by_url[key]
        mention.id = _mention_id(mention)
        result.append(mention)
    return result


def _mention_id(mention: Mention) -> str:
    raw = "|".join(
        [
            mention.source,
            canonical_url(mention.url),
            ",".join(sorted(mention.target_ids)),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
