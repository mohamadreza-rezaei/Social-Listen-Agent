import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request

import pytest

from social_listen.adapters.agent_reach import AgentReachAdapter
from social_listen.adapters.base import Phrase, SearchAdapter, SearchContext, rollup
from social_listen.adapters.hackernews import HackerNewsAdapter
from social_listen.adapters.last30days import Last30DaysAdapter
from social_listen.adapters.reddit import RedditAdapter
from social_listen.adapters.web import DuckDuckGoProvider, ReviewsAdapter, _parse_ddg
from social_listen.adapters.x_api import XAdapter, _fit_x_query
from social_listen.adapters.youtube import YouTubeAdapter
from social_listen.cli import _load_dotenv
from social_listen.config import Config, SourceToggles, Target, load_config
from social_listen.digest import display_quote, render_digest
from social_listen.http import HttpError, HttpResponse, redirected_request
from social_listen.models import AdapterBatch, Mention, RunResult, SourceReport
from social_listen.pipeline import execute
from social_listen.writers import write_run

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


class FakeHttp:
    def __init__(self, json_pages: list[tuple[int, object]], html: str = "") -> None:
        self.json_pages = list(json_pages)
        self.html = html
        self.gets: list[str] = []

    def get_json(self, url: str, params: dict[str, str] | None = None, headers: dict[str, str] | None = None) -> tuple[int, object]:
        self.gets.append(url)
        if not self.json_pages:
            return 599, None
        return self.json_pages.pop(0)

    def get(self, url: str, params: dict[str, str] | None = None, headers: dict[str, str] | None = None) -> HttpResponse:
        self.gets.append(url)
        return HttpResponse(200, self.html.encode("utf-8"), url)


def _ctx(client: FakeHttp) -> SearchContext:
    config = Config(
        name="t",
        window_days=7,
        targets=[Target(id="sqlite", role="brand", names=["SQLite"], handles=["sqlite"])],
    )
    return SearchContext(
        config=config,
        since_epoch=int((NOW - timedelta(days=7)).timestamp()),
        client=client,  # type: ignore[arg-type]
        phrases=[Phrase("SQLite", "sqlite", ())],
        max_hits=5,
    )


def test_hackernews_parses_story_and_comment() -> None:
    payload = {
        "hits": [
            {
                "objectID": "10",
                "title": "SQLite tips",
                "url": "https://example.com/tips",
                "author": "ada",
                "created_at": "2026-10-30T00:00:00Z",
                "points": 12,
                "num_comments": 3,
                "story_text": None,
            }
        ]
    }
    comments = {
        "hits": [
            {
                "objectID": "11",
                "comment_text": "SQLite is excellent for this",
                "story_title": "SQLite tips",
                "author": "grace",
                "created_at_i": 1761782400,
            },
            {
                "objectID": "12",
                "comment_text": "unrelated database note",
                "author": "bea",
                "created_at": "2026-10-30T00:00:00Z",
            },
        ]
    }
    client = FakeHttp([(200, payload), (200, comments)])
    batch = HackerNewsAdapter().collect(_ctx(client))
    assert batch.report.status == "ok"
    assert batch.report.fetched == 3
    assert len(batch.mentions) == 2
    assert batch.mentions[0].url == "https://news.ycombinator.com/item?id=10"
    assert batch.mentions[0].engagement["points"] == 12
    assert "excellent" in batch.mentions[1].text


def test_reddit_blocks_on_403_and_parses_listing() -> None:
    blocked = FakeHttp([(403, None), (403, None)])
    batch = RedditAdapter().collect(_ctx(blocked))
    assert batch.report.status == "blocked"

    listing = {
        "data": {
            "children": [
                {
                    "data": {
                        "title": "SQLite thread",
                        "selftext": "SQLite broke our backup",
                        "author": "someone",
                        "permalink": "/r/db/comments/abc/sqlite_thread/",
                        "created_utc": 1761782400,
                        "ups": 4,
                        "num_comments": 1,
                        "url": "https://www.reddit.com/r/db/comments/abc/sqlite_thread/",
                    }
                }
            ]
        }
    }
    ok = FakeHttp([(200, listing)])
    batch = RedditAdapter().collect(_ctx(ok))
    assert batch.report.status == "ok"
    assert batch.mentions[0].url.startswith("https://www.reddit.com/r/db/")
    assert batch.mentions[0].engagement["upvotes"] == 4


def test_x_and_youtube_stay_unconfigured_without_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    client = FakeHttp([])
    assert XAdapter().collect(_ctx(client)).report.status == "unconfigured"
    assert YouTubeAdapter().collect(_ctx(client)).report.status == "unconfigured"
    assert client.gets == []


def test_duckduckgo_unwraps_result_links() -> None:
    html = """
    <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpost&amp;rut=1">SQLite notes</a>
    <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpost">People said SQLite is solid.</a>
    """
    hits = _parse_ddg(html)
    assert hits[0].url == "https://example.com/post"
    assert "solid" in hits[0].snippet
    lite = """
    <a class='result-link' href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fnotes">SQLite notes</a>
    <td class='result-snippet'>People said SQLite is solid.</td>
    """
    lite_hits = _parse_ddg(lite)
    assert lite_hits[0].url == "https://example.com/notes"
    assert "solid" in lite_hits[0].snippet
    provider_hits = DuckDuckGoProvider().search(FakeHttp([], html), "SQLite", 5)  # type: ignore[arg-type]
    assert provider_hits[0].url == "https://example.com/post"


def test_agent_reach_and_last30days_report_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("social_listen.adapters.agent_reach.shutil.which", lambda _name: None)
    monkeypatch.setattr("social_listen.adapters.last30days.shutil.which", lambda _name: None)
    reach = AgentReachAdapter("youtube").collect(_ctx(FakeHttp([])))
    last = Last30DaysAdapter().collect(_ctx(FakeHttp([])))
    assert reach.report.status == "not_installed"
    assert last.report.status == "not_installed"


def test_agent_reach_blocked_hint_and_real_item(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_which(name: str) -> str:
        return "/usr/bin/agent-reach"

    seen: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: object) -> object:
        seen.append(args)

        class Result:
            returncode = 1
            stdout = json.dumps({"ok": False, "result": None, "error": {"hint": "login required"}})
            stderr = "COOKIE=secret-token"

        return Result()

    monkeypatch.setattr("social_listen.adapters.agent_reach.shutil.which", fake_which)
    monkeypatch.setattr("social_listen.adapters.agent_reach.os.path.isfile", lambda _path: True)
    monkeypatch.setattr("social_listen.adapters.agent_reach.subprocess.run", fake_run)
    blocked = AgentReachAdapter("twitter").collect(_ctx(FakeHttp([])))
    assert blocked.report.status == "blocked"
    assert blocked.mentions == []
    assert seen[0][-2:] == ["--", "SQLite"]
    assert "secret-token" not in blocked.report.detail

    def fake_ok(args: list[str], **kwargs: object) -> object:
        class Result:
            returncode = 0
            stdout = json.dumps(
                {
                    "ok": True,
                    "result": {
                        "url": "https://example.com/search",
                        "title": "SQLite search page",
                        "items": [
                            {
                                "url": "https://www.youtube.com/watch?v=abc",
                                "title": "SQLite walkthrough",
                                "text": "SQLite is great here",
                                "author": {"name": "Ada Lovelace", "username": "ada"},
                            }
                        ],
                    },
                }
            )
            stderr = ""

        return Result()

    monkeypatch.setattr("social_listen.adapters.agent_reach.subprocess.run", fake_ok)
    found = AgentReachAdapter("youtube").collect(_ctx(FakeHttp([])))
    assert found.report.status == "ok"
    urls = {mention.url for mention in found.mentions}
    assert "https://www.youtube.com/watch?v=abc" in urls
    nested = next(mention for mention in found.mentions if mention.url.endswith("v=abc"))
    assert nested.source == "agent_reach:youtube"
    assert nested.handle == "ada"


def test_last30days_ignores_prose_without_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("social_listen.adapters.last30days.shutil.which", lambda _name: "/bin/last30days")
    monkeypatch.setattr("social_listen.adapters.last30days.os.path.isfile", lambda _path: True)

    seen: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: object) -> object:
        seen.append(args)

        class Result:
            returncode = 0
            stdout = "People love it but this prose has no JSON url."
            stderr = "COOKIE=secret-token"

        return Result()

    monkeypatch.setattr("social_listen.adapters.last30days.subprocess.run", fake_run)
    batch = Last30DaysAdapter().collect(_ctx(FakeHttp([])))
    assert batch.report.status == "UPSTREAM_BROKEN"
    assert batch.mentions == []
    assert "secret-token" not in batch.report.detail
    assert seen[0][1:5] == ["--emit", "json", "--days", "7"]
    assert seen[0][-2:] == ["--", "SQLite"]


def test_challenge_response_is_blocked() -> None:
    class Adapter(SearchAdapter):
        source = "web"

        def search_phrase(self, ctx: SearchContext, phrase: Phrase) -> tuple[list, int]:
            raise HttpError(202, "DuckDuckGo refused the query (HTTP 202)")

    report = Adapter().collect(_ctx(FakeHttp([]))).report
    assert report.status == "blocked"


def test_rollup_is_explicit() -> None:
    assert rollup(queries=2, blocked=2, broken=0, kept_candidates=0, errors=["no"])[0] == "blocked"
    assert rollup(queries=1, blocked=0, broken=1, kept_candidates=0, errors=["x"])[0] == "UPSTREAM_BROKEN"
    assert rollup(queries=1, blocked=0, broken=0, kept_candidates=1, errors=[])[0] == "ok"
    assert rollup(queries=2, blocked=1, broken=1, kept_candidates=0, errors=["a", "b"])[0] == "blocked"
    assert rollup(queries=2, blocked=1, broken=1, kept_candidates=2, errors=["a"])[0] == "ok"


def test_pipeline_writes_only_real_urls_and_stays_quiet(tmp_path: Path) -> None:
    config = Config(
        name="northwind-watch",
        window_days=7,
        languages=["en"],
        targets=[
            Target(
                id="northwind",
                role="brand",
                names=["Northwind Labs"],
                domains=["northwind.example"],
                handles=["northwindlabs"],
                negative_keywords=["north wind"],
            )
        ],
        sources=_all_off(),
    )

    class Fake:
        source = "hackernews"

        def collect(self, ctx: SearchContext) -> object:
            return _batch(
                [
                    Mention(
                        source="hackernews",
                        url="https://news.ycombinator.com/item?id=99",
                        text="Northwind Labs is excellent",
                        author="ada",
                        handle="ada",
                        published_at=NOW - timedelta(days=1),
                        engagement={"points": 5},
                    ),
                    Mention(
                        source="hackernews",
                        url="https://northwind.example/blog",
                        text="Northwind Labs official post",
                        published_at=NOW - timedelta(days=1),
                    ),
                    Mention(
                        source="web",
                        url="https://example.com/undated",
                        text="Northwind Labs mentioned",
                    ),
                ]
            )

    result = execute(
        config,
        mode="run",
        now=NOW,
        quiet_empty=False,
        adapters=[Fake()],
        summarizer=None,
    )
    assert [mention.url for mention in result.mentions] == [
        "https://news.ycombinator.com/item?id=99",
        "https://example.com/undated",
    ]
    assert result.verdict_7d == "active"
    assert result.mentions_7d == 1
    assert result.signal == "THIN"
    digest = render_digest(result, config)
    assert "https://news.ycombinator.com/item?id=99" in digest
    assert "https://northwind.example" not in digest
    quote = display_quote(result.mentions[0].text, 280)
    assert quote in result.mentions[0].text or quote.rstrip("…") in " ".join(result.mentions[0].text.split())
    paths = write_run(result, config, tmp_path / "out", marketing=True)
    assert (tmp_path / "out" / "digest.md").exists()
    assert any(path.name == "mentions.csv" for path in paths)

    class Empty:
        source = "hackernews"

        def collect(self, ctx: SearchContext) -> object:
            return _batch([])

    quiet = execute(
        config,
        mode="run",
        now=NOW,
        quiet_empty=True,
        adapters=[Empty()],
        summarizer=None,
    )
    assert quiet.quiet
    assert quiet.signal == "NONE"
    write_run(quiet, config, tmp_path / "quiet", marketing=True)
    assert (tmp_path / "quiet" / "quiet.txt").read_text() == "quiet\n"
    assert not (tmp_path / "quiet" / "digest.md").exists()
    assert not (tmp_path / "quiet" / "mentions.json").exists()


def test_research_fallback_runs_web_when_social_is_thin(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class Social:
        source = "hackernews"

        def collect(self, ctx: SearchContext) -> object:
            calls.append(self.source)
            return _batch([])

    class Web:
        def __init__(self, source: str) -> None:
            self.source = source

        def collect(self, ctx: SearchContext) -> object:
            calls.append(self.source)
            if self.source != "web":
                return _batch([], source=self.source)
            return _batch(
                [
                    Mention(
                        source="web",
                        url="https://example.com/review",
                        text="Northwind Labs feels expensive",
                        published_at=NOW - timedelta(days=3),
                    )
                ],
                source=self.source,
            )

    monkeypatch.setattr(
        "social_listen.pipeline.selected_adapters",
        lambda config, include_community, include_supplemental: [Social()] if include_community else [],
    )
    monkeypatch.setattr(
        "social_listen.pipeline.disabled_reports",
        lambda config: [SourceReport("web", "skipped", "turned off", 0, 0)],
    )
    monkeypatch.setattr(
        "social_listen.pipeline.WebSearchAdapter",
        lambda source: Web(source),
    )
    monkeypatch.setattr("social_listen.pipeline.NewsAdapter", lambda: Web("news"))
    monkeypatch.setattr("social_listen.pipeline.ReviewsAdapter", lambda: Web("reviews"))
    config = Config(
        name="northwind-watch",
        window_days=30,
        targets=[Target(id="northwind", role="brand", names=["Northwind Labs"])],
        research_fallback=True,
    )
    result = execute(config, mode="research", now=NOW, quiet_empty=False, summarizer=None)
    assert "web" in calls
    assert result.mentions[0].url == "https://example.com/review"
    assert any("thin-signal fallback" in item for item in result.limitations)
    assert all(report.source != "web" or report.status != "skipped" for report in result.reports)


def test_sample_configs_load() -> None:
    english = load_config(ROOT / "configs" / "example.yaml")
    persian = load_config(ROOT / "configs" / "example-persian.yaml")
    assert english.targets[0].names == ["Vellamore Studio"]
    assert "ولامور" in persian.targets[0].names
    assert "fa" in persian.languages
    assert "emalls.ir" in persian.review_sites
    with pytest.raises(ValueError):
        load_config(ROOT / "configs" / "does-not-exist.yaml")


def test_redirect_drops_credentials_and_refuses_private_targets() -> None:
    request = Request(
        "https://api.search.brave.com/res/v1/web/search",
        method="GET",
        headers={"X-Subscription-Token": "secret", "Accept": "application/json"},
    )
    redirected = redirected_request(request, "https://evil.example/steal")
    names = {key.lower() for key, _value in redirected.header_items()}
    assert "x-subscription-token" not in names
    with pytest.raises(HttpError):
        redirected_request(request, "https://127.0.0.1/x")
    with pytest.raises(HttpError):
        redirected_request(request, "http://evil.example/plain")


def test_hackernews_keeps_partial_results_when_one_tag_fails() -> None:
    comments = {
        "hits": [
            {
                "objectID": "11",
                "comment_text": "SQLite is excellent for this",
                "author": "grace",
                "created_at_i": 1761782400,
            },
            {"objectID": "not-a-number", "comment_text": "SQLite nope", "author": "bea"},
        ]
    }
    batch = HackerNewsAdapter().collect(_ctx(FakeHttp([(500, None), (200, comments)])))
    assert batch.report.status == "ok"
    assert "HTTP 500" in batch.report.detail
    assert len(batch.mentions) == 1
    assert batch.mentions[0].external_url is None


def test_duckduckgo_keeps_an_unclosed_last_row() -> None:
    html = """
    <a class='result-link' href="https://example.com/last">Last title</a>
    <td class='result-snippet'>SQLite tail
    """
    hits = _parse_ddg(html)
    assert hits[0].url == "https://example.com/last"
    assert "tail" in hits[0].snippet


def test_x_query_is_not_cut_mid_token() -> None:
    base = "S" * 505
    assert _fit_x_query(base, ["alpha"]) == base
    fitted = _fit_x_query("S" * 500, ["ab", "this-handle-is-way-too-long-to-append"])
    assert fitted == ("S" * 500) + " -from:ab"
    with pytest.raises(HttpError):
        _fit_x_query("Q" * 513, [])


def test_empty_review_sites_are_skipped() -> None:
    config = Config(
        name="t",
        targets=[Target(id="sqlite", role="brand", names=["SQLite"])],
        review_sites=[],
    )
    ctx = SearchContext(
        config=config,
        since_epoch=0,
        client=FakeHttp([]),  # type: ignore[arg-type]
        phrases=[Phrase("SQLite", "sqlite", ())],
    )
    batch = ReviewsAdapter(provider=DuckDuckGoProvider()).collect(ctx)
    assert batch.report.status == "skipped"
    assert FakeHttp([]).gets == []


def test_dotenv_strips_export_and_comment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
    path = tmp_path / ".env"
    path.write_text("export X_BEARER_TOKEN=abc # not part of the token\n", encoding="utf-8")
    _load_dotenv(path)
    assert os.environ["X_BEARER_TOKEN"] == "abc"


def test_csv_prefixes_formula_cells(tmp_path: Path) -> None:
    mention = Mention(
        source="web",
        url="https://example.com/a",
        text="=cmd|'/c calc'!A0",
        target_ids=["sqlite"],
    )
    config = Config(name="t", targets=[Target(id="sqlite", role="brand", names=["SQLite"])])
    result = RunResult(
        name="t",
        mode="run",
        generated_at=NOW,
        window_days=7,
        signal="THIN",
        verdict_7d="quiet",
        mentions_7d=0,
        mentions=[mention],
        reports=[],
        patterns=[],
        cleared_bar=False,
        quiet=False,
        llm_used=False,
        limitations=[],
    )
    write_run(result, config, tmp_path, marketing=False)
    text = (tmp_path / "mentions.csv").read_text(encoding="utf-8-sig")
    assert "'=cmd|'/c calc'!A0" in text


def test_social_adapters_do_not_post() -> None:
    adapters = ROOT / "src" / "social_listen" / "adapters"
    for path in adapters.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if path.name == "web.py":
            assert text.count('method="POST"') == 1
            continue
        assert 'method="POST"' not in text
        assert "method='POST'" not in text


def _all_off() -> SourceToggles:
    return SourceToggles(
        hackernews=False,
        reddit=False,
        x=False,
        youtube=False,
        news=False,
        web=False,
        reviews=False,
    )


def _batch(mentions: list[Mention], source: str = "hackernews") -> AdapterBatch:
    return AdapterBatch(mentions, SourceReport(source, "ok", "fake", len(mentions), 0))
