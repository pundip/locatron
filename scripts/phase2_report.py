#!/usr/bin/env python3
"""
Locatron phase 2 test report.

Runs a fixed series of checks and writes one markdown report that can be
pasted into a chat for review. Standard library only.

Put this file at scripts/phase2_report.py and run from the repo root:

    uv run python scripts/phase2_report.py
    uv run python scripts/phase2_report.py --deep --latency
    uv run python scripts/phase2_report.py --live --base-url https://urlloom.com/locatron \
        --header "X-Locatron-Edge: <value>"

Sections:
  1. Environment and git state
  2. locatron check (add --deep for the full mirror digest)
  3. pytest
  4. golden set, compared against a baseline
  5. parser probe (needs the `locatron parse` CLI command)
  6. latency_check.py (opt in with --latency)
  7. live API smoke test (opt in with --live)

Secrets are redacted from all captured output, including any --header value.
Exit code is 1 if any section FAILs, otherwise 0.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

AU_CASES = [
    # Prompt B and C required cases
    "65 clifton park drive 3201 carrum downs",
    "65 Clifton Park Dr Carrum Downs VIC 3201",
    "5/12 Smith Street Fitzroy VIC 3065",
    "Unit 5 12 Smith Street Fitzroy VIC 3065",
    "14-40 Wills Street Melbourne VIC 3000",
    "Clifton Park Drive Carrum Downs",
    "Carrum Downs VIC",
    "3201",
    "PO Box 45 World Square NSW 2002",
    "Hamilton Crescent Ryde NSW 2112",
    "Richmond",
    "Richmond VIC",
    "Perth 7300",
    # postcode gate and street type handling
    "12 Clifton Street 3201",
    "Richmond Road 3201",
    "12 Woolloomooloo Street 3201",
    "10 Simmons Street South Yarra VIC 3141",
    "10 Simmons Court South Yarra VIC 3141",
    # stripped leading zeros
    "800",
    "Darwin NT 800",
    "810 Stuart Highway Winnellie NT 0820",
    "810 Stuart Highway Winnellie",
    "200",
    "123",
]

LIVE_CASES = [
    "Greater Melbourne",
    "Sydney Australia",
    "Las Vegas",
    "Delhi",
    "Carrum Downs VIC",
    "3201",
]

DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) locatron-report/1"

# --------------------------------------------------------------------------
# Output cleaning
# --------------------------------------------------------------------------

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
KV_SECRET_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|api[_-]?key|x-locatron-edge|authorization)"
    r"(\s*[=:]\s*)(['\"]?)[^\s'\"]+"
)
URL_CRED_RE = re.compile(r"(\w+://[^:/\s@]+:)[^@\s]+(@)")
EXTRA_SECRETS: list[str] = []


def clean(text: str) -> str:
    text = ANSI_RE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    text = KV_SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}[REDACTED]", text)
    text = URL_CRED_RE.sub(r"\1[REDACTED]\2", text)
    for secret in EXTRA_SECRETS:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


def clip(text: str, max_lines: int, keep: str = "tail") -> str:
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    cut = len(lines) - max_lines
    if keep == "head":
        return "\n".join(lines[:max_lines] + [f"... [{cut} more lines clipped]"])
    if keep == "both":
        head = max_lines // 3
        tail = max_lines - head
        return "\n".join(lines[:head] + [f"... [{cut} lines clipped] ..."] + lines[-tail:])
    return "\n".join([f"[{cut} earlier lines clipped] ..."] + lines[-max_lines:])


def fence(text: str) -> str:
    return "```text\n" + (text.rstrip() or "(no output)") + "\n```"


# --------------------------------------------------------------------------
# Running commands
# --------------------------------------------------------------------------


@dataclass
class Result:
    cmd: str
    rc: int | None
    out: str
    secs: float

    @property
    def ok(self) -> bool:
        return self.rc == 0


def _decode(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _display(args: list[str], max_args: int = 8) -> str:
    shown = [f'"{a}"' if " " in a else a for a in args[:max_args]]
    if len(args) > max_args:
        shown.append(f"... (+{len(args) - max_args} more args)")
    return " ".join(shown)


def run(args: list[str], timeout: int = 600) -> Result:
    env = dict(
        os.environ,
        PYTHONIOENCODING="utf-8",
        NO_COLOR="1",
        FORCE_COLOR="0",
        PY_COLORS="0",
        TERM="dumb",
    )
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            args,
            cwd=REPO,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        rc = proc.returncode
        out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
    except subprocess.TimeoutExpired as exc:
        rc = None
        out = f"TIMEOUT after {timeout}s\n{_decode(exc.stdout)}{_decode(exc.stderr)}"
    except FileNotFoundError as exc:
        rc = None
        out = f"COMMAND NOT FOUND: {exc}"
    return Result(_display(args), rc, clean(out).strip(), time.perf_counter() - started)


def uv(*args: str, timeout: int = 600) -> Result:
    return run(["uv", "run", *args], timeout=timeout)


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, float, str]] = []
        self.sections: list[str] = []

    def add(self, name: str, status: str, secs: float, note: str, body: str) -> None:
        self.rows.append((name, status, secs, note))
        self.sections.append(f"## {name}\n\n**{status}**" + (f" · {note}" if note else "") + f"\n\n{body}\n")

    @property
    def failed(self) -> bool:
        return any(status == "FAIL" for _, status, _, _ in self.rows)

    def render(self, header: str) -> str:
        table = ["| Section | Status | Time | Note |", "|---|---|---|---|"]
        for name, status, secs, note in self.rows:
            table.append(f"| {name} | {status} | {secs:.1f}s | {note} |")
        return header + "\n\n" + "\n".join(table) + "\n\n" + "\n".join(self.sections)


def status_of(result: Result) -> str:
    if result.rc is None:
        return "ERROR"
    return "PASS" if result.ok else "FAIL"


# --------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------


def section_environment(report: Report, expected_branch: str) -> None:
    started = time.perf_counter()
    branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"]).out
    head = run(["git", "log", "-1", "--format=%h %s"]).out
    dirty = run(["git", "status", "--porcelain"]).out
    recent = run(["git", "log", "--oneline", "-12"]).out
    uv_version = run(["uv", "--version"]).out

    notes = []
    status = "PASS"
    if branch != expected_branch:
        status, notes = "WARN", notes + [f"branch is {branch}, expected {expected_branch}"]
    dirty_count = len([line for line in dirty.splitlines() if line.strip()])
    if dirty_count:
        status, notes = "WARN", notes + [f"{dirty_count} uncommitted changes"]

    body = "\n".join(
        [
            f"- Host: {platform.node()} ({platform.platform()})",
            f"- Python: {sys.version.split()[0]}",
            f"- uv: {uv_version}",
            f"- Branch: {branch}",
            f"- HEAD: {head}",
            "",
            "Recent commits:",
            fence(recent),
        ]
        + (["", "Uncommitted changes:", fence(dirty)] if dirty_count else [])
    )
    report.add("Environment", status, time.perf_counter() - started, "; ".join(notes), body)


def section_check(report: Report, deep: bool) -> None:
    args = ["locatron", "check"] + (["--deep"] if deep else [])
    result = uv(*args, timeout=300)
    report.add(
        "locatron check" + (" --deep" if deep else ""),
        status_of(result),
        result.secs,
        "",
        f"`{result.cmd}`\n\n" + fence(clip(result.out, 80, "both")),
    )


def section_pytest(report: Report) -> None:
    result = uv("pytest", "-q", "-rfE", timeout=900)
    summary = ""
    for line in reversed(result.out.splitlines()):
        if re.search(r"\b(passed|failed|error)\b", line):
            summary = line.strip(" =")
            break
    report.add("pytest", status_of(result), result.secs, summary, f"`{result.cmd}`\n\n" + fence(clip(result.out, 80)))


def section_golden(report: Report, baseline: int) -> None:
    result = uv("locatron", "golden", timeout=600)
    matches = [
        (int(a), int(b)) for a, b in re.findall(r"\b(\d+)\s*/\s*(\d+)\b", result.out) if int(b) >= 10
    ]
    if result.rc is None:
        status, note = "ERROR", "did not complete"
    elif not matches:
        status, note = "WARN", "could not find an N/M score in the output"
    else:
        passed, total = matches[-1]
        if passed < baseline:
            status, note = "FAIL", f"{passed}/{total}, REGRESSION below baseline {baseline}"
        elif passed > baseline:
            status, note = "PASS", f"{passed}/{total}, above baseline {baseline}"
        else:
            status, note = "PASS", f"{passed}/{total}, matches baseline"
    report.add("golden", status, result.secs, note, f"`{result.cmd}`\n\n" + fence(clip(result.out, 120, "both")))


def section_parse(report: Report, cases: list[str], max_lines: int) -> None:
    probe = uv("locatron", "parse", "--help", timeout=120)
    if not probe.ok:
        report.add(
            "parser probe",
            "SKIP",
            probe.secs,
            "`locatron parse` command not available",
            "Ask Claude Code to add the `locatron parse` CLI command, then rerun.\n\n"
            + fence(clip(probe.out, 20)),
        )
        return
    result = uv("locatron", "parse", *cases, timeout=900)
    body = (
        f"{len(cases)} inputs:\n\n"
        + "\n".join(f"{i}. `{case}`" for i, case in enumerate(cases, 1))
        + "\n\nOutput:\n\n"
        + fence(clip(result.out, max_lines, "head"))
    )
    report.add("parser probe", status_of(result), result.secs, f"{len(cases)} inputs", body)


def section_latency(report: Report) -> None:
    script = REPO / "scripts" / "latency_check.py"
    if not script.exists():
        report.add("latency_check", "SKIP", 0.0, "scripts/latency_check.py not found", "")
        return
    result = uv("python", str(script.relative_to(REPO)), timeout=300)
    report.add("latency_check", status_of(result), result.secs, "", f"`{result.cmd}`\n\n" + fence(clip(result.out, 60)))


def _http_get(url: str, headers: dict[str, str], timeout: int = 20) -> tuple[int | None, float, str]:
    request = urllib.request.Request(url, headers=headers, method="GET")
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            return response.status, (time.perf_counter() - started) * 1000, body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return exc.code, (time.perf_counter() - started) * 1000, body
    except Exception as exc:  # network errors, TLS, timeouts
        return None, (time.perf_counter() - started) * 1000, f"{type(exc).__name__}: {exc}"


def _compact(body: str, limit: int = 600) -> str:
    try:
        text = json.dumps(json.loads(body), separators=(",", ":"), ensure_ascii=False)
    except (ValueError, TypeError):
        text = " ".join(body.split())
    text = clean(text)
    return text if len(text) <= limit else text[:limit] + " ...[truncated]"


def section_live(report: Report, base_url: str, headers: dict[str, str]) -> None:
    started = time.perf_counter()
    base = base_url.rstrip("/")
    lines: list[str] = []
    timings: list[float] = []
    failures = 0

    code, ms, body = _http_get(f"{base}/healthz", headers)
    lines.append(f"GET /healthz -> {code} in {ms:.0f} ms\n  {_compact(body, 200)}")
    if code != 200:
        failures += 1

    for text in LIVE_CASES:
        url = f"{base}/v1/resolve?text={urllib.parse.quote_plus(text)}"
        code, ms, body = _http_get(url, headers)
        if code == 200:
            timings.append(ms)
        else:
            failures += 1
        lines.append(f"GET /v1/resolve?text={text!r} -> {code} in {ms:.0f} ms\n  {_compact(body)}")

    note = f"{failures} non-200 responses"
    if timings:
        note += f"; median {statistics.median(timings):.0f} ms round trip"
    if any(" -> 403 " in line for line in lines):
        note += "; 403 is likely Cloudflare bot protection"
    status = "PASS" if failures == 0 else "FAIL"
    report.add(f"live API ({base})", status, time.perf_counter() - started, note, fence("\n\n".join(lines)))


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Locatron phase 2 checks and write a markdown report.")
    parser.add_argument("--out", type=Path, help="report path (default: phase2-report-<timestamp>.md in repo root)")
    parser.add_argument("--deep", action="store_true", help="run `locatron check --deep` (full mirror digest)")
    parser.add_argument("--latency", action="store_true", help="run scripts/latency_check.py")
    parser.add_argument("--live", action="store_true", help="smoke test the deployed API")
    parser.add_argument("--base-url", default="https://urlloom.com/locatron", help="API base for --live")
    parser.add_argument("--header", action="append", default=[], help='extra header for --live, "Name: value" (repeatable)')
    parser.add_argument("--ua", default=DEFAULT_UA, help="User-Agent for --live")
    parser.add_argument("--golden-baseline", type=int, default=23, help="golden passes expected (default 23)")
    parser.add_argument("--cases", type=Path, help="file of parser inputs, one per line, replacing the built-in list")
    parser.add_argument("--parse-lines", type=int, default=600, help="max lines of parser output kept (default 600)")
    parser.add_argument("--skip-tests", action="store_true", help="skip pytest (faster reruns)")
    parser.add_argument("--branch", default="master", help="expected git branch (default master)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if shutil.which("uv") is None:
        print("uv is not on PATH. Run this from a shell where `uv --version` works.", file=sys.stderr)
        return 2
    if not (REPO / "pyproject.toml").exists():
        print(f"Expected the repo root at {REPO}; put this file in scripts/.", file=sys.stderr)
        return 2

    headers = {"User-Agent": args.ua, "Accept": "application/json"}
    for raw in args.header:
        name, sep, value = raw.partition(":")
        if not sep:
            print(f"Ignoring malformed --header {raw!r}; use 'Name: value'", file=sys.stderr)
            continue
        headers[name.strip()] = value.strip()
        EXTRA_SECRETS.append(value.strip())

    cases = AU_CASES
    if args.cases:
        cases = [line.strip() for line in args.cases.read_text(encoding="utf-8").splitlines() if line.strip()]

    report = Report()
    steps = [
        ("environment", lambda: section_environment(report, args.branch)),
        ("locatron check", lambda: section_check(report, args.deep)),
    ]
    if not args.skip_tests:
        steps.append(("pytest", lambda: section_pytest(report)))
    steps += [
        ("golden", lambda: section_golden(report, args.golden_baseline)),
        ("parser probe", lambda: section_parse(report, cases, args.parse_lines)),
    ]
    if args.latency:
        steps.append(("latency", lambda: section_latency(report)))
    if args.live:
        steps.append(("live API", lambda: section_live(report, args.base_url, headers)))

    for label, step in steps:
        print(f"-> {label} ...", flush=True)
        try:
            step()
        except Exception as exc:  # keep going, record the crash in the report
            report.add(label, "ERROR", 0.0, f"{type(exc).__name__} in report script", fence(clean(repr(exc))))

    now = dt.datetime.now()
    header = (
        f"# Locatron phase 2 report\n\n"
        f"Generated {now:%Y-%m-%d %H:%M:%S} on {platform.node()}"
    )
    out_path = args.out or REPO / f"phase2-report-{now:%Y%m%d-%H%M}.md"
    out_path.write_text(report.render(header), encoding="utf-8")

    print()
    for name, status, secs, note in report.rows:
        print(f"  {status:5}  {name:32} {secs:6.1f}s  {note}")
    print(f"\nReport written to {out_path}")
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())