"""Shared records. Every mention must carry a real http(s) URL."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Never

SourceStatusKind = Literal[
    "ok",
    "UPSTREAM_BROKEN",
    "blocked",
    "skipped",
    "unconfigured",
    "not_installed",
]
Sentiment = Literal["positive", "negative", "mixed", "unknown"]
SignalLabel = Literal["STRONG", "THIN", "NONE"]
Verdict = Literal["quiet", "active"]
TargetRole = Literal["brand", "competitor", "question"]


def assert_never_status(kind: SourceStatusKind) -> None:
    """Exhaustive check so a new status cannot pass through unlabeled."""
    if kind == "ok":
        return
    if kind == "UPSTREAM_BROKEN":
        return
    if kind == "blocked":
        return
    if kind == "skipped":
        return
    if kind == "unconfigured":
        return
    if kind == "not_installed":
        return
    _never: Never = kind
    raise AssertionError(_never)


@dataclass
class Mention:
    source: str
    url: str
    text: str
    title: str | None = None
    author: str | None = None
    handle: str | None = None
    published_at: datetime | None = None
    language: str | None = None
    engagement: dict[str, int] = field(default_factory=dict)
    target_ids: list[str] = field(default_factory=list)
    sentiment: Sentiment = "unknown"
    themes: list[str] = field(default_factory=list)
    external_url: str | None = None
    id: str = ""

    def to_dict(self) -> dict[str, object]:
        published = None
        if self.published_at is not None:
            published = self.published_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        return {
            "id": self.id,
            "source": self.source,
            "url": self.url,
            "external_url": self.external_url,
            "title": self.title,
            "author": self.author,
            "handle": self.handle,
            "published_at": published,
            "language": self.language,
            "text": self.text,
            "engagement": dict(self.engagement),
            "target_ids": list(self.target_ids),
            "sentiment": self.sentiment,
            "themes": list(self.themes),
        }


@dataclass
class SourceReport:
    source: str
    status: SourceStatusKind
    detail: str
    fetched: int = 0
    kept: int = 0

    def to_dict(self) -> dict[str, object]:
        assert_never_status(self.status)
        return {
            "source": self.source,
            "status": self.status,
            "detail": self.detail,
            "fetched": self.fetched,
            "kept": self.kept,
        }


@dataclass
class AdapterBatch:
    mentions: list[Mention]
    report: SourceReport


@dataclass
class Pattern:
    label: str
    mention_ids: list[str]
    count: int
    sources: list[str]
    summary: str


@dataclass
class RunResult:
    name: str
    mode: str
    generated_at: datetime
    window_days: int
    signal: SignalLabel
    verdict_7d: Verdict
    mentions_7d: int
    mentions: list[Mention]
    reports: list[SourceReport]
    patterns: list[Pattern]
    cleared_bar: bool
    quiet: bool
    llm_used: bool
    limitations: list[str]
