"""
Are the numbers actually right?

The pipeline now leans on three datasets that were all recently repaired: real
wages from Capology, season-aligned valuations from Transfermarkt, and per-club
finances derived from both. Each was broken in a way that looked like working
software — empty tables blamed on Cloudflare, valuations from the wrong year,
budgets that were a model's guess. So this checks them rather than trusting them.

Every check is deterministic and offline. It costs nothing to run and is meant
to be run before a demo or a deploy.

    python validate_data.py
    python validate_data.py --verbose    # list every offending row

Exit code is non-zero if any check fails, so it works in CI.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

DB = ROOT / "data" / "fbref_cache.db"
SEASON = "2425"

#: A Big-5 first-team wage outside this weekly band in EUR is a parsing error —
#: an annual figure, a transfer fee, or a squad total — not a wage.
WAGE_MIN, WAGE_MAX = 400, 700_000
#: Likewise for a market value.
VALUE_MIN, VALUE_MAX = 25_000, 300_000_000

#: Published figures used as ground truth. These are the ones that were most
#: badly wrong before the repairs, so they are the ones worth pinning.
KNOWN_WAGES = {
    # name, expected weekly EUR, tolerance
    "Erling Haaland":  (612_648, 0.15),
    "Mohamed Salah":   (466_779, 0.15),
    "Virgil van Dijk": (408_432, 0.15),
}
KNOWN_VALUES_2025 = {
    "Erling Haaland": (180_000_000, 0.20),
    "Mohamed Salah":  (50_000_000, 0.25),
}

failures: list[str] = []
warnings_: list[str] = []


def ok(msg: str) -> None:
    print(f"  [OK]   {msg}")


def warn(msg: str) -> None:
    warnings_.append(msg)
    print(f"  [WARN] {msg}")


def bad(msg: str) -> None:
    failures.append(msg)
    print(f"  [FAIL] {msg}")


def _norm(name: str) -> str:
    import unicodedata
    return "".join(
        c for c in unicodedata.normalize("NFKD", str(name).lower())
        if not unicodedata.combining(c)
    ).strip()


# ── wages ────────────────────────────────────────────────────────────────────

def check_wages(conn: sqlite3.Connection, verbose: bool) -> None:
    print("\nWages")
    rows = conn.execute(
        "SELECT name, squad, league, wage_eur FROM player_wages WHERE season = ?",
        (SEASON,),
    ).fetchall()
    if not rows:
        bad("player_wages is empty — every wage shown will be modelled")
        return
    ok(f"{len(rows)} wage rows")

    out_of_band = [r for r in rows if not (WAGE_MIN <= (r[3] or 0) <= WAGE_MAX)]
    if out_of_band:
        bad(f"{len(out_of_band)} wages outside EUR {WAGE_MIN:,}-{WAGE_MAX:,}/week")
        if verbose:
            for n, s, _l, w in out_of_band[:10]:
                print(f"         {n} ({s}): {w:,}")
    else:
        ok(f"every wage sits inside EUR {WAGE_MIN:,}-{WAGE_MAX:,}/week")

    # The failure mode worth catching is a tier estimate stamped on everyone:
    # one value covering a large share of the squad. A plain uniqueness ratio
    # does not detect that and flags healthy data — real Capology figures repeat
    # constantly because a GBP wage converted to EUR lands on the same rounded
    # number for everyone on the same contract (GBP 50k/wk is EUR 58,347 for 51
    # different players). Concentration is the honest test: on real data the
    # largest bucket is 1.7% of rows, where a tier fallback would be near 100%.
    from collections import Counter

    counts = Counter(r[3] for r in rows)
    top_value, top_n = counts.most_common(1)[0]
    share = top_n / len(rows)
    if share > 0.25:
        bad(f"EUR {top_value:,}/wk covers {share * 100:.0f}% of all rows — "
            "that is a tier estimate, not measured data")
    else:
        ok(f"{len(counts)} distinct values, largest bucket {share * 100:.1f}% "
           "(no single figure dominates)")

    by_name = {_norm(n): w for n, _s, _l, w in rows}
    for name, (expected, tol) in KNOWN_WAGES.items():
        got = by_name.get(_norm(name))
        if got is None:
            warn(f"{name}: no wage row to check against the published figure")
        elif abs(got - expected) / expected > tol:
            bad(f"{name}: EUR {got:,}/wk against a published EUR {expected:,} "
                f"({(got - expected) / expected * +100:+.0f}%)")
        else:
            ok(f"{name}: EUR {got:,}/wk matches the published figure")


# ── market values ────────────────────────────────────────────────────────────

def check_values(conn: sqlite3.Connection, verbose: bool) -> None:
    print("\nMarket values")
    rows = [json.loads(p) for (p,) in conn.execute(
        "SELECT payload FROM player_market_value")]
    if not rows:
        bad("player_market_value is empty")
        return

    cohort = conn.execute(
        "SELECT COUNT(*) FROM player_cohort WHERE season = ?", (SEASON,)
    ).fetchone()[0]
    aligned = [r for r in rows if r.get("market_value_asof_eur")]
    ok(f"{len(rows)} valuation rows, {len(aligned)} season-aligned "
       f"({len(aligned) / cohort * 100:.0f}% of the {cohort}-player cohort)")

    if len(aligned) < cohort * 0.5:
        warn(f"under half the cohort has a season-aligned valuation — the fee "
             f"gate cannot price the rest and will keep them unfiltered")

    out_of_band = [
        r for r in aligned
        if not (VALUE_MIN <= r["market_value_asof_eur"] <= VALUE_MAX)
    ]
    if out_of_band:
        bad(f"{len(out_of_band)} valuations outside EUR {VALUE_MIN:,}-{VALUE_MAX:,}")
        if verbose:
            for r in out_of_band[:10]:
                print(f"         {r.get('name')}: {r['market_value_asof_eur']:,}")
    else:
        ok("every season-aligned valuation is inside a plausible band")

    # The whole point of the backfill: these must not be today's numbers.
    dated = [r for r in aligned if r.get("market_value_asof_date")]
    wrong_era = [r for r in dated if not r["market_value_asof_date"].endswith(
        ("2024", "2025"))]
    if wrong_era:
        bad(f"{len(wrong_era)} valuations are dated outside 2024-2025")
        if verbose:
            for r in wrong_era[:10]:
                print(f"         {r.get('name')}: {r['market_value_asof_date']}")
    else:
        ok(f"all {len(dated)} dated valuations fall in 2024-2025")

    by_name = {_norm(r.get("query_name") or r.get("name") or ""): r for r in aligned}
    for name, (expected, tol) in KNOWN_VALUES_2025.items():
        row = by_name.get(_norm(name))
        if not row:
            warn(f"{name}: no season-aligned valuation to check")
            continue
        got = row["market_value_asof_eur"]
        if abs(got - expected) / expected > tol:
            bad(f"{name}: EUR {got / 1e6:.0f}M against an expected "
                f"EUR {expected / 1e6:.0f}M for mid-2025")
        else:
            ok(f"{name}: EUR {got / 1e6:.0f}M matches the mid-2025 figure")


# ── club finances ────────────────────────────────────────────────────────────

def check_clubs(conn: sqlite3.Connection, verbose: bool) -> None:
    print("\nClub finances")
    try:
        rows = [json.loads(p) for (p,) in conn.execute(
            "SELECT payload FROM club_finances WHERE season = ?", (SEASON,))]
    except sqlite3.OperationalError:
        bad("club_finances table does not exist — run backend.data_pipeline.club_finances")
        return
    if not rows:
        bad("club_finances is empty")
        return

    with_payroll = [r for r in rows if r.get("top_earner_weekly_eur")]
    with_spend = [r for r in rows if r.get("transfer_expenditure_eur") is not None]
    ok(f"{len(rows)} clubs, {len(with_payroll)} with payroll, "
       f"{len(with_spend)} with a transfer record")
    if len(with_spend) < len(rows):
        missing = sorted(r["club"] for r in rows if r.get("transfer_expenditure_eur") is None)
        warn(f"{len(missing)} clubs fall back to the generic fee rule: "
             f"{', '.join(missing[:6])}")

    # A top earner above the whole bill is arithmetically impossible.
    broken = [
        r for r in with_payroll
        if r["top_earner_weekly_eur"] > (r.get("weekly_wage_bill_eur") or 0)
    ]
    if broken:
        bad(f"{len(broken)} clubs have a top earner above their entire wage bill")
    else:
        ok("every top earner sits inside the club's wage bill")

    # A club whose ceiling is the league minimum is a sign the payroll join failed.
    tiny = [r for r in with_payroll if r["top_earner_weekly_eur"] < 5_000]
    if tiny:
        warn(f"{len(tiny)} clubs have a top earner under EUR 5,000/wk: "
             + ", ".join(r["club"] for r in tiny[:5]))
    else:
        ok("no club has an implausibly low ceiling")

    spread = sorted(r["top_earner_weekly_eur"] for r in with_payroll)
    if spread:
        ok(f"wage ceilings span EUR {spread[0]:,} to EUR {spread[-1]:,}/wk "
           f"({spread[-1] / max(spread[0], 1):.0f}x) — club-specific, not generic")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true", help="list offending rows")
    args = ap.parse_args()

    if not DB.exists():
        print(f"no cache at {DB}")
        return 1

    print("=" * 68)
    print("  Scoutly - data accuracy checks")
    print("=" * 68)

    conn = sqlite3.connect(str(DB), timeout=30)
    check_wages(conn, args.verbose)
    check_values(conn, args.verbose)
    check_clubs(conn, args.verbose)
    conn.close()

    print("\n" + "=" * 68)
    if failures:
        print(f"  {len(failures)} FAILED, {len(warnings_)} warning(s)")
        print("=" * 68)
        return 1
    print(f"  all checks passed, {len(warnings_)} warning(s)")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
