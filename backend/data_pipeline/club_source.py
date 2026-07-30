"""
Sofascore team-level season statistics for the Big 5 — offline source.

The club tactical profiles that shipped with the project were authored, not
measured: 96 clubs shared 13 distinct trait requirement blocks, and 192 of the
306 evidence entries were the same two boilerplate sentences repeated. This
module fetches the numbers that let those profiles be derived instead.

Sofascore's team endpoint returns ~115 season fields per club, including several
that FBref's squad tables have no equivalent for:

  fastBreaks / fastBreakShots / fastBreakGoals   transitions, counted directly
  ownHalfPassesTotalAgainst                      where we let the opponent play
  totalPassesAgainst                             the numerator of PPDA
  bigChances / bigChancesCreated / Missed        chance quality

One request per club, 96 in total. Like every other Sofascore call in this
project it goes through botasaurus' browser because the API sits behind
Cloudflare and refuses plain HTTP. That is fine here: this runs offline, never
during a request.

Usage:
    python -m backend.data_pipeline.club_source            # fetch and stage
    python -m backend.data_pipeline.club_source --show Bologna
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import unicodedata
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
_mcp_dir = str(ROOT / "backend" / "mcp_server")
if _mcp_dir not in sys.path:
    sys.path.insert(0, _mcp_dir)

API = "https://api.sofascore.com/api/v1"

#: Sofascore league ids — same table sofascore_source.py uses for players, so a
#: club and its players are always drawn from the same competition id.
BIG5_LEAGUES: dict[str, int] = {
    "Premier League": 17,
    "La Liga":         8,
    "Serie A":        23,
    "Bundesliga":     35,
    "Ligue 1":        34,
}

#: Country per league, for the club metadata block.
LEAGUE_COUNTRY = {
    "Premier League": "England",
    "La Liga":        "Spain",
    "Serie A":        "Italy",
    "Bundesliga":     "Germany",
    "Ligue 1":        "France",
}

#: The season the player cohort covers. Scoring 2024-25 players against a
#: 2025/26 club profile is the mismatch this whole rebuild removes.
SEASON_LABEL = "24/25"

STAGE_PATH = ROOT / "data" / "_build_stage" / "club_stats_sofascore.json"

_REQUEST_GAP = 0.8


def _fold(name: str) -> str:
    """Diacritic-free, lowercased key. Mirrors sofascore_source._fold."""
    decomposed = unicodedata.normalize("NFKD", str(name))
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(stripped.lower().split())


def _get_json(url: str) -> dict:
    from ScraperFC.utils.botasaurus_getters import botasaurus_browser_get_json
    return botasaurus_browser_get_json(url)


def _season_id(league_id: int, season_label: str = SEASON_LABEL) -> int | None:
    data = _get_json(f"{API}/unique-tournament/{league_id}/seasons")
    for season in data.get("seasons", []):
        if season.get("year") == season_label:
            return season["id"]
    return None


def fetch_league(league_name: str, league_id: int,
                 season_label: str = SEASON_LABEL) -> dict[str, dict]:
    """Return {folded_club_name: {sofascore field: value}} for one league season."""
    season_id = _season_id(league_id, season_label)
    if not season_id:
        logger.warning(f"Sofascore: no {season_label} season for {league_name}")
        return {}

    try:
        teams = _get_json(
            f"{API}/unique-tournament/{league_id}/season/{season_id}/teams"
        ).get("teams", [])
    except Exception as exc:
        logger.warning(f"Sofascore: team list for {league_name} failed: {exc}")
        return {}

    clubs: dict[str, dict] = {}
    for team in teams:
        team_id, team_name = team.get("id"), team.get("name")
        if not team_id or not team_name:
            continue
        url = (f"{API}/team/{team_id}/unique-tournament/{league_id}"
               f"/season/{season_id}/statistics/overall")
        try:
            stats = _get_json(url).get("statistics") or {}
        except Exception as exc:
            logger.warning(f"  {team_name}: statistics failed ({exc})")
            stats = {}

        if not stats:
            logger.warning(f"  {team_name}: no statistics returned")
            continue

        # statisticsType is a nested descriptor, not a measurement.
        stats.pop("statisticsType", None)
        stats["_team_name"] = team_name
        stats["_team_id"] = team_id
        stats["_league"] = league_name
        stats["_country"] = LEAGUE_COUNTRY.get(league_name, "")
        stats["_season"] = season_label
        clubs[_fold(team_name)] = stats
        time.sleep(_REQUEST_GAP)

    logger.info(f"Sofascore {league_name}: {len(clubs)} clubs")
    return clubs


def fetch_big5(season_label: str = SEASON_LABEL) -> dict[str, dict]:
    """Fetch every Big-5 club, keyed by folded club name."""
    everything: dict[str, dict] = {}
    for league_name, league_id in BIG5_LEAGUES.items():
        try:
            everything.update(fetch_league(league_name, league_id, season_label))
        except Exception as exc:
            logger.warning(f"Sofascore club fetch failed for {league_name}: {exc}")
        time.sleep(_REQUEST_GAP)
    logger.info(f"Sofascore clubs total: {len(everything)}")
    return everything


def load_staged() -> dict[str, dict]:
    """Read the staged payload, or {} if it has not been fetched yet."""
    if not STAGE_PATH.exists():
        return {}
    with open(STAGE_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def build(force: bool = False, season_label: str = SEASON_LABEL) -> dict[str, dict]:
    """Fetch and stage. Reuses the staged file unless `force`."""
    if not force:
        staged = load_staged()
        if staged:
            logger.info(f"Using staged club stats ({len(staged)} clubs)")
            return staged

    clubs = fetch_big5(season_label)
    if clubs:
        STAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(STAGE_PATH, "w", encoding="utf-8") as fh:
            json.dump(clubs, fh, ensure_ascii=False, indent=1)
        logger.info(f"Staged {len(clubs)} clubs to {STAGE_PATH}")
    return clubs


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fresh", action="store_true", help="re-fetch even if staged")
    ap.add_argument("--show", metavar="CLUB", help="print one club's fields and exit")
    args = ap.parse_args()

    if args.show:
        clubs = load_staged()
        if not clubs:
            print("Nothing staged yet — run without --show first.")
            return 1
        key = _fold(args.show)
        match = clubs.get(key) or next(
            (v for k, v in clubs.items() if key in k), None
        )
        if not match:
            print(f"No club matching {args.show!r}. Have: {len(clubs)} clubs.")
            return 1
        for field in sorted(match):
            print(f"  {field} = {match[field]}")
        return 0

    clubs = build(force=args.fresh)
    print(f"{len(clubs)} clubs staged at {STAGE_PATH}")
    by_league: dict[str, int] = {}
    for club in clubs.values():
        by_league[club.get("_league", "?")] = by_league.get(club.get("_league", "?"), 0) + 1
    for league, count in sorted(by_league.items()):
        print(f"  {league}: {count}")
    return 0 if clubs else 1


if __name__ == "__main__":
    raise SystemExit(main())
