"""Command line. `run` is the daily watch. `research` is the 30-day mode."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from social_listen import __version__
from social_listen.config import load_config
from social_listen.http import HttpClient
from social_listen.llm import load_summarizer
from social_listen.pipeline import execute
from social_listen.writers import write_run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="social-listen",
        description="Read-only social listening. Searches public sources and writes a digest. Never posts.",
    )
    parser.add_argument("--version", action="version", version=f"social-listen {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="Daily watch using the config window")
    _add_common(run_parser)
    run_parser.add_argument(
        "--no-quiet-empty",
        action="store_true",
        help="Write the digest even when no dated mention clears the bar",
    )

    research_parser = sub.add_parser(
        "research",
        help="Last-30-days research: patterns, signal strength, web fallback",
    )
    _add_common(research_parser)
    research_parser.add_argument("--days", type=int, default=30, help="Lookback days (default 30)")
    research_parser.add_argument(
        "--quiet-empty",
        action="store_true",
        help="Stay silent when nothing dated clears the bar",
    )

    doctor_parser = sub.add_parser("doctor", help="Show whether each source can be called")
    doctor_parser.add_argument("--config", required=True)

    args = parser.parse_args(argv)
    _load_dotenv(Path(".env"))
    try:
        if args.command == "doctor":
            return _doctor(args.config)
        if args.command == "run":
            return _execute_command(args, mode="run", window=None, quiet_default=True)
        if args.command == "research":
            return _execute_command(args, mode="research", window=args.days, quiet_default=False)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"error: unknown command {args.command}", file=sys.stderr)
    return 2


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", required=True, help="YAML target file")
    parser.add_argument("--out", required=True, help="Output directory")
    parser.add_argument(
        "--no-marketing",
        action="store_true",
        help="Do not write the plain-language marketing summary",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.2,
        help="Seconds to wait between queries inside one source (default 0.2)",
    )


def _execute_command(args: argparse.Namespace, *, mode: str, window: int | None, quiet_default: bool) -> int:
    config = load_config(args.config)
    if window is not None:
        if window < 1 or window > 3650:
            raise ValueError("--days must be from 1 to 3650")
        config.window_days = window
    quiet_empty = quiet_default
    if getattr(args, "no_quiet_empty", False):
        quiet_empty = False
    if getattr(args, "quiet_empty", False) and mode == "research":
        quiet_empty = True
    if mode == "run" and args.no_quiet_empty:
        quiet_empty = False
    result = execute(
        config,
        mode=mode,
        client=HttpClient(),
        summarizer=load_summarizer(),
        quiet_empty=quiet_empty,
        delay_seconds=max(0.0, args.delay),
    )
    paths = write_run(
        result,
        config,
        Path(args.out),
        marketing=not args.no_marketing,
    )
    for report in result.reports:
        print(
            f"{report.source}: {report.status} fetched={report.fetched} kept={report.kept} {report.detail}",
            file=sys.stderr,
        )
    if result.quiet:
        print("quiet: no dated mention cleared the bar", file=sys.stderr)
    else:
        print(
            f"signal={result.signal} verdict_7d={result.verdict_7d} kept={len(result.mentions)}",
            file=sys.stderr,
        )
    for path in paths:
        print(path)
    return 0


def _doctor(path: str) -> int:
    config = load_config(path)
    config.max_hits = 1
    result = execute(
        config,
        mode="run",
        client=HttpClient(),
        summarizer=None,
        quiet_empty=False,
        delay_seconds=0.0,
    )
    print(f"config: {config.name}")
    print(f"targets: {', '.join(target.id for target in config.targets)}")
    for report in result.reports:
        print(
            f"{report.source}: {report.status} fetched={report.fetched} kept={report.kept} {report.detail}"
        )
    llm = "configured" if os.environ.get("OPENAI_API_KEY") else "not configured"
    reach = "installed" if _on_path(os.environ.get("AGENT_REACH_BIN", "agent-reach")) else "not installed"
    last30 = "installed" if _on_path(os.environ.get("LAST30DAYS_BIN", "last30days")) else "not installed"
    print(f"llm: {llm}")
    print(f"agent-reach binary: {reach}")
    print(f"last30days binary: {last30}")
    return 0


def _on_path(binary: str) -> bool:
    if os.path.sep in binary:
        return os.path.isfile(binary)
    return shutil.which(binary) is not None


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].strip()
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        else:
            marker = value.find(" #")
            if marker >= 0:
                value = value[:marker].rstrip()
        if key and key not in os.environ:
            os.environ[key] = value
