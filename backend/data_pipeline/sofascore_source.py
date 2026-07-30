"""
Sofascore season statistics — the source for everything FBref will not serve.

FBref returns five columns entirely empty to this scraper (tackles, pass
completion, Att Pen touches, SCA, GCA), reproducibly across separate scrapes.
That left centre-back and striker briefs partially scorable and midfielder and
goalkeeper briefs not scorable at all — every candidate at zero trait coverage.

Sofascore publishes the same season on a JSON API, including several things
FBref never gave us: duels, aerial duels, clearances and goalkeeper saves. Those
cover traits every club profile asks for and nothing could previously score.

The API is behind Cloudflare and refuses plain HTTP (403 on every host), so it is
fetched through botasaurus' browser like the FBref path. That is acceptable here
because this module only ever runs in the offline builder — serving a request
still touches nothing but SQLite.

Run via the cache builder:
    python -m backend.data_pipeline.cache_builder --sofascore-only
"""
from __future__ import annotations

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

#: Sofascore's league ids, from ScraperFC's comps.yaml.
BIG5_LEAGUES = {
    "Premier League": 17,
    "La Liga":         8,
    "Serie A":        23,
    "Bundesliga":     35,
    "Ligue 1":        34,
}

#: Sofascore season label for the season our cohort covers.
SEASON_LABEL = "24/25"

# ── Field mapping ─────────────────────────────────────────────────────────────
# left: our canonical metric name, right: Sofascore's field.
# Season totals, converted to per-90 by the caller using minutesPlayed.
DIRECT_FIELDS: dict[str, str] = {
    "tackles":        "tackles",
    "interceptions":  "interceptions",
    "clearances":     "clearances",
    "key_passes":     "keyPasses",
    "saves":          "saves",
    "shots":          "totalShots",
    "shots_on_target": "shotsOnTarget",
    "goals":          "goals",
    "assists":        "assists",
    "big_chances_created": "bigChancesCreated",
    "possession_won_att_third": "possessionWonAttThird",
    # Stands in for FBref's Att Pen touches, which no source we have supplies:
    # shots taken from inside the box is the closest measure of box presence.
    "att_pen_touches": "shotsFromInsideTheBox",
    "crosses":         "totalCross",
}
#: Percentages — already 0-100 and must NOT be divided by 90s.
PERCENT_FIELDS: dict[str, str] = {
    "pass_cmp_pct":    "accuratePassesPercentage",
    "duels_won_pct":   "totalDuelsWonPercentage",
    "aerial_won_pct":  "aerialDuelsWonPercentage",
    "ground_duels_won_pct": "groundDuelsWonPercentage",
    "dribble_success_pct":  "successfulDribblesPercentage",
    "tackles_won_pct": "tacklesWonPercentage",
}

_REQUEST_FIELDS = sorted(
    set(DIRECT_FIELDS.values()) | set(PERCENT_FIELDS.values()) | {"minutesPlayed", "appearances"}
)
_PAGE_SIZE = 100
_REQUEST_GAP = 1.0


def _fold(name: str) -> str:
    """Diacritic-free key — must match server.py's _normalise_name."""
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
    """
    Return {folded_player_name: {metric: value}} for one league season.

    Values are season totals except the *_pct fields, which are percentages.
    `minutes` is carried through so the caller can compute per-90 itself.
    """
    season_id = _season_id(league_id, season_label)
    if not season_id:
        logger.warning(f"Sofascore: no {season_label} season for {league_name}")
        return {}

    fields = "%2C".join(_REQUEST_FIELDS)
    players: dict[str, dict] = {}
    offset, pages = 0, None

    while True:
        url = (
            f"{API}/unique-tournament/{league_id}/season/{season_id}/statistics"
            f"?limit={_PAGE_SIZE}&offset={offset}&accumulation=total"
            f"&fields={fields}&filters=position.in.G~D~M~F"
        )
        try:
            data = _get_json(url)
        except Exception as exc:
            logger.warning(f"Sofascore {league_name} page at offset {offset} failed: {exc}")
            break

        rows = data.get("results") or []
        for row in rows:
            name = (row.get("player") or {}).get("name")
            if not name:
                continue
            record = {
                "minutes": float(row.get("minutesPlayed") or 0),
                "squad": (row.get("team") or {}).get("name", ""),
                "league": league_name,
            }
            for canon, field in DIRECT_FIELDS.items():
                if row.get(field) is not None:
                    record[canon] = float(row[field])
            for canon, field in PERCENT_FIELDS.items():
                if row.get(field) is not None:
                    record[canon] = float(row[field])
            players[_fold(name)] = record

        pages = data.get("pages", 0)
        page = data.get("page", 0)
        if not rows or not pages or page >= pages:
            break
        offset += _PAGE_SIZE
        time.sleep(_REQUEST_GAP)

    logger.info(f"Sofascore {league_name}: {len(players)} players over {pages or 0} pages")
    return players


def fetch_big5(season_label: str = SEASON_LABEL) -> dict[str, dict]:
    """Fetch every Big-5 league. A league that fails is skipped, not fatal."""
    everyone: dict[str, dict] = {}
    for name, league_id in BIG5_LEAGUES.items():
        logger.info(f"Sofascore: fetching {name}...")
        try:
            everyone.update(fetch_league(name, league_id, season_label))
        except Exception as exc:
            logger.warning(f"Sofascore {name} failed entirely: {exc}")
        time.sleep(_REQUEST_GAP)
    logger.info(f"Sofascore total: {len(everyone)} players")
    return everyone
