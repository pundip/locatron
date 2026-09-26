"""scripts/dedupe_locality.py planning tests.

The script deletes rows, so the parts that decide *which* rows are tested here
with no database in the way: survivor selection, the recomputed flags, and the
alias remap/collapse plan. Running it for real needs locatron_build
credentials, which the test suite deliberately does not have.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "dedupe_locality", Path(__file__).resolve().parent.parent / "scripts" / "dedupe_locality.py"
)
assert _SPEC and _SPEC.loader
dedupe = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(dedupe)


def _row(lid: int, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "locality_id": lid,
        "locality": f"PLACE{lid}",
        "state": "QLD",
        "postcode": "4514",
        "norm_key": "PLACE",
        "in_gnaf": 0,
        "in_auspost": 0,
        "is_postal_only": 0,
        "address_count": 0,
        "street_count": 0,
        "geo_source": "none",
        "delivery_type": None,
        "lat": None,
        "lng": None,
        "postcode_lat": None,
        "postcode_lng": None,
        "sa2_name": None,
        "sa3_name": None,
        "sa4_name": None,
        "lga_name": None,
        "remoteness": None,
    }
    base.update(kw)
    return base


def _alias(aid: int, display: str, lid: int, conf: float = 1.0) -> dict[str, Any]:
    return {
        "alias_id": aid,
        "alias_norm_key": display.replace("'", " "),
        "alias_display": display,
        "locality_id": lid,
        "alias_type": "gnaf_alias",
        "confidence": conf,
    }


def _group(survivor: dict[str, Any], *dropped: dict[str, Any]) -> Any:
    return dedupe.Group(
        state=survivor["state"],
        postcode=survivor["postcode"],
        norm_key=survivor["norm_key"],
        survivor=survivor,
        dropped=list(dropped),
    )


# ---------------------------------------------------------------------------
# recomputed flags
# ---------------------------------------------------------------------------


def test_in_auspost_is_ored_across_the_group() -> None:
    """The dropped AusPost twin is why the survivor is in AusPost at all."""
    g = _group(_row(1, in_gnaf=1, in_auspost=0), _row(2, in_auspost=1))
    assert g.in_auspost == 1


def test_in_auspost_stays_zero_when_no_member_has_it() -> None:
    g = _group(_row(1, in_gnaf=1), _row(2))
    assert g.in_auspost == 0


def test_is_postal_only_is_recomputed_from_the_survivor_not_inherited() -> None:
    """The bug being fixed: a place with 1,069 G-NAF addresses was flagged
    postal-only because its AusPost twin was."""
    g = _group(_row(1, in_gnaf=1, address_count=1069), _row(2, is_postal_only=1))
    assert g.is_postal_only == 0


def test_is_postal_only_stays_set_when_the_survivor_has_no_gnaf() -> None:
    g = _group(_row(1, in_gnaf=0, is_postal_only=1), _row(2, in_gnaf=0))
    assert g.is_postal_only == 1


def test_lost_fields_reports_columns_only_the_dropped_row_fills() -> None:
    g = _group(_row(1, in_gnaf=1), _row(2, sa2_name="Amaroo", remoteness="R2"))
    assert g.lost_fields() == {2: ["sa2_name", "remoteness"]}


def test_lost_fields_is_empty_when_the_survivor_already_has_them() -> None:
    g = _group(_row(1, in_gnaf=1, sa2_name="Amaroo"), _row(2, sa2_name="Amaroo"))
    assert g.lost_fields() == {}


# ---------------------------------------------------------------------------
# alias plan
# ---------------------------------------------------------------------------


def test_alias_on_a_dropped_row_is_remapped_to_the_survivor() -> None:
    g = _group(_row(1, in_gnaf=1), _row(2))
    plan = dedupe._plan_from_rows([g], [_alias(10, "D'AGUILAR", 2)])
    assert plan.remap == [(10, 1)]
    assert plan.delete == []


def test_alias_already_on_the_survivor_is_left_alone() -> None:
    g = _group(_row(1, in_gnaf=1), _row(2))
    plan = dedupe._plan_from_rows([g], [_alias(10, "D'AGUILAR", 1)])
    assert plan.remap == []
    assert plan.delete == []


def test_colliding_display_collapses_keeping_the_higher_confidence() -> None:
    """uq_lla_alias_target is (alias_display, locality_id), so two rows with the
    same display cannot both land on the survivor."""
    g = _group(_row(1, in_gnaf=1), _row(2))
    plan = dedupe._plan_from_rows(
        [g],
        [
            _alias(10, "DAGUILAR", 1, conf=1.00),
            _alias(11, "DAGUILAR", 2, conf=0.80),
        ],
    )
    assert plan.remap == []
    assert plan.delete == [11]
    assert plan.collisions == [("DAGUILAR", 10, 11)]


def test_collapse_prefers_confidence_over_which_row_survives() -> None:
    """A low-trust auspost_variant on the survivor must not displace a
    higher-confidence alias just because of where it sits."""
    g = _group(_row(1, in_gnaf=1), _row(2))
    plan = dedupe._plan_from_rows(
        [g],
        [
            _alias(10, "DAGUILAR", 1, conf=0.80),
            _alias(11, "DAGUILAR", 2, conf=1.00),
        ],
    )
    # The dropped row's alias is the keeper, so it moves and the other goes.
    assert plan.remap == [(11, 1)]
    assert plan.delete == [10]


def test_collapse_tiebreaks_on_alias_id_when_confidence_ties() -> None:
    g = _group(_row(1, in_gnaf=1), _row(2))
    plan = dedupe._plan_from_rows([g], [_alias(11, "X", 2, conf=0.9), _alias(10, "X", 1, conf=0.9)])
    assert plan.delete == [11]
    assert plan.remap == []


def test_two_dropped_rows_sharing_a_display_collapse_together() -> None:
    g = _group(_row(1, in_gnaf=1), _row(2), _row(3))
    plan = dedupe._plan_from_rows([g], [_alias(20, "X", 2, conf=0.9), _alias(21, "X", 3, conf=0.9)])
    assert plan.remap == [(20, 1)]
    assert plan.delete == [21]


def test_distinct_displays_all_move_without_collapsing() -> None:
    g = _group(_row(1, in_gnaf=1), _row(2))
    plan = dedupe._plan_from_rows([g], [_alias(20, "A", 2), _alias(21, "B", 2), _alias(22, "C", 1)])
    assert sorted(plan.remap) == [(20, 1), (21, 1)]
    assert plan.delete == []


def test_groups_are_planned_independently() -> None:
    """A display shared across two different places must not be collapsed."""
    g1 = _group(_row(1, in_gnaf=1), _row(2))
    g2 = _group(
        _row(3, in_gnaf=1, state="WA", postcode="6163"), _row(4, state="WA", postcode="6163")
    )
    plan = dedupe._plan_from_rows([g1, g2], [_alias(20, "O'CONNOR", 2), _alias(21, "O'CONNOR", 4)])
    assert sorted(plan.remap) == [(20, 1), (21, 3)]
    assert plan.delete == []


@pytest.mark.parametrize("n_dropped", [1, 2, 3])
def test_every_dropped_row_is_accounted_for(n_dropped: int) -> None:
    dropped = [_row(i + 2) for i in range(n_dropped)]
    g = _group(_row(1, in_gnaf=1), *dropped)
    assert len(g.dropped) == n_dropped
    aliases = [_alias(100 + i, f"D{i}", r["locality_id"]) for i, r in enumerate(dropped)]
    plan = dedupe._plan_from_rows([g], aliases)
    assert len(plan.remap) == n_dropped
    assert plan.delete == []


# ---------------------------------------------------------------------------
# the write is opt-in
# ---------------------------------------------------------------------------


@pytest.fixture
def stubbed(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """main() with the database and the write both replaced."""
    calls: dict[str, Any] = {"applied": 0, "verified": 0}
    g = _group(_row(1, in_gnaf=1), _row(2))

    monkeypatch.setattr(dedupe, "load_groups", lambda: [g])
    monkeypatch.setattr(dedupe, "plan_aliases", lambda _g: dedupe.AliasPlan([], [], []))
    monkeypatch.setattr(
        dedupe, "apply", lambda *_a: calls.__setitem__("applied", calls["applied"] + 1)
    )
    monkeypatch.setattr(
        dedupe, "verify", lambda: calls.__setitem__("verified", calls["verified"] + 1) or True
    )
    return calls


def test_bare_invocation_writes_nothing(
    stubbed: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The reason the flag was inverted: the bare command deletes rows."""
    monkeypatch.setattr("sys.argv", ["dedupe_locality.py"])
    assert dedupe.main() == 0
    assert stubbed["applied"] == 0
    assert "Nothing written" in capsys.readouterr().out


def test_dry_run_flag_still_writes_nothing(
    stubbed: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.argv", ["dedupe_locality.py", "--dry-run"])
    assert dedupe.main() == 0
    assert stubbed["applied"] == 0


def test_apply_flag_writes(stubbed: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["dedupe_locality.py", "--apply"])
    assert dedupe.main() == 0
    assert stubbed["applied"] == 1
    assert stubbed["verified"] == 1


def test_apply_and_dry_run_together_is_refused(
    stubbed: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.argv", ["dedupe_locality.py", "--apply", "--dry-run"])
    assert dedupe.main() == 1
    assert stubbed["applied"] == 0
