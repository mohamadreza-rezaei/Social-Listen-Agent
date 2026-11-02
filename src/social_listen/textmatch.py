"""Phrase matching that does not treat a short Latin token as a substring."""

from __future__ import annotations

import re


def fold_for_match(text: str) -> str:
    """Casefold and unify common Persian/Arabic spelling variants.

    Quotes stored on a mention are never passed through this helper.
    """
    folded = text.casefold()
    folded = folded.replace("\u200c", "").replace("\u200d", "")
    folded = folded.replace("\u064a", "\u06cc").replace("\u0649", "\u06cc")
    folded = folded.replace("\u0643", "\u06a9")
    return folded


def contains_term(text: str, term: str) -> bool:
    folded_term = fold_for_match(term.strip())
    if not folded_term:
        return False
    folded_text = fold_for_match(text)
    if " " in folded_term:
        return folded_term in folded_text
    return re.search(rf"(?<!\w){re.escape(folded_term)}(?!\w)", folded_text) is not None
