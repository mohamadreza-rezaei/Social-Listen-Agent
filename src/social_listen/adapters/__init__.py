"""Build the adapter list for one run. Disabled sources are reported as skipped."""

from __future__ import annotations

from social_listen.adapters.agent_reach import AgentReachAdapter
from social_listen.adapters.base import SearchAdapter
from social_listen.adapters.hackernews import HackerNewsAdapter
from social_listen.adapters.last30days import Last30DaysAdapter
from social_listen.adapters.reddit import RedditAdapter
from social_listen.adapters.web import NewsAdapter, ReviewsAdapter, WebSearchAdapter
from social_listen.adapters.x_api import XAdapter
from social_listen.adapters.youtube import YouTubeAdapter
from social_listen.config import Config
from social_listen.models import SourceReport

COMMUNITY = ("hackernews", "reddit", "x", "youtube")
SUPPLEMENTAL = ("web", "news", "reviews")


def _native(name: str) -> SearchAdapter:
    if name == "hackernews":
        return HackerNewsAdapter()
    if name == "reddit":
        return RedditAdapter()
    if name == "x":
        return XAdapter()
    if name == "youtube":
        return YouTubeAdapter()
    if name == "web":
        return WebSearchAdapter("web")
    if name == "news":
        return NewsAdapter()
    if name == "reviews":
        return ReviewsAdapter()
    raise KeyError(name)


def selected_adapters(
    config: Config,
    *,
    include_supplemental: bool,
    include_community: bool,
) -> list[object]:
    adapters: list[object] = []
    for name in (*COMMUNITY, *SUPPLEMENTAL):
        enabled = config.sources.enabled(name)
        in_wave = (name in COMMUNITY and include_community) or (
            name in SUPPLEMENTAL and include_supplemental
        )
        if enabled and in_wave:
            adapters.append(_native(name))
    if config.sources.agent_reach and include_community:
        channels = config.agent_reach_channels or ["web"]
        for channel in channels:
            adapters.append(AgentReachAdapter(channel))
    if config.sources.last30days and include_community:
        adapters.append(Last30DaysAdapter())
    return adapters


def disabled_reports(config: Config) -> list[SourceReport]:
    reports: list[SourceReport] = []
    for name in (*COMMUNITY, *SUPPLEMENTAL):
        if not config.sources.enabled(name):
            reports.append(SourceReport(name, "skipped", "turned off in config", 0, 0))
    if not config.sources.agent_reach:
        reports.append(
            SourceReport(
                "agent_reach",
                "skipped",
                "optional Agent Reach CLI is off (sources.agent_reach)",
                0,
                0,
            )
        )
    if not config.sources.last30days:
        reports.append(
            SourceReport(
                "last30days",
                "skipped",
                "optional last30days CLI is off; built-in clustering still runs on items this process collected",
                0,
                0,
            )
        )
    return reports
