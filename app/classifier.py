"""Sub/dub/language classification for anime release titles.

Pure functions, zero I/O — everything here is unit-testable against fixture
titles without touching a network. The patterns below are drawn directly
from real Sonarr release-search results collected during manual Bleach
curation (see the project plan for the full session writeup): "Multi-Subs",
"Dual-Audio", "English-Sub", "(Japanese, English Dubs)", "ENG DUB", and
plain untagged streaming-service rips (DSNP/HULU/AMZN with no audio marker
at all) that are genuinely ambiguous from title text alone.

This module is honest about that ambiguity on purpose: UNKNOWN is a real,
first-class outcome, not something the heuristics try to eliminate by
guessing harder.
"""

import re
from dataclasses import dataclass
from typing import Literal

Kind = Literal["sub", "dub", "dual", "unknown"]
Confidence = Literal["high", "medium", "unknown"]
Preference = Literal["prefer_sub", "prefer_dub", "no_preference"]

_DUAL_PATTERNS = [
    re.compile(r"dual[\s._-]*audio", re.IGNORECASE),
    re.compile(r"\bdual\b", re.IGNORECASE),
    re.compile(r"multi[\s._-]*(sub|dub)", re.IGNORECASE),
]

_ENGLISH_SUB_PATTERNS = [
    re.compile(r"english[\s._-]*sub", re.IGNORECASE),
    re.compile(r"\bsub(s|bed)?\b", re.IGNORECASE),
]

_DUB_ONLY_PATTERNS = [
    re.compile(r"eng(?:lish)?[\s._-]*dub", re.IGNORECASE),
    re.compile(r"\(dub\)", re.IGNORECASE),
    re.compile(r"\bdubbed\b", re.IGNORECASE),
]

# Streaming-service tags that, alone with no other marker, are genuinely
# ambiguous — confirmed live: plain "S17E34 ... DSNP WEB-DL ... -VARYG"
# releases carry no audio-track info in the title at all. Listed here only
# so the classifier can note *why* it landed on unknown, not to guess.
_AMBIGUOUS_SOURCE_TAGS = ("dsnp", "hulu", "amzn", "nf ", "netflix")


@dataclass
class Classification:
    kind: Kind
    confidence: Confidence
    matched_terms: list[str]
    reason: str


def classify_release(title: str, known_fansub_groups: list[str] | None = None) -> Classification:
    matched: list[str] = []

    dub_only_hits = [p.pattern for p in _DUB_ONLY_PATTERNS if p.search(title)]
    dual_hits = [p.pattern for p in _DUAL_PATTERNS if p.search(title)]
    sub_hits = [p.pattern for p in _ENGLISH_SUB_PATTERNS if p.search(title)]

    # Dual/multi wins over a bare "dub" mention — a "Dual-Audio ENG DUB"
    # release still has a sub-compatible Japanese track, it's just also
    # advertising the dub it includes.
    if dual_hits:
        matched.extend(dual_hits)
        return Classification("dual", "high", matched, "explicit dual-audio/multi-sub marker")

    if dub_only_hits and not sub_hits:
        matched.extend(dub_only_hits)
        return Classification("dub", "high", matched, "explicit English-dub marker, no sub marker present")

    if sub_hits:
        matched.extend(sub_hits)
        return Classification("sub", "high", matched, "explicit English-sub marker")

    if known_fansub_groups:
        for group in known_fansub_groups:
            if re.search(re.escape(group), title, re.IGNORECASE):
                return Classification(
                    "sub", "high", [group],
                    f"from known fansub group '{group}' (consistently soft-sub + Japanese audio by community convention)",
                )

    lowered = title.lower()
    ambiguous_tag = next((tag for tag in _AMBIGUOUS_SOURCE_TAGS if tag in lowered), None)
    if ambiguous_tag:
        return Classification(
            "unknown", "unknown", [ambiguous_tag],
            f"plain '{ambiguous_tag.strip().upper()}' streaming rip with no audio/sub marker — genuinely unknowable from the title alone",
        )

    return Classification("unknown", "unknown", [], "no sub/dub/dual marker found in title")


def matches_preference(classification: Classification, preference: Preference) -> bool:
    """UNKNOWN never matches any preference except no_preference — this is
    the enforcement point for "always route unknowns to manual review",
    regardless of how eager the auto-grab setting is.
    """
    if preference == "no_preference":
        return classification.confidence != "unknown"
    if classification.confidence == "unknown":
        return False
    if preference == "prefer_sub":
        return classification.kind in ("sub", "dual")
    if preference == "prefer_dub":
        return classification.kind in ("dub", "dual")
    return False
