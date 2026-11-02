"""GET-only HTTP client for public read APIs.

Social adapters must use this client. The only POST helpers live next to the
optional Serper provider and the optional LLM call, which are not social actions.

Redirects are followed only to a public https host. Authorization and API-key
headers are dropped when the host changes, so a token is not forwarded to
another site.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from social_listen.models import Mention
from social_listen.normalize import is_public_http_url

DEFAULT_USER_AGENT = (
    "social-listen-agent/0.1 "
    "(+https://github.com/mohamadreza-rezaei/Social-Listen-Agent; read-only research)"
)

_AUTH_HEADERS = ("Authorization", "X-subscription-token", "X-api-key")


class HttpError(Exception):
    def __init__(
        self,
        status: int | None,
        message: str,
        *,
        partial_mentions: list[Mention] | None = None,
        partial_fetched: int = 0,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.partial_mentions = list(partial_mentions or [])
        self.partial_fetched = partial_fetched


@dataclass
class HttpResponse:
    status: int
    body: bytes
    url: str

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> object:
        return json.loads(self.text())


class _PublicRedirectHandler(HTTPRedirectHandler):
    """Follow https redirects to public hosts and strip credentials on a host change."""

    def redirect_request(
        self,
        req: Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> Request | None:
        if not _safe_redirect_target(newurl):
            raise HttpError(code, "refused redirect to a non-public URL")
        redirected = HTTPRedirectHandler.redirect_request(self, req, fp, code, msg, headers, newurl)
        if redirected is None:
            return None
        old_host = (urlparse(req.full_url).hostname or "").lower()
        new_host = (urlparse(newurl).hostname or "").lower()
        if old_host != new_host:
            for name in _AUTH_HEADERS:
                redirected.remove_header(name)
        return redirected


def _safe_redirect_target(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and is_public_http_url(url)


def redirected_request(request: Request, newurl: str, code: int = 302) -> Request:
    """Apply the redirect policy once. Used by tests and by the opener."""
    handler = _PublicRedirectHandler()
    redirected = handler.redirect_request(request, None, code, "Found", {}, newurl)
    if redirected is None:
        raise HttpError(code, "redirect was not followed")
    return redirected


def open_request(request: Request, timeout: float, max_bytes: int) -> HttpResponse:
    opener = build_opener(_PublicRedirectHandler)
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read(max_bytes + 1)
            status = int(getattr(response, "status", 200))
            final_url = response.geturl()
    except HttpError:
        raise
    except HTTPError as exc:
        raw = exc.read(max_bytes) if exc.fp is not None else b""
        return HttpResponse(exc.code, raw, request.full_url)
    except URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise HttpError(None, f"request failed: {reason}") from exc
    if len(body) > max_bytes:
        body = body[:max_bytes]
    return HttpResponse(status, body, final_url)


class HttpClient:
    """Issues HTTP GET requests. Does not expose a social write method."""

    def __init__(
        self,
        user_agent: str | None = None,
        timeout: float = 20.0,
        max_bytes: int = 2_000_000,
    ) -> None:
        configured = os.environ.get("SOCIAL_LISTEN_USER_AGENT", "").strip()
        self.user_agent = user_agent or configured or DEFAULT_USER_AGENT
        self.timeout = timeout
        self.max_bytes = max_bytes

    def get(
        self,
        url: str,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> HttpResponse:
        if params:
            joiner = "&" if "?" in url else "?"
            url = f"{url}{joiner}{urlencode(params)}"
        request_headers = {"User-Agent": self.user_agent, "Accept": "*/*"}
        if headers:
            request_headers.update(headers)
        request = Request(url, method="GET", headers=request_headers)
        return open_request(request, timeout=self.timeout, max_bytes=self.max_bytes)

    def get_json(
        self,
        url: str,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, object]:
        response = self.get(url, params=params, headers=headers)
        if response.status >= 400:
            return response.status, None
        try:
            return response.status, response.json()
        except json.JSONDecodeError as exc:
            raise HttpError(response.status, "response was not JSON") from exc
