"""Address component extractors. Pure functions over a `TokenStream`.

No database, no gazetteer, no I/O. Every function here proposes candidates and
validates nothing, because validation needs the gazetteer and that belongs to a
later stage. The parser generates hypotheses and scores them against real data;
this module's job is to generate them generously and say exactly which tokens
each one consumed.

Two consequences of that split are deliberate and worth stating up front:

Candidates overlap. '5/12' is a unit *and* a street number, and '3201' is a
postcode *and* a plausible street number. Both readings are returned and the
scoring stage picks. Suppressing one here would silently decide the very thing
the hypothesis scorer exists to decide.

Nothing is rejected for being implausible. A four-digit token is a postcode
candidate whether or not 9999 is a real postcode, because the check against
`AustralianPostcodes` happens where that table is in scope.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from locatron.parse.tokens import Span, TokenStream

# A street-number-shaped atom: digits with an optional single alpha suffix.
# G-NAF keeps the suffix inside NUMBER_FIRST ('59B', with NUMBER_LAST blank),
# so it is one atom here too rather than a number plus a separate suffix field.
_NUMBER = r"[0-9]{1,6}[A-Z]?"

_POSTCODE_RE = re.compile(r"^[0-9]{4}$")
#: Three digits, which is what a postcode looks like after a careless load has
#: dropped its leading zero. NT is 0800-0899, so the whole territory is exposed
#: to it. See `PostcodeCandidate.padded`.
_SHORT_POSTCODE_RE = re.compile(r"^[0-9]{3}$")
_BOX_NUMBER_RE = re.compile(rf"^{_NUMBER}$")


@dataclass(frozen=True, slots=True)
class PostcodeCandidate:
    """A token that could be a postcode. Unvalidated."""

    postcode: str
    """Always four characters. Never an int: 0800 must not become 800."""
    span: Span
    padded: bool = False
    """True when recovered from a three-digit token by left-padding a zero.

    Separate from the rest so a caller can ignore these entirely. They are
    genuinely ambiguous -- the 800 in 'Level 3 800 Bourke St' pads to 0800,
    which is a real Darwin postcode -- so a scorer should weight them well below
    a four-digit hit rather than treat them alike.
    """


@dataclass(frozen=True, slots=True)
class PoBox:
    """A postal box. Never matches G-NAF; resolves via is_postal_only = 1."""

    kind: str
    """'PO' or 'GPO'."""
    number: str
    span: Span


def find_postcodes(ts: TokenStream) -> tuple[PostcodeCandidate, ...]:
    """Every token that could be a postcode, in token order.

    Returns all of them. '65 CLIFTON PARK DRIVE 3201 CARRUM DOWNS' and
    '65 CLIFTON PARK DR CARRUM DOWNS VIC 3201' both yield 3201 from different
    positions, which is the point: the parser is not positional.

    >>> from locatron.parse.tokens import tokenize
    >>> [c.postcode for c in find_postcodes(tokenize("Darwin NT 0800"))]
    ['0800']
    """
    out: list[PostcodeCandidate] = []
    for token in ts:
        if _POSTCODE_RE.match(token.text):
            out.append(PostcodeCandidate(postcode=token.text, span=token.span))
        elif _SHORT_POSTCODE_RE.match(token.text):
            out.append(
                PostcodeCandidate(
                    postcode=token.text.rjust(4, "0"), span=token.span, padded=True
                )
            )
    return tuple(out)


#: Token sequences that introduce a postal box, longest first so 'G P O BOX'
#: is not consumed as a bare 'O BOX'. The split forms are not alternatives to
#: the joined ones, they are what normalize() produces: 'P.O. BOX' has its
#: periods turned into separators and arrives as ['P', 'O', 'BOX'].
_BOX_PREFIXES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("G", "P", "O", "BOX"), "GPO"),
    (("P", "O", "BOX"), "PO"),
    (("GPO", "BOX"), "GPO"),
    (("PO", "BOX"), "PO"),
    (("GPOBOX",), "GPO"),
    (("POBOX",), "PO"),
)


def find_po_boxes(ts: TokenStream) -> tuple[PoBox, ...]:
    """Every postal box in the stream, in token order.

    >>> from locatron.parse.tokens import tokenize
    >>> b = find_po_boxes(tokenize("P.O. Box 45 World Square NSW 2002"))[0]
    >>> (b.kind, b.number, b.span.start, b.span.end)
    ('PO', '45', 0, 4)
    """
    out: list[PoBox] = []
    i = 0
    while i < len(ts):
        for prefix, kind in _BOX_PREFIXES:
            end = i + len(prefix)
            if ts.texts[i:end] != prefix:
                continue
            number = ts.at(end)
            if number is None or not _BOX_NUMBER_RE.match(number.text):
                continue
            out.append(PoBox(kind=kind, number=number.text, span=Span(i, end + 1)))
            i = end  # the loop's own increment steps past the number
            break
        i += 1
    return tuple(out)
