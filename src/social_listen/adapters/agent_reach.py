"""Optional adapter for the Agent Reach CLI.

Upstream: https://github.com/Panniantong/Agent-Reach (MIT, Copyright (c) 2025 Agent Eyes).
This module does not vendor that project. It runs `agent-reach <channel> search`
when the binary is installed and records ok / UPSTREAM_BROKEN / blocked /
not_installed from the CLI's JSON envelope. The PyPI package named agent-reach
is a different project and is not used here.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Literal

from social_listen.adapters.base import Phrase, SearchContext, rollup
from social_listen.models import AdapterBatch, Mention, SourceReport
from social_listen.normalize import is_public_http_url, parse_time
from social_listen.textmatch import contains_term

_Outcome = Literal["ok", "blocked", "broken"]


class AgentReachAdapter:
    def __init__(self, channel: str) -> None:
        self.channel = channel
        self.source = f"agent_reach:{channel}"

    def collect(self, ctx: SearchContext) -> AdapterBatch:
        binary = os.environ.get("AGENT_REACH_BIN", "agent-reach").strip() or "agent-reach"
        resolved = shutil.which(binary) if os.path.sep not in binary else binary
        if resolved is None or (os.path.sep in binary and not os.path.isfile(binary)):
            return AdapterBatch(
                [],
                SourceReport(
                    self.source,
                    "not_installed",
                    "agent-reach CLI is not on PATH. Install Panniantong/Agent-Reach from GitHub, not the unrelated PyPI name.",
                    0,
                    0,
                ),
            )
        mentions: list[Mention] = []
        fetched = 0
        blocked = 0
        broken = 0
        notes: list[str] = []
        for phrase in ctx.phrases:
            outcome, found, raw_count, note = _search_channel(resolved, self.channel, phrase, ctx)
            fetched += raw_count
            mentions.extend(found)
            if outcome == "blocked":
                blocked += 1
                notes.append(note)
            elif outcome == "broken":
                broken += 1
                notes.append(note)
        status, detail = rollup(
            queries=len(ctx.phrases),
            blocked=blocked,
            broken=broken,
            kept_candidates=len(mentions),
            errors=notes,
        )
        if status == "ok" and not notes:
            detail = "queried via agent-reach"
        return AdapterBatch(mentions, SourceReport(self.source, status, detail, fetched, 0))


def _search_channel(
    binary: str,
    channel: str,
    phrase: Phrase,
    ctx: SearchContext,
) -> tuple[_Outcome, list[Mention], int, str]:
    try:
        completed = subprocess.run(
            [binary, channel, "search", "--", phrase.text],
            check=False,
            capture_output=True,
            text=True,
            timeout=90,
        )
    except subprocess.TimeoutExpired:
        return "broken", [], 0, f"{channel} timed out"
    except OSError as exc:
        return "broken", [], 0, f"{channel} could not start: {exc}"
    payload = _parse_stdout(completed.stdout)
    hint = _error_hint(payload)
    if payload is None:
        return "broken", [], 0, f"{channel} did not print a JSON object"
    ok = payload.get("ok")
    if ok is False:
        if _looks_blocked(hint):
            return "blocked", [], 0, hint or f"{channel} blocked"
        return "broken", [], 0, hint or f"{channel} exited {completed.returncode}"
    found, fetched = _mentions_from_result(payload.get("result"), f"agent_reach:{channel}", phrase.text)
    if completed.returncode != 0 and not found:
        if _looks_blocked(hint):
            return "blocked", [], fetched, hint or f"{channel} blocked"
        return "broken", [], fetched, f"{channel} exited {completed.returncode}"
    return "ok", found, fetched, ""


def _parse_stdout(stdout: str) -> dict[str, object] | None:
    text = stdout.strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    if isinstance(payload, dict):
        return payload
    return None


def _error_hint(payload: dict[str, object] | None) -> str:
    if not payload:
        return ""
    error = payload.get("error")
    if isinstance(error, dict):
        hint = error.get("hint") or error.get("message")
        if isinstance(hint, str):
            return hint.strip()
    if isinstance(error, str):
        return error.strip()
    return ""


def _looks_blocked(hint: str) -> bool:
    lowered = hint.lower()
    markers = (
        "login",
        "cookie",
        "auth",
        "blocked",
        "captcha",
        "forbidden",
        "401",
        "403",
        "429",
        "sign in",
        "not logged",
    )
    return any(marker in lowered for marker in markers)


def _mentions_from_result(
    result: object,
    source: str,
    phrase: str,
) -> tuple[list[Mention], int]:
    rows = list(_walk_items(result))
    mentions: list[Mention] = []
    fetched = 0
    for row in rows:
        mention = _row_to_mention(row, source)
        if mention is None:
            continue
        fetched += 1
        blob = f"{mention.title or ''}\n{mention.text}"
        if contains_term(blob, phrase):
            mentions.append(mention)
    return mentions, fetched


def _walk_items(node: object) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    if isinstance(node, list):
        for item in node:
            found.extend(_walk_items(item))
        return found
    if not isinstance(node, dict):
        return found
    if _url_of(node) and (_text_of(node) or _title_of(node)):
        found.append(node)
    for key in ("items", "results", "data", "notes", "videos", "posts", "list", "comments"):
        if key in node:
            found.extend(_walk_items(node[key]))
    return found


def _url_of(row: dict[str, object]) -> str | None:
    for key in ("url", "link", "permalink", "share_url", "webpage_url"):
        value = row.get(key)
        if isinstance(value, str) and is_public_http_url(value):
            return value
    return None


def _title_of(row: dict[str, object]) -> str:
    for key in ("title", "name"):
        value = row.get(key)
        if isinstance(value, str):
            return value.strip()
    return ""


def _text_of(row: dict[str, object]) -> str:
    for key in ("text", "content", "description", "snippet", "body", "desc", "comment"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _row_to_mention(row: dict[str, object], source: str) -> Mention | None:
    url = _url_of(row)
    if url is None:
        return None
    title = _title_of(row)
    text = _text_of(row) or title
    if not text:
        return None
    author = row.get("author") or row.get("user") or row.get("uploader")
    handle = row.get("handle") or row.get("username")
    author_text = author if isinstance(author, str) else None
    handle_text = handle if isinstance(handle, str) else None
    if isinstance(author, dict):
        name = author.get("name") or author.get("username") or author.get("screen_name")
        username = author.get("username") or author.get("screen_name") or author.get("handle")
        if isinstance(name, str):
            author_text = name
        if isinstance(username, str):
            handle_text = username
    published = None
    for key in ("published_at", "created_at", "date", "pubdate"):
        published = parse_time(row.get(key))
        if published is not None:
            break
    return Mention(
        source=source,
        url=url,
        text=text,
        title=title or None,
        author=author_text,
        handle=handle_text or author_text,
        published_at=published,
    )
