"""Fan out adapters, then normalize, filter, dedupe, and distill."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from social_listen.adapters import disabled_reports, selected_adapters
from social_listen.adapters.base import SearchContext, build_phrases
from social_listen.adapters.web import NewsAdapter, ReviewsAdapter, WebSearchAdapter
from social_listen.config import Config
from social_listen.dedupe import dedupe
from social_listen.digest import (
    base_limitations,
    build_patterns,
    limitations_from_reports,
)
from social_listen.filters import assign_targets, dated_in_last, within_window
from social_listen.http import HttpClient
from social_listen.llm import OpenAICompatibleSummarizer
from social_listen.models import AdapterBatch, Mention, RunResult, SourceReport
from social_listen.normalize import is_public_http_url
from social_listen.signal import signal_label, verdict_7d
from social_listen.tagging import tag_mentions


def execute(
    config: Config,
    *,
    mode: str,
    client: HttpClient | None = None,
    now: datetime | None = None,
    summarizer: OpenAICompatibleSummarizer | None = None,
    quiet_empty: bool,
    delay_seconds: float = 0.0,
    adapters: list[object] | None = None,
) -> RunResult:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    since = moment - timedelta(days=config.window_days)
    http = client or HttpClient()
    ctx = SearchContext(
        config=config,
        since_epoch=int(since.timestamp()),
        client=http,
        phrases=build_phrases(config),
        delay_seconds=delay_seconds,
        max_hits=config.max_hits,
    )
    fell_back = False
    if adapters is not None:
        batches = _fanout(adapters, ctx)
        reports = [batch.report for batch in batches]
    else:
        chosen = selected_adapters(
            config,
            include_community=True,
            include_supplemental=True,
        )
        batches = _fanout(chosen, ctx)
        if mode == "research" and config.research_fallback:
            early = _prepare(
                [item for batch in batches for item in batch.mentions],
                config,
                since,
                moment,
            )
            if signal_label(early, moment, config.window_days) != "STRONG":
                already = {getattr(adapter, "source", "") for adapter in chosen}
                extra = [
                    adapter
                    for adapter in (WebSearchAdapter("web"), NewsAdapter(), ReviewsAdapter())
                    if adapter.source not in already
                ]
                if extra:
                    batches.extend(_fanout(extra, ctx))
                    fell_back = True
        reports = [batch.report for batch in batches]
        reports.extend(disabled_reports(config))
        if fell_back:
            reports = _drop_skipped_supplemental(reports)

    mentions = _prepare(
        [item for batch in batches for item in batch.mentions],
        config,
        since,
        moment,
    )
    _fill_kept(reports, mentions)
    label = signal_label(mentions, moment, config.window_days)
    verdict, mentions_7d = verdict_7d(mentions, moment)
    patterns = build_patterns(mentions)
    llm_used = False
    limitations = base_limitations(config.max_hits) + limitations_from_reports(reports)
    clearing = [mention for mention in mentions if dated_in_last(mention, moment, config.window_days)]
    cleared = len(clearing) >= config.quiet_below
    if cleared and summarizer is not None and len(mentions) >= 2:
        try:
            clustered = summarizer.cluster(mentions)
        except RuntimeError as exc:
            limitations.append(f"LLM clustering was not used: {exc}")
        else:
            if clustered:
                patterns = clustered
                llm_used = True
            else:
                limitations.append(
                    "LLM clustering returned nothing usable. Theme labels below are from the built-in lexicon."
                )
    quiet = quiet_empty and not cleared
    if fell_back:
        limitations.append(
            "Social evidence was not STRONG, so web, news, and review search ran as the thin-signal fallback."
        )
    return RunResult(
        name=config.name,
        mode=mode,
        generated_at=moment,
        window_days=config.window_days,
        signal=label,
        verdict_7d=verdict,
        mentions_7d=mentions_7d,
        mentions=mentions,
        reports=_unique_reports(reports),
        patterns=patterns,
        cleared_bar=cleared,
        quiet=quiet,
        llm_used=llm_used,
        limitations=limitations,
    )


def _prepare(
    mentions: list[Mention],
    config: Config,
    since: datetime,
    now: datetime,
) -> list[Mention]:
    kept: list[Mention] = []
    for mention in mentions:
        if not is_public_http_url(mention.url) or not mention.text.strip():
            continue
        if not within_window(mention, since, now):
            continue
        assigned = assign_targets(mention, config)
        if assigned is None:
            continue
        kept.append(assigned)
    return dedupe(tag_mentions(kept))


def _fanout(adapters: list[object], ctx: SearchContext) -> list[AdapterBatch]:
    if not adapters:
        return []
    workers = min(8, len(adapters))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_safe_collect, adapter, ctx) for adapter in adapters]
        return [future.result() for future in futures]


def _safe_collect(adapter: object, ctx: SearchContext) -> AdapterBatch:
    source = getattr(adapter, "source", "unknown")
    collect = getattr(adapter, "collect", None)
    if not callable(collect):
        return AdapterBatch(
            [],
            SourceReport(str(source), "UPSTREAM_BROKEN", "adapter has no collect method", 0, 0),
        )
    try:
        batch = collect(ctx)
    except Exception as exc:
        return AdapterBatch(
            [],
            SourceReport(str(source), "UPSTREAM_BROKEN", f"{type(exc).__name__}: {exc}", 0, 0),
        )
    if not isinstance(batch, AdapterBatch):
        return AdapterBatch(
            [],
            SourceReport(str(source), "UPSTREAM_BROKEN", "adapter returned an unexpected value", 0, 0),
        )
    return batch


def _drop_skipped_supplemental(reports: list[SourceReport]) -> list[SourceReport]:
    """Fallback ran those sources, so a prior 'turned off' row would be a lie."""
    forced = {"web", "news", "reviews"}
    ran = {report.source for report in reports if report.status != "skipped"}
    return [
        report
        for report in reports
        if not (report.source in forced and report.status == "skipped" and report.source in ran)
    ]


def _fill_kept(reports: list[SourceReport], mentions: list[Mention]) -> None:
    counts: dict[str, int] = {}
    for mention in mentions:
        counts[mention.source] = counts.get(mention.source, 0) + 1
    for report in reports:
        report.kept = counts.get(report.source, 0)


def _unique_reports(reports: list[SourceReport]) -> list[SourceReport]:
    seen: set[str] = set()
    unique: list[SourceReport] = []
    for report in reports:
        if report.source in seen:
            continue
        seen.add(report.source)
        unique.append(report)
    return unique
