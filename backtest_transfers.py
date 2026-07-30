"""
Backtest: would the system have found the players clubs actually signed?

The pipeline only ever sees 2024-25. Summer 2025 is the window that followed it,
so every completed transfer is a decision a real recruitment department made on
the same evidence. Where the buyer got it right, the system had the chance to
get it right too — and this measures whether it did.

Method, deliberately free of opinion:

  1. A transfer is detected, not looked up: a player whose Sofascore 2025-26 club
     differs from their 2024-25 club in our cache moved between the two seasons.
  2. "Performed well" is the 2025-26 outcome, not a pundit's grade — minutes and
     Sofascore match rating at the new club.
  3. The prediction is the deterministic core only. compute_fit_verdict scores
     the player against the buying club's derived profile using 2024-25 data,
     and their rank is taken within the cohort the scouter would actually pull.

Ranking is reported twice: within the buying club's own league, which is what
the scouter restricts itself to today, and across the whole Big 5. The gap
between those two numbers is the cost of that restriction.

    python backtest_transfers.py --fetch      # pull 2025-26 outcomes (slow)
    python backtest_transfers.py              # score against the staged file
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend" / "mcp_server"))

from backend.agents.scouter import _SAME_LEAGUE_BONUS as SAME_LEAGUE_BONUS  # noqa: E402
from backend.data_pipeline.club_names import fold, reconcile          # noqa: E402
from backend.engine.fit_engine import compute_fit_verdict             # noqa: E402
from backend.engine.trait_engine import compute_trait_scores          # noqa: E402
from backend.rag.vectorstore import get_vectorstore                   # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logging.getLogger("backend").setLevel(logging.WARNING)
logger = logging.getLogger("backtest")

API = "https://api.sofascore.com/api/v1"
BIG5 = {"Premier League": 17, "La Liga": 8, "Serie A": 23,
        "Bundesliga": 35, "Ligue 1": 34}
OUTCOME_SEASON = "25/26"
STAGE = ROOT / "data" / "_build_stage" / "player_outcomes_2526.json"
CACHE_DB = ROOT / "data" / "fbref_cache.db"

#: What counts as having performed. A season of football at a decent standard —
#: Sofascore's league-wide average rating sits near 6.8, so 7.0 is above par
#: rather than exceptional, and 1,500 minutes is roughly half a league season.
MIN_MINUTES = 1500
GOOD_RATING = 7.00

#: The fit engine's own position buckets, keyed by the FBref position code.
_POSITION_LABEL = {"GK": "goalkeeper", "DF": "centre_back",
                   "MF": "midfielder", "FW": "striker"}


def _get_json(url: str) -> dict:
    from ScraperFC.utils.botasaurus_getters import botasaurus_browser_get_json
    return botasaurus_browser_get_json(url)


def fetch_outcomes() -> dict[str, dict]:
    """{folded player name: {team, rating, minutes, league}} for 2025-26."""
    out: dict[str, dict] = {}
    for league, league_id in BIG5.items():
        seasons = _get_json(f"{API}/unique-tournament/{league_id}/seasons")["seasons"]
        season_id = next((s["id"] for s in seasons if s["year"] == OUTCOME_SEASON), None)
        if not season_id:
            logger.warning(f"no {OUTCOME_SEASON} season for {league}")
            continue

        offset, pages = 0, None
        while True:
            url = (f"{API}/unique-tournament/{league_id}/season/{season_id}/statistics"
                   f"?limit=100&offset={offset}&accumulation=total"
                   f"&fields=rating%2CminutesPlayed%2Cappearances"
                   f"&filters=position.in.G~D~M~F")
            try:
                data = _get_json(url)
            except Exception as exc:
                logger.warning(f"{league} offset {offset} failed: {exc}")
                break
            rows = data.get("results") or []
            for row in rows:
                name = (row.get("player") or {}).get("name")
                if not name:
                    continue
                out[fold(name)] = {
                    "name": name,
                    "team": (row.get("team") or {}).get("name", ""),
                    "league": league,
                    "rating": row.get("rating"),
                    "minutes": row.get("minutesPlayed") or 0,
                }
            pages, page = data.get("pages", 0), data.get("page", 0)
            if not rows or not pages or page >= pages:
                break
            offset += 100
            time.sleep(0.6)
        logger.info(f"{league}: {len(out)} players so far")

    STAGE.parent.mkdir(parents=True, exist_ok=True)
    with open(STAGE, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False)
    logger.info(f"staged {len(out)} outcomes to {STAGE}")
    return out


def load_cohort() -> list[dict]:
    """Every cached 2024-25 player, with trait scores computed."""
    conn = sqlite3.connect(str(CACHE_DB))
    try:
        rows = conn.execute(
            "SELECT raw_data FROM player_cohort WHERE season = '2425'").fetchall()
    finally:
        conn.close()

    players = []
    for (raw,) in rows:
        p = json.loads(raw)
        p["trait_scores"] = compute_trait_scores(
            p.get("raw_metrics", {}), p.get("position", "MF"), {})
        players.append(p)
    return players


def find_transfers(players: list[dict], outcomes: dict[str, dict],
                   club_names: list[str]) -> list[dict]:
    """
    Players whose 2025-26 club differs from their 2024-25 one.

    Sofascore and FBref name clubs differently, so both sides go through the
    same reconciliation the profile builder uses — otherwise every player at
    "FC Bayern München" reads as a transfer from "Bayern Munich".
    """
    sofa_clubs = sorted({o["team"] for o in outcomes.values() if o["team"]})
    mapping, unmatched, _ = reconcile(sofa_clubs, club_names)
    for name in unmatched:
        logger.debug(f"2025-26 club not in the 2024-25 Big 5: {name}")

    transfers = []
    for p in players:
        outcome = outcomes.get(fold(p["name"]))
        if not outcome:
            continue                       # left the Big 5, retired, or injured out
        old_club = p.get("squad", "")
        new_club = mapping.get(outcome["team"])
        if not new_club:
            continue                       # joined a promoted club with no profile
        if fold(new_club) == fold(old_club):
            continue                       # stayed
        transfers.append({**p, "_outcome": outcome, "_new_club": new_club,
                          "_old_club": old_club})
    return transfers


def rank_within(player: dict, pool: list[dict], profile: dict,
                home_league: str = "", bonus: float = 0.0) -> tuple[int, int, float]:
    """
    Where this player would sit in a shortlist drawn from `pool`.

    `bonus` mirrors the scouter's same-league preference so the ranking here is
    the ranking the pipeline would actually produce.
    """
    scored = []
    for other in pool:
        verdict = compute_fit_verdict(other, profile)
        score = verdict.get("demonstrated_fit") or 0.0
        if bonus and home_league and home_league.lower() in (other.get("league", "") or "").lower():
            score += bonus
        scored.append((other["name"], score))
    scored.sort(key=lambda kv: -kv[1])
    for idx, (name, fit) in enumerate(scored, start=1):
        if name == player["name"]:
            return idx, len(scored), fit
    return len(scored), len(scored), 0.0


def backtest(min_minutes: int = MIN_MINUTES, good_rating: float = GOOD_RATING,
             limit: int | None = None) -> list[dict]:
    if not STAGE.exists():
        raise SystemExit("No staged outcomes. Run with --fetch first.")
    with open(STAGE, encoding="utf-8") as fh:
        outcomes = json.load(fh)

    players = load_cohort()
    vs = get_vectorstore()
    profiles = {fold(p["club"]["club_name"]): p for p in vs._payloads.values()}
    club_names = [p["club"]["club_name"] for p in vs._payloads.values()]

    transfers = find_transfers(players, outcomes, club_names)
    logger.info(f"{len(transfers)} transfers detected between 2024-25 and 2025-26")

    successes = [
        t for t in transfers
        if (t["_outcome"]["minutes"] or 0) >= min_minutes
        and float(t["_outcome"]["rating"] or 0) >= good_rating
    ]
    successes.sort(key=lambda t: -float(t["_outcome"]["rating"] or 0))
    if limit:
        successes = successes[:limit]
    logger.info(f"{len(successes)} of them played {min_minutes}+ minutes at "
                f"{good_rating}+ rating")

    results = []
    for t in successes:
        profile = profiles.get(fold(t["_new_club"]))
        if not profile:
            continue

        position = (t.get("position") or "MF").split(",")[0].strip().upper()
        same_position = [
            p for p in players
            if (p.get("position") or "").split(",")[0].strip().upper() == position
            and int(p.get("minutes", 0)) >= 450
        ]
        buying_league = profile["club"]["league"]
        in_league = [p for p in same_position
                     if buying_league.lower() in (p.get("league", "") or "").lower()
                     and fold(p.get("squad", "")) != fold(t["_new_club"])]
        big5 = [p for p in same_position
                if fold(p.get("squad", "")) != fold(t["_new_club"])]

        league_rank, league_n, _ = rank_within(t, in_league, profile)
        # The scouter now ranks the whole Big 5 with a same-league bonus, so the
        # pool that decides whether a player is ever seen is this one.
        big5_rank, big5_n, _ = rank_within(
            t, big5, profile,
            home_league=buying_league, bonus=SAME_LEAGUE_BONUS)
        # Take the fit from its own verdict, not from rank_within: a player
        # outside the league pool is absent from that ranking and would report
        # a fit of zero when the score is perfectly well defined.
        verdict = compute_fit_verdict(t, profile)
        fit = verdict.get("demonstrated_fit") or 0.0

        results.append({
            "player": t["name"],
            "position": position,
            "from": t["_old_club"],
            "to": t["_new_club"],
            "to_style": profile["team_identity"]["primary_style"]["value"],
            "outcome_rating": float(t["_outcome"]["rating"] or 0),
            "outcome_minutes": int(t["_outcome"]["minutes"] or 0),
            "fit": round(fit, 1),
            "coverage": verdict.get("trait_coverage"),
            "same_league": t["_old_club"] and buying_league.lower() in
                           (t.get("league", "") or "").lower(),
            "league_rank": league_rank, "league_pool": league_n,
            "big5_rank": big5_rank, "big5_pool": big5_n,
            # Rank alone is not comparable across positions: a goalkeeper cohort
            # is 25 deep in one league and a midfield cohort is 200.
            "league_pctile": round((1 - (league_rank - 1) / max(league_n - 1, 1)) * 100),
            "big5_pctile": round((1 - (big5_rank - 1) / max(big5_n - 1, 1)) * 100),
        })

    # A player who changed clubs mid-2024-25 has two cohort rows and would be
    # counted twice for the same move.
    deduped, seen = [], set()
    for r in results:
        key = (r["player"], r["to"])
        if key not in seen:
            seen.add(key)
            deduped.append(r)
    return deduped


def report(results: list[dict], slate_size: int = 30) -> None:
    if not results:
        print("No qualifying transfers found.")
        return

    print(f"\n{'player':22s} {'pos':4s} {'from -> to':32s} {'25/26':>12s} "
          f"{'fit':>5s} {'searchable pool':>18s} {'Big 5':>16s}")
    print("-" * 118)
    for r in results:
        move = f"{r['from']} -> {r['to']}"
        outcome = f"{r['outcome_rating']:.2f}/{r['outcome_minutes']}"
        league = (f"{r['league_rank']}/{r['league_pool']} (p{r['league_pctile']})"
                  if r["same_league"] else "NOT SEARCHED")
        big5 = f"{r['big5_rank']}/{r['big5_pool']} (p{r['big5_pctile']})"
        print(f"{r['player'][:21]:22s} {r['position']:4s} {move[:31]:32s} "
              f"{outcome:>12s} {r['fit']:5.1f} {league:>18s} {big5:>16s}")

    n = len(results)
    cross = sum(1 for r in results if not r["same_league"])
    decile = [r for r in results if r["big5_pctile"] >= 90]
    quartile = [r for r in results if r["big5_pctile"] >= 75]
    # The slate the model is shown. A signing outside it was never a candidate.
    in_slate = [r for r in results if r["big5_rank"] <= slate_size]

    print(f"\n{n} successful summer-2025 signings scored against the Big-5 cohort "
          f"(same-league bonus {SAME_LEAGUE_BONUS:+.0f}).\n")
    print(f"  top decile of the cohort:   {len(decile)} ({len(decile) / n * 100:.0f}%)")
    print(f"  top quartile of the cohort: {len(quartile)} ({len(quartile) / n * 100:.0f}%)")
    print(f"  inside the top {slate_size} the scouter prices and shows: "
          f"{len(in_slate)} ({len(in_slate) / n * 100:.0f}%)")
    print(f"\n  {cross} of {n} ({cross / n * 100:.0f}%) were cross-league moves, "
          f"all of them unreachable before the league gate became a preference.")
    if cross:
        same_r = [r["outcome_rating"] for r in results if r["same_league"]]
        cross_r = [r["outcome_rating"] for r in results if not r["same_league"]]
        if same_r and cross_r:
            print(f"  their 2025-26 ratings: {sum(cross_r) / len(cross_r):.3f} "
                  f"against {sum(same_r) / len(same_r):.3f} for same-league "
                  f"signings — no penalty for shopping abroad.")


def correlate(min_minutes: int = 900) -> None:
    """
    Does the fit score predict the outcome across ALL transfers, not just good ones?

    Reporting only successful signings conditions on the outcome: it can show
    the system ranks good moves highly while saying nothing about whether it
    also ranks bad ones highly. This scores every detected transfer with enough
    2025-26 minutes to have a meaningful rating, and correlates fit against it.
    """
    with open(STAGE, encoding="utf-8") as fh:
        outcomes = json.load(fh)

    players = load_cohort()
    vs = get_vectorstore()
    profiles = {fold(p["club"]["club_name"]): p for p in vs._payloads.values()}
    club_names = [p["club"]["club_name"] for p in vs._payloads.values()]
    transfers = find_transfers(players, outcomes, club_names)

    pairs, seen = [], set()
    for t in transfers:
        outcome = t["_outcome"]
        if (outcome["minutes"] or 0) < min_minutes or not outcome["rating"]:
            continue
        key = (t["name"], t["_new_club"])
        if key in seen:
            continue
        seen.add(key)
        profile = profiles.get(fold(t["_new_club"]))
        if not profile:
            continue
        verdict = compute_fit_verdict(t, profile)
        fit = verdict.get("demonstrated_fit")
        if fit is None:
            continue
        pairs.append((float(fit), float(outcome["rating"])))

    if len(pairs) < 10:
        print(f"\nOnly {len(pairs)} transfers with {min_minutes}+ minutes — too few "
              f"to correlate.")
        return

    def _rank(values: list[float]) -> list[float]:
        order = sorted(range(len(values)), key=lambda i: values[i])
        ranks = [0.0] * len(values)
        i = 0
        while i < len(order):            # average ranks within ties
            j = i
            while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
                j += 1
            shared = (i + j) / 2 + 1
            for k in range(i, j + 1):
                ranks[order[k]] = shared
            i = j + 1
        return ranks

    fits = _rank([p[0] for p in pairs])
    rates = _rank([p[1] for p in pairs])
    n = len(pairs)
    mean_f, mean_r = sum(fits) / n, sum(rates) / n
    cov = sum((f - mean_f) * (r - mean_r) for f, r in zip(fits, rates))
    var_f = sum((f - mean_f) ** 2 for f in fits) ** 0.5
    var_r = sum((r - mean_r) ** 2 for r in rates) ** 0.5
    rho = cov / (var_f * var_r) if var_f and var_r else 0.0

    top = sorted(pairs, key=lambda p: -p[0])[: max(n // 4, 1)]
    bottom = sorted(pairs, key=lambda p: p[0])[: max(n // 4, 1)]
    print(f"\nPredictive validity over all {n} transfers with {min_minutes}+ "
          f"minutes in 2025-26:")
    print(f"  Spearman rho between fit and outcome rating: {rho:+.3f}")
    print(f"  mean rating, top quartile by fit:    {sum(p[1] for p in top) / len(top):.3f}")
    print(f"  mean rating, bottom quartile by fit: "
          f"{sum(p[1] for p in bottom) / len(bottom):.3f}")


def main() -> int:
    # Player names carry diacritics and the console here is cp1252, which raises
    # on the first accented character rather than dropping it.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fetch", action="store_true", help="pull 2025-26 outcomes first")
    ap.add_argument("--min-minutes", type=int, default=MIN_MINUTES)
    ap.add_argument("--rating", type=float, default=GOOD_RATING)
    ap.add_argument("--limit", type=int, default=None, help="score only the best N")
    ap.add_argument("--json", type=Path, help="also write results as JSON")
    ap.add_argument("--correlate", action="store_true",
                    help="also test fit against outcome over every transfer, "
                         "not only the successful ones")
    args = ap.parse_args()

    if args.fetch:
        fetch_outcomes()

    results = backtest(args.min_minutes, args.rating, args.limit)
    report(results)
    if args.correlate:
        correlate()
    if args.json:
        args.json.write_text(json.dumps(results, ensure_ascii=False, indent=1),
                             encoding="utf-8")
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
