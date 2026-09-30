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

Kind = Literal["sub", "dub", "dual", "foreign_sub", "unknown"]
Confidence = Literal["high", "medium", "unknown"]
Preference = Literal["prefer_sub", "prefer_dub", "no_preference"]

_DUAL_PATTERNS = [
    re.compile(r"dual[\s._-]*audio", re.IGNORECASE),
    re.compile(r"\bdual\b", re.IGNORECASE),
    re.compile(r"multi[\s._-]*(sub|dub)", re.IGNORECASE),
]

# Only patterns that explicitly say the subs are ENGLISH count as a
# confirmed English-sub match. A bare "sub"/"subs"/"subbed" mention with no
# language qualifier does NOT belong here - it doesn't tell you which
# language the subtitles are actually in, and treating it as English by
# default was the actual bug (a French- or German-subbed release that just
# says "Subbed" in the title, or a "Multi-Subs" release where English isn't
# actually one of the included languages, would silently pass as a
# confirmed English match). See _AMBIGUOUS_SUB_PATTERNS below for that case.
_ENGLISH_SUB_PATTERNS = [
    re.compile(r"english[\s._-]*sub", re.IGNORECASE),
    re.compile(r"\beng[\s._-]*sub", re.IGNORECASE),
]

# A bare sub/subbed mention with no language marker either way - genuinely
# unknowable from the title alone unless it's from a known fansub group
# (checked separately) or backed by an explicit language tag.
_AMBIGUOUS_SUB_PATTERNS = [
    re.compile(r"\bsub(s|bed)?\b", re.IGNORECASE),
]

# Explicit non-English subtitle markers. VOSTFR is the common French-fansub
# convention ("Version Originale Sous-Titrée FRançais"); the rest are the
# straightforward "<language> sub(bed)" forms. Matching one of these means
# the release is confirmed to carry non-English subs (possibly *only*
# non-English subs) - it must never be treated as satisfying an English-sub
# preference, confirmed or ambiguous.
_FOREIGN_SUB_PATTERNS = [
    re.compile(r"\bvostfr\b", re.IGNORECASE),
    re.compile(r"\bvosta\b", re.IGNORECASE),  # French sub of the English dub
    re.compile(r"french[\s._-]*sub", re.IGNORECASE),
    re.compile(r"german[\s._-]*sub", re.IGNORECASE),
    re.compile(r"spanish[\s._-]*sub", re.IGNORECASE),
    re.compile(r"italian[\s._-]*sub", re.IGNORECASE),
    re.compile(r"portuguese[\s._-]*sub", re.IGNORECASE),
    re.compile(r"\besp[\s._-]*sub", re.IGNORECASE),
    re.compile(r"\bger[\s._-]*sub", re.IGNORECASE),
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
    english_sub_hits = [p.pattern for p in _ENGLISH_SUB_PATTERNS if p.search(title)]
    foreign_sub_hits = [p.pattern for p in _FOREIGN_SUB_PATTERNS if p.search(title)]

    # Dual/multi wins over a bare "dub" mention — a "Dual-Audio ENG DUB"
    # release still has a sub-compatible Japanese track, it's just also
    # advertising the dub it includes.
    if dual_hits:
        matched.extend(dual_hits)
        return Classification("dual", "high", matched, "explicit dual-audio/multi-sub marker")

    if dub_only_hits and not english_sub_hits:
        matched.extend(dub_only_hits)
        return Classification("dub", "high", matched, "explicit English-dub marker, no sub marker present")

    # Explicit English-sub marker wins outright, even if a foreign-sub tag
    # also appears (e.g. a release listing both "ENG SUB" and "VOSTFR" for
    # separate tracks) - English is confirmed present either way.
    if english_sub_hits:
        matched.extend(english_sub_hits)
        return Classification("sub", "high", matched, "explicit English-sub marker")

    # Foreign-sub marker with no English marker: confirmed non-English subs,
    # must never satisfy a prefer_sub (English) preference.
    if foreign_sub_hits:
        matched.extend(foreign_sub_hits)
        return Classification(
            "foreign_sub", "high", matched,
            "explicit non-English sub marker, no English-sub marker present",
        )

    if known_fansub_groups:
        for group in known_fansub_groups:
            if re.search(re.escape(group), title, re.IGNORECASE):
                return Classification(
                    "sub", "high", [group],
                    f"from known fansub group '{group}' (consistently soft-sub + Japanese audio by community convention)",
                )

    ambiguous_sub_hits = [p.pattern for p in _AMBIGUOUS_SUB_PATTERNS if p.search(title)]
    if ambiguous_sub_hits:
        return Classification(
            "unknown", "unknown", ambiguous_sub_hits,
            "bare 'sub'/'subbed' mention with no language marker - which language isn't knowable from the title alone",
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
    regardless of how eager the auto-grab setting is. foreign_sub never
    matches prefer_sub - it's a confirmed non-English result, not an
    unknown one, so it's rejected outright rather than sent to review.
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
