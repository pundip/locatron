"""Country lookup: name, ISO codes, and loose string variants.

Two tables feed this. `Countries` is the ISO register and the source of truth
for a country's identity. `country_bucket` maps 358 loose strings onto an
alpha-3 — including misspellings ("Austtralia", "Australi"), demonyms
("Australian"), and bare alpha-2 codes ("AU").

Every country_bucket row's `country` column joins `Countries`.`alpha-3`, so
alpha-3 is the internal identifier throughout Locatron. Note that the two
tables disagree on display names: Cities says "United States", Countries says
"United States of America". Countries wins, so one input never resolves to two
different spellings of the same country.
"""

from __future__ import annotations

from dataclasses import dataclass

from locatron.gazetteer.loader import cached_gazetteer, rows
from locatron.normalize import normalize
from locatron.schemas import Country


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
    #: normalize(any variant) -> alpha-3. Covers ISO names, both code forms,
    #: and every country_bucket variant.
    variants: dict[str, str]

    def get(self, alpha3: str | None) -> CountryRow | None:
        if not alpha3:
            return None
        return self.by_alpha3.get(alpha3.strip().upper())

    def lookup(self, norm: str) -> CountryRow | None:
        """Resolve an already-normalised string to a country, or None."""
        alpha3 = self.variants.get(norm)
        return self.by_alpha3.get(alpha3) if alpha3 else None

    def resolve_bias(self, bias: str | None) -> str | None:
        """Accept a bias as alpha-3, alpha-2, or a name, and return alpha-3.

        Config holds `country_bias` as a free string so it can be set on the
        box without anyone remembering which code system Locatron uses.
        """
        if not bias:
            return None
        row = self.get(bias) or self.lookup(normalize(bias))
        return row.alpha3 if row else None


@cached_gazetteer
def load_countries() -> CountryGazetteer:
    by_alpha3: dict[str, CountryRow] = {}
    variants: dict[str, str] = {}

    for r in rows("SELECT name, `alpha-2` AS alpha2, `alpha-3` AS alpha3, region FROM Countries"):
        alpha3 = (r["alpha3"] or "").strip().upper()
        if not alpha3:
            continue
        alpha2 = (r["alpha2"] or "").strip().upper() or None
        name = (r["name"] or "").strip() or alpha3
        by_alpha3[alpha3] = CountryRow(
            alpha3=alpha3, alpha2=alpha2, name=name, region=(r["region"] or "").strip() or None
        )
        for variant in (alpha3, alpha2, name):
            key = normalize(variant)
            if key:
                variants.setdefault(key, alpha3)

    # country_bucket last: it must not shadow an ISO code that means something
    # else, but it is otherwise the widest source of loose spellings.
    for r in rows("SELECT country, value FROM country_bucket"):
        alpha3 = (r["country"] or "").strip().upper()
        key = normalize(r["value"])
        if key and alpha3 in by_alpha3:
            variants.setdefault(key, alpha3)

    return CountryGazetteer(by_alpha3=by_alpha3, variants=variants)
