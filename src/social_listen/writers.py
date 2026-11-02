"""Write JSON, CSV, and Markdown. Quiet runs skip the human-facing digest."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from social_listen.config import Config
from social_listen.digest import render_digest, render_marketing, render_mentions_markdown
from social_listen.models import RunResult


def write_run(result: RunResult, config: Config, out_dir: Path, *, marketing: bool) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    run_path = out_dir / "run.json"
    run_path.write_text(json.dumps(_run_payload(result), indent=2), encoding="utf-8")
    written.append(run_path)
    sources_path = out_dir / "sources.json"
    sources_path.write_text(
        json.dumps([report.to_dict() for report in result.reports], indent=2),
        encoding="utf-8",
    )
    written.append(sources_path)
    if result.quiet:
        quiet_path = out_dir / "quiet.txt"
        quiet_path.write_text("quiet\n", encoding="utf-8")
        written.append(quiet_path)
        return written
    mentions_path = out_dir / "mentions.json"
    mentions_path.write_text(
        json.dumps([mention.to_dict() for mention in result.mentions], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    written.append(mentions_path)
    csv_path = out_dir / "mentions.csv"
    _write_csv(csv_path, result)
    written.append(csv_path)
    table_path = out_dir / "mentions.md"
    table_path.write_text(
        render_mentions_markdown(result.mentions, result.generated_at),
        encoding="utf-8",
    )
    written.append(table_path)
    digest_path = out_dir / "digest.md"
    digest_path.write_text(render_digest(result, config), encoding="utf-8")
    written.append(digest_path)
    if marketing:
        marketing_path = out_dir / "marketing.md"
        marketing_path.write_text(render_marketing(result, config), encoding="utf-8")
        written.append(marketing_path)
    return written


def _run_payload(result: RunResult) -> dict[str, object]:
    undated = sum(1 for mention in result.mentions if mention.published_at is None)
    return {
        "name": result.name,
        "mode": result.mode,
        "generated_at": result.generated_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_days": result.window_days,
        "signal": result.signal,
        "verdict_7d": result.verdict_7d,
        "mentions_7d": result.mentions_7d,
        "mentions_kept": len(result.mentions),
        "undated_not_counted_in_verdict": undated,
        "cleared_bar": result.cleared_bar,
        "quiet": result.quiet,
        "llm_used": result.llm_used,
        "limitations": result.limitations,
        "sources": [report.to_dict() for report in result.reports],
    }


def _write_csv(path: Path, result: RunResult) -> None:
    fields = [
        "id",
        "source",
        "url",
        "external_url",
        "author",
        "handle",
        "published_at",
        "language",
        "sentiment",
        "themes",
        "target_ids",
        "engagement",
        "title",
        "text",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for mention in result.mentions:
            row = mention.to_dict()
            writer.writerow(
                {
                    "id": _csv_cell(row["id"]),
                    "source": _csv_cell(row["source"]),
                    "url": _csv_cell(row["url"]),
                    "external_url": _csv_cell(row["external_url"] or ""),
                    "author": _csv_cell(row["author"] or ""),
                    "handle": _csv_cell(row["handle"] or ""),
                    "published_at": _csv_cell(row["published_at"] or ""),
                    "language": _csv_cell(row["language"] or ""),
                    "sentiment": _csv_cell(row["sentiment"]),
                    "themes": _csv_cell(",".join(mention.themes)),
                    "target_ids": _csv_cell(",".join(mention.target_ids)),
                    "engagement": _csv_cell(json.dumps(mention.engagement, sort_keys=True)),
                    "title": _csv_cell(row["title"] or ""),
                    "text": _csv_cell(row["text"]),
                }
            )


def _csv_cell(value: object) -> str:
    text = "" if value is None else str(value)
    if text.startswith(("=", "+", "-", "@")):
        return "'" + text
    return text
