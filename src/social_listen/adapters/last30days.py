"""Optional adapter for the last30days CLI.

Upstream: https://github.com/mvanhorn/last30days-skill (MIT, Copyright (c) 2026 Matt Van Horn).
This module does not vendor that project. It runs the CLI with `--emit json`
when installed. Items are kept only when the JSON includes both a public URL
and text. Prose that is not JSON is not turned into quotes.

The built-in research mode (clustering, STRONG/THIN/NONE, 30-day window) lives
in the pipeline and does not require this CLI.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

from social_listen.adapters.agent_reach import _error_hint, _looks_blocked, _mentions_from_result
from social_listen.adapters.base import SearchContext, rollup
from social_listen.models import AdapterBatch, SourceReport


class Last30DaysAdapter:
    source = "last30days"

    def collect(self, ctx: SearchContext) -> AdapterBatch:
        binary = os.environ.get("LAST30DAYS_BIN", "last30days").strip() or "last30days"
        resolved = shutil.which(binary) if os.path.sep not in binary else binary
        if resolved is None or (os.path.sep in binary and not os.path.isfile(binary)):
            return AdapterBatch(
                [],
                SourceReport(
                    self.source,
                    "not_installed",
                    "last30days CLI is not on PATH. Built-in 30-day clustering still runs; upstream JSON was not fetched.",
                    0,
                    0,
                ),
            )
        mentions = []
        fetched = 0
        notes: list[str] = []
        broken = 0
        blocked = 0
        for phrase in ctx.phrases:
            try:
                completed = subprocess.run(
                    [
                        resolved,
                        "--emit",
                        "json",
                        "--days",
                        str(ctx.config.window_days),
                        "--",
                        phrase.text,
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=180,
                )
            except subprocess.TimeoutExpired:
                broken += 1
                notes.append(f"{phrase.text}: timed out")
                continue
            except OSError:
                broken += 1
                notes.append(f"{phrase.text}: last30days could not start")
                continue
            payload = _load_json(completed.stdout)
            if payload is None:
                broken += 1
                notes.append(
                    "last30days did not emit JSON; prose was ignored so no quote would be invented"
                )
                continue
            if isinstance(payload, dict) and payload.get("ok") is False:
                hint = _error_hint(payload)
                if _looks_blocked(hint):
                    blocked += 1
                    notes.append(hint or f"{phrase.text}: last30days blocked")
                else:
                    broken += 1
                    notes.append(hint or f"last30days exited {completed.returncode}")
                continue
            found, raw_count = _mentions_from_result(payload, "last30days", phrase.text)
            fetched += raw_count
            mentions.extend(found)
            if completed.returncode != 0 and not found:
                broken += 1
                notes.append(f"last30days exited {completed.returncode}")
        status, detail = rollup(
            queries=len(ctx.phrases),
            blocked=blocked,
            broken=broken,
            kept_candidates=len(mentions),
            errors=notes,
        )
        if status == "ok" and not notes:
            detail = "queried via last30days --emit json"
        return AdapterBatch(mentions, SourceReport(self.source, status, detail, fetched, 0))


def _load_json(stdout: str) -> object | None:
    text = stdout.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            start = text.find("[")
            end = text.rfind("]")
        if start < 0 or end <= start:
            return None
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
