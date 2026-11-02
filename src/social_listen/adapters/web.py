"""Web, news, and review-site search through a pluggable provider.

DuckDuckGo HTML is the keyless default. Brave and Serper are used only when
their keys are set. Snippets are stored as the quote. Pages are not crawled.
Relative ages from the provider are not turned into timestamps.
"""

from __future__ import annotations

import json
import os
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request

from social_listen.adapters.base import Phrase, SearchAdapter, SearchContext, with_negatives
from social_listen.http import HttpClient, HttpError, open_request
from social_listen.models import AdapterBatch, Mention, SourceReport
from social_listen.normalize import is_public_http_url, parse_time
from social_listen.textmatch import contains_term


class WebHit:
    def __init__(self, url: str, title: str, snippet: str, published_at: object = None) -> None:
        self.url = url
        self.title = title
        self.snippet = snippet
        self.published_at = published_at


class WebProvider:
    name = "base"

    def search(self, client: HttpClient, query: str, limit: int) -> list[WebHit]:
        raise NotImplementedError


class DuckDuckGoProvider(WebProvider):
    name = "duckduckgo"

    def search(self, client: HttpClient, query: str, limit: int) -> list[WebHit]:
        # The HTML endpoint often answers datacenter IPs with HTTP 202 and a
        # bot check. Lite is the same public index and still returns results.
        last_error = HttpError(None, "DuckDuckGo returned no parseable results")
        for url in (
            "https://lite.duckduckgo.com/lite/",
            "https://html.duckduckgo.com/html/",
        ):
            response = client.get(url, params={"q": query}, headers={"Accept": "text/html"})
            text = response.text()
            if response.status in {401, 403, 429} or _ddg_challenge(response.status, text):
                last_error = HttpError(
                    response.status,
                    f"DuckDuckGo refused the query (HTTP {response.status})",
                )
                continue
            if response.status >= 400:
                last_error = HttpError(response.status, f"DuckDuckGo HTTP {response.status}")
                continue
            hits = _parse_ddg(text)
            if hits:
                return hits[:limit]
            if _ddg_empty_page(text):
                return []
            last_error = HttpError(response.status, "DuckDuckGo returned no parseable results")
        raise last_error


class BraveProvider(WebProvider):
    name = "brave"

    def __init__(self, key: str) -> None:
        self._key = key

    def search(self, client: HttpClient, query: str, limit: int) -> list[WebHit]:
        status, payload = client.get_json(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": str(min(limit, 20))},
            headers={"X-Subscription-Token": self._key, "Accept": "application/json"},
        )
        if status in {401, 402, 403, 429}:
            raise HttpError(status, f"Brave HTTP {status}")
        if status >= 400 or not isinstance(payload, dict):
            raise HttpError(status, f"Brave HTTP {status}")
        web = payload.get("web")
        results = web.get("results") if isinstance(web, dict) else None
        if not isinstance(results, list):
            return []
        hits: list[WebHit] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            if not isinstance(url, str):
                continue
            title = item.get("title") if isinstance(item.get("title"), str) else ""
            snippet = item.get("description") if isinstance(item.get("description"), str) else ""
            published = item.get("page_age")
            hits.append(WebHit(url, title, snippet, published))
        return hits


class SerperProvider(WebProvider):
    """Serper's search API is a POST. It is a search call, not a social write."""

    name = "serper"

    def __init__(self, key: str) -> None:
        self._key = key

    def search(self, client: HttpClient, query: str, limit: int) -> list[WebHit]:
        body = json.dumps({"q": query, "num": min(limit, 20)}).encode("utf-8")
        request = Request(
            "https://google.serper.dev/search",
            data=body,
            method="POST",
            headers={
                "X-API-KEY": self._key,
                "Content-Type": "application/json",
                "User-Agent": client.user_agent,
            },
        )
        try:
            response = open_request(request, timeout=client.timeout, max_bytes=client.max_bytes)
        except HttpError as exc:
            raise HttpError(exc.status, f"Serper failed: {exc.message}") from exc
        status = response.status
        raw = response.body
        if status >= 400:
            raise HttpError(status, f"Serper HTTP {status}")
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise HttpError(status, "Serper returned non-JSON") from exc
        organic = payload.get("organic") if isinstance(payload, dict) else None
        if not isinstance(organic, list):
            return []
        hits: list[WebHit] = []
        for item in organic:
            if not isinstance(item, dict):
                continue
            url = item.get("link")
            if not isinstance(url, str):
                continue
            title = item.get("title") if isinstance(item.get("title"), str) else ""
            snippet = item.get("snippet") if isinstance(item.get("snippet"), str) else ""
            hits.append(WebHit(url, title, snippet, item.get("date")))
        return hits


def resolve_provider(explicit: str | None) -> WebProvider:
    choice = (explicit or os.environ.get("WEB_SEARCH_PROVIDER") or "").strip().lower()
    brave = os.environ.get("BRAVE_SEARCH_API_KEY", "").strip()
    serper = os.environ.get("SERPER_API_KEY", "").strip()
    if choice == "brave":
        if not brave:
            raise HttpError(None, "WEB_SEARCH_PROVIDER=brave but BRAVE_SEARCH_API_KEY is empty")
        return BraveProvider(brave)
    if choice == "serper":
        if not serper:
            raise HttpError(None, "WEB_SEARCH_PROVIDER=serper but SERPER_API_KEY is empty")
        return SerperProvider(serper)
    if choice in {"", "duckduckgo"}:
        if not choice and brave:
            return BraveProvider(brave)
        if not choice and serper:
            return SerperProvider(serper)
        return DuckDuckGoProvider()
    raise HttpError(None, f"unknown web provider {choice}")


class WebSearchAdapter(SearchAdapter):
    def __init__(self, source: str, provider: WebProvider | None = None) -> None:
        self.source = source
        self._provider = provider

    def search_phrase(self, ctx: SearchContext, phrase: Phrase) -> tuple[list[Mention], int]:
        provider = self._provider or resolve_provider(ctx.config.web_provider)
        query = self.build_query(ctx, phrase)
        hits = provider.search(ctx.client, query, ctx.max_hits)
        mentions: list[Mention] = []
        fetched = 0
        for hit in hits:
            mention = _hit_to_mention(self.source, hit)
            if mention is None:
                continue
            fetched += 1
            blob = f"{mention.title or ''}\n{mention.text}"
            if contains_term(blob, phrase.text):
                mentions.append(mention)
        return mentions, fetched

    def build_query(self, ctx: SearchContext, phrase: Phrase) -> str:
        return with_negatives(phrase.text, phrase.negatives)


class NewsAdapter(WebSearchAdapter):
    def __init__(self, provider: WebProvider | None = None) -> None:
        super().__init__("news", provider)

    def collect(self, ctx: SearchContext) -> AdapterBatch:
        sites = [site.strip() for site in ctx.config.news_sites if site.strip()]
        if len(sites) > 6:
            self.extra_detail = f"Only the first 6 of {len(sites)} news sites were queried."
        return super().collect(ctx)

    def build_query(self, ctx: SearchContext, phrase: Phrase) -> str:
        base = with_negatives(phrase.text, phrase.negatives)
        sites = [site.strip() for site in ctx.config.news_sites if site.strip()]
        if not sites:
            return base
        joined = " OR ".join(f"site:{site}" for site in sites[:6])
        return f"{base} ({joined})"


class ReviewsAdapter(WebSearchAdapter):
    def __init__(self, provider: WebProvider | None = None) -> None:
        super().__init__("reviews", provider)
        self._review_errors: list[str] = []

    def collect(self, ctx: SearchContext) -> AdapterBatch:
        sites = [site.strip() for site in ctx.config.review_sites if site.strip()]
        if not sites:
            return AdapterBatch(
                [],
                SourceReport(self.source, "skipped", "no review sites configured", 0, 0),
            )
        self._review_errors = []
        if len(sites) > 6:
            self.extra_detail = f"Only the first 6 of {len(sites)} review sites were queried."
        batch = super().collect(ctx)
        if self._review_errors:
            extra = "; ".join(self._review_errors[:4])
            batch.report.detail = f"{batch.report.detail} {extra}".strip()
        return batch

    def search_phrase(self, ctx: SearchContext, phrase: Phrase) -> tuple[list[Mention], int]:
        provider = self._provider or resolve_provider(ctx.config.web_provider)
        sites = [site.strip() for site in ctx.config.review_sites if site.strip()]
        mentions: list[Mention] = []
        fetched = 0
        base = with_negatives(phrase.text, phrase.negatives)
        errors: list[str] = []
        statuses: list[int | None] = []
        any_ok = False
        chosen = sites[:6]
        per_site = max(1, min(5, ctx.max_hits // max(1, len(chosen))))
        for site in chosen:
            query = f"{base} site:{site}"
            try:
                hits = provider.search(ctx.client, query, per_site)
            except HttpError as exc:
                errors.append(f"{site}: {exc.message}")
                statuses.append(exc.status)
                continue
            any_ok = True
            for hit in hits:
                mention = _hit_to_mention(self.source, hit)
                if mention is None:
                    continue
                fetched += 1
                blob = f"{mention.title or ''}\n{mention.text}"
                if contains_term(blob, phrase.text):
                    mentions.append(mention)
        if any_ok:
            self._review_errors.extend(errors)
        if errors and not any_ok:
            blocked_codes = {202, 401, 402, 403, 429}
            status = statuses[0] if statuses and all(code in blocked_codes for code in statuses) else None
            if status is None and any(code in blocked_codes for code in statuses):
                status = next(code for code in statuses if code in blocked_codes)
            raise HttpError(status, "; ".join(errors[:4]))
        return mentions, fetched


def _hit_to_mention(source: str, hit: WebHit) -> Mention | None:
    if not is_public_http_url(hit.url):
        return None
    title = " ".join(hit.title.split())
    snippet = " ".join(hit.snippet.split())
    text = snippet or title
    if not text:
        return None
    return Mention(
        source=source,
        url=hit.url,
        text=text,
        title=title or None,
        published_at=parse_time(hit.published_at) if isinstance(hit.published_at, str) else None,
    )


class _DDGParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict[str, str]] = []
        self._capture: str | None = None
        self._snippet_tag: str | None = None
        self._buf: list[str] = []
        self._current: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {key: value or "" for key, value in attrs}
        classes = set(attr.get("class", "").split())
        href = attr.get("href", "")
        if tag == "a" and ("result__a" in classes or "result-link" in classes):
            if self._current and self._current.get("title") and self._capture != "snippet":
                self.rows.append(self._current)
            self._current = {"title": "", "snippet": "", "href": href}
            self._capture = "title"
            self._snippet_tag = None
            self._buf = []
        elif tag == "a" and "result__snippet" in classes and self._current is not None:
            self._capture = "snippet"
            self._snippet_tag = "a"
            self._buf = []
            if not self._current.get("href"):
                self._current["href"] = href
        elif tag == "td" and "result-snippet" in classes and self._current is not None:
            self._capture = "snippet"
            self._snippet_tag = "td"
            self._buf = []

    def handle_endtag(self, tag: str) -> None:
        if self._capture is None or self._current is None:
            return
        if self._capture == "title" and tag == "a":
            self._current["title"] = " ".join("".join(self._buf).split())
            self._capture = None
            self._buf = []
            return
        if self._capture == "snippet" and tag == self._snippet_tag:
            self._current["snippet"] = " ".join("".join(self._buf).split())
            self.rows.append(self._current)
            self._current = None
            self._capture = None
            self._snippet_tag = None
            self._buf = []

    def handle_data(self, data: str) -> None:
        if self._capture is not None:
            self._buf.append(data)

    def close(self) -> None:
        super().close()
        if self._current is None:
            return
        if self._capture == "snippet" and self._buf:
            self._current["snippet"] = " ".join("".join(self._buf).split())
        elif self._capture == "title" and self._buf and not self._current.get("title"):
            self._current["title"] = " ".join("".join(self._buf).split())
        if self._current.get("href") or self._current.get("title"):
            self.rows.append(self._current)
        self._current = None
        self._capture = None
        self._buf = []


def _ddg_challenge(status: int, text: str) -> bool:
    if status == 202:
        return True
    if "result-link" in text or "result__a" in text:
        return False
    lowered = text.lower()
    return "anomaly-modal" in lowered or "unfortunately, bots use duckduckgo" in lowered


def _ddg_empty_page(text: str) -> bool:
    lowered = text.lower()
    if "result-link" in lowered or "result__a" in lowered:
        return False
    return "no results" in lowered


def _parse_ddg(html: str) -> list[WebHit]:
    parser = _DDGParser()
    parser.feed(html)
    parser.close()
    hits: list[WebHit] = []
    for row in parser.rows:
        url = _unwrap_ddg(row.get("href", ""))
        if url is None:
            continue
        hits.append(WebHit(url, row.get("title", ""), row.get("snippet", "")))
    return hits


def _unwrap_ddg(href: str) -> str | None:
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    query = parse_qs(parsed.query)
    if "uddg" in query and query["uddg"]:
        return unquote(query["uddg"][0])
    if parsed.scheme in {"http", "https"} and "duckduckgo.com" not in (parsed.hostname or ""):
        return href
    return None
