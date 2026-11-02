"""Optional, provider-agnostic clustering. The pipeline never requires this.

The only network call here is to the configured chat endpoint. It is not a
social post. If the model cites an id we did not send, that cluster is dropped.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from urllib.request import Request

from social_listen.digest import _pattern_summary
from social_listen.http import HttpError, open_request
from social_listen.models import Mention, Pattern

_SYSTEM = (
    "You cluster social-listening evidence. Reply with JSON only, no markdown. "
    'Shape: {"clusters":[{"label":"short theme","mention_ids":["id"],"summary":"one sentence"}]}. '
    "Use only mention ids from the input. The summary may only restate those items. "
    "Do not add quotes, counts, urls, or events that are not in the input."
)


@dataclass
class OpenAICompatibleSummarizer:
    api_key: str
    base_url: str
    model: str
    timeout: float = 40.0

    def cluster(self, mentions: list[Mention]) -> list[Pattern] | None:
        if len(mentions) < 2:
            return None
        packed = [
            {
                "id": mention.id,
                "source": mention.source,
                "url": mention.url,
                "text": mention.text[:500],
            }
            for mention in mentions[:40]
        ]
        body = json.dumps(
            {
                "model": self.model,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": json.dumps({"mentions": packed})},
                ],
            }
        ).encode("utf-8")
        request = Request(
            self.base_url.rstrip("/") + "/chat/completions",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            response = open_request(request, timeout=self.timeout, max_bytes=2_000_000)
        except HttpError as exc:
            raise RuntimeError(exc.message) from exc
        if response.status >= 400:
            raise RuntimeError(f"LLM HTTP {response.status}")
        return _patterns_from_response(response.body, mentions)


def load_summarizer() -> OpenAICompatibleSummarizer | None:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        return None
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").strip()
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"
    return OpenAICompatibleSummarizer(api_key=key, base_url=base, model=model)


def _patterns_from_response(raw: bytes, mentions: list[Mention]) -> list[Pattern] | None:
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if not isinstance(content, str):
        return None
    parsed = _parse_json_object(content)
    if not isinstance(parsed, dict):
        return None
    clusters = parsed.get("clusters")
    if not isinstance(clusters, list):
        return None
    known = {mention.id: mention for mention in mentions}
    patterns: list[Pattern] = []
    for cluster in clusters:
        if not isinstance(cluster, dict):
            continue
        label = cluster.get("label")
        ids = cluster.get("mention_ids")
        if not isinstance(label, str) or not label.strip():
            continue
        if not isinstance(ids, list) or not ids or not all(isinstance(item, str) for item in ids):
            continue
        if any(item not in known for item in ids):
            continue
        unique = list(dict.fromkeys(ids))
        group = [known[item] for item in unique]
        theme = label.strip()[:80]
        sources = sorted({mention.source for mention in group})
        patterns.append(
            Pattern(
                label=theme,
                mention_ids=unique,
                count=len(group),
                sources=sources,
                summary=_pattern_summary(theme, group),
            )
        )
    return patterns or None


def _parse_json_object(content: str) -> object | None:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None
