"""Country lookup: name, ISO codes, and loose string variants.

`Countries` is the ISO register and the source of truth for a country's
identity. Alpha-3 is the internal identifier throughout Locatron, which holds
because all 358 `country_bucket` rows join `Countries`.`alpha-3`.

The two tables disagree on display names — Cities says "United States",
Countries says "United States of America". Countries wins, so one input never
resolves to two spellings of the same country.

## Why country_bucket is split in two

`country_bucket` is not a list of country name variants. It is a map from any
loose location string to a country, which is a different and broader thing:

    'Australia'                      -> AUS      names the country
    'Austtralia'                     -> AUS      names it, misspelled
    'UK'                             -> GBR      names it, abbreviated
    'melbourne'                      -> AUS      does NOT name it
    'Bangkok'                        -> THA      does NOT name it
    'Queensland'                     -> AUS      does NOT name it
    'Greater Melbourne Area'         -> AUS      does NOT name it
    'Maryborough Victoria Australia' -> AUS      does NOT name it

All of those are correct for the table's original purpose. But peeling a
country token out of a longer string needs only the first group. Treating
'melbourne' as a peelable country token consumes the city name and answers
"Melbourne" with a bare country — which is exactly what it did before this
split existed.

`tokens`  may be peeled out of a longer string. A value qualifies if it names
          its own country: an alpha-2/alpha-3 code, a short acronym, the ISO
          name, a leading prefix of the ISO name, or something close enough to
          the ISO name to be a misspelling or demonym. A value that does not
          name its country still qualifies as long as it is not a known
          sub-national place — that keeps 'Czech Republic', 'East Timor', and
          'Scotland' peelable.

`hints`   all 358. Used only as a last resort on a whole string that matched
          nothing else, which is how 'The land down under' still returns AUS.

Australian state tokens are excluded from `tokens` outright. 'Queensland' maps
to AUS here and to QLD in `aus_state_bucket`, and the state reading is strictly
more informative: it implies the country anyway.

## Why bare ISO codes are positional

A bare alpha-2 or alpha-3 code is a word first and a country second:

    'ST'  Sao Tome    but 'ST KILDA EAST' is a Melbourne suburb
    'MT'  Malta       but 'MT ELIZA' is one too
    'AND' Andorra     and normalize() MANUFACTURES this token, because it
                      expands '&' — so 'Ryde & Eastwood' becomes
                      'RYDE AND EASTWOOD'
    'ARE' UAE         'CAN', 'FIN', 'GIN', 'JAM', 'PAN', 'TON', 'VAT' likewise

Only 83 of the 249 alpha-2 codes appear in `country_bucket`, which is the
hand-curated record of how people actually write countries in this data. The
other 166 are peelable purely because ISO lists them, and that is not a good
enough reason.

So a code taken from the ISO table alone lives in `code_tokens` and is peeled
only when it trails the segment — where a country abbreviation actually goes.
Full country names stay peelable anywhere, and `country_bucket` values follow
the token rules above, curation being the thing that earns them the trust.
"""

from __future__ import annotations

from dataclasses import dataclass

from rapidfuzz.distance import JaroWinkler

from locatron.gazetteer.loader import cached_gazetteer, rows
from locatron.normalize import normalize
from locatron.schemas import Country

#: How close a bucket value must be to its own country's ISO name to count as
#: naming it. Tuned against the real table: 'Austtralia' scores 0.980 against
#: 'AUSTRALIA', 'Nederland' 0.898 against 'NETHERLANDS', and 'melbourne' scores
#: far below either.
_NAMES_COUNTRY_SIMILARITY = 0.80

#: Values this short with no space are code-like. Covers 'UK', 'KSA', 'RSA',
#: 'AUST', and the genuine four-letter country names ('Iraq', 'Mali', 'Cuba').
_ACRONYM_MAX_LEN = 4


@dataclass(frozen=True, slots=True)
class CountryRow:
    alpha3: str
    alpha2: str | None
    name: str
    region: str | None = None

    def to_schema(self) -> Country:
        return Country(name=self.name, alpha2=self.alpha2, alpha3=self.alpha3)


@dataclass(frozen=True, slots=True)
class CountryGazetteer:
    by_alpha3: dict[str, CountryRow]
    tokens: dict[str, str]
    """normalize(variant) -> alpha-3, safe to peel out of a longer string."""
    code_tokens: dict[str, str]
    """Bare ISO codes the bucket does not corroborate. Trailing position only."""
    hints: dict[str, str]
    """normalize(any bucket value) -> alpha-3. Whole-string last resort only."""

    def get(self, alpha3: str | None) -> CountryRow | None:
        if not alpha3:
            return None
        return self.by_alpha3.get(alpha3.strip().upper())

    def lookup_token(self, norm: str) -> CountryRow | None:
        """Resolve a string that names a country. Use this when peeling."""
        alpha3 = self.tokens.get(norm)
        return self.by_alpha3.get(alpha3) if alpha3 else None

    def lookup_trailing_code(self, norm: str) -> CountryRow | None:
        """Resolve a bare ISO code. Only valid on the last token of a segment.

        'ST KILDA EAST' must not lose its first token to Sao Tome, so this is
        deliberately not reachable from `lookup_token`.
        """
        alpha3 = self.code_tokens.get(norm) or self.tokens.get(norm)
        return self.by_alpha3.get(alpha3) if alpha3 else None

    def lookup_hint(self, norm: str) -> CountryRow | None:
        """Resolve a string that merely implies a country.

        Never use this on a fragment of a larger string — that is what the
        token/hint split exists to prevent.
        """
        alpha3 = self.hints.get(norm)
        return self.by_alpha3.get(alpha3) if alpha3 else None

    def resolve_bias(self, bias: str | None) -> str | None:
        """Accept a bias as alpha-3, alpha-2, or a name, and return alpha-3.

        Config holds `country_bias` as a free string so it can be set on the box
        without anyone remembering which code system Locatron uses. Hints are
        allowed here because this is operator configuration, not input text.
        """
        if not bias:
            return None
        key = normalize(bias)
        row = (
            self.get(bias)
            or self.lookup_token(key)
            or self.lookup_trailing_code(key)
            or self.lookup_hint(key)
        )
        return row.alpha3 if row else None


def _names_its_country(norm: str, iso_name: str, alpha2: str | None, alpha3: str) -> bool:
    """Whether `norm` is a way of writing this country's own name."""
    if norm in {alpha3, alpha2}:
        return True
    if len(norm) <= _ACRONYM_MAX_LEN and " " not in norm:
        return True
    if norm == iso_name:
        return True
    iso_toks = iso_name.split()
    norm_toks = norm.split()
    if norm_toks and iso_toks[: len(norm_toks)] == norm_toks:
        return True
    return JaroWinkler.normalized_similarity(norm, iso_name) >= _NAMES_COUNTRY_SIMILARITY


def _subnational_names() -> set[str]:
    """Normalised names of places below country level.

    Imported lazily: the resolver loads all three gazetteers anyway, but a
    module-level import would make `load_countries()` alone pull in 48k cities
    and 18.5k localities as a side effect of its own import statement.
    """
    from locatron.gazetteer.au import load_au
    from locatron.gazetteer.cities import load_cities

    au = load_au()
    return set(load_cities().by_norm) | set(au.by_norm) | set(au.state_tokens)


@cached_gazetteer
def load_countries() -> CountryGazetteer:
    by_alpha3: dict[str, CountryRow] = {}
    tokens: dict[str, str] = {}
    code_tokens: dict[str, str] = {}
    hints: dict[str, str] = {}

    for r in rows("SELECT name, `alpha-2` AS alpha2, `alpha-3` AS alpha3, region FROM Countries"):
        alpha3 = (r["alpha3"] or "").strip().upper()
        if not alpha3:
            continue
        alpha2 = (r["alpha2"] or "").strip().upper() or None
        name = (r["name"] or "").strip() or alpha3
        by_alpha3[alpha3] = CountryRow(
            alpha3=alpha3, alpha2=alpha2, name=name, region=(r["region"] or "").strip() or None
        )

        name_key = normalize(name)
        if name_key:
            tokens.setdefault(name_key, alpha3)
            hints.setdefault(name_key, alpha3)
        # Bare codes are positional. See the module docstring.
        for code in (alpha3, alpha2):
            key = normalize(code)
            if key:
                code_tokens.setdefault(key, alpha3)
                hints.setdefault(key, alpha3)

    subnational = _subnational_names()

    for r in rows("SELECT country, value FROM country_bucket"):
        alpha3 = (r["country"] or "").strip().upper()
        key = normalize(r["value"])
        if not key or alpha3 not in by_alpha3:
            continue

        hints.setdefault(key, alpha3)

        iso = by_alpha3[alpha3]
        names_it = _names_its_country(key, normalize(iso.name), iso.alpha2, alpha3)
        if names_it or key not in subnational:
            tokens.setdefault(key, alpha3)

    # A state token always loses to the state reading, which implies the country
    # anyway. Applied after the loops so ordering cannot let one slip through.
    for state_key in _state_token_keys():
        tokens.pop(state_key, None)
        code_tokens.pop(state_key, None)

    return CountryGazetteer(
        by_alpha3=by_alpha3, tokens=tokens, code_tokens=code_tokens, hints=hints
    )


def _state_token_keys() -> set[str]:
    from locatron.gazetteer.au import load_au

    return set(load_au().state_tokens)
