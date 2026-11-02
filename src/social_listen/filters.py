"""Drop own accounts, spam, name collisions, and out-of-window items."""

from __future__ import annotations

from datetime import datetime, timedelta

from social_listen.config import Config, Target
from social_listen.models import Mention
from social_listen.normalize import (
    clean_handle,
    guess_language,
    handle_from_url,
    host_matches_domain,
    host_of,
)
from social_listen.textmatch import contains_term


def _blob(mention: Mention) -> str:
    parts = [mention.title or "", mention.text, mention.author or "", mention.handle or ""]
    return "\n".join(parts)


def _target_terms(config: Config, target: Target) -> list[str]:
    return config.terms_for(target)


def assign_targets(mention: Mention, config: Config) -> Mention | None:
    """Keep a mention only when at least one target survives collision and own-account checks."""
    blob = _blob(mention)
    author = clean_handle(mention.handle) or clean_handle(mention.author)
    url_handle = handle_from_url(mention.url)
    page_host = host_of(mention.url)
    matched: list[str] = []
    for target in config.targets:
        if not any(contains_term(blob, term) for term in _target_terms(config, target)):
            continue
        if any(contains_term(blob, term) for term in target.negative_keywords):
            continue
        identities = {item.casefold() for item in [*target.handles, *target.names, *target.aliases]}
        if author and author.casefold() in identities:
            continue
        if url_handle and url_handle.casefold() in identities:
            continue
        if any(host_matches_domain(page_host, domain) for domain in target.domains):
            continue
        matched.append(target.id)
    if not matched:
        return None
    mention.target_ids = matched
    mention.language = guess_language(mention.text, mention.language)
    if config.languages and mention.language and mention.language not in config.languages:
        return None
    if any(contains_term(blob, pattern) for pattern in config.spam_patterns):
        return None
    return mention


def within_window(mention: Mention, since: datetime, now: datetime) -> bool:
    """Undated items stay in the list. Dated items outside the window are dropped."""
    if mention.published_at is None:
        return True
    published = mention.published_at
    return since <= published <= now + timedelta(minutes=5)


def dated_in_last(mention: Mention, now: datetime, days: int) -> bool:
    if mention.published_at is None:
        return False
    return now - timedelta(days=days) <= mention.published_at <= now + timedelta(minutes=5)
