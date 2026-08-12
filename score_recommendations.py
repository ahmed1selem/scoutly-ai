"""
Did the players the system recommended go on to succeed?

predict_real_signings.py asks the harshest possible question — did the shortlist
contain the exact player one club happened to sign, out of a cohort of up to 870?
A recruitment department that surfaced four credible alternatives and missed the
one name would not consider that a failure, and neither should this.

So this asks the fairer version: of everyone the system put on a shortlist, what
share went on to a strong 2025-26 season? Measured against the base rate for all
Big-5 players, that says whether the recommendations carry signal.

Deterministic and free — it reads the shortlists from the newest prediction run
and scores them against the staged Sofascore outcomes. No LLM, no tokens.

    python score_recommendations.py
    python score_recommendations.py --run benchmarks/20260812_160412_predict
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

#: What counts as a strong season. Sofascore's league-wide average rating sits
#: near 6.8, so 7.0 is above par rather than exceptional, and 1,500 minutes is
#: roughly half a league season — enough that the rating is not noise.
MIN_MINUTES = 1500
GOOD_RATING = 7.0

OUTCOMES = ROOT / "data" / "_build_stage" / "player_outcomes_2526.json"


def _norm(name: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", str(name).lower())
        if not unicodedata.combining(c)
    ).strip()


def load_outcomes() -> dict[str, dict]:
    raw = json.load(open(OUTCOMES, encoding="utf-8"))
    rows = raw if isinstance(raw, list) else list(raw.values())
    return {
        _norm(r["name"]): r
        for r in rows if isinstance(r, dict) and r.get("name")
    }


def base_rate(outcomes: dict[str, dict]) -> tuple[int, int]:
    """How often ANY Big-5 player clears the bar — the number to beat."""
    strong = sum(
        1 for r in outcomes.values()
        if (r.get("minutes") or 0) >= MIN_MINUTES and (r.get("rating") or 0) >= GOOD_RATING
    )
    return strong, len(outcomes)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", help="prediction run directory (defaults to newest)")
    args = ap.parse_args()

    if args.run:
        path = Path(args.run) / "runs.json"
    else:
        found = sorted(glob.glob(str(ROOT / "benchmarks/*_predict/runs.json")),
                       key=os.path.getmtime)
        if not found:
            print("no prediction runs found — run predict_real_signings.py first")
            return 1
        path = Path(found[-1])

    runs = json.load(open(path, encoding="utf-8"))
    outcomes = load_outcomes()
    strong_all, total_all = base_rate(outcomes)
    rate = strong_all / total_all

    print("=" * 72)
    print(f"  Recommendation quality - {path.parent.name}")
    print("=" * 72)

    recommended = 0
    with_record = 0
    strong = 0
    for run in runs:
        club = run["brief"]["club"]
        picks = run.get("shortlist", [])
        if not picks:
            continue
        print(f"\n{club}:")
        for pick in picks:
            recommended += 1
            outcome = outcomes.get(_norm(pick["name"]))
            if not outcome:
                print(f"    {pick['name']:<26} no 2025-26 record")
                continue
            with_record += 1
            mins = outcome.get("minutes") or 0
            rating = outcome.get("rating") or 0
            good = mins >= MIN_MINUTES and rating >= GOOD_RATING
            strong += good
            print(f"    {pick['name']:<26} {rating:.2f} / {mins:>4} min"
                  f"{'   <-- strong' if good else ''}")

    print("\n" + "=" * 72)
    print(f"  recommended            : {recommended}")
    print(f"  with a 2025-26 record  : {with_record}")
    print(f"  went on to a strong season: {strong}")
    if with_record:
        hit = strong / with_record
        print(f"\n  recommendation hit rate : {hit * 100:.0f}%")
        print(f"  base rate for any Big-5 player: {rate * 100:.1f}% "
              f"({strong_all} of {total_all})")
        print(f"  lift                    : {hit / rate:.1f}x")
        if with_record < 30:
            print(f"\n  Caveat: {with_record} is a small sample. Treat the lift as "
                  "directional, not as a measured effect size.")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
