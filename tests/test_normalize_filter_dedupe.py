from datetime import datetime, timedelta, timezone

from social_listen.config import Config, Target
from social_listen.dedupe import dedupe
from social_listen.filters import assign_targets, dated_in_last
from social_listen.models import Mention
from social_listen.normalize import (
    canonical_url,
    guess_language,
    html_to_text,
    is_public_http_url,
    parse_time,
)
from social_listen.signal import per_day_counts, signal_label, verdict_7d
from social_listen.tagging import tag_mentions
from social_listen.textmatch import contains_term

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)


def _config() -> Config:
    return Config(
        name="t",
        languages=["en", "fa"],
        targets=[
            Target(
                id="northwind",
                role="brand",
                names=["Northwind Labs", "نورث‌ویند"],
                aliases=["Northwind"],
                domains=["northwind.example"],
                handles=["northwindlabs"],
                negative_keywords=["north wind", "باد شمال"],
            ),
            Target(
                id="contoso",
                role="competitor",
                names=["Contoso"],
                domains=["contoso.example"],
                handles=["contoso"],
            ),
        ],
    )


def _mention(**kwargs: object) -> Mention:
    payload = {
        "source": "hackernews",
        "url": "https://news.ycombinator.com/item?id=1",
        "text": "Northwind Labs shipped a solid release",
        "published_at": NOW - timedelta(days=1),
    }
    payload.update(kwargs)
    return Mention(**payload)  # type: ignore[arg-type]


def test_html_comment_becomes_plain_text() -> None:
    assert html_to_text("I&#x27;m here<p>next") == "I'm here next"
    assert html_to_text("if a &lt; b &gt; c then SQLite") == "if a < b > c then SQLite"
    assert "github.com/acme" in html_to_text(
        '<a href="https://github.com/acme">issue</a>'
    )


def test_canonical_url_strips_tracking_and_maps_twitter() -> None:
    url = canonical_url(
        "https://www.twitter.com/Acme/status/9/?utm_source=tw&fbclid=abc"
    )
    assert url == "https://x.com/Acme/status/9"
    assert canonical_url("https://m.reddit.com/r/db/comments/abc/post/") == canonical_url(
        "https://old.reddit.com/r/db/comments/abc/post"
    )
    assert canonical_url("https://youtu.be/abc") == "https://youtube.com/watch?v=abc"
    assert canonical_url("https://m.youtube.com/watch?v=abc&t=12") == "https://youtube.com/watch?v=abc"


def test_rejects_private_urls() -> None:
    assert is_public_http_url("https://example.com/a")
    assert is_public_http_url("https://1.1.1.1/")
    assert not is_public_http_url("http://127.0.0.1/a")
    assert not is_public_http_url("http://127.1/")
    assert not is_public_http_url("http://0x7f000001/")
    assert not is_public_http_url("http://2130706433/")
    assert not is_public_http_url("http://0177.0.0.1/")
    assert not is_public_http_url("http://10.1.2.3/")
    assert not is_public_http_url("http://localhost/a")
    assert not is_public_http_url("http://[::1]/a")
    assert not is_public_http_url("javascript:alert(1)")


def test_persian_spelling_variants_match() -> None:
    assert contains_term("محصول نورث‌ویند خوب است", "نورثویند")
    assert contains_term("محصول نورثویند خوب است", "نورث‌ویند")
    assert not contains_term("the north wind was cold", "Northwind")


def test_short_latin_token_is_not_a_substring() -> None:
    assert not contains_term("classification of snails", "ai")
    assert contains_term("the AI lab opened", "AI")


def test_collision_own_account_and_spam_filters() -> None:
    config = _config()
    kept = assign_targets(_mention(), config)
    assert kept is not None
    assert kept.target_ids == ["northwind"]

    collision = assign_targets(
        _mention(text="The north wind delayed the Northwind Labs truck"),
        config,
    )
    assert collision is None

    own_author = assign_targets(
        _mention(text="Northwind Labs update", handle="northwindlabs"),
        config,
    )
    assert own_author is None
    own_name = assign_targets(
        _mention(text="Northwind Labs update", author="Northwind Labs", handle=None),
        config,
    )
    assert own_name is None
    fans = assign_targets(
        _mention(text="Northwind Labs update", author="Northwind Labs Fans", handle=None),
        config,
    )
    assert fans is not None

    own_site = assign_targets(
        _mention(
            url="https://blog.northwind.example/hello",
            text="Northwind Labs update",
        ),
        config,
    )
    assert own_site is None

    spam = assign_targets(
        _mention(text="Northwind Labs crypto airdrop, dm for promo"),
        config,
    )
    assert spam is None

    both = assign_targets(
        _mention(text="Northwind Labs versus Contoso on price"),
        config,
    )
    assert both is not None
    assert both.target_ids == ["northwind", "contoso"]


def test_persian_negative_keyword_drops_idiom() -> None:
    config = _config()
    idiom = assign_targets(
        _mention(text="باد شمال و نورث‌ویند در یک جمله آمده", language="fa"),
        config,
    )
    assert idiom is None
    real = assign_targets(
        _mention(text="بازار نورث‌ویند خدمات خوبی دارد", language="fa"),
        config,
    )
    assert real is not None
    assert real.target_ids == ["northwind"]


def test_language_filter_drops_detected_mismatch() -> None:
    config = _config()
    config.languages = ["fa"]
    english = assign_targets(_mention(text="Northwind Labs is fast"), config)
    assert english is None
    persian = assign_targets(
        _mention(text="نورث‌ویند پیشنهاد خوبی بود", language=None),
        config,
    )
    assert persian is not None
    assert guess_language(persian.text, None) == "fa"


def test_dedupe_merges_urls_and_keeps_richer_text() -> None:
    short = _mention(
            url="https://www.news.ycombinator.com/item?id=1&utm_source=x",
        text="Northwind Labs",
        target_ids=["northwind"],
    )
    long = _mention(
        source="reddit",
        url="https://news.ycombinator.com/item?id=1",
        text="Northwind Labs shipped a solid release with notes",
        target_ids=["contoso"],
        engagement={"upvotes": 3},
    )
    merged = dedupe([short, long])
    assert len(merged) == 1
    assert "notes" in merged[0].text
    assert merged[0].target_ids == ["contoso", "northwind"] or set(merged[0].target_ids) == {
        "contoso",
        "northwind",
    }
    assert merged[0].engagement["upvotes"] == 3
    assert merged[0].id
    other = _mention(url="https://news.ycombinator.com/item?id=2", text=long.text)
    assert len(dedupe([long, other])) == 2


def test_signal_and_verdict_ignore_undated_web() -> None:
    dated = [
        _mention(source="hackernews", url=f"https://news.ycombinator.com/item?id={i}")
        for i in range(2)
    ]
    dated.append(
        _mention(
            source="reddit",
            url="https://www.reddit.com/r/test/comments/abc/post",
            published_at=NOW - timedelta(days=2),
        )
    )
    assert signal_label(dated, NOW, 7) == "STRONG"
    verdict, count = verdict_7d(dated, NOW)
    assert verdict == "active"
    assert count == 3

    undated = [
        Mention(source="web", url="https://example.com/a", text="Northwind Labs"),
        Mention(source="news", url="https://example.com/b", text="Northwind Labs"),
    ]
    assert signal_label(undated, NOW, 7) == "THIN"
    assert verdict_7d(undated, NOW) == ("quiet", 0)
    assert signal_label([], NOW, 7) == "NONE"
    assert not dated_in_last(undated[0], NOW, 7)

    old = _mention(published_at=NOW - timedelta(days=10))
    assert verdict_7d([old], NOW) == ("quiet", 0)
    assert signal_label([old], NOW, 14) == "THIN"
    counts = per_day_counts([dated[0]], NOW, 7)
    assert sum(count for _, count in counts) == 1
    edge_now = datetime(2026, 10, 31, 18, 0, tzinfo=timezone.utc)
    edge_hit = _mention(published_at=datetime(2026, 10, 24, 19, 0, tzinfo=timezone.utc))
    assert verdict_7d([edge_hit], edge_now) == ("active", 1)
    edge_counts = per_day_counts([edge_hit], edge_now, 7)
    assert sum(count for _, count in edge_counts) == 1
    assert edge_counts[0][0] == "2026-10-24"
    web_only = [
        _mention(source="agent_reach:web", url=f"https://example.com/{index}", published_at=NOW)
        for index in range(3)
    ]
    web_only.append(_mention(source="news", url="https://example.com/n", published_at=NOW))
    assert signal_label(web_only, NOW, 7) == "THIN"


def test_undetermined_language_and_millisecond_time() -> None:
    assert guess_language("SQLite is solid", "und") == "en"
    assert guess_language("SQLite is solid", "qme") == "en"
    parsed = parse_time(1761782400000)
    assert parsed == datetime.fromtimestamp(1761782400, tz=timezone.utc)
    assert parse_time("1761782400000") == parsed


def test_tagging_does_not_invent_sentiment() -> None:
    mention = tag_mentions([_mention(text="Northwind Labs released a build")])[0]
    assert mention.sentiment == "unknown"
    praised = tag_mentions([_mention(text="Northwind Labs is excellent")])[0]
    assert praised.sentiment == "positive"
    assert "praise" in praised.themes
