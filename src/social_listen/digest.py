"""Deterministic digest. Quotes are copied from mention text; links are mention URLs."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from social_listen.config import Config
from social_listen.models import Mention, Pattern, RunResult, SourceReport
from social_listen.signal import age_label, per_day_counts, source_family

_PLAIN_STATUS = {
    "ok": "was checked",
    "UPSTREAM_BROKEN": "failed before it could be checked",
    "blocked": "refused the request",
    "skipped": "was turned off",
    "unconfigured": "needs an API key that is not set",
    "not_installed": "needs an optional tool that is not installed",
}


def build_patterns(mentions: list[Mention]) -> list[Pattern]:
    grouped: dict[str, list[Mention]] = defaultdict(list)
    for mention in mentions:
        for theme in mention.themes:
            grouped[theme].append(mention)
    labels = [label for label in grouped if not (label == "other" and len(grouped) > 1)]
    patterns: list[Pattern] = []
    for label in labels:
        group = grouped[label]
        sources = sorted({mention.source for mention in group})
        patterns.append(
            Pattern(
                label=label,
                mention_ids=[mention.id for mention in group],
                count=len(group),
                sources=sources,
                summary=_pattern_summary(label, group),
            )
        )
    patterns.sort(key=lambda pattern: (-pattern.count, pattern.label))
    return patterns


def _pattern_summary(label: str, group: list[Mention]) -> str:
    sources = ", ".join(sorted({source_family(mention.source) for mention in group}))
    return (
        f"{len(group)} mention{'s' if len(group) != 1 else ''} tagged {label}, "
        f"from {sources}."
    )


def render_digest(result: RunResult, config: Config) -> str:
    lines: list[str] = [
        f"# Social listen digest — {result.name}",
        "",
        f"Generated at {result.generated_at.strftime('%Y-%m-%dT%H:%M:%SZ')} ({result.mode}).",
        "This digest lists only items collected in this run. It is not a census of the internet.",
        "",
        f"- Window: {result.window_days} days",
        f"- Signal: {result.signal}",
        f"- Last 7 days: {result.verdict_7d} ({result.mentions_7d} dated mention"
        f"{'s' if result.mentions_7d != 1 else ''})",
        f"- Kept in this run: {len(result.mentions)}",
        "",
        "## Last 7 days",
        "",
        _verdict_sentence(result),
        "",
        "## Per-day counts",
        "",
        *_per_day_section(result),
        "",
        "## Key patterns",
        "",
        *_patterns_section(result),
        "",
        "## Notable quotes",
        "",
        *_quotes_section(result),
        "",
        "## Competitor comparison",
        "",
        *_comparison_section(result, config),
        "",
        "## Source status",
        "",
        "| Source | Status | Fetched | Kept | Detail |",
        "| --- | --- | ---: | ---: | --- |",
    ]
    for report in result.reports:
        lines.append(
            "| "
            + " | ".join(
                [
                    _cell(report.source),
                    report.status,
                    str(report.fetched),
                    str(report.kept),
                    _cell(report.detail),
                ]
            )
            + " |"
        )
    lines.extend(["", "## Limitations", ""])
    for item in result.limitations:
        lines.append(f"- {item}")
    if result.llm_used:
        lines.append("- Cluster labels came from the configured LLM. Quotes below are still source text.")
    lines.append("")
    return "\n".join(lines)


def render_mentions_markdown(mentions: list[Mention], now: datetime) -> str:
    lines = [
        "# Mentions",
        "",
        "| Source | Age | Author | Targets | Sentiment | Text | Link |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for mention in mentions:
        author = mention.handle or mention.author or "not provided"
        text = _cell(display_quote(mention.text, 160))
        lines.append(
            "| "
            + " | ".join(
                [
                    _cell(mention.source),
                    age_label(mention, now),
                    _cell(author),
                    _cell(", ".join(mention.target_ids)),
                    mention.sentiment,
                    text,
                    mention.url,
                ]
            )
            + " |"
        )
    if not mentions:
        lines.append("| — | — | — | — | — | none | — |")
    lines.append("")
    return "\n".join(lines)


def render_marketing(result: RunResult, config: Config) -> str:
    brand_names = [target.names[0] for target in config.targets if target.role == "brand"]
    subject = ", ".join(brand_names) if brand_names else result.name
    lines = [
        f"# Marketing summary — {subject}",
        "",
        (
            f"In the last 7 days there {'was' if result.mentions_7d == 1 else 'were'} "
            f"{result.mentions_7d} dated public mention"
            f"{'s' if result.mentions_7d != 1 else ''} we could verify. "
            f"The watch is {result.verdict_7d}."
        ),
        "",
        (
            f"Across the {result.window_days}-day window we kept {len(result.mentions)} item"
            f"{'s' if len(result.mentions) != 1 else ''}. "
            f"Signal strength is {result.signal}."
        ),
        "",
    ]
    if result.patterns:
        top = result.patterns[0]
        lines.append(f"The strongest theme label is {top.label} ({top.count}).")
        lines.append("")
    quotes = _notable(result.mentions, result.generated_at)[:3]
    if quotes:
        lines.append("What people wrote, with the original link:")
        lines.append("")
        for mention in quotes:
            who = mention.handle or mention.author or "author not provided"
            lines.append(f"- {who}: “{display_quote(mention.text, 220)}”")
            lines.append(f"  {mention.url}")
        lines.append("")
    problems = [report for report in result.reports if report.status != "ok" and report.status != "skipped"]
    if problems:
        lines.append("Sources we could not fully use:")
        lines.append("")
        for report in problems:
            plain = _PLAIN_STATUS.get(report.status, report.status)
            lines.append(f"- {report.source} {plain}.")
        lines.append("")
    if "fa" in config.languages:
        lines.extend(_persian_summary(result))
    lines.append("Sentiment tags are keyword hints, not a survey.")
    lines.append("")
    return "\n".join(lines)


def _persian_summary(result: RunResult) -> list[str]:
    verdict = "فعال" if result.verdict_7d == "active" else "آرام"
    return [
        "## خلاصه فارسی",
        "",
        (
            f"در ۷ روز گذشته {result.mentions_7d} اشاره تاریخ‌دار پیدا شد. "
            f"وضعیت این بازه: {verdict}. "
            f"در پنجره {result.window_days} روزه، {len(result.mentions)} مورد نگه داشته شد. "
            f"شدت سیگنال: {result.signal}."
        ),
        "نقل‌قول‌ها ترجمه نشده‌اند. متن اصلی و لینک در بخش انگلیسی همین فایل است.",
        "",
    ]


def display_quote(text: str, limit: int) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    cut = compact[:limit].rsplit(" ", 1)[0].rstrip()
    if not cut:
        cut = compact[:limit].rstrip()
    return cut + "…"


def base_limitations(max_hits: int) -> list[str]:
    return [
        "Undated web, news, and review snippets are listed when a run writes output, but they do not flip the 7-day verdict and they do not count toward STRONG.",
        f"Each query is capped at {max_hits} hits per request. A cap is a limit of this run, not a claim that nothing else exists.",
        "Sentiment and theme tags come from a small lexicon in English and Persian. Unknown means the lexicon did not match.",
        "Language detection is coarse: Latin text is tagged en, and Persian-specific letters are tagged fa. Set languages only when that guess is good enough.",
        "Own-account posts and pages on the target's own domains are excluded. Third-party posts that link to those domains are kept.",
        "This tool never posts, likes, replies, follows, or sends direct messages.",
    ]


def limitations_from_reports(reports: list[SourceReport]) -> list[str]:
    notes: list[str] = []
    for report in reports:
        if report.status == "ok":
            continue
        notes.append(f"{report.source}: {report.status}. {report.detail}")
    return notes


def _verdict_sentence(result: RunResult) -> str:
    if result.verdict_7d == "quiet":
        return (
            "Quiet over the last 7 days: no dated mention fell in that window. "
            "A wider window can still contain older dated items, listed with their age below."
        )
    return (
        f"Active over the last 7 days: {result.mentions_7d} dated mention"
        f"{'s' if result.mentions_7d != 1 else ''}."
    )


def _per_day_section(result: RunResult) -> list[str]:
    counts = per_day_counts(result.mentions, result.generated_at, result.window_days)
    nonzero = [(day, count) for day, count in counts if count]
    if not nonzero:
        return ["No dated mentions in this window."]
    lines = ["| Day (UTC) | Dated mentions |", "| --- | ---: |"]
    for day, count in nonzero:
        lines.append(f"| {day} | {count} |")
    zeros = sum(1 for _, count in counts if count == 0)
    lines.append("")
    lines.append(f"Days in the window with zero dated mentions: {zeros}.")
    undated = sum(1 for mention in result.mentions if mention.published_at is None)
    lines.append(f"Undated items kept but not placed on a day: {undated}.")
    return lines


def _patterns_section(result: RunResult) -> list[str]:
    if result.signal == "NONE" or not result.patterns:
        return ["No pattern. Nothing kept in this run cleared a theme."]
    multi = [pattern for pattern in result.patterns if pattern.count >= 2 or len(pattern.sources) >= 2]
    if not multi:
        lines = [
            "No pattern cleared a multi-mention bar. Single observations:",
            "",
        ]
    else:
        lines = ["KEY PATTERNS from the research:", ""]
    shown = multi or result.patterns
    for index, pattern in enumerate(shown, start=1):
        lines.append(f"{index}. **{pattern.label}** — {pattern.summary}")
    return lines


def _quotes_section(result: RunResult) -> list[str]:
    chosen = _notable(result.mentions, result.generated_at)
    if not chosen:
        return ["No quote. There is no kept text to cite."]
    lines: list[str] = []
    for mention in chosen:
        who = mention.handle or mention.author or "author not provided"
        lines.append(f"> {display_quote(mention.text, 280)}")
        lines.append("")
        lines.append(
            f"— {who}, {mention.source}, {age_label(mention, result.generated_at)}. {mention.url}"
        )
        lines.append("")
    return lines


def _notable(mentions: list[Mention], now: datetime) -> list[Mention]:
    def sort_key(mention: Mention) -> tuple[int, int, int]:
        dated = 0 if mention.published_at is not None else 1
        engagement = sum(mention.engagement.values())
        age_seconds = 0
        if mention.published_at is not None:
            age_seconds = int((now - mention.published_at).total_seconds())
        return (dated, -engagement, age_seconds)

    return sorted(mentions, key=sort_key)[:5]


def _comparison_section(result: RunResult, config: Config) -> list[str]:
    competitors = [target for target in config.targets if target.role == "competitor"]
    if not competitors and not any(target.role == "brand" for target in config.targets):
        return ["No brand or competitor targets were configured."]
    lines = [
        "| Target | Role | Dated in 7 days | Dated in window | Undated |",
        "| --- | --- | ---: | ---: | ---: |",
    ]
    for target in config.targets:
        rows = [mention for mention in result.mentions if target.id in mention.target_ids]
        dated_7 = 0
        dated_window = 0
        undated = 0
        for mention in rows:
            if mention.published_at is None:
                undated += 1
                continue
            dated_window += 1
            age = result.generated_at - mention.published_at
            if age.total_seconds() <= 7 * 86400:
                dated_7 += 1
        lines.append(
            f"| {_cell(target.names[0])} | {target.role} | {dated_7} | {dated_window} | {undated} |"
        )
    if not competitors:
        lines.append("")
        lines.append("No competitors configured, so there is no comparison to make.")
    return lines


def _cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").strip() or "—"
