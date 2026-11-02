"""Lexicon sentiment and themes. These are hints, not a measurement of opinion."""

from __future__ import annotations

from social_listen.models import Mention, Sentiment
from social_listen.textmatch import contains_term

_POSITIVE = (
    "love",
    "great",
    "excellent",
    "amazing",
    "helpful",
    "recommend",
    "solid",
    "best",
    "عالی",
    "ممنون",
    "پیشنهاد",
    "باکیفیت",
)
_NEGATIVE = (
    "hate",
    "terrible",
    "awful",
    "bug",
    "broken",
    "scam",
    "worst",
    "disappointed",
    "crash",
    "بد",
    "افتضاح",
    "کلاهبرداری",
    "خراب",
    "ناراضی",
)
_THEMES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("pricing", ("price", "pricing", "expensive", "cheap", "گران", "قیمت")),
    ("support", ("support", "customer service", "پشتیبانی")),
    ("reliability", ("bug", "crash", "broken", "باگ", "خراب", "قطعی")),
    ("praise", ("love", "great", "excellent", "عالی", "ممنون")),
    ("comparison", ("versus", "alternative", "compared", "رقیب", "در مقایسه")),
    ("launch", ("launch", "released", "announcing", "رونمایی", "منتشر")),
)


def _sentiment(text: str) -> Sentiment:
    positive = any(contains_term(text, word) for word in _POSITIVE)
    negative = any(contains_term(text, word) for word in _NEGATIVE)
    if positive and negative:
        return "mixed"
    if positive:
        return "positive"
    if negative:
        return "negative"
    return "unknown"


def _themes(text: str) -> list[str]:
    found = [
        label
        for label, words in _THEMES
        if any(contains_term(text, word) for word in words)
    ]
    return found or ["other"]


def tag_mentions(mentions: list[Mention]) -> list[Mention]:
    for mention in mentions:
        blob = f"{mention.title or ''}\n{mention.text}"
        mention.sentiment = _sentiment(blob)
        mention.themes = _themes(blob)
    return mentions
