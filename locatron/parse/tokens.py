"""Tokenisation, with positions kept so later stages can say what was consumed.

Splitting is delegated to `locatron.normalize`: this module calls `normalize()`
and `tokens()` and adds nothing of its own to either. Reimplementing any part of
normalisation here would be the drift failure CLAUDE.md is built to avoid, so
the split rule lives in exactly one place and this module only records where the
results landed.

Positions are offsets into the **normalised** string, not the raw input. Raw
offsets are not recoverable without instrumenting `normalize()`: it
transliterates (anyascii can change a character's width), expands `&` to ` AND `,
turns punctuation into spaces and collapses runs of whitespace. Reversing that
mapping would mean reimplementing it. Normalised offsets are exact, and they are
what the gazetteer stages compare against anyway, since every `norm_key` in the
database is in the same space.

The parser is not positional — it generates hypotheses and validates them
against the gazetteer — so what it needs from this module is the ability to ask
"which tokens has some extractor already claimed, and what is left over for the
street?". That is `Span` and `remaining()`.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from locatron.normalize import normalize
from locatron.normalize import tokens as split_tokens


@dataclass(frozen=True, slots=True, order=True)
class Span:
    """A half-open range of token indices, `[start, end)`.

    Half-open so that `len()` is `end - start` and an empty span is
    representable without a special case.
    """

    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start < 0 or self.end < self.start:
            raise ValueError(f"invalid span: [{self.start}, {self.end})")

    def __len__(self) -> int:
        return self.end - self.start

    def __iter__(self) -> Iterator[int]:
        return iter(range(self.start, self.end))

    @property
    def indices(self) -> tuple[int, ...]:
        return tuple(range(self.start, self.end))

    def overlaps(self, other: Span) -> bool:
        """Whether two spans claim any token in common.

        Extractors are deliberately allowed to overlap — '5/12' is both a unit
        and a street number — so the stage that picks a hypothesis needs to be
        able to detect it rather than be protected from it.
        """
        return self.start < other.end and other.start < self.end


@dataclass(frozen=True, slots=True)
class Token:
    """One normalised token and where it sits."""

    index: int
    """Position in the token list. This is what spans refer to."""
    text: str
    """The normalised token. Never empty."""
    start: int
    """Character offset into the normalised string."""
    end: int

    @property
    def span(self) -> Span:
        """The one-token span covering just this token."""
        return Span(self.index, self.index + 1)

    @property
    def is_digits(self) -> bool:
        return self.text.isdigit()


@dataclass(frozen=True, slots=True)
class TokenStream:
    """The tokenised form of one input, plus the strings it came from."""

    raw: str
    norm: str
    items: tuple[Token, ...]

    def __len__(self) -> int:
        return len(self.items)

    def __iter__(self) -> Iterator[Token]:
        return iter(self.items)

    def __getitem__(self, i: int) -> Token:
        return self.items[i]

    @property
    def texts(self) -> tuple[str, ...]:
        return tuple(t.text for t in self.items)

    def at(self, index: int) -> Token | None:
        """The token at `index`, or None when out of range.

        Extractors look ahead constantly ('UNIT' then a number), and a None is
        easier to get right at every call site than a length check.
        """
        if 0 <= index < len(self.items):
            return self.items[index]
        return None

    def slice(self, span: Span) -> tuple[Token, ...]:
        return self.items[span.start : span.end]

    def text_of(self, span: Span) -> str:
        """The normalised text of a span, rejoined with single spaces.

        Safe to rejoin this way: `normalize()` guarantees single-space
        separation, so this reproduces the original substring exactly.
        """
        return " ".join(t.text for t in self.slice(span))

    def remaining(self, spans: tuple[Span, ...] | list[Span]) -> tuple[Token, ...]:
        """Tokens not claimed by any of `spans`, in order.

        This is how the street falls out: once number, unit, postcode and
        locality spans are known, what is left is the street name and type.
        """
        claimed = {i for s in spans for i in s.indices}
        return tuple(t for t in self.items if t.index not in claimed)

    def runs(self, spans: tuple[Span, ...] | list[Span]) -> tuple[Span, ...]:
        """`remaining()` grouped into contiguous spans.

        A street name is contiguous, so a caller matching one wants the runs
        rather than a flat token list: '65 SMITH ST 3000' with the number and
        postcode claimed leaves one run, not two loose tokens.
        """
        claimed = {i for s in spans for i in s.indices}
        out: list[Span] = []
        start: int | None = None
        for i in range(len(self.items)):
            if i in claimed:
                if start is not None:
                    out.append(Span(start, i))
                    start = None
            elif start is None:
                start = i
        if start is not None:
            out.append(Span(start, len(self.items)))
        return tuple(out)


def tokenize(raw: str | None) -> TokenStream:
    """Normalise and tokenise, recording each token's offset.

    >>> tokenize("65 Clifton Park Dr").texts
    ('65', 'CLIFTON', 'PARK', 'DR')
    >>> tokenize("  ").items
    ()
    """
    norm = normalize(raw)
    parts = split_tokens(norm)

    items: list[Token] = []
    offset = 0
    for index, text in enumerate(parts):
        # normalize() collapses whitespace to single spaces, so each token
        # follows the previous one across exactly one separator.
        items.append(Token(index=index, text=text, start=offset, end=offset + len(text)))
        offset += len(text) + 1

    return TokenStream(raw=raw or "", norm=norm, items=tuple(items))
