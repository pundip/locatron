"""Gazetteer loader tests.

These run against the live ReferenceDB, because the thing most worth testing is
that the loaders agree with the real data. Every assertion is either a loose
lower bound on row counts or a named fact about a specific row, so upstream
growth does not turn them red. They skip rather than fail when the database is
unreachable, so `pytest` still works offline.

The traps each test pins down are the ones from CLAUDE.md and from the schema
inspection: varchar numerics, transliteration, leading-zero postcodes, and the
aus_state_bucket token/hint split.
"""

from __future__ import annotations

import pytest

from locatron.db import mysql
from locatron.gazetteer.au import load_au
from locatron.gazetteer.cities import load_cities
from locatron.gazetteer.countries import load_countries
from locatron.gazetteer.loader import as_float, as_int, cached_gazetteer, reset_gazetteers
from locatron.normalize import normalize


def _db_available() -> bool:
    try:
        return bool(mysql.health().get("connected"))
    except Exception:
        return False


needs_db = pytest.mark.skipif(not _db_available(), reason="ReferenceDB unreachable")


# ---------------------------------------------------------------------------
# loader plumbing, no database needed
# ---------------------------------------------------------------------------


def test_cached_gazetteer_loads_once_and_resets() -> None:
    calls = []

    @cached_gazetteer
    def loader() -> str:
        calls.append(1)
        return "payload"

    assert not loader.is_loaded()
    assert loader() == "payload"
    assert loader() == "payload"
    assert len(calls) == 1
    assert loader.is_loaded()

    reset_gazetteers()
    assert not loader.is_loaded()
    assert loader() == "payload"
    assert len(calls) == 2


def test_cached_gazetteer_caches_falsy_results() -> None:
    """An empty gazetteer must not be reloaded on every call.

    A legitimately empty result is indistinguishable from "not cached" under a
    truthiness check, which would turn one bad query into a load per request.
    """
    calls = []

    @cached_gazetteer
    def loader() -> dict[str, str]:
        calls.append(1)
        return {}

    assert loader() == {}
    assert loader() == {}
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1234", 1234),
        ("", 0),
        (None, 0),
        ("  567 ", 567),
        ("11001.00", 11001),  # real Cities.population rows are written this way
        ("not a number", 0),
    ],
)
def test_as_int_survives_upstream_varchars(raw: str | None, expected: int) -> None:
    assert as_int(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("-37.8136", -37.8136),
        ("", None),
        ("   ", None),
        (None, None),
        ("junk", None),
        ("0", 0.0),
    ],
)
def test_as_float_returns_none_for_blanks_not_zero(raw: str | None, expected: float | None) -> None:
    """Blank coordinates must be None. Zero is a real place off West Africa."""
    assert as_float(raw) == expected


# ---------------------------------------------------------------------------
# countries
# ---------------------------------------------------------------------------


@needs_db
def test_countries_loads_every_iso_row() -> None:
    g = load_countries()
    assert len(g.by_alpha3) >= 249


@needs_db
def test_countries_prefers_iso_name_over_cities_spelling() -> None:
    """Cities says 'United States'; Countries says 'United States of America'.

    One input must never resolve to two spellings of the same country.
    """
    usa = g_get("USA")
    assert usa is not None
    assert usa.name == "United States of America"
    assert usa.alpha2 == "US"


@needs_db
@pytest.mark.parametrize(
    ("variant", "alpha3"),
    [
        ("Australia", "AUS"),
        ("australia", "AUS"),
        ("AU", "AUS"),
        ("AUS", "AUS"),
        ("Austtralia", "AUS"),  # misspelling carried by country_bucket
        ("Australian", "AUS"),  # demonym
        ("USA", "USA"),
        ("US", "USA"),
        ("United States", "USA"),
        ("United States of America", "USA"),
        ("UK", "GBR"),
        ("United Kingdom", "GBR"),
        ("Switzerland", "CHE"),
        ("Brazil", "BRA"),
        ("India", "IND"),
    ],
)
def test_country_variants_resolve(variant: str, alpha3: str) -> None:
    g = load_countries()
    key = normalize(variant)
    row = g.lookup_token(key) or g.lookup_trailing_code(key)
    assert row is not None, f"{variant!r} did not resolve"
    assert row.alpha3 == alpha3


@needs_db
def test_country_bucket_never_shadows_an_iso_code() -> None:
    """Every country_bucket variant points at a country the ISO table knows."""
    g = load_countries()
    for mapping in (g.tokens, g.code_tokens, g.hints):
        assert all(a3 in g.by_alpha3 for a3 in mapping.values())


@needs_db
@pytest.mark.parametrize("value", ["melbourne", "Bangkok", "Queensland", "Scotland"])
def test_bucket_values_that_are_places_are_hints_at_most(value: str) -> None:
    """country_bucket maps loose location strings, not just country names.

    'melbourne' -> AUS is correct for its own purpose and ruinous for peeling:
    it eats the city name and answers a bare country.
    """
    g = load_countries()
    key = normalize(value)
    assert g.lookup_hint(key) is not None, f"{value!r} should still imply a country"
    if value != "Scotland":
        assert g.lookup_token(key) is None, f"{value!r} must not be peelable"


@needs_db
@pytest.mark.parametrize("code", ["ST", "MT", "IN", "LA", "PA", "AND", "ARE", "CAN"])
def test_bare_iso_codes_are_positional(code: str) -> None:
    """'ST KILDA EAST' must not lose its first token to Sao Tome.

    normalize() even manufactures AND, by expanding '&'.
    """
    g = load_countries()
    assert g.lookup_token(code) is None, f"{code} must not peel mid-string"
    assert g.lookup_trailing_code(code) is not None, f"{code} should work trailing"


@needs_db
def test_state_tokens_never_read_as_countries() -> None:
    """'Queensland' is AUS in country_bucket and QLD in aus_state_bucket.

    The state reading wins: it implies the country anyway, and says more.
    """
    g = load_countries()
    for key in load_au().state_tokens:
        assert g.lookup_token(key) is None, f"{key!r} should read as a state"
        assert g.lookup_trailing_code(key) is None


@needs_db
@pytest.mark.parametrize("bias", ["AUS", "AU", "Australia", "aus"])
def test_resolve_bias_accepts_any_code_system(bias: str) -> None:
    assert load_countries().resolve_bias(bias) == "AUS"


@needs_db
def test_resolve_bias_of_garbage_is_none() -> None:
    g = load_countries()
    assert g.resolve_bias(None) is None
    assert g.resolve_bias("") is None
    assert g.resolve_bias("Freedonia") is None


def g_get(alpha3: str):  # noqa: ANN201 - test helper
    return load_countries().get(alpha3)


# ---------------------------------------------------------------------------
# cities
# ---------------------------------------------------------------------------


@needs_db
def test_cities_loads_full_table() -> None:
    g = load_cities()
    assert len(g.by_norm) >= 30_000


@needs_db
def test_city_indexed_under_both_spellings() -> None:
    """Zurich/Zürich and Sao Paulo/São Paulo must both be findable."""
    g = load_cities()
    for ascii_form, local_form in [("Zurich", "Zürich"), ("Sao Paulo", "São Paulo")]:
        assert g.lookup(normalize(ascii_form)), ascii_form
        assert g.lookup(normalize(local_form)), local_form
        assert g.lookup(normalize(ascii_form)) == g.lookup(normalize(local_form))


@needs_db
def test_city_row_not_double_indexed_when_spellings_fold_together() -> None:
    """Zurich has exactly one row, so it must not look ambiguous.

    normalize() transliterates, so city and city_ascii fold to the same key.
    Appending per-spelling would make the row its own runner-up and fire the
    ambiguity penalty on an unambiguous city.
    """
    hits = load_cities().lookup(normalize("Zurich"))
    assert len(hits) == 1
    assert hits[0].iso3 == "CHE"


@needs_db
def test_population_parsed_and_sorted_descending() -> None:
    g = load_cities()
    delhis = g.lookup(normalize("Delhi"))
    assert len(delhis) >= 2
    assert delhis[0].iso3 == "IND"
    assert delhis[0].population > 30_000_000
    assert [c.population for c in delhis] == sorted((c.population for c in delhis), reverse=True)


@needs_db
def test_blank_population_becomes_zero_not_a_crash() -> None:
    """251 Cities rows have population ''. Loading must not raise."""
    g = load_cities()
    assert any(c.population == 0 for rows_ in g.by_norm.values() for c in rows_)


@needs_db
@pytest.mark.parametrize(
    ("name", "top_iso3", "admin_name"),
    [
        ("Melbourne", "AUS", "Victoria"),
        ("Perth", "AUS", "Western Australia"),
        ("Sydney", "AUS", "New South Wales"),
        ("Delhi", "IND", "Delhi"),
        ("London", "GBR", "London, City of"),
        ("Las Vegas", "USA", "Nevada"),
        ("New York", "USA", "New York"),
    ],
)
def test_population_order_puts_the_expected_city_first(
    name: str, top_iso3: str, admin_name: str
) -> None:
    top = load_cities().lookup(normalize(name))[0]
    assert top.iso3 == top_iso3
    assert top.admin_name == admin_name


@needs_db
def test_springfield_is_ambiguous_within_one_country() -> None:
    """The deliberate low-confidence case: many US Springfields, none dominant."""
    hits = load_cities().lookup(normalize("Springfield"))
    assert len(hits) >= 10
    assert {c.iso3 for c in hits} == {"USA", "CAN"}
    # Runner-up is a sizeable fraction of the winner, so the scorer must not
    # report this as a confident answer.
    assert hits[1].population / hits[0].population > 0.5


@needs_db
def test_city_fuzzy_finds_a_near_miss() -> None:
    g = load_cities()
    hits = g.fuzzy(normalize("Melbourn"), min_score=85)
    assert any(key == "MELBOURNE" for key, _ in hits)


@needs_db
def test_city_fuzzy_rejects_garbage() -> None:
    assert load_cities().fuzzy(normalize("asdfghjkl"), min_score=90) == []


# ---------------------------------------------------------------------------
# AU localities and aliases
# ---------------------------------------------------------------------------


@needs_db
def test_au_localities_load() -> None:
    g = load_au()
    assert len(g.by_id) >= 18_000
    assert len(g.aliases) >= 10_000


@needs_db
def test_locality_lookup_and_state_filter() -> None:
    g = load_au()
    hits = g.lookup(normalize("Carrum Downs"))
    assert len(hits) == 1
    assert (hits[0].state, hits[0].postcode) == ("VIC", "3201")
    assert g.lookup(normalize("Carrum Downs"), state="NSW") == ()


@needs_db
def test_localities_sorted_so_the_populated_one_wins() -> None:
    """MELBOURNE has 15 rows, 13 of them postal-only with zero addresses."""
    hits = load_au().lookup(normalize("Melbourne"))
    assert len(hits) > 1
    assert hits[0].postcode == "3000"
    assert hits[0].address_count > 100_000
    assert [r.size for r in hits] == sorted((r.size for r in hits), reverse=True)


@needs_db
def test_postal_only_locality_outranks_nothing_but_never_a_suburb() -> None:
    g = load_au()
    postal = [r for r in g.by_id.values() if r.is_postal_only]
    assert postal, "no postal-only localities loaded"
    assert all(r.size >= 1 for r in postal)
    assert all(r.size == 1 for r in postal if r.address_count == 0)


@needs_db
def test_postcodes_keep_their_leading_zero() -> None:
    """NT is 0800-0899. A four-char postcode must survive the round trip."""
    g = load_au()
    assert all(len(r.postcode) == 4 for r in g.by_id.values())
    nt = [r for r in g.by_id.values() if r.state == "NT" and r.postcode.startswith("08")]
    assert nt, "no NT postcodes in the 08xx range"


@needs_db
def test_lookup_by_postcode() -> None:
    hits = load_au().lookup_postcode("3201")
    assert hits
    assert all(r.postcode == "3201" for r in hits)
    assert any(r.locality == "CARRUM DOWNS" for r in hits)


@needs_db
def test_alias_lookup_reaches_a_locality() -> None:
    """Pick a real alias from the table and confirm it resolves to its target."""
    g = load_au()
    key, aliases = next(
        (k, v) for k, v in g.aliases.items() if v[0].locality_id in g.by_id and k not in g.by_norm
    )
    hits = g.lookup_alias(key)
    assert hits
    assert aliases[0].locality_id in {r.locality_id for r in hits}


@needs_db
def test_alias_confidence_is_a_multiplier_not_a_score() -> None:
    g = load_au()
    key, aliases = next(iter(g.aliases.items()))
    assert 0.0 < g.alias_confidence(key, aliases[0].locality_id) <= 1.0
    # An unrelated locality_id gets the neutral default.
    assert g.alias_confidence(key, -1) == 1.0


@needs_db
def test_locality_fuzzy_finds_a_near_miss() -> None:
    hits = load_au().fuzzy(normalize("Carrum Down"), min_score=85)
    assert any(key == "CARRUM DOWNS" for key, _ in hits)


# ---------------------------------------------------------------------------
# the aus_state_bucket split — the trap this module exists to avoid
# ---------------------------------------------------------------------------


@needs_db
def test_state_tokens_are_few_and_cover_all_eight_states() -> None:
    g = load_au()
    assert set(g.state_tokens.values()) == {"NSW", "VIC", "QLD", "SA", "WA", "TAS", "NT", "ACT"}
    # A tight set by construction: codes and full names only, not 65k rows.
    assert len(g.state_tokens) < 40


@needs_db
@pytest.mark.parametrize(
    ("text_in", "code"),
    [
        ("VIC", "VIC"),
        ("vic", "VIC"),
        ("Victoria", "VIC"),
        ("NSW", "NSW"),
        ("New South Wales", "NSW"),
        ("A.C.T", "ACT"),
        ("A C T", "ACT"),
        ("ACT", "ACT"),
        ("Australian Capital Territory", "ACT"),
        ("Western Australia", "WA"),
        ("Queensland", "QLD"),
        ("Tasmania", "TAS"),
        ("Northern Territory", "NT"),
        ("South Australia", "SA"),
    ],
)
def test_state_token_recognises_real_state_forms(text_in: str, code: str) -> None:
    assert load_au().state_token(normalize(text_in)) == code


@needs_db
@pytest.mark.parametrize("place", ["Toronto", "York", "Newcastle"])
def test_bare_locality_in_the_bucket_is_a_hint_not_a_state_token(place: str) -> None:
    """The whole reason state_tokens and state_hints are separate.

    These three sit in aus_state_bucket as bare values, so a naive "any bucket
    hit means the input named a state" rule sends Toronto to New South Wales.
    """
    g = load_au()
    key = normalize(place)
    assert g.state_hint(key) is not None, f"{place} should be in the wider bucket"
    assert g.state_token(key) is None, f"{place} must not count as a state token"


#: The only two normalised keys in aus_state_bucket that map to two states.
#: Both are upstream data errors invisible in the raw values — 'HAYMARKET NSW'
#: vs 'HAYMARKET, NSW' and 'N.S.W.' vs 'N.S.W' differ only in punctuation that
#: normalize() removes. Each maps to the state the string itself names.
KNOWN_STATE_COLLISIONS = {"N S W": "NSW", "HAYMARKET NSW": "NSW"}


def _bucket_collisions() -> dict[str, set[str]]:
    seen: dict[str, set[str]] = {}
    for r in mysql.fetch_all("SELECT state, value FROM aus_state_bucket"):
        key = normalize(r["value"])
        if key:
            seen.setdefault(key, set()).add((r["state"] or "").strip().upper())
    return {k: v for k, v in seen.items() if len(v) > 1}


@needs_db
def test_no_new_state_collisions_appear_upstream() -> None:
    """Catch a new contradiction rather than silently picking a side.

    The two known ones are handled deliberately. A third would mean a loader
    tiebreak is quietly deciding something nobody reviewed.
    """
    assert set(_bucket_collisions()) == set(KNOWN_STATE_COLLISIONS)


@needs_db
def test_colliding_keys_resolve_the_same_way_every_load() -> None:
    """Resolution must not depend on the order MyISAM returns rows in."""
    g = load_au()
    for key, expected in KNOWN_STATE_COLLISIONS.items():
        assert g.state_hint(key) == expected, key


@needs_db
def test_the_mislabelled_state_row_never_becomes_a_token() -> None:
    """'N.S.W' is labelled VIC upstream. It must not make 'N S W' mean VIC.

    The token test filters it out for free: the row's squashed key is 'NSW',
    which does not equal its own state column, so it never qualifies.
    """
    assert load_au().state_token("N S W") == "NSW"


@needs_db
def test_state_display_names_round_trip() -> None:
    g = load_au()
    for code in ("VIC", "NSW", "QLD", "WA", "SA", "TAS", "NT", "ACT"):
        name = g.state_name(code)
        assert name and name != code
        assert g.code_for_state_name(name) == code


@needs_db
def test_code_for_state_name_maps_cities_admin_names() -> None:
    """Cities.admin_name is the input; Admin1.code is the output."""
    g = load_au()
    assert g.code_for_state_name("Victoria") == "VIC"
    assert g.code_for_state_name("New South Wales") == "NSW"
    assert g.code_for_state_name("Nevada") is None
    assert g.code_for_state_name(None) is None


@needs_db
def test_external_territories_present_but_absent_from_the_bucket() -> None:
    """locatron_locality carries OT rows; aus_state_bucket has no OT.

    state_name falls back to the code so an OT locality still serialises.
    """
    g = load_au()
    assert any(r.state == "OT" for r in g.by_id.values())
    assert "OT" not in set(g.state_tokens.values())
    assert g.state_name("OT") == "OT"
