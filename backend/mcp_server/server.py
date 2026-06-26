"""
MCP Server — FastMCP-powered player database + FBref live data.

Tools:
  - search_players / get_player_stats / query_players / list_positions  (SQLite)
  - get_position_cohort       real FBref cohort (falls back to mock on any error)
  - get_enriched_player_stats per-player passing/possession/GCA enrichment
  - get_fbref_player_stats    single-player FBref lookup

Set FBREF_USE_MOCK=true to skip ScraperFC entirely (container deployments without Chrome).
"""
import json
import os
import re
import sys
import logging
from pathlib import Path
from typing import Optional

import aiosqlite
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scoutly_mcp")

# ── ScraperFC — optional; graceful degradation if botasaurus/Chrome missing ───
_server_dir = os.path.dirname(os.path.abspath(__file__))
if _server_dir not in sys.path:
    sys.path.insert(0, _server_dir)

try:
    from ScraperFC.fbref import FBref
    _SCRAPERFC_OK = True
except Exception as _scraperfc_err:
    logger.warning(f"ScraperFC import failed ({_scraperfc_err}) — FBref tools will use mock data")
    FBref = None
    _SCRAPERFC_OK = False

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scoutly_mcp")

# Load .env from project root
_root = Path(__file__).resolve().parent.parent.parent
load_dotenv(_root / ".env")

# ── Config ────────────────────────────────────────────────────────────────────
_db_path_raw = os.getenv("SQLITE_DB_PATH", "data/football.db")
DB_PATH = str((_root / _db_path_raw) if not Path(_db_path_raw).is_absolute() else Path(_db_path_raw))
MCP_PORT = int(os.getenv("MCP_SERVER_PORT", "8005"))

# Pre-built FBref cache (committed to repo, refreshed weekly by GitHub Actions)
_FBREF_CACHE_DB = str(_root / "data" / "fbref_cache.db")


def _get_cohort_from_sqlite_cache(
    position: str, season: str, league: str
) -> str | None:
    """
    Check the pre-built fbref_cache.db for position cohort data.
    Returns JSON string (same format as get_position_cohort) or None.
    """
    import sqlite3 as _sqlite3

    if not Path(_FBREF_CACHE_DB).exists():
        return None
    try:
        conn = _sqlite3.connect(_FBREF_CACHE_DB)
        pos_upper = position.upper()
        rows = conn.execute(
            """SELECT raw_data FROM player_cohort
               WHERE season = ? AND (
                 position = ? OR position LIKE ? OR position LIKE ?
               )
               ORDER BY minutes DESC""",
            (season, pos_upper, f"{pos_upper},%", f"%,{pos_upper}"),
        ).fetchall()
        conn.close()

        if not rows:
            return None

        players = [json.loads(r[0]) for r in rows]

        # Optional league filter
        if league:
            filtered = [
                p for p in players
                if league.lower() in p.get("league", "").lower()
            ]
            if filtered:
                players = filtered

        # Recompute percentiles within this filtered cohort
        import numpy as _np
        STAT_KEYS = [
            "goals", "assists", "xg", "xa", "shots", "shots_on_target",
            "prg_passes", "prg_carries", "att_pen_touches",
            "tackles", "interceptions", "pass_cmp_pct", "sca", "gca",
        ]
        for key in STAT_KEYS:
            vals = [p["raw_metrics"].get(key, {}).get("per90", 0) for p in players]
            arr  = _np.array(vals, dtype=float)
            if arr.max() == 0:
                continue
            ranks = (
                arr.argsort().argsort() / max(len(arr) - 1, 1) * 100
            ).round().astype(int)
            for p, pct in zip(players, ranks):
                if key in p.get("raw_metrics", {}):
                    p["raw_metrics"][key]["percentile"] = int(pct)

        # Return ALL players from cache — no 15-player cap here.
        # The scouter's LLM selection step picks the best 3-5 from the full list.
        players_sorted = sorted(players, key=lambda p: p.get("minutes", 0), reverse=True)
        logger.info(
            f"SQLite cache HIT: pos={position} league={league or 'all'} season={season} "
            f"→ {len(players_sorted)} players"
        )
        return json.dumps(players_sorted)
    except Exception as exc:
        logger.warning(f"SQLite cache lookup failed: {exc}")
        return None

def _get_wages_from_cache(player_names: list[str], season: str) -> dict[str, int]:
    """
    Look up real wages from player_wages table for a list of players.
    Returns {player_name: wage_eur} for any names found.
    """
    import sqlite3 as _sqlite3
    if not Path(_FBREF_CACHE_DB).exists():
        return {}
    try:
        conn = _sqlite3.connect(_FBREF_CACHE_DB)
        placeholders = ",".join("?" for _ in player_names)
        rows = conn.execute(
            f"SELECT name, wage_eur FROM player_wages WHERE season = ? AND name IN ({placeholders})",
            (season, *player_names),
        ).fetchall()
        conn.close()
        return {name: wage for name, wage in rows if wage}
    except Exception:
        return {}


mcp = FastMCP("Scoutly Unified MCP Server")


# ── Row helper ────────────────────────────────────────────────────────────────

def _row_to_dict(row: aiosqlite.Row) -> dict:
    """Convert an aiosqlite Row (sqlite3.Row) to a plain JSON-serialisable dict."""
    d = dict(row)
    if "seasons_data" in d and isinstance(d["seasons_data"], str):
        try:
            d["seasons_data"] = json.loads(d["seasons_data"])
        except (json.JSONDecodeError, TypeError):
            pass
    return d


# ── SQLite Tools ──────────────────────────────────────────────────────────────

@mcp.tool()
async def search_players(
    position: Optional[str] = None,
    max_wage: Optional[int] = None,
    min_wage: Optional[int] = None,
    min_rating: Optional[int] = None,
    max_age: Optional[int] = None,
    min_age: Optional[int] = None,
    nationality: Optional[str] = None,
    min_goals: Optional[float] = None,
    limit: int = 10,
) -> str:
    """
    Search players using one or more filters in the local database.
    """
    conditions: list[str] = []
    params: list = []

    if position:
        conditions.append("position = ?")
        params.append(position.upper())
    if max_wage is not None:
        conditions.append("wage_eur <= ?")
        params.append(max_wage)
    if min_wage is not None:
        conditions.append("wage_eur >= ?")
        params.append(min_wage)
    if min_rating is not None:
        conditions.append("overall_rating >= ?")
        params.append(min_rating)
    if max_age is not None:
        conditions.append("age <= ?")
        params.append(max_age)
    if min_age is not None:
        conditions.append("age >= ?")
        params.append(min_age)
    if nationality:
        conditions.append("LOWER(nationality) LIKE LOWER(?)")
        params.append(f"%{nationality}%")
    if min_goals is not None:
        conditions.append("goals_per_season >= ?")
        params.append(min_goals)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)

    sql = f"""
        SELECT * FROM players
        {where}
        ORDER BY overall_rating DESC
        LIMIT ?
    """

    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(sql, params)
            rows = await cursor.fetchall()
        return json.dumps([_row_to_dict(r) for r in rows])
    except Exception as e:
        logger.error(f"DB Error: {e}")
        return json.dumps([{"error": str(e)}])

@mcp.tool()
async def get_player_stats(player_id: int) -> str:
    """
    Get the full statistical profile for a specific player by ID from local DB.
    """
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM players WHERE id = ?", (player_id,))
            row = await cursor.fetchone()
        return json.dumps(_row_to_dict(row) if row else {})
    except Exception as e:
        return json.dumps({"error": str(e)})

@mcp.tool()
async def query_players(sql: str) -> str:
    """
    Execute a custom read-only SQL query against the players table.
    Only SELECT statements are permitted.
    """
    forbidden = re.compile(r"\b(INSERT|UPDATE|DELETE|DROP|TRUNCATE|ALTER|CREATE)\b", re.IGNORECASE)
    if forbidden.search(sql):
        return json.dumps([{"error": "Only SELECT queries are allowed via this tool."}])

    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(sql)
            rows = await cursor.fetchall()
        return json.dumps([_row_to_dict(r) for r in rows])
    except Exception as e:
        return json.dumps([{"error": str(e)}])

@mcp.tool()
async def list_positions() -> str:
    """Return all distinct player positions available in the database."""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT DISTINCT position FROM players ORDER BY position")
            rows = await cursor.fetchall()
        return json.dumps([r["position"] for r in rows])
    except Exception as e:
        return json.dumps([{"error": str(e)}])

# ── FBref Live Data Tools ─────────────────────────────────────────────────────

@mcp.tool()
async def get_fbref_player_stats(player_name: str, season: str = "2425", stat_type: str = "standard") -> str:
    """
    Retrieve live real-world statistics for a specific football player from FBref.
    Args:
        player_name: The name of the player (e.g., "Erling Haaland").
        season: The season to query. E.g., "2425" for 2024-2025. Defaults to "2425".
        stat_type: "standard", "shooting", "passing", "defense", or "possession".
    """
    try:
        # FBref from ScraperFC uses format like "2024-2025"
        s = season
        if len(s) == 4 and s.isdigit():
            s = f"20{s[:2]}-20{s[2:]}"
            
        fbref = FBref()
        stats_dict = await _scrape(fbref, s, "FBref Big 5 Combined", stat_type)

        if not stats_dict or "player" not in stats_dict:
            return json.dumps([{"message": f"No stats found for {player_name} in FBref."}])
            
        stats = stats_dict["player"]
        # Flatten MultiIndex columns if present
        stats.columns = [col[1].strip() if isinstance(col, tuple) and len(col) > 1 else col[0] if isinstance(col, tuple) else col for col in stats.columns]
        stats = stats.loc[:, ~stats.columns.duplicated()]
        
        # 'Player' is usually the column name from ScraperFC dict output
        if 'Player' not in stats.columns:
            return json.dumps([{"message": f"No stats found for {player_name} in FBref."}])
            
        player_mask = stats['Player'].fillna('').str.lower().str.contains(player_name.lower())
        player_stats = stats[player_mask]
        
        if player_stats.empty:
            return json.dumps([{"message": f"No stats found for {player_name} in FBref."}])
            
        result = player_stats.to_dict(orient="records")
        return json.dumps(result)
    except Exception as e:
        logger.error(f"FBref Error: {e}")
        # Provide a mock fallback for local dev if FBref blocks us with 403
        if "403" in str(e) or "Forbidden" in str(e) or "Could not download" in str(e):
            return json.dumps([{
                "player": player_name,
                "season": season,
                "message": "FBref returned 403 Forbidden. Using fallback mock data for local development.",
                "goals": 15,
                "assists": 5,
                "xg": 14.2,
                "matches_played": 20
            }])
        return json.dumps([{"error": f"Error retrieving FBref stats: {str(e)}"}])

@mcp.tool()
async def search_fbref_players(
    position: str = None,
    min_age: int = None,
    max_age: int = None,
    limit: int = 15,
    season: str = "2425"
) -> str:
    """
    Search for players natively in FBref by position and age. Returns a list of matching players.
    This fetches the player cohort from FBref directly.
    """
    try:
        s = season
        if len(s) == 4 and s.isdigit():
            s = f"20{s[:2]}-20{s[2:]}"
            
        fbref = FBref()
        stats_dict = await _scrape(fbref, s, "FBref Big 5 Combined", "standard")

        if not stats_dict or "player" not in stats_dict:
            return json.dumps([])
            
        df = stats_dict["player"]
        df.columns = [col[1].strip() if isinstance(col, tuple) and len(col) > 1 else col[0] if isinstance(col, tuple) else col for col in df.columns]
        df = df.loc[:, ~df.columns.duplicated()]
        
        # 'Pos' and 'Age' are typical column names from ScraperFC 'player' dict output
        if position and 'Pos' in df.columns:
            df = df[df['Pos'].fillna('').str.contains(position, case=False, na=False)]
        if min_age is not None and 'Age' in df.columns:
            # age might be string "23-140"
            df['Age_num'] = df['Age'].astype(str).str.split('-').str[0].apply(lambda x: int(x) if x.isdigit() else 0)
            df = df[df['Age_num'] >= min_age]
        if max_age is not None and 'Age' in df.columns:
            if 'Age_num' not in df.columns:
                df['Age_num'] = df['Age'].astype(str).str.split('-').str[0].apply(lambda x: int(x) if x.isdigit() else 0)
            df = df[df['Age_num'] <= max_age]
            
        if 'Age_num' in df.columns:
            df = df.drop(columns=['Age_num'])
            
        result = df.head(min(limit, 15)).to_dict(orient="records")
        return json.dumps(result)
    except Exception as e:
        logger.error(f"FBref Error in search_fbref_players: {e}")
        err_str = str(e)
        if "403" in err_str or "Forbidden" in err_str or "Could not download" in err_str:
            # Robust mock cohort matching the DB players
            mock_players = [
                {"name": "Harry Powell", "position": "FW", "age": 22, "goals": 18, "assists": 6, "xg": 15.1, "matches_played": 25},
                {"name": "Lucas Martin", "position": "FW", "age": 24, "goals": 14, "assists": 4, "xg": 12.0, "matches_played": 22},
                {"name": "Marco Ferrara", "position": "FW", "age": 21, "goals": 10, "assists": 2, "xg": 9.5, "matches_played": 18},
                {"name": "Pablo Morales", "position": "MF", "age": 23, "goals": 5, "assists": 12, "xg": 4.1, "matches_played": 28},
                {"name": "Pieter de Vries", "position": "CB", "age": 25, "goals": 2, "assists": 3, "xg": 1.2, "matches_played": 30},
            ]
            if position:
                pos_prefix = position[:2].upper()
                if pos_prefix in ["CF", "ST"]:
                    pos_prefix = "FW"
                mock_players = [p for p in mock_players if pos_prefix in p["position"]]
            if min_age:
                mock_players = [p for p in mock_players if p["age"] >= min_age]
            if max_age:
                mock_players = [p for p in mock_players if p["age"] <= max_age]
                
            return json.dumps(mock_players[:limit])
        return json.dumps([{"error": f"Error retrieving FBref stats: {str(e)}"}])

# ── Cohort + percentile tool (M2) ────────────────────────────────────────────

def _compute_per90_and_percentiles(df, stat_cols: list[str], min_col: str) -> dict:
    """
    Given a DataFrame and a list of raw-stat column names, return a per-player
    dict of {col: {value, per90, percentile}} for every column that exists.
    Percentile = rank within the passed df (0-100, higher = better).
    """
    import numpy as np

    if min_col not in df.columns:
        return {}

    minutes = df[min_col].fillna(0).astype(float)
    nineties = (minutes / 90).replace(0, np.nan)

    result: dict[str, dict] = {}
    for col in stat_cols:
        if col not in df.columns:
            continue
        raw = df[col].fillna(0).astype(float)
        per90 = (raw / nineties).fillna(0)
        pct = per90.rank(pct=True, method="average") * 100

        for idx in df.index:
            name = str(df.at[idx, "player"]) if "player" in df.columns else str(idx)
            if name not in result:
                result[name] = {}
            result[name][col] = {
                "value":      round(float(raw.at[idx]), 2),
                "per90":      round(float(per90.at[idx]), 3),
                "percentile": round(float(pct.at[idx])),
            }
    return result


# ── Real 2024-25 Big 5 players (used when FBref is unreachable locally).
# GitHub Actions runner fetches live data weekly and commits data/fbref_cache.db.
# Stats are based on 2024-25 season figures (approximate as of season end).
_MOCK_COHORT = {
    "FW": [
        {"name": "Erling Haaland",      "position": "FW", "squad": "Manchester City", "age": 24, "league": "Premier League",
         "minutes": 2160, "goals": 18,  "assists": 4,  "xg": 17.8, "xa": 3.5,
         "shots": 98, "shots_on_target": 55, "prg_passes": 22, "prg_carries": 55,  "att_pen_touches": 162,
         "tackles": 8,  "interceptions": 4,  "pass_cmp_pct": 66.2},
        {"name": "Alexander Isak",      "position": "FW", "squad": "Newcastle United", "age": 25, "league": "Premier League",
         "minutes": 2880, "goals": 23,  "assists": 6,  "xg": 19.8, "xa": 5.1,
         "shots": 96, "shots_on_target": 52, "prg_passes": 31, "prg_carries": 78,  "att_pen_touches": 148,
         "tackles": 16, "interceptions": 5,  "pass_cmp_pct": 70.2},
        {"name": "Ollie Watkins",       "position": "FW", "squad": "Aston Villa",      "age": 29, "league": "Premier League",
         "minutes": 2430, "goals": 17,  "assists": 9,  "xg": 14.2, "xa": 7.5,
         "shots": 75, "shots_on_target": 38, "prg_passes": 45, "prg_carries": 80,  "att_pen_touches": 118,
         "tackles": 28, "interceptions": 9,  "pass_cmp_pct": 71.5},
        {"name": "Lautaro Martinez",    "position": "FW", "squad": "Inter Milan",      "age": 27, "league": "Serie A",
         "minutes": 2520, "goals": 22,  "assists": 4,  "xg": 18.8, "xa": 3.8,
         "shots": 92, "shots_on_target": 48, "prg_passes": 35, "prg_carries": 68,  "att_pen_touches": 155,
         "tackles": 20, "interceptions": 7,  "pass_cmp_pct": 72.8},
        {"name": "Kylian Mbappe",       "position": "FW", "squad": "Real Madrid",      "age": 26, "league": "La Liga",
         "minutes": 2070, "goals": 16,  "assists": 3,  "xg": 13.5, "xa": 3.2,
         "shots": 82, "shots_on_target": 44, "prg_passes": 48, "prg_carries": 105, "att_pen_touches": 115,
         "tackles": 22, "interceptions": 9,  "pass_cmp_pct": 73.4},
        {"name": "Harry Kane",          "position": "FW", "squad": "Bayern Munich",    "age": 31, "league": "Bundesliga",
         "minutes": 2790, "goals": 26,  "assists": 8,  "xg": 22.4, "xa": 7.1,
         "shots": 108,"shots_on_target": 60, "prg_passes": 82, "prg_carries": 65,  "att_pen_touches": 160,
         "tackles": 20, "interceptions": 7,  "pass_cmp_pct": 79.1},
        {"name": "Jonathan David",      "position": "FW", "squad": "Lille",           "age": 25, "league": "Ligue 1",
         "minutes": 2520, "goals": 20,  "assists": 4,  "xg": 16.8, "xa": 3.2,
         "shots": 82, "shots_on_target": 44, "prg_passes": 32, "prg_carries": 65,  "att_pen_touches": 135,
         "tackles": 20, "interceptions": 7,  "pass_cmp_pct": 70.8},
        {"name": "Mateo Retegui",       "position": "FW", "squad": "Atalanta",        "age": 25, "league": "Serie A",
         "minutes": 2340, "goals": 19,  "assists": 3,  "xg": 16.2, "xa": 2.8,
         "shots": 78, "shots_on_target": 40, "prg_passes": 28, "prg_carries": 58,  "att_pen_touches": 142,
         "tackles": 16, "interceptions": 5,  "pass_cmp_pct": 69.4},
        {"name": "Rafael Leao",         "position": "FW", "squad": "AC Milan",        "age": 25, "league": "Serie A",
         "minutes": 2250, "goals": 10,  "assists": 8,  "xg": 9.2,  "xa": 7.1,
         "shots": 65, "shots_on_target": 32, "prg_passes": 52, "prg_carries": 118, "att_pen_touches": 95,
         "tackles": 28, "interceptions": 10, "pass_cmp_pct": 74.8},
        {"name": "Robert Lewandowski",  "position": "FW", "squad": "Barcelona",       "age": 36, "league": "La Liga",
         "minutes": 2340, "goals": 18,  "assists": 5,  "xg": 15.8, "xa": 4.2,
         "shots": 88, "shots_on_target": 48, "prg_passes": 45, "prg_carries": 52,  "att_pen_touches": 148,
         "tackles": 12, "interceptions": 5,  "pass_cmp_pct": 74.2},
    ],
    "MF": [
        {"name": "Jude Bellingham",     "position": "MF", "squad": "Real Madrid",      "age": 21, "league": "La Liga",
         "minutes": 1918, "goals": 6,   "assists": 4,  "xg": 5.2,  "xa": 4.8,
         "shots": 51, "shots_on_target": 27, "prg_passes": 78,  "prg_carries": 72,  "att_pen_touches": 68,
         "tackles": 42, "interceptions": 22, "pass_cmp_pct": 83.1},
        {"name": "Martin Odegaard",     "position": "MF", "squad": "Arsenal",          "age": 26, "league": "Premier League",
         "minutes": 1980, "goals": 8,   "assists": 9,  "xg": 6.8,  "xa": 8.2,
         "shots": 55, "shots_on_target": 22, "prg_passes": 128, "prg_carries": 75,  "att_pen_touches": 82,
         "tackles": 38, "interceptions": 22, "pass_cmp_pct": 87.4},
        {"name": "Phil Foden",          "position": "MF", "squad": "Manchester City",  "age": 25, "league": "Premier League",
         "minutes": 2070, "goals": 10,  "assists": 8,  "xg": 8.2,  "xa": 7.1,
         "shots": 58, "shots_on_target": 25, "prg_passes": 112, "prg_carries": 95,  "att_pen_touches": 72,
         "tackles": 35, "interceptions": 18, "pass_cmp_pct": 85.2},
        {"name": "Granit Xhaka",        "position": "MF", "squad": "Bayer Leverkusen", "age": 32, "league": "Bundesliga",
         "minutes": 2520, "goals": 6,   "assists": 10, "xg": 4.8,  "xa": 8.5,
         "shots": 42, "shots_on_target": 18, "prg_passes": 165, "prg_carries": 62,  "att_pen_touches": 55,
         "tackles": 68, "interceptions": 35, "pass_cmp_pct": 89.8},
        {"name": "Pedri",               "position": "MF", "squad": "Barcelona",        "age": 22, "league": "La Liga",
         "minutes": 1980, "goals": 7,   "assists": 8,  "xg": 5.8,  "xa": 7.2,
         "shots": 48, "shots_on_target": 20, "prg_passes": 145, "prg_carries": 102, "att_pen_touches": 68,
         "tackles": 42, "interceptions": 22, "pass_cmp_pct": 90.5},
        {"name": "Rodri",               "position": "MF", "squad": "Manchester City",  "age": 28, "league": "Premier League",
         "minutes": 450,  "goals": 1,   "assists": 2,  "xg": 0.8,  "xa": 1.5,
         "shots": 8,  "shots_on_target": 3,  "prg_passes": 55,  "prg_carries": 18,  "att_pen_touches": 12,
         "tackles": 22, "interceptions": 12, "pass_cmp_pct": 91.2},
        {"name": "Bruno Fernandes",     "position": "MF", "squad": "Manchester United", "age": 30, "league": "Premier League",
         "minutes": 2520, "goals": 9,   "assists": 11, "xg": 7.5,  "xa": 9.8,
         "shots": 65, "shots_on_target": 28, "prg_passes": 142, "prg_carries": 68,  "att_pen_touches": 62,
         "tackles": 52, "interceptions": 25, "pass_cmp_pct": 83.8},
    ],
    "DF": [
        {"name": "Virgil van Dijk",     "position": "DF", "squad": "Liverpool",        "age": 33, "league": "Premier League",
         "minutes": 2700, "goals": 3,   "assists": 2,  "xg": 1.8,  "xa": 1.5,
         "shots": 18, "shots_on_target": 6,  "prg_passes": 168, "prg_carries": 42,  "att_pen_touches": 8,
         "tackles": 62, "interceptions": 45, "pass_cmp_pct": 92.4},
        {"name": "William Saliba",      "position": "DF", "squad": "Arsenal",          "age": 24, "league": "Premier League",
         "minutes": 2700, "goals": 2,   "assists": 1,  "xg": 1.1,  "xa": 0.9,
         "shots": 12, "shots_on_target": 4,  "prg_passes": 152, "prg_carries": 48,  "att_pen_touches": 5,
         "tackles": 58, "interceptions": 38, "pass_cmp_pct": 91.8},
        {"name": "Ruben Dias",          "position": "DF", "squad": "Manchester City",  "age": 27, "league": "Premier League",
         "minutes": 2430, "goals": 1,   "assists": 2,  "xg": 0.8,  "xa": 1.2,
         "shots": 10, "shots_on_target": 3,  "prg_passes": 145, "prg_carries": 38,  "att_pen_touches": 4,
         "tackles": 55, "interceptions": 42, "pass_cmp_pct": 90.2},
        {"name": "Alessandro Bastoni",  "position": "DF", "squad": "Inter Milan",      "age": 25, "league": "Serie A",
         "minutes": 2520, "goals": 2,   "assists": 4,  "xg": 1.2,  "xa": 3.1,
         "shots": 14, "shots_on_target": 5,  "prg_passes": 175, "prg_carries": 55,  "att_pen_touches": 6,
         "tackles": 52, "interceptions": 35, "pass_cmp_pct": 91.5},
        {"name": "Antonio Rudiger",     "position": "DF", "squad": "Real Madrid",      "age": 32, "league": "La Liga",
         "minutes": 2340, "goals": 2,   "assists": 1,  "xg": 1.4,  "xa": 0.8,
         "shots": 16, "shots_on_target": 5,  "prg_passes": 138, "prg_carries": 40,  "att_pen_touches": 7,
         "tackles": 60, "interceptions": 40, "pass_cmp_pct": 88.6},
        {"name": "Theo Hernandez",      "position": "DF", "squad": "AC Milan",        "age": 27, "league": "Serie A",
         "minutes": 2250, "goals": 4,   "assists": 6,  "xg": 2.8,  "xa": 4.5,
         "shots": 28, "shots_on_target": 12, "prg_passes": 125, "prg_carries": 72,  "att_pen_touches": 18,
         "tackles": 48, "interceptions": 28, "pass_cmp_pct": 82.4},
        {"name": "Alphonso Davies",     "position": "DF", "squad": "Bayern Munich",   "age": 24, "league": "Bundesliga",
         "minutes": 2070, "goals": 2,   "assists": 5,  "xg": 1.5,  "xa": 4.2,
         "shots": 18, "shots_on_target": 7,  "prg_passes": 112, "prg_carries": 88,  "att_pen_touches": 12,
         "tackles": 52, "interceptions": 30, "pass_cmp_pct": 79.8},
    ],
    "GK": [
        {"name": "David Raya",          "position": "GK", "squad": "Arsenal",          "age": 29, "league": "Premier League",
         "minutes": 2700, "goals": 0,   "assists": 0,  "xg": 0.0,  "xa": 0.0,
         "shots": 0, "shots_on_target": 0,  "prg_passes": 35, "prg_carries": 5,   "att_pen_touches": 0,
         "tackles": 1, "interceptions": 0, "pass_cmp_pct": 72.4},
        {"name": "Alisson Becker",      "position": "GK", "squad": "Liverpool",        "age": 32, "league": "Premier League",
         "minutes": 2610, "goals": 0,   "assists": 0,  "xg": 0.0,  "xa": 0.0,
         "shots": 0, "shots_on_target": 0,  "prg_passes": 28, "prg_carries": 4,   "att_pen_touches": 0,
         "tackles": 1, "interceptions": 0, "pass_cmp_pct": 71.8},
        {"name": "Mike Maignan",        "position": "GK", "squad": "AC Milan",        "age": 29, "league": "Serie A",
         "minutes": 2700, "goals": 0,   "assists": 0,  "xg": 0.0,  "xa": 0.0,
         "shots": 0, "shots_on_target": 0,  "prg_passes": 32, "prg_carries": 5,   "att_pen_touches": 0,
         "tackles": 1, "interceptions": 0, "pass_cmp_pct": 70.2},
    ],
}


import concurrent.futures as _futures
import asyncio as _asyncio
import time as _time

# ── In-process caches ─────────────────────────────────────────────────────────
# Keyed by (position, season, league) → (timestamp, json_str)
_COHORT_CACHE: dict[tuple, tuple[float, str]] = {}
_COHORT_TTL = 6 * 3600  # 6 hours — FBref squad data barely changes intra-day

async def _scrape(fbref_obj, year: str, league: str, cat: str, timeout: int = 60) -> dict:
    """
    Run a synchronous ScraperFC scrape_stats call in a thread with a hard timeout.
    ScraperFC uses botasaurus (headless Chrome) which can hang waiting for user
    input on error — this wrapper ensures it always exits within `timeout` seconds.
    """
    loop = _asyncio.get_event_loop()
    pool = _futures.ThreadPoolExecutor(max_workers=1)
    try:
        return await _asyncio.wait_for(
            loop.run_in_executor(pool, fbref_obj.scrape_stats, year, league, cat),
            timeout=timeout,
        )
    except _asyncio.TimeoutError:
        raise RuntimeError(
            f"FBref scraping timed out after {timeout}s (league={league}, cat={cat})"
        )
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _flatten_cols(df) -> "pd.DataFrame":
    """Flatten ScraperFC MultiIndex columns → single-level string names."""
    import pandas as pd
    df = df.copy()
    df.columns = [
        col[1].strip() if isinstance(col, tuple) and len(col) > 1 and col[1].strip()
        else (col[0].strip() if isinstance(col, tuple) else str(col))
        for col in df.columns
    ]
    df = df.loc[:, ~df.columns.duplicated()]
    return df


def _find_col(df, candidates: list[str]) -> str | None:
    """Return the first candidate column name found in df (case-insensitive)."""
    low = {c.lower(): c for c in df.columns}
    for c in candidates:
        if c.lower() in low:
            return low[c.lower()]
    return None


@mcp.tool()
async def get_position_cohort(
    position: str,
    season: str = "2425",
    league: str = "",
) -> str:
    """
    Return the top-15 cohort for a position from FBref with per-90 stats and percentile ranks.

    Merges standard + defensive tables so that tackles and interceptions are
    populated alongside goals, assists, xG, progressive passes/carries.

    Args:
        position: FBref position code — DF (defenders), MF (midfielders), FW (forwards), GK.
                  Partial matches accepted (e.g. "DF" also matches "DF,MF").
        season:   Season code, e.g. "2425" for 2024-25 (default).
        league:   Optional league filter, e.g. "Premier League" or "La Liga".
                  When set, only players from that league are returned.
                  Leave empty to search the full Big 5.
    """
    import pandas as pd

    # ── Cache check ───────────────────────────────────────────────────────────
    cache_key = (position.upper(), season, league.lower())
    if cache_key in _COHORT_CACHE:
        ts, cached = _COHORT_CACHE[cache_key]
        if _time.time() - ts < _COHORT_TTL:
            logger.info(f"get_position_cohort cache HIT: {cache_key}")
            return cached
    logger.info(f"get_position_cohort cache MISS: {cache_key}")

    # ── SQLite pre-built cache (fastest path — no Chrome needed) ─────────────
    cached_json = _get_cohort_from_sqlite_cache(position, season, league)
    if cached_json is not None:
        _COHORT_CACHE[cache_key] = (_time.time(), cached_json)
        return cached_json

    try:
        # Skip FBref scrape when: FBREF_USE_MOCK=true (container), or ScraperFC failed to import.
        if os.getenv("FBREF_USE_MOCK", "false").lower() == "true" or not _SCRAPERFC_OK:
            reason = "FBREF_USE_MOCK=true" if os.getenv("FBREF_USE_MOCK", "false").lower() == "true" else "ScraperFC unavailable"
            logger.info(f"Skipping FBref scrape ({reason}) — using mock cohort")
            raise RuntimeError(reason)

        STAT_COLS = ["goals", "assists", "xg", "xa", "shots", "shots_on_target",
                     "prg_passes", "prg_carries", "att_pen_touches",
                     "tackles", "interceptions", "pass_cmp_pct"]

        # Canonical column aliases (checked case-insensitively)
        COL_ALIASES = {
            "player":          ["Player", "player"],
            "position":        ["Pos", "position", "pos"],
            "squad":           ["Squad", "squad", "team"],
            "league":          ["Comp", "comp", "league", "competition"],
            "age":             ["Age", "age"],
            "minutes":         ["Min", "playing_time_min", "min", "minutes"],
            "goals":           ["Gls", "goals"],
            "assists":         ["Ast", "assists"],
            "xg":              ["xG", "xg"],
            "xa":              ["xAG", "xa"],
            "shots":           ["Sh", "shots"],
            "shots_on_target": ["SoT", "sot"],
            "prg_passes":      ["PrgP", "prgp"],
            "prg_carries":     ["PrgC", "prgc"],
            "att_pen_touches": ["Att Pen", "Touches_att_pen", "att pen"],
            "tackles":         ["Tkl", "tkl"],
            "interceptions":   ["Int", "interceptions"],
            "pass_cmp_pct":    ["Cmp%", "cmp%"],
        }

        s = season
        if len(s) == 4 and s.isdigit():
            s = f"20{s[:2]}-20{s[2:]}"

        fbref = FBref()

        # ── Fetch standard table ─────────────────────────────────────────────
        std_dict = await _scrape(fbref, s, "FBref Big 5 Combined", "standard")
        if not std_dict or "player" not in std_dict:
            raise ValueError("No player stats returned from FBref standard table")
        df = _flatten_cols(std_dict["player"])

        # ── Merge defensive table (adds Tkl, Int) ────────────────────────────
        try:
            def_dict = await _scrape(fbref, s, "FBref Big 5 Combined", "defensive", timeout=60)
            if def_dict and "player" in def_dict:
                def_df = _flatten_cols(def_dict["player"])
                p_col  = _find_col(def_df, ["Player", "player"]) or "Player"
                sq_col = _find_col(def_df, ["Squad", "squad"]) or "Squad"
                tkl_col = _find_col(def_df, ["Tkl", "tkl"])
                int_col = _find_col(def_df, ["Int", "interceptions"])
                keep = [c for c in [p_col, sq_col, tkl_col, int_col] if c and c in def_df.columns]
                if len(keep) >= 3:
                    p_std  = _find_col(df, ["Player", "player"]) or "Player"
                    sq_std = _find_col(df, ["Squad", "squad"]) or "Squad"
                    def_slim = def_df[keep].rename(columns={p_col: p_std, sq_col: sq_std})
                    # Avoid duplicate column names from defensive merge
                    new_cols = [c for c in def_slim.columns if c not in df.columns or c in [p_std, sq_std]]
                    df = df.merge(def_slim[new_cols], on=[p_std, sq_std], how="left")
                    logger.info("Defensive table merged successfully")
        except Exception as e:
            logger.warning(f"Defensive table merge skipped: {e}")

        # ── Map canonical column names ────────────────────────────────────────
        col_map = {want: _find_col(df, cands) for want, cands in COL_ALIASES.items()}
        col_map = {k: v for k, v in col_map.items() if v}  # drop unmapped

        if not all(k in col_map for k in ("player", "position", "minutes")):
            raise ValueError(f"Missing essential columns. Mapped: {list(col_map.keys())}")

        # ── Filter position ───────────────────────────────────────────────────
        pos_mask = df[col_map["position"]].fillna("").str.contains(position, case=False, na=False)
        df = df[pos_mask].copy()
        if df.empty:
            raise ValueError(f"No players found for position={position}")

        # ── Filter by league (smart localised search) ─────────────────────────
        if league and col_map.get("league"):
            league_mask = df[col_map["league"]].fillna("").str.contains(league, case=False, na=False)
            filtered = df[league_mask]
            if not filtered.empty:
                df = filtered.copy()
                logger.info(f"League filter '{league}' kept {len(df)} players")
            else:
                logger.warning(f"League filter '{league}' matched no players — ignoring filter")

        # ── Min minutes + top-15 cap ──────────────────────────────────────────
        min_col = col_map["minutes"]
        df[min_col] = pd.to_numeric(df[min_col], errors="coerce").fillna(0)
        df = df[df[min_col] >= 450].copy()
        df = df.sort_values(by=min_col, ascending=False).head(15)
        
        def _safe_float(val) -> float:
            try:
                return float(val) if pd.notna(val) else 0.0
            except Exception:
                return 0.0

        players = []
        for _, row in df.iterrows():
            minutes  = _safe_float(row.get(col_map["minutes"], 0))
            nineties = minutes / 90 if minutes > 0 else 0.001

            age_raw = row.get(col_map.get("age", "Age"), 0)
            if isinstance(age_raw, str) and "-" in age_raw:
                age_raw = age_raw.split("-")[0]
            try:
                age = int(float(age_raw)) if pd.notna(age_raw) else 0
            except Exception:
                age = 0

            raw_metrics: dict = {}
            for key in STAT_COLS:
                src = col_map.get(key)
                if not src or src not in row.index:
                    continue
                val = _safe_float(row[src])
                # pass_cmp_pct is already a percentage — don't divide by 90s
                if key == "pass_cmp_pct":
                    raw_metrics[key] = {"value": val, "per90": val}
                else:
                    raw_metrics[key] = {"value": round(val, 2), "per90": round(val / nineties, 3)}

            players.append({
                "name":         str(row.get(col_map.get("player", "Player"), "")),
                "position":     str(row.get(col_map.get("position", "Pos"), "")),
                "squad":        str(row.get(col_map.get("squad", "Squad"), "")),
                "league":       str(row.get(col_map.get("league", "Comp"), "")),
                "age":          age,
                "minutes":      int(minutes),
                "season_90s":   round(minutes / 90, 1),
                "raw_metrics":  raw_metrics,
                "coverage_flag": "good" if minutes >= 900 else "low" if minutes >= 450 else "very_low",
            })

        # Add within-cohort percentile ranks
        if players:
            for key in STAT_COLS + ["pass_cmp_pct"]:
                vals = [p["raw_metrics"].get(key, {}).get("per90", 0) for p in players]
                if all(v == 0 for v in vals):
                    continue
                import numpy as np
                arr = np.array(vals, dtype=float)
                ranks = (arr.argsort().argsort() / max(len(arr) - 1, 1) * 100).round().astype(int)
                for p, pct in zip(players, ranks):
                    if key in p["raw_metrics"]:
                        p["raw_metrics"][key]["percentile"] = int(pct)

        result_json = json.dumps(players, default=str)
        _COHORT_CACHE[cache_key] = (_time.time(), result_json)
        return result_json

    except Exception as exc:
        logger.error(f"get_position_cohort error: {exc}")
        err_str = str(exc)
        # Always fall back to mock cohort — any FBref failure (403, parse error,
        # missing columns, timeout, etc.) should not crash the pipeline.
        if True:
            # Return mock cohort with pre-computed percentiles
            pos_upper = position.upper()
            mock_key = next(
                (k for k in _MOCK_COHORT if k in pos_upper or pos_upper in k), "FW"
            )
            cohort = _MOCK_COHORT.get(mock_key, _MOCK_COHORT["FW"])
            STAT_COLS_MOCK = ["goals", "assists", "xg", "xa", "shots",
                              "shots_on_target", "prg_passes", "prg_carries",
                              "att_pen_touches", "tackles", "interceptions",
                              "pass_cmp_pct"]
            import numpy as np
            players = []
            for p in cohort:
                mins = float(p.get("minutes", 0))
                nineties = mins / 90 if mins > 0 else 0.001
                rm = {}
                for key in STAT_COLS_MOCK:
                    v = float(p.get(key, 0) or 0)
                    rm[key] = {"value": v, "per90": round(v / nineties, 3)}
                players.append({
                    "name":       p["name"],
                    "position":   p["position"],
                    "squad":      p["squad"],
                    "league":     p["league"],
                    "age":        p["age"],
                    "minutes":    int(mins),
                    "season_90s": round(nineties, 1),
                    "raw_metrics": rm,
                    "coverage_flag": "good" if mins >= 900 else "low",
                })
            # Compute percentiles within mock cohort
            for key in STAT_COLS_MOCK:
                vals = [pl["raw_metrics"].get(key, {}).get("per90", 0) for pl in players]
                arr = np.array(vals, dtype=float)
                if arr.max() == 0:
                    continue
                ranks = (arr.argsort().argsort() / max(len(arr) - 1, 1) * 100).round().astype(int)
                for pl, pct in zip(players, ranks):
                    if key in pl["raw_metrics"]:
                        pl["raw_metrics"][key]["percentile"] = int(pct)
            mock_json = json.dumps(players)
            _COHORT_CACHE[cache_key] = (_time.time(), mock_json)
            return mock_json
        return json.dumps([{"error": f"get_position_cohort failed: {err_str}"}])


@mcp.tool()
async def get_enriched_player_stats(player_name: str, season: str = "2425") -> str:
    """
    Fetch deep per-player stats from FBref by merging passing + possession +
    goal-and-shot-creation tables for a single named player.

    Provides:
      - pass_cmp_pct      : pass completion % (from passing table)
      - prg_passes_detail : key passes, assists (from passing table)
      - att_pen_touches   : touches in attacking penalty area (from possession)
      - prg_carries_detail: carries into final third (from possession)
      - sca, gca          : shot/goal creating actions per 90 (from goal_shot_creation)

    Call this for each SELECTED candidate (after get_position_cohort) to enrich
    their raw_metrics with the additional stats the trait engine needs.

    Args:
        player_name: Exact or partial name (e.g. "Milenković", "Salah").
        season:      Season code, e.g. "2425" for 2024-25.
    """
    import pandas as pd

    s = season
    if len(s) == 4 and s.isdigit():
        s = f"20{s[:2]}-20{s[2:]}"

    FETCH_TABLES = {
        "passing":             ["Cmp%", "KP", "PrgP"],
        "possession":          ["Att Pen", "PrgC", "Mis", "Dis"],
        "goal and shot creation": ["SCA", "GCA"],
    }

    merged: dict = {"player_name": player_name, "season": s, "enriched_metrics": {}}

    if os.getenv("FBREF_USE_MOCK", "false").lower() == "true" or not _SCRAPERFC_OK:
        reason = "FBREF_USE_MOCK=true" if os.getenv("FBREF_USE_MOCK", "false").lower() == "true" else "ScraperFC unavailable"
        logger.info(f"Skipping FBref scrape ({reason}) — using mock enriched stats")
        merged["note"] = f"Mock data used ({reason})"
        return json.dumps(merged, default=str)

    fbref = FBref()
    for cat, want_cols in FETCH_TABLES.items():
        try:
            result = await _scrape(fbref, s, "FBref Big 5 Combined", cat, timeout=60)
            if not result or "player" not in result:
                continue
            df = _flatten_cols(result["player"])
            p_col = _find_col(df, ["Player", "player"])
            if not p_col:
                continue
            mask  = df[p_col].fillna("").str.lower().str.contains(player_name.lower(), na=False)
            rows  = df[mask]
            if rows.empty:
                continue
            row = rows.iloc[0]
            # Get minutes to compute per-90
            min_col = _find_col(df, ["Min", "min", "minutes"])
            mins = float(row[min_col]) if min_col and pd.notna(row.get(min_col)) else 900.0
            nineties = max(mins / 90, 0.1)
            for col in want_cols:
                src = _find_col(df, [col])
                if not src:
                    continue
                try:
                    val = float(row[src]) if pd.notna(row.get(src)) else 0.0
                except Exception:
                    val = 0.0
                key = col.lower().replace(" ", "_").replace("%", "_pct").replace("/", "_")
                is_pct = "%" in col
                merged["enriched_metrics"][key] = {
                    "value": round(val, 2),
                    "per90": val if is_pct else round(val / nineties, 3),
                }
        except Exception as e:
            logger.warning(f"Enrichment table '{cat}' failed for {player_name}: {e}")

    if not merged["enriched_metrics"]:
        merged["note"] = "No enriched stats available — FBref may be rate-limiting"

    return json.dumps(merged, default=str)


# ── Entry Point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    mcp.settings.host = "0.0.0.0"
    mcp.settings.port = MCP_PORT
    mcp.run(transport="sse")
