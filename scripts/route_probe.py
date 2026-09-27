"""Show which path each input would take, and at what granularity it lands.

The evidence behind the routing rule in CLAUDE.md. Run it after any change to
the triggers, the gate or the granularity mapping: a rule that sends
'New York' down the Australian path, or 'Victoria Australia' to a locality,
is wrong however reasonable it reads.

    uv run python scripts/route_probe.py             # golden rows and probe cases
    uv run python scripts/route_probe.py --failures  # only rows that miss golden
    uv run python scripts/route_probe.py --confidence # the calibration table

The routing logic here is deliberately a copy of nothing: until the pipeline
wiring lands it IS the proposal, and after that it imports the real function so
the two cannot drift.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from locatron.cli import _extract_components
from locatron.gazetteer.au import load_au
from locatron.parse.locality import find_state_tokens, generate_hypotheses
from locatron.parse.lookup import Granularity as Rung
from locatron.parse.lookup import lookup
from locatron.parse.scoring import SCORE_FULL, SCORE_FULL_WITH_STREET, au_confidence
from locatron.parse.street import resolve_streets
from locatron.parse.tokens import tokenize
from locatron.resolve import pipeline

REPO = Path(__file__).resolve().parent.parent

#: Floor on the winning hypothesis's joint score. Not a tuning knob: it rejects a
#: parse that explains less than it fails to, which is what a negative score
#: means. The discrimination is done by the triggers and the fuzzy rule.
AU_MIN_SCORE = 0.0

#: The parser ladder's rungs mapped onto the response vocabulary. LOCALITY is
#: absent because it splits -- see `mapped_granularity`.
#: Warning fragments that correspond to a confidence ceiling, so the table can
#: name why a row was capped.
_CAP_WARNINGS = ("substituted", "the range", "not found", "alias of", "fell back to the street")

RUNG_TO_SCHEMA = {
    Rung.UNIT: "unit",
    Rung.ADDRESS: "address",
    Rung.STREET: "street",
    Rung.POSTAL: "postcode",
}


class Parsed:
    """Everything one parse produced, so routing and granularity share the work."""

    def __init__(self, text: str, au, known: frozenset[str]) -> None:
        self.ts = tokenize(text)
        (
            self.boxes,
            self.units,
            self.postcodes,
            self.numbers,
            claimed,
        ) = _extract_components(self.ts, known)
        self.states = find_state_tokens(self.ts, au)
        hyps = generate_hypotheses(
            self.ts,
            au,
            consumed=claimed,
            postcodes=self.postcodes,
            po_box_found=bool(self.boxes),
            fuzzy_min=88,
        )
        self.joint = resolve_streets(self.ts, hyps, consumed=claimed)
        self.top = self.joint[0] if self.joint else None
        self.runner_up = self.joint[1].score if len(self.joint) > 1 else None


def triggers(p: Parsed) -> list[str]:
    """Signals that the input is an Australian address rather than a place name.

    Each one is something a loose world place string does not carry. The street
    ones are narrow on purpose: 'New York' matches street NEW ST in locality
    YORK at a healthy score, so a bare street match cannot be a trigger. What
    separates a real AU street from that is either an explicit street-type word
    in the input ('DRIVE', 'CRESCENT') or a street number in front of it.
    """
    out = []
    if p.postcodes:
        out.append("postcode")
    if any(s.strong for s in p.states):
        out.append("state")
    if p.boxes:
        out.append("pobox")
    if p.top is not None and p.top.street is not None:
        if p.top.street.reading == "name+type":
            out.append("street+type")
        if p.numbers:
            out.append("number+street")
    return out


def route(p: Parsed | None) -> tuple[bool, list[str], str]:
    """(take the AU path, which triggers fired, why not if it did not)."""
    if p is None:
        return False, [], "empty"
    trig = triggers(p)
    if not trig:
        return False, [], "no AU signal"
    if p.top is None:
        return False, trig, "no locality hypothesis"
    if p.top.score < AU_MIN_SCORE:
        return False, trig, f"score {p.top.score:.2f} below floor"
    # A fuzzy locality is only worth trusting when the whole input is accounted
    # for. 'Victoria Australia' fuzzy-matches TORRITA and leaves AUSTRALIA
    # unexplained, which is a state name being read as a suburb.
    if p.top.locality.candidate.match == "fuzzy" and p.top.unexplained:
        return False, trig, "fuzzy locality, unexplained tokens"
    return True, trig, ""


def ladder(p: Parsed):
    """The AU ladder's result for this parse."""
    unit = next((u.value for u in p.units if u.kind == "unit"), None)
    num = p.numbers[0] if p.numbers else None
    return lookup(
        p.top,
        number_first=num.number_first if num else None,
        number_last=num.number_last if num else None,
        unit=unit,
        po_box=bool(p.boxes),
    )


def mapped_granularity(p: Parsed, r) -> str:
    """Where the AU ladder lands, in the response vocabulary."""
    if r.granularity != Rung.LOCALITY:
        return RUNG_TO_SCHEMA[r.granularity]
    # A locality the input never named came from the postcode alone, and a
    # postcode can span several localities. The input only proved the postcode.
    return "postcode" if len(p.top.locality.locality_span) == 0 else "locality"


def cases() -> list[tuple[str, str, str]]:
    rows = []
    with (REPO / "tests/golden/golden.csv").open(newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            rows.append(("golden", r["input"], r["expected_granularity"]))
    # Run as a script, so scripts/ is already sys.path[0].
    from phase2_report import AU_CASES

    seen = {r[1] for r in rows}
    return rows + [("probe", c, "") for c in AU_CASES if c not in seen]


def _confidence_row(text: str, take: bool, got: str, p: Parsed | None, r) -> str:
    """One row of the calibration table. World rows show their own confidence so
    the two paths can be compared on the same page."""
    if not take:
        w = pipeline.resolve_one(text)
        return (
            f"{text[:39]:<40} {'world':<6} {got:<11} {'':>7} {'':>7} "
            f"{'':>7} {'':>6} {'':>5} {w.confidence:>6.3f}  -"
        )
    assert p is not None and r is not None
    top = p.top
    score = top.score
    runner = p.runner_up
    has_street = top.street is not None
    full = SCORE_FULL_WITH_STREET if has_street else SCORE_FULL
    absolute = max(0.0, min(1.0, score / full))
    margin = max(0.0, (score - runner) / score) if runner is not None and score > 0 else None
    conf = au_confidence(score, runner, has_street=has_street, cap=r.confidence_cap)
    caps = [w for w in r.warnings if any(k in w for k in _CAP_WARNINGS)]
    return (
        f"{text[:39]:<40} {'AU':<6} {got:<11} {score:>7.3f} "
        f"{('-' if runner is None else f'{runner:7.3f}'):>7} "
        f"{('-' if margin is None else f'{min(margin, 1.0):7.3f}'):>7} "
        f"{absolute:>6.3f} {('-' if r.confidence_cap is None else f'{r.confidence_cap:5.2f}'):>5} "
        f"{conf:>6.3f}  {'; '.join(c.split(';')[0] for c in caps) or '-'}"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--failures", action="store_true", help="only rows missing golden")
    ap.add_argument("--confidence", action="store_true", help="the calibration table")
    args = ap.parse_args()

    au = load_au()
    known = frozenset(au.by_postcode)

    if args.confidence:
        header = (
            f"{'input':<40} {'path':<6} {'granularity':<11} {'score':>7} {'runner':>7} "
            f"{'margin':>7} {'abs':>6} {'cap':>5} {'conf':>6}  cap reason"
        )
    else:
        header = (
            f"{'src':<7} {'input':<40} {'route':<6} {'trigger':<27} "
            f"{'want':<10} {'got':<10} {'':<4} why"
        )

    lines, bad = [], 0
    for src, text, want in cases():
        p = Parsed(text, au, known) if text.strip() else None
        take, trig, why = route(p)
        r = None
        if take:
            assert p is not None
            r = ladder(p)
            got = mapped_granularity(p, r)
        else:
            got = pipeline.resolve_one(text).granularity.value
        flag = "" if not want else ("ok" if got == want else "FAIL")
        if flag == "FAIL":
            bad += 1

        if args.confidence:
            lines.append(_confidence_row(text, take, got, p, r))
        elif not args.failures or flag == "FAIL":
            lines.append(
                f"{src:<7} {text[:39]:<40} {'AU' if take else 'world':<6} "
                f"{'+'.join(trig)[:27]:<27} {want:<10} {got:<10} {flag:<4} {why}"
            )

    print(header)
    print("\n".join(lines))
    if args.confidence:
        print(
            f"\nreference: SCORE_FULL {SCORE_FULL:.2f}, "
            f"SCORE_FULL_WITH_STREET {SCORE_FULL_WITH_STREET:.2f}"
        )
    else:
        print(f"\n{bad} golden mismatches under this routing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
