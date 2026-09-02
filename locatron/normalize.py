"""The normalisation contract.

This module is the ONLY place normalisation happens. Build scripts, the
resolver, and the cache key builder all call `normalize()` from here.

Do not reimplement any of this in SQL. SQL build scripts leave `norm_key`
columns NULL and `scripts/normalize_pass.py` fills them by importing this
function. Build-time and query-time normalisation drifting apart produces
silent misses that look like bad data rather than a bug.

Changing `normalize()` requires all three of:
    1. bump NORM_VERSION
    2. rerun scripts/normalize_pass.py --all
    3. flush the Redis cache

`normalize()` is byte-compatible with the version used to populate the
existing locatron_* norm_key columns. The helpers below it are additive and
do not affect its output.
"""

from __future__ import annotations

import re

from anyascii import anyascii

NORM_VERSION = "1"

_PUNCT = re.compile(r"[^A-Z0-9/\- ]+")
_SPACE = re.compile(r"\s+")
_DASH = re.compile(r"\s*-\s*")


def normalize(s: str | None) -> str:
    """Fold an arbitrary location string to a stable comparison key.

    Uppercases, transliterates to ASCII, expands ampersands, strips
    punctuation other than slash and hyphen, and collapses whitespace.

    >>> normalize("  St Kilda   East ")
    'ST KILDA EAST'
    >>> normalize("Kur-ring-gai")
    'KUR-RING-GAI'
    >>> normalize("Ryde & Eastwood")
    'RYDE AND EASTWOOD'
    >>> normalize(None)
    ''
    """
    if not s:
        return ""
    s = anyascii(s).upper()
    s = s.replace("&", " AND ")
    s = _PUNCT.sub(" ", s)
    s = _DASH.sub("-", s)
    s = _SPACE.sub(" ", s).strip()
    return s


# ---------------------------------------------------------------------------
# Helpers. Additive only. These must never change what normalize() returns.
# ---------------------------------------------------------------------------

# Words that qualify a place without changing which place it is.
# "Greater Melbourne" and "Melbourne Metropolitan Area" are both Melbourne.
_QUALIFIER_PREFIXES = (
    "GREATER",
    "METROPOLITAN",
    "METRO",
    "INNER",
    "OUTER",
    "CENTRAL",
    "CITY OF",
    "SHIRE OF",
    "MUNICIPALITY OF",
    "TOWN OF",
    "BOROUGH OF",
)

_QUALIFIER_SUFFIXES = (
    "METROPOLITAN AREA",
    "METRO AREA",
    "AND SURROUNDS",
    "METROPOLITAN",
    "SURROUNDS",
    "DISTRICT",
    "REGION",
    "COUNTY",
    "METRO",
    "AREA",
    "CBD",
)


def strip_qualifiers(norm: str) -> str:
    """Remove framing words from an already-normalised string.

    Applied as a fallback when the raw normalised form does not match a
    gazetteer entry. Always try the unstripped form first: some real place
    names legitimately contain these words.

    >>> strip_qualifiers("GREATER MELBOURNE")
    'MELBOURNE'
    >>> strip_qualifiers("SYDNEY METROPOLITAN AREA")
    'SYDNEY'
    >>> strip_qualifiers("CITY OF PORT PHILLIP")
    'PORT PHILLIP'
    """
    out = norm
    changed = True
    while changed:
        changed = False
        for p in _QUALIFIER_PREFIXES:
            if out.startswith(p + " ") and len(out) > len(p) + 1:
                out = out[len(p) + 1 :]
                changed = True
        for s in _QUALIFIER_SUFFIXES:
            if out.endswith(" " + s) and len(out) > len(s) + 1:
                out = out[: -(len(s) + 1)]
                changed = True
    return out.strip()


def tokens(norm: str) -> list[str]:
    """Split a normalised string into tokens.

    >>> tokens("65 CLIFTON PARK DRIVE 3201 CARRUM DOWNS")
    ['65', 'CLIFTON', 'PARK', 'DRIVE', '3201', 'CARRUM', 'DOWNS']
    """
    return norm.split(" ") if norm else []


def ngrams(toks: list[str], max_len: int = 4) -> list[tuple[int, int, str]]:
    """Every contiguous token span up to max_len, longest first.

    Returns (start, end, joined) tuples. Used to find the longest span of a
    string that matches a gazetteer entry, which is how locality extraction
    works when the input has no reliable delimiters.
    """
    out: list[tuple[int, int, str]] = []
    n = len(toks)
    for length in range(min(max_len, n), 0, -1):
        for start in range(n - length + 1):
            end = start + length
            out.append((start, end, " ".join(toks[start:end])))
    return out


def cache_key(prefix: str, raw: str, *parts: str) -> str:
    """Build a Redis key that is invalidated by a NORM_VERSION bump."""
    import hashlib

    payload = "\x1f".join([normalize(raw), *parts])
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()
    return f"{prefix}:{NORM_VERSION}:{digest}"
