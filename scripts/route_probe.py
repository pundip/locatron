"""Show which path each input would take, and at what granularity it lands.

The evidence behind the routing rule in CLAUDE.md. Run it after any change to
the triggers, the gate or the granularity mapping: a rule that sends
'New York' down the Australian path, or 'Victoria Australia' to a locality,
is wrong however reasonable it reads.

    uv run python scripts/route_probe.py            # golden rows and probe cases
    uv run python scripts/route_probe.py --failures # only rows that miss golden

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
        joint = resolve_streets(self.ts, hyps, consumed=claimed)
        self.top = joint[0] if joint else None


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


def mapped_granularity(p: Parsed) -> str:
    """Where the AU ladder lands, in the response vocabulary."""
    unit = next((u.value for u in p.units if u.kind == "unit"), None)
    num = p.numbers[0] if p.numbers else None
    r = lookup(
        p.top,
        number_first=num.number_first if num else None,
        number_last=num.number_last if num else None,
        unit=unit,
        po_box=bool(p.boxes),
    )
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--failures", action="store_true", help="only rows missing golden")
    args = ap.parse_args()

    au = load_au()
    known = frozenset(au.by_postcode)

    header = (
        f"{'src':<7} {'input':<40} {'route':<6} {'trigger':<27} "
        f"{'want':<10} {'got':<10} {'':<4} why"
    )
    lines, bad = [], 0
    for src, text, want in cases():
        p = Parsed(text, au, known) if text.strip() else None
        take, trig, why = route(p)
        if take:
            assert p is not None
            got = mapped_granularity(p)
        else:
            got = pipeline.resolve_one(text).granularity.value
        flag = "" if not want else ("ok" if got == want else "FAIL")
        if flag == "FAIL":
            bad += 1
        elif args.failures:
            continue
        lines.append(
            f"{src:<7} {text[:39]:<40} {'AU' if take else 'world':<6} "
            f"{'+'.join(trig)[:27]:<27} {want:<10} {got:<10} {flag:<4} {why}"
        )

    print(header)
    print("\n".join(lines))
    print(f"\n{bad} golden mismatches under this routing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
