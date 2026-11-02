"""YAML target config. Nothing about a specific client is built into the loader."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from social_listen.models import TargetRole

_SOURCE_KEYS = (
    "hackernews",
    "reddit",
    "x",
    "youtube",
    "news",
    "web",
    "reviews",
    "agent_reach",
    "last30days",
)
_DEFAULT_SPAM = (
    "buy followers",
    "crypto airdrop",
    "dm for promo",
    "onlyfans.com",
)
_DEFAULT_NEWS_SITES = ("reuters.com", "apnews.com", "bbc.com", "techcrunch.com")
_DEFAULT_REVIEW_SITES = ("trustpilot.com", "g2.com", "capterra.com")
_DEFAULT_REACH_CHANNELS = (
    "youtube",
    "github",
    "bilibili",
    "v2ex",
    "reddit",
    "twitter",
    "xiaohongshu",
)


@dataclass
class Target:
    id: str
    role: TargetRole
    names: list[str]
    aliases: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    handles: list[str] = field(default_factory=list)
    negative_keywords: list[str] = field(default_factory=list)


@dataclass
class SourceToggles:
    hackernews: bool = True
    reddit: bool = True
    x: bool = True
    youtube: bool = True
    news: bool = True
    web: bool = True
    reviews: bool = True
    agent_reach: bool = False
    last30days: bool = False

    def enabled(self, name: str) -> bool:
        if not hasattr(self, name):
            raise KeyError(name)
        value = getattr(self, name)
        if not isinstance(value, bool):
            raise TypeError(name)
        return value


@dataclass
class Config:
    name: str
    targets: list[Target]
    window_days: int = 7
    languages: list[str] = field(default_factory=list)
    sources: SourceToggles = field(default_factory=SourceToggles)
    spam_patterns: list[str] = field(default_factory=lambda: list(_DEFAULT_SPAM))
    news_sites: list[str] = field(default_factory=lambda: list(_DEFAULT_NEWS_SITES))
    review_sites: list[str] = field(default_factory=lambda: list(_DEFAULT_REVIEW_SITES))
    web_provider: str | None = None
    max_hits: int = 25
    agent_reach_channels: list[str] = field(
        default_factory=lambda: list(_DEFAULT_REACH_CHANNELS)
    )
    quiet_below: int = 1
    research_fallback: bool = True

    def terms_for(self, target: Target) -> list[str]:
        seen: set[str] = set()
        terms: list[str] = []
        for raw in [*target.names, *target.aliases]:
            text = " ".join(raw.split())
            if not text:
                continue
            key = text.casefold()
            if key in seen:
                continue
            seen.add(key)
            terms.append(text)
        return terms


def _as_str_list(value: object, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} must be a list of strings")
    return [" ".join(item.split()) for item in value if item.strip()]


def _as_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be true or false")
    return value


def _parse_role(role: str, index: int) -> TargetRole:
    if role == "brand":
        return "brand"
    if role == "competitor":
        return "competitor"
    if role == "question":
        return "question"
    _never: str = role
    raise ValueError(
        f"targets[{index}].role must be brand, competitor, or question (got {_never})"
    )


def _load_target(raw: object, index: int) -> Target:
    if not isinstance(raw, dict):
        raise ValueError(f"targets[{index}] must be a mapping")
    target_id = raw.get("id")
    if not isinstance(target_id, str) or not target_id.strip():
        raise ValueError(f"targets[{index}].id is required")
    role_raw = raw.get("role", "brand")
    if not isinstance(role_raw, str):
        raise ValueError(f"targets[{index}].role must be brand, competitor, or question")
    role = _parse_role(role_raw, index)
    names = _as_str_list(raw.get("names"), f"targets[{index}].names")
    if not names:
        raise ValueError(f"targets[{index}].names must list at least one name")
    aliases = _as_str_list(raw.get("aliases"), f"targets[{index}].aliases")
    for term in [*names, *aliases]:
        if len(term.strip()) < 2:
            raise ValueError(
                f"targets[{index}] term {term!r} is too short to search safely"
            )
    return Target(
        id=target_id.strip(),
        role=role,
        names=names,
        aliases=aliases,
        domains=[item.lower() for item in _as_str_list(raw.get("domains"), "domains")],
        handles=[
            item.lstrip("@").lower()
            for item in _as_str_list(raw.get("handles"), "handles")
        ],
        negative_keywords=_as_str_list(
            raw.get("negative_keywords"), f"targets[{index}].negative_keywords"
        ),
    )


def _load_sources(raw: object) -> SourceToggles:
    toggles = SourceToggles()
    if raw is None:
        return toggles
    if not isinstance(raw, dict):
        raise ValueError("sources must be a mapping")
    for key in _SOURCE_KEYS:
        if key in raw:
            setattr(toggles, key, _as_bool(raw[key], f"sources.{key}"))
    unknown = sorted(set(raw) - set(_SOURCE_KEYS))
    if unknown:
        raise ValueError(f"unknown sources: {', '.join(unknown)}")
    return toggles


def load_config(path: str | Path) -> Config:
    file_path = Path(path)
    try:
        raw = yaml.safe_load(file_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"could not read config {file_path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML in {file_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("config root must be a mapping")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name is required")
    targets_raw = raw.get("targets")
    if not isinstance(targets_raw, list) or not targets_raw:
        raise ValueError("targets must be a non-empty list")
    targets = [_load_target(item, index) for index, item in enumerate(targets_raw)]
    ids = [target.id for target in targets]
    if len(ids) != len(set(ids)):
        raise ValueError("target ids must be unique")
    window = raw.get("window_days", 7)
    if not isinstance(window, int) or isinstance(window, bool) or window < 1 or window > 3650:
        raise ValueError("window_days must be an integer from 1 to 3650")
    max_hits = raw.get("max_hits", 25)
    if (
        not isinstance(max_hits, int)
        or isinstance(max_hits, bool)
        or max_hits < 1
        or max_hits > 100
    ):
        raise ValueError("max_hits must be an integer from 1 to 100")
    quiet_below = raw.get("quiet_below", 1)
    if (
        not isinstance(quiet_below, int)
        or isinstance(quiet_below, bool)
        or quiet_below < 1
    ):
        raise ValueError("quiet_below must be an integer >= 1")
    provider = raw.get("web_provider")
    if provider is not None and (
        not isinstance(provider, str)
        or provider not in {"duckduckgo", "brave", "serper"}
    ):
        raise ValueError("web_provider must be duckduckgo, brave, or serper")
    languages = [item.lower() for item in _as_str_list(raw.get("languages"), "languages")]
    spam = raw.get("spam_patterns")
    news_sites = raw.get("news_sites")
    review_sites = raw.get("review_sites")
    channels = raw.get("agent_reach_channels")
    fallback = raw.get("research_fallback", True)
    if not isinstance(fallback, bool):
        raise ValueError("research_fallback must be true or false")
    return Config(
        name=name.strip(),
        targets=targets,
        window_days=window,
        languages=languages,
        sources=_load_sources(raw.get("sources")),
        spam_patterns=list(_DEFAULT_SPAM) if spam is None else _as_str_list(spam, "spam_patterns"),
        news_sites=(
            list(_DEFAULT_NEWS_SITES)
            if news_sites is None
            else _as_str_list(news_sites, "news_sites")
        ),
        review_sites=(
            list(_DEFAULT_REVIEW_SITES)
            if review_sites is None
            else _as_str_list(review_sites, "review_sites")
        ),
        web_provider=provider,
        max_hits=max_hits,
        agent_reach_channels=(
            list(_DEFAULT_REACH_CHANNELS)
            if channels is None
            else _as_str_list(channels, "agent_reach_channels")
        ),
        quiet_below=quiet_below,
        research_fallback=fallback,
    )
