"""Show which path each input takes, and at what granularity and confidence.

The evidence behind the Routing section of CLAUDE.md. Run it after any change to
the triggers, the gate, the granularity mapping or the weights: a rule that sends
'New York' down the Australian path, or 'Victoria Australia' to a locality, is
wrong however reasonable it reads.

    uv run python scripts/route_probe.py             # golden rows and probe cases
    uv run python scripts/route_probe.py --failures  # only rows that miss golden
    uv run python scripts/route_probe.py --confidence # the calibration table

Every decision shown here is made by `locatron.resolve.au` and the granularity by
`locatron.resolve.pipeline`, not by a copy of either, so the table cannot drift
from what the resolver actually serves.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from locatron.parse.lookup import lookup
from locatron.parse.scoring import SCORE_FULL, SCORE_FULL_WITH_STREET
from locatron.resolve import au as au_path
from locatron.resolve import pipeline

REPO = Path(__file__).resolve().parent.parent

#: Warning fragments that correspond to a confidence ceiling, so the table can
#: name why a row was capped.
_CAP_WARNINGS = (
    "substituted",
    "the range",
    "not found",
    "alias of",
    "fell back to the street",
)


def cases() -> list[tuple[str, str, str]]:
    """(source, input, expected granularity) for the golden set plus the probes."""
    rows = []
    with (REPO / "tests/golden/golden.csv").open(newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            rows.append(("golden", r["input"], r["expected_granularity"]))
    # Run as a script, so scripts/ is already sys.path[0].
    from phase2_report import AU_CASES

    seen = {r[1] for r in rows}
    return rows + [("probe", c, "") for c in AU_CASES if c not in seen]


def ladder_result(p: au_path.AuParse):
    """The ladder's own result for a parse, so the table can show the cap."""
    top = p.top
    number = p.components.number
    return lookup(
        top,
        number_first=number.number_first if number else None,
        number_last=number.number_last if number else None,
        unit=p.components.unit,
        po_box=bool(p.components.boxes),
        street_centroid=(top.street.row.lat, top.street.row.lng) if top.street else None,
    )


def _route_row(src, text, want, got, take, trig, why) -> str:
    flag = "" if not want else ("ok" if got == want else "FAIL")
    return (
        f"{src:<7} {text[:39]:<40} {'AU' if take else 'world':<6} "
        f"{'+'.join(trig)[:27]:<27} {want:<10} {got:<10} {flag:<4} {why}"
    )


def _confidence_row(text, take, got, conf, p, result) -> str:
    """One row of the calibration table. A world row shows only its confidence, so
    the two paths can still be compared on the same page."""
    if not take or p is None or result is None:
        return (
            f"{text[:39]:<40} {'world':<6} {got:<11} {'':>7} {'':>7} "
            f"{'':>7} {'':>6} {'':>5} {conf:>6.3f}  -"
        )
    top = p.top
    score, runner = top.score, p.runner_up
    has_street = top.street is not None
    full = SCORE_FULL_WITH_STREET if has_street else SCORE_FULL
    absolute = max(0.0, min(1.0, score / full))
    margin = max(0.0, (score - runner) / score) if runner is not None and score > 0 else None
    caps = [w for w in result.warnings if any(k in w for k in _CAP_WARNINGS)]
    return (
        f"{text[:39]:<40} {'AU':<6} {got:<11} {score:>7.3f} "
        f"{('-' if runner is None else f'{runner:7.3f}'):>7} "
        f"{('-' if margin is None else f'{min(margin, 1.0):7.3f}'):>7} "
        f"{absolute:>6.3f} "
        f"{('-' if result.confidence_cap is None else f'{result.confidence_cap:5.2f}'):>5} "
        f"{conf:>6.3f}  {'; '.join(c.split(';')[0] for c in caps) or '-'}"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--failures", action="store_true", help="only rows missing golden")
    ap.add_argument("--confidence", action="store_true", help="the calibration table")
    args = ap.parse_args()

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
        p = au_path.parse(text) if text.strip() else None
        take, why = au_path.takes_au_path(p) if p is not None else (False, "empty")
        trig = au_path.triggers(p) if p is not None else ()

        # The served answer, so the table reports the pipeline rather than a
        # reimplementation of it.
        response = pipeline.resolve_one(text, record_unresolved=False)
        got = response.granularity.value
        missed = bool(want) and got != want
        bad += missed

        if args.confidence:
            result = ladder_result(p) if take and p is not None else None
            lines.append(_confidence_row(text, take, got, response.confidence, p, result))
        elif not args.failures or missed:
            lines.append(_route_row(src, text, want, got, take, trig, why))

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
