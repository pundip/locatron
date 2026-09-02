"""Tests for the normalisation contract.

If a change here breaks a test, that is the signal to bump NORM_VERSION and
rebuild, not to edit the test.
"""

import pytest

from locatron.normalize import (
    NORM_VERSION,
    cache_key,
    ngrams,
    normalize,
    strip_qualifiers,
    tokens,
)


class TestNormalize:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Carrum Downs", "CARRUM DOWNS"),
            ("  St Kilda   East ", "ST KILDA EAST"),
            ("st. kilda", "ST KILDA"),
            ("Ryde & Eastwood", "RYDE AND EASTWOOD"),
            ("Kur-ring-gai", "KUR-RING-GAI"),
            ("Kur - ring - gai", "KUR-RING-GAI"),
            ("O'Connor", "O CONNOR"),
            ("Ku-ring-gai, NSW", "KU-RING-GAI NSW"),
            ("65 Clifton Park Dr", "65 CLIFTON PARK DR"),
            ("5/12 Smith St", "5/12 SMITH ST"),
            ("", ""),
            (None, ""),
            ("   ", ""),
        ],
    )
    def test_cases(self, raw, expected):
        assert normalize(raw) == expected

    def test_transliterates_non_ascii(self):
        assert normalize("Zürich") == "ZURICH"
        assert normalize("São Paulo") == "SAO PAULO"

    def test_is_idempotent(self):
        for raw in ["Greater Melbourne", "5/12 Smith St", "Ryde & Eastwood"]:
            once = normalize(raw)
            assert normalize(once) == once

    def test_preserves_slash_for_unit_numbers(self):
        # The AU parser relies on 5/12 surviving normalisation.
        assert "/" in normalize("5/12 Smith Street")

    def test_version_is_set(self):
        assert NORM_VERSION


class TestStripQualifiers:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("GREATER MELBOURNE", "MELBOURNE"),
            ("SYDNEY METROPOLITAN AREA", "SYDNEY"),
            ("SYDNEY AREA", "SYDNEY"),
            ("CITY OF PORT PHILLIP", "PORT PHILLIP"),
            ("GREATER WESTERN SYDNEY", "WESTERN SYDNEY"),
            ("MELBOURNE", "MELBOURNE"),
            ("PERTH AND SURROUNDS", "PERTH"),
        ],
    )
    def test_cases(self, raw, expected):
        assert strip_qualifiers(raw) == expected

    def test_does_not_empty_a_bare_qualifier(self):
        # "GREATER" alone is not a place, but stripping to "" would be worse
        # than leaving it for the gazetteer to reject.
        assert strip_qualifiers("GREATER") == "GREATER"


class TestTokens:
    def test_splits(self):
        assert tokens("65 CLIFTON PARK DRIVE") == ["65", "CLIFTON", "PARK", "DRIVE"]

    def test_empty(self):
        assert tokens("") == []


class TestNgrams:
    def test_longest_first(self):
        got = ngrams(["A", "B", "C"], max_len=3)
        assert got[0] == (0, 3, "A B C")
        assert (0, 1, "A") in got
        assert (2, 3, "C") in got

    def test_respects_max_len(self):
        got = ngrams(["A", "B", "C", "D"], max_len=2)
        assert all(len(g[2].split()) <= 2 for g in got)


class TestCacheKey:
    def test_includes_version(self):
        assert f":{NORM_VERSION}:" in cache_key("loc", "Melbourne")

    def test_stable_across_equivalent_inputs(self):
        assert cache_key("loc", "  melbourne ") == cache_key("loc", "Melbourne")

    def test_differs_on_options(self):
        assert cache_key("loc", "Melbourne", "AUS") != cache_key("loc", "Melbourne", "USA")
