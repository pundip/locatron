"""World city lookup, with population as the disambiguator.

`Cities` carries both the local spelling and a pre-transliterated ASCII form:
"São Paulo" / "Sao Paulo", "Zürich" / "Zurich". Both are indexed, because a
scrape can arrive either way and `normalize()` folds them to the same key
anyway (anyascii does the same work as the city_ascii column).

Population is a varchar, blank on 251 rows and written as '11001.00' on a few
Canadian ones. It is the tiebreak that makes Delhi IN beat Delhi CA, so it has
to parse rather than throw.

The table is not unique on (city_ascii, iso3, admin_name) — nine rows share
"Dumri, IND, Bihar" — so lookups return every match and let the scorer decide.
"""

from __future__ import annotations

from dataclasses import dataclass

from rapidfuzz import process
from rapidfuzz.distance import JaroWinkler

from locatron.gazetteer.loader import as_float, as_int, cached_gazetteer, rows
from locatron.normalize import normalize


@dataclass(frozen=True, slots=True)
class CityRow:
    name: str
    """Display form, preferring the local spelling."""
    iso3: str
    country_name: str
    """Cities.country. Only a fallback: prefer the Countries table's spelling."""
    admin_name: str | None
    population: int
    lat: float | None
    lng: float | None
    capital: str | None
    """'primary', 'admin', 'minor', or None. A national or state capital is
    more likely to be what a loose string meant."""

    @property
    def is_capital(self) -> bool:
        return self.capital in ("primary", "admin")


@dataclass(frozen=True, slots=True)
class CityGazetteer:
    by_norm: dict[str, tuple[CityRow, ...]]
    _keys: tuple[str, ...]

    def lookup(self, norm: str) -> tuple[CityRow, ...]:
        """Every city whose name normalises to `norm`. Empty tuple if none."""
        return self.by_norm.get(norm, ())

    def fuzzy(self, norm: str, *, min_score: int, limit: int = 5) -> list[tuple[str, float]]:
        """Closest city keys to `norm` as (key, score) above `min_score`.

        Jaro-Winkler rather than plain edit distance: it weights the start of
        the string, which suits place names, where a typo late in a long name
        should cost less than a wrong first syllable.
        """
        if not norm:
            return []
        hits = process.extract(
            norm,
            self._keys,
            scorer=JaroWinkler.normalized_similarity,
            limit=limit,
            score_cutoff=min_score / 100.0,
        )
        return [(key, score * 100.0) for key, score, _ in hits]


@cached_gazetteer
def load_cities() -> CityGazetteer:
    grouped: dict[str, list[CityRow]] = {}

    for r in rows(
        "SELECT city, city_ascii, lat, lng, country, iso3, admin_name, capital, population "
        "FROM Cities"
    ):
        iso3 = (r["iso3"] or "").strip().upper()
        display = (r["city"] or "").strip() or (r["city_ascii"] or "").strip()
        if not iso3 or not display:
            continue

        row = CityRow(
            name=display,
            iso3=iso3,
            country_name=(r["country"] or "").strip() or iso3,
            admin_name=(r["admin_name"] or "").strip() or None,
            population=as_int(r["population"]),
            lat=as_float(r["lat"]),
            lng=as_float(r["lng"]),
            capital=(r["capital"] or "").strip() or None,
        )

        # Index both spellings. They usually fold to the same key, in which case
        # the row must not be added twice or it becomes its own runner-up and
        # the ambiguity penalty fires on a city that is not ambiguous at all.
        for key in {normalize(r["city"]), normalize(r["city_ascii"])}:
            if key:
                grouped.setdefault(key, []).append(row)

    by_norm = {
        key: tuple(sorted(v, key=lambda c: (-c.population, c.iso3, c.name)))
        for key, v in grouped.items()
    }
    return CityGazetteer(by_norm=by_norm, _keys=tuple(by_norm))
