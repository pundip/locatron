"""Scoring and world-resolver tests.

The scoring half is pure arithmetic over a Settings and needs no database. The
resolver half runs against the live ReferenceDB and skips when it is
unreachable, same as the gazetteer tests.

Most of the resolver tests here are regressions for traps found by running the
golden set, not guesses. Each one names the trap.
"""

from __future__ import annotations

import pytest

from locatron.config import Settings, get_settings
from locatron.db import mysql
from locatron.gazetteer.au import load_au
from locatron.gazetteer.countries import load_countries
from locatron.resolve import scoring
from locatron.resolve.pipeline import resolve_one
from locatron.resolve.scoring import MatchKind, ScoreParts
from locatron.resolve.world import extract_evidence
from locatron.schemas import Granularity, MatchMethod


def _db_available() -> bool:
    try:
        return bool(mysql.health().get("connected"))
    except Exception:
        return False


needs_db = pytest.mark.skipif(not _db_available(), reason="ReferenceDB unreachable")


@pytest.fixture
def s() -> Settings:
    return get_settings()


# ---------------------------------------------------------------------------
# scoring, no database
# ---------------------------------------------------------------------------


def test_base_scores_are_ordered(s: Settings) -> None:
    """An exact hit must always start above the same name reached otherwise."""
    exact = scoring.base_score(MatchKind.EXACT, s)
    stripped = scoring.base_score(MatchKind.QUALIFIER_STRIPPED, s)
    alias = scoring.base_score(MatchKind.ALIAS, s)
    best_fuzzy = scoring.base_score(MatchKind.FUZZY, s, fuzzy_ratio=1.0)
    assert exact > stripped > alias > best_fuzzy


def test_fuzzy_base_scales_with_similarity(s: Settings) -> None:
    """A 98% near-miss must not score the same as one that barely qualified."""
    close = scoring.base_score(MatchKind.FUZZY, s, fuzzy_ratio=0.98)
    far = scoring.base_score(MatchKind.FUZZY, s, fuzzy_ratio=0.88)
    assert close > far
    assert scoring.base_score(MatchKind.FUZZY, s, fuzzy_ratio=2.0) == s.score_fuzzy_max
    assert scoring.base_score(MatchKind.FUZZY, s, fuzzy_ratio=-1.0) == 0.0


def test_unknown_match_kind_raises(s: Settings) -> None:
    with pytest.raises(ValueError, match="unknown match kind"):
        scoring.base_score("telepathy", s)


@pytest.mark.parametrize(
    ("winner", "runner_up", "expected"),
    [
        (100, 0, 1.0),  # nothing to be ambiguous with
        (100, 100, 0.5),  # dead heat
        (0, 0, 1.0),  # no runner-up beats no size information
        (32_226_000, 10_525, pytest.approx(0.99967, abs=1e-5)),  # Delhi IN vs CA
        (438_889, 289_041, pytest.approx(0.60293, abs=1e-5)),  # the Springfields
    ],
)
def test_dominance(winner: int, runner_up: int, expected: float) -> None:
    assert scoring.dominance(winner, runner_up) == expected


def test_dominance_with_no_sizes_on_either_side_is_a_dead_heat() -> None:
    """Two zero-size rivals are ambiguous, not certain."""
    assert scoring.dominance(0, 1) < 1.0


def test_ambiguity_penalty_is_full_at_a_dead_heat(s: Settings) -> None:
    assert scoring.ambiguity_penalty(0.5, s) == s.score_ambiguity_penalty_max


def test_ambiguity_penalty_is_zero_once_dominant(s: Settings) -> None:
    assert scoring.ambiguity_penalty(s.score_dominance_clear, s) == 0.0
    assert scoring.ambiguity_penalty(1.0, s) == 0.0


def test_ambiguity_penalty_decreases_monotonically(s: Settings) -> None:
    doms = [0.50, 0.60, 0.70, 0.85, 0.95, 0.99]
    penalties = [scoring.ambiguity_penalty(d, s) for d in doms]
    assert penalties == sorted(penalties, reverse=True)


def test_springfield_ambiguity_lands_below_the_low_confidence_line(s: Settings) -> None:
    """The whole point of measuring ambiguity by margin rather than by count.

    Six Springfields must produce a doubtful answer; two Delhis must not.
    """
    springfield = s.score_exact - scoring.ambiguity_penalty(scoring.dominance(438_889, 289_041), s)
    delhi = s.score_exact - scoring.ambiguity_penalty(scoring.dominance(32_226_000, 10_525), s)
    assert springfield < s.low_confidence_threshold
    assert delhi > s.low_confidence_threshold


def test_score_parts_applies_multiplier_before_bonuses() -> None:
    """Alias trust discounts the base, not the evidence the input supplied."""
    parts = ScoreParts(base=0.80, multiplier=0.5, bonuses={"country": 0.10})
    assert parts.total == pytest.approx(0.50)


def test_score_parts_clamps_to_the_unit_interval() -> None:
    assert ScoreParts(base=0.9, bonuses={"a": 0.5}).total == 1.0
    assert ScoreParts(base=0.1, penalties={"a": 0.9}).total == 0.0


def test_score_parts_reason_is_auditable() -> None:
    reason = ScoreParts(base=0.8, bonuses={"country": 0.12}, penalties={"ambiguous": 0.3}).reason
    assert "base=0.80" in reason
    assert "+country 0.12" in reason
    assert "-ambiguous 0.30" in reason


def test_country_bias_only_touches_the_biased_country(s: Settings) -> None:
    assert scoring.apply_country_bias(0.70, "AUS", "AUS", s) > 0.70
    assert scoring.apply_country_bias(0.70, "USA", "AUS", s) == 0.70
    assert scoring.apply_country_bias(0.70, "AUS", None, s) == 0.70


def test_country_bias_is_too_small_to_overturn_a_real_gap(s: Settings) -> None:
    """It separates near-equals. It must not rewrite an answer."""
    assert scoring.apply_country_bias(0.46, "AUS", "AUS", s) < 0.79


def test_candidates_cut_at_the_margin(s: Settings) -> None:
    rows = [
        ("near", 0.70, Granularity.CITY, "USA", None, None, "r"),
        ("far", 0.20, Granularity.CITY, "USA", None, None, "r"),
    ]
    out = scoring.to_candidates(rows, winner_score=0.80, s=s)
    assert [c.label for c in out] == ["near"]


def test_candidates_respect_the_maximum(s: Settings) -> None:
    rows = [(f"c{i}", 0.79, Granularity.CITY, "USA", None, None, "r") for i in range(50)]
    assert len(scoring.to_candidates(rows, winner_score=0.80, s=s)) == s.candidate_max


# ---------------------------------------------------------------------------
# the no-raise invariant
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "\n\t",
        "asdfghjkl",
        "Remote / Work from home",
        "!!!???",
        "...",
        "-",
        "0",
        "\x00\x01",
        "🙂🙃",
        "x" * 5000,
        ",,,,,,",
    ],
)
def test_unresolvable_input_never_raises(text: str) -> None:
    """CLAUDE.md's hardest requirement: a column beats an exception downstream."""
    got = resolve_one(text)
    assert got.granularity == Granularity.UNRESOLVED
    assert got.confidence == 0.0
    assert got.match_method == MatchMethod.NONE
    assert got.resolved is False
    assert got.norm_version


@needs_db
def test_an_internal_failure_still_returns_a_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bulk job must not die on row 400,000 of 2,000,000."""
    import locatron.resolve.world as world_mod

    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("gazetteer on fire")

    monkeypatch.setattr(world_mod, "resolve_place", boom)
    got = resolve_one("Melbourne")
    assert got.granularity == Granularity.UNRESOLVED
    assert got.confidence == 0.0
    assert any("gazetteer on fire" in w for w in got.warnings)


# ---------------------------------------------------------------------------
# evidence extraction
# ---------------------------------------------------------------------------


def _evidence(text: str):  # noqa: ANN202 - test helper
    return extract_evidence(text, countries=load_countries(), au=load_au())


@needs_db
@pytest.mark.parametrize(
    ("text", "alpha3", "state", "postcode", "places"),
    [
        ("Sydney Australia", "AUS", None, None, ["SYDNEY"]),
        ("Ryde NSW 2112", None, "NSW", "2112", ["RYDE"]),
        ("Victoria Australia", "AUS", "VIC", None, []),
        ("VIC", None, "VIC", None, []),
        ("3201", None, None, "3201", []),
        ("Fitzroy, Victoria, Australia", "AUS", "VIC", None, ["FITZROY"]),
        ("London, UK", "GBR", None, None, ["LONDON"]),
    ],
)
def test_evidence_separates_what_the_input_states(
    text: str, alpha3: str | None, state: str | None, postcode: str | None, places: list[str]
) -> None:
    ev = _evidence(text)
    assert (ev.country.alpha3 if ev.country else None) == alpha3
    assert ev.state == state
    assert ev.postcode == postcode
    assert list(ev.place_texts) == places


@needs_db
def test_country_peel_prefers_leaving_a_remainder() -> None:
    """'Maryborough Victoria Australia' is itself a country_bucket value.

    Peeling the whole-string match would answer a bare country. Peeling
    'AUSTRALIA' leaves a state and a locality behind, which is the better read.
    """
    ev = _evidence("Maryborough Victoria Australia")
    assert ev.country is not None and ev.country.alpha3 == "AUS"
    assert ev.state == "VIC"
    assert list(ev.place_texts) == ["MARYBOROUGH"]


@needs_db
def test_a_stated_country_is_not_an_australian_signal() -> None:
    """'Sydney Australia' means the city, not a postcode-level suburb."""
    assert _evidence("Sydney Australia").has_au_signal is False
    assert _evidence("Sydney NSW").has_au_signal is True


# ---------------------------------------------------------------------------
# peeling traps — every one of these shipped as a bug first
# ---------------------------------------------------------------------------


@needs_db
def test_leading_st_is_not_sao_tome() -> None:
    """'ST' is Sao Tome's alpha-2, and 'ST KILDA EAST' is a Melbourne suburb.

    The bare-ISO-code rule confines codes to trailing position for this.
    """
    ev = _evidence("St Kilda East VIC")
    assert ev.country is None
    assert list(ev.place_texts) == ["ST KILDA EAST"]

    got = resolve_one("St Kilda East VIC")
    assert got.granularity == Granularity.LOCALITY
    assert got.locality == "ST KILDA EAST"


@needs_db
def test_leading_mt_is_not_malta() -> None:
    ev = _evidence("Mt Eliza VIC")
    assert ev.country is None
    assert list(ev.place_texts) == ["MT ELIZA"]


@needs_db
def test_and_is_not_andorra() -> None:
    """The nastiest one: normalize() MANUFACTURES this token by expanding '&'.

    'Ryde & Eastwood' becomes 'RYDE AND EASTWOOD', and AND is Andorra's alpha-3.
    """
    ev = _evidence("Ryde & Eastwood")
    assert ev.country is None
    assert list(ev.place_texts) == ["RYDE AND EASTWOOD"]


@needs_db
@pytest.mark.parametrize("code", ["ARE", "CAN", "FIN", "GIN", "JAM", "PAN", "TON", "VAT"])
def test_alpha3_codes_that_are_english_words_do_not_peel_mid_string(code: str) -> None:
    ev = _evidence(f"{code} STREET PARK")
    assert ev.country is None, f"{code} peeled as a country mid-string"


@needs_db
def test_a_trailing_code_still_peels() -> None:
    """The restriction is positional, not a blanket ban."""
    ev = _evidence("London UK")
    assert ev.country is not None and ev.country.alpha3 == "GBR"
    assert list(ev.place_texts) == ["LONDON"]


@needs_db
def test_a_city_name_in_country_bucket_is_not_a_peelable_country() -> None:
    """country_bucket maps 'melbourne' to AUS and 'Bangkok' to THA.

    Correct for its own purpose, ruinous for peeling: it consumes the city name
    and leaves a bare country.
    """
    assert _evidence("Melbourne").country is None
    assert _evidence("Bangkok").country is None
    assert resolve_one("Melbourne").granularity == Granularity.CITY
    assert resolve_one("Bangkok").country.alpha3 == "THA"


@needs_db
def test_queensland_reads_as_a_state_not_a_country() -> None:
    """It is in country_bucket as AUS. The state reading implies that anyway."""
    ev = _evidence("Brisbane Queensland")
    assert ev.state == "QLD"
    assert list(ev.place_texts) == ["BRISBANE"]


@needs_db
def test_a_string_that_only_implies_a_country_still_resolves() -> None:
    """Hints are the last resort, and they still have to work."""
    got = resolve_one("The land down under")
    assert got.granularity == Granularity.COUNTRY
    assert got.country.alpha3 == "AUS"


# ---------------------------------------------------------------------------
# city versus locality
# ---------------------------------------------------------------------------


@needs_db
@pytest.mark.parametrize(
    ("text", "alpha3", "admin1"),
    [
        ("Greater Melbourne", "AUS", "VIC"),
        ("Sydney Australia", "AUS", "NSW"),
        ("New York", "USA", "New York"),
        ("Las Vegas", "USA", "Nevada"),
        ("Delhi", "IND", "Delhi"),
        ("Melbourne", "AUS", "VIC"),
        ("Perth", "AUS", "WA"),
        ("London", "GBR", None),
        ("Zurich", "CHE", None),
        ("Sao Paulo", "BRA", None),
    ],
)
def test_world_places_resolve_to_a_city(text: str, alpha3: str, admin1: str | None) -> None:
    got = resolve_one(text)
    assert got.granularity == Granularity.CITY
    assert got.country.alpha3 == alpha3
    if admin1:
        assert admin1 in {got.admin1.code, got.admin1.name}


@needs_db
def test_qualifiers_are_stripped_only_as_a_fallback() -> None:
    """'GREATER MELBOURNE' is tried before 'MELBOURNE', and scores lower."""
    greater = resolve_one("Greater Melbourne")
    plain = resolve_one("Melbourne")
    assert greater.country.alpha3 == plain.country.alpha3 == "AUS"
    assert greater.confidence < plain.confidence


@needs_db
def test_locality_is_suppressed_without_an_australian_signal() -> None:
    """MELBOURNE is 15 locality rows and 2 city rows. The city must win."""
    got = resolve_one("Melbourne", include_candidates=True)
    assert got.granularity == Granularity.CITY
    assert any("suppressed" in w for w in got.warnings)


@needs_db
def test_locality_competes_once_the_input_corroborates_it() -> None:
    """RYDE is both a city and a locality. The postcode settles it."""
    bare = resolve_one("Ryde")
    qualified = resolve_one("Ryde NSW 2112")
    assert bare.granularity == Granularity.CITY
    assert qualified.granularity == Granularity.LOCALITY
    assert qualified.locality == "RYDE"
    assert qualified.postcode == "2112"


@needs_db
def test_a_locality_absent_from_cities_resolves_without_any_signal() -> None:
    """Suppression only applies when a city of the same name exists."""
    got = resolve_one("Carrum Downs")
    assert got.granularity == Granularity.LOCALITY
    assert got.locality == "CARRUM DOWNS"


@needs_db
def test_springfield_is_doubtful_and_says_so() -> None:
    """Ambiguous within one country: low confidence and populated candidates."""
    got = resolve_one("Springfield")
    s = get_settings()
    assert got.granularity == Granularity.CITY
    assert got.country.alpha3 == "USA"
    assert got.confidence < s.low_confidence_threshold
    assert got.candidates, "an ambiguous winner with no alternatives cannot be debugged"
    assert any("low confidence" in w for w in got.warnings)


@needs_db
def test_candidates_appear_without_being_asked_for_when_confidence_is_low() -> None:
    """The flag forces them on; doubt turns them on by itself."""
    assert resolve_one("Springfield", include_candidates=False).candidates
    assert not resolve_one("Delhi", include_candidates=False).candidates
    assert resolve_one("Delhi", include_candidates=True).candidates


# ---------------------------------------------------------------------------
# coarse answers
# ---------------------------------------------------------------------------


@needs_db
@pytest.mark.parametrize(
    ("text", "granularity", "alpha3", "admin1"),
    [
        ("Victoria Australia", Granularity.ADMIN1, "AUS", "VIC"),
        ("VIC", Granularity.ADMIN1, "AUS", "VIC"),
        ("New South Wales", Granularity.ADMIN1, "AUS", "NSW"),
        ("Australia", Granularity.COUNTRY, "AUS", None),
        ("United Kingdom", Granularity.COUNTRY, "GBR", None),
        ("3201", Granularity.POSTCODE, "AUS", "VIC"),
    ],
)
def test_coarse_answers(
    text: str, granularity: Granularity, alpha3: str, admin1: str | None
) -> None:
    got = resolve_one(text)
    assert got.granularity == granularity
    assert got.country.alpha3 == alpha3
    if admin1:
        assert got.admin1.code == admin1


@needs_db
def test_a_coarse_answer_never_outranks_a_place_match() -> None:
    """A bare ADMIN1 (0.74) once beat a corroborated fuzzy locality and won.

    That threw away the more specific answer for 'Ku-ring-gai NSW'.
    """
    got = resolve_one("Ku-ring-gai NSW")
    assert got.granularity == Granularity.LOCALITY
    assert got.admin1.code == "NSW"


# ---------------------------------------------------------------------------
# bias, confidence, and options
# ---------------------------------------------------------------------------


@needs_db
def test_bias_never_overrides_a_stated_country() -> None:
    assert resolve_one("Delhi India", country_bias="AUS").country.alpha3 == "IND"
    assert resolve_one("Springfield USA", country_bias="AUS").country.alpha3 == "USA"
    assert resolve_one("London United Kingdom", country_bias="AUS").country.alpha3 == "GBR"


@needs_db
def test_fuzzy_will_not_match_by_discarding_a_token() -> None:
    """'MELBOURNE FLORIDA' scores highly against 'MELBOURNE' under Jaro-Winkler.

    Accepting it answers the US city with the Australian one. Unresolved is the
    correct outcome here: this resolver models Australian states, not US ones,
    so it cannot read 'Florida' and must not pretend the token is not there.
    """
    got = resolve_one("Melbourne Florida")
    assert got.granularity == Granularity.UNRESOLVED
    assert resolve_one("Melbourne").granularity == Granularity.CITY


@needs_db
def test_bias_does_not_reach_across_a_real_population_gap() -> None:
    for bias in ("AUS", "CAN", "USA", None):
        assert resolve_one("London", country_bias=bias).country.alpha3 == "GBR"


@needs_db
def test_confidence_stays_below_the_gazetteer_ceiling() -> None:
    """Exact name plus state plus postcode sums past 1.0 and saturates.

    Reporting 1.0 for a locality lookup claims certainty this layer cannot have;
    above the ceiling belongs to an exact G-NAF address match.
    """
    s = get_settings()
    got = resolve_one("Ryde NSW 2112")
    assert got.confidence == pytest.approx(s.score_max)
    assert got.confidence < 1.0


@needs_db
def test_min_granularity_stops_early() -> None:
    """Same answer, no fuzzy pass. Bulk jobs pay for the work they skip."""
    full = resolve_one("Carrum Downs VIC")
    early = resolve_one("Carrum Downs VIC", min_granularity=Granularity.LOCALITY)
    assert early.granularity == full.granularity
    assert early.locality == full.locality


@needs_db
def test_match_method_is_set_accurately() -> None:
    """The main debugging signal when a result looks wrong."""
    assert resolve_one("Delhi").match_method == MatchMethod.CITY_POPULATION_TIEBREAK
    assert resolve_one("Zurich").match_method == MatchMethod.CITY_EXACT
    assert resolve_one("Carrum Downs VIC").match_method == MatchMethod.LOCALITY_EXACT
    assert resolve_one("Australia").match_method == MatchMethod.COUNTRY_BUCKET
    assert resolve_one("VIC").match_method == MatchMethod.STATE_BUCKET
    assert resolve_one("asdfghjkl").match_method == MatchMethod.NONE


@needs_db
def test_response_carries_geometry_where_the_gazetteer_has_it() -> None:
    got = resolve_one("Melbourne")
    assert got.geo is not None
    assert -44 < got.geo.lat < -9
    assert 112 < got.geo.lng < 154


@needs_db
def test_response_is_always_well_formed() -> None:
    got = resolve_one("Sydney Australia")
    assert got.query == "Sydney Australia"
    assert got.normalized == "SYDNEY AUSTRALIA"
    assert got.norm_version
    assert got.elapsed_ms is not None and got.elapsed_ms >= 0
    assert got.resolved_at is not None
    assert 0.0 <= got.confidence <= 1.0
