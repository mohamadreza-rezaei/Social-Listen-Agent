"""Shared search context and per-query status rollup."""

from __future__ import annotations

import time
from dataclasses import dataclass

from social_listen.config import Config
from social_listen.http import HttpClient, HttpError
from social_listen.models import AdapterBatch, Mention, SourceReport, SourceStatusKind


@dataclass(frozen=True)
class Phrase:
    text: str
    target_id: str
    negatives: tuple[str, ...]


@dataclass
class SearchContext:
    config: Config
    since_epoch: int
    client: HttpClient
    phrases: list[Phrase]
    delay_seconds: float = 0.0
    max_hits: int = 25


def build_phrases(config: Config) -> list[Phrase]:
    phrases: list[Phrase] = []
    seen: set[tuple[str, str]] = set()
    for target in config.targets:
        for text in config.terms_for(target):
            key = (target.id, text.casefold())
            if key in seen:
                continue
            seen.add(key)
            phrases.append(
                Phrase(text=text, target_id=target.id, negatives=tuple(target.negative_keywords))
            )
    return phrases


def quoted(phrase: str) -> str:
    if any(character.isspace() for character in phrase) or any(ord(character) > 127 for character in phrase):
        escaped = phrase.replace('"', " ")
        return f'"{escaped}"'
    return phrase


def with_negatives(phrase: str, negatives: tuple[str, ...], limit: int = 5) -> str:
    parts = [quoted(phrase)]
    for negative in negatives[:limit]:
        token = negative.strip()
        if not token or any(ord(character) > 127 for character in token):
            continue
        if any(character.isspace() for character in token):
            parts.append(f'-"{token}"')
        else:
            parts.append(f"-{token}")
    return " ".join(parts)


class SearchAdapter:
    source: str

    def collect(self, ctx: SearchContext) -> AdapterBatch:
        mentions: list[Mention] = []
        fetched = 0
        blocked = 0
        broken = 0
        errors: list[str] = []
        for index, phrase in enumerate(ctx.phrases):
            if index and ctx.delay_seconds:
                time.sleep(ctx.delay_seconds)
            try:
                found, raw_count = self.search_phrase(ctx, phrase)
            except HttpError as exc:
                mentions.extend(exc.partial_mentions)
                fetched += exc.partial_fetched
                label = f"{phrase.text}: {exc.message}"
                if exc.status in {202, 401, 402, 403, 429}:
                    blocked += 1
                    if exc.status is not None and f"HTTP {exc.status}" not in exc.message:
                        label = f"{label} (HTTP {exc.status})"
                    errors.append(label)
                else:
                    broken += 1
                    errors.append(label)
                continue
            fetched += raw_count
            mentions.extend(found)
        status, detail = rollup(
            queries=len(ctx.phrases),
            blocked=blocked,
            broken=broken,
            kept_candidates=len(mentions),
            errors=errors,
        )
        extra = getattr(self, "extra_detail", "")
        if extra:
            detail = f"{detail} {extra}".strip()
        return AdapterBatch(
            mentions,
            SourceReport(self.source, status, detail, fetched, 0),
        )

    def search_phrase(self, ctx: SearchContext, phrase: Phrase) -> tuple[list[Mention], int]:
        raise NotImplementedError


def rollup(
    *,
    queries: int,
    blocked: int,
    broken: int,
    kept_candidates: int,
    errors: list[str],
) -> tuple[SourceStatusKind, str]:
    detail = "; ".join(errors[:4])
    if queries == 0:
        return "skipped", "no search phrases"
    if kept_candidates > 0:
        if errors:
            prefix = f"{len(errors)} quer{'y' if len(errors) == 1 else 'ies'} had errors"
            return "ok", f"{prefix}. {detail}" if detail else prefix
        return "ok", "queried"
    if blocked and blocked + broken == queries:
        return "blocked", detail or "source refused every query"
    if broken and broken + blocked == queries:
        return "UPSTREAM_BROKEN", detail or "source failed every query"
    if errors:
        prefix = f"{len(errors)} quer{'y' if len(errors) == 1 else 'ies'} failed"
        return "ok", f"{prefix}. {detail}" if detail else prefix
    return "ok", "queried"
