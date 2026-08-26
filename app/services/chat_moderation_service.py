from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class ChatModerationResult:
    allowed: bool
    reason: str | None = None


_ARABIC_DIACRITICS_RE = re.compile(r"[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed]")
_FORMAT_MARKS_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069]")
_SEPARATORS_RE = re.compile(r"[\s\-_.,،;:!؟?*~`'\"()\[\]{}<>/\\|+=]+")
_REPEATED_CHAR_RE = re.compile(r"(.)\1{2,}")
_ARABIC_OR_LATIN = r"0-9a-z\u0600-\u06ff"

_ARABIC_TRANSLATION = str.maketrans(
    {
        "أ": "ا",
        "إ": "ا",
        "آ": "ا",
        "ٱ": "ا",
        "ى": "ي",
        "ئ": "ي",
        "ؤ": "و",
        "ة": "ه",
        "ـ": "",
    }
)

_ARABIC_BLOCKED_COMPACT_PATTERNS = [
    re.compile(pattern)
    for pattern in (
        r"ك+س+م+",
        r"م+ت+ن+ا+ك+",
        r"ش+ر+م+و+ط+",
        r"ق+ح+ب+",
        r"ه?ق+ت+ل+ك+",
        r"ا+ق+ت+ل+ك+",
        r"ه?م+و+ت+ك+",
    )
]

_ARABIC_BLOCKED_WORDS = (
    "احا",
    "خول",
    "عرص",
    "زبر",
    "طيز",
    "نيك",
    "منيك",
    "لبوه",
)

_ARABIC_BLOCKED_WORD_RE = re.compile(
    rf"(?<![{_ARABIC_OR_LATIN}])({'|'.join(_ARABIC_BLOCKED_WORDS)})(?![{_ARABIC_OR_LATIN}])"
)

_ENGLISH_BLOCKED_WORD_RE = re.compile(
    r"\b(fuck|fucking|shit|bitch|cunt|dick|pussy|porn)\b"
)
_ENGLISH_THREAT_RE = re.compile(r"\b(kill|murder)\s+(you|u)\b")


def moderate_chat_text(text: str) -> ChatModerationResult:
    normalized_spaced, normalized_compact = _normalize(text)
    if not normalized_spaced.strip():
        return ChatModerationResult(allowed=True)

    for pattern in _ARABIC_BLOCKED_COMPACT_PATTERNS:
        if pattern.search(normalized_compact):
            return ChatModerationResult(allowed=False, reason="blocked_terms")

    if _ARABIC_BLOCKED_WORD_RE.search(normalized_spaced):
        return ChatModerationResult(allowed=False, reason="blocked_terms")

    if _ENGLISH_BLOCKED_WORD_RE.search(normalized_spaced):
        return ChatModerationResult(allowed=False, reason="blocked_terms")

    if _ENGLISH_THREAT_RE.search(normalized_spaced):
        return ChatModerationResult(allowed=False, reason="threat")

    return ChatModerationResult(allowed=True)


def _normalize(text: str) -> tuple[str, str]:
    value = unicodedata.normalize("NFKC", text).casefold()
    value = _FORMAT_MARKS_RE.sub("", value)
    value = _ARABIC_DIACRITICS_RE.sub("", value)
    value = value.translate(_ARABIC_TRANSLATION)
    value = _REPEATED_CHAR_RE.sub(r"\1\1", value)
    spaced = _SEPARATORS_RE.sub(" ", value)
    compact = _SEPARATORS_RE.sub("", value)
    return f" {spaced.strip()} ", compact
