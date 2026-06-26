"""
Full FBref + Capology cache builder.

Uses ScraperFC (Botasaurus / Chrome) — works locally and on GitHub Actions.
Does NOT use plain requests (FBref blocks those).

What it builds:
  player_cohort   — standard stats for ALL ≥900 min starters in Big 5 (all positions)
  player_wages    — real weekly wages from Capology for each Big 5 league

Run:
  python -m backend.data_pipeline.cache_builder
  python -m backend.data_pipeline.cache_builder --pos FW  (single position)
  python -m backend.data_pipeline.cache_builder --no-wages  (skip Capology)
"""
from __future__ import annotations

import argparse
import asyncio
import concurrent.futures as _futures
import json
import logging
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

ROOT         = Path(__file__).resolve().parent.parent.parent
CACHE_DB     = ROOT / "data" / "fbref_cache.db"
SEASON_CODE  = "2425"
SEASON_LABEL = "2024-2025"
MIN_MINUTES  = 900   # true starters only

# ── Add ScraperFC to path ────────────────────────────────────────────────────
_mcp_dir = str(ROOT / "backend" / "mcp_server")
if _mcp_dir not in sys.path:
    sys.path.insert(0, _mcp_dir)

try:
    from ScraperFC.fbref import FBref
    from ScraperFC.capology import Capology
    _SCRAPERFC_OK = True
except Exception as e:
    logger.error(f"ScraperFC import failed: {e}")
    _SCRAPERFC_OK = False

# ── Column aliases (same as in server.py) ────────────────────────────────────
_COL_ALIASES: dict[str, list[str]] = {
    "player":          ["Player"],
    "position":        ["Pos"],
    "squad":           ["Squad"],
    "league":          ["Comp"],
    "age":             ["Age"],
    "minutes":         ["Min", "MP"],
    "goals":           ["Gls"],
    "assists":         ["Ast"],
    "xg":              ["xG"],
    "xa":              ["xAG"],
    "shots":           ["Sh"],
    "shots_on_target": ["SoT"],
    "prg_passes":      ["PrgP"],
    "prg_carries":     ["PrgC"],
    "att_pen_touches": ["Att Pen"],
    "tackles":         ["Tkl"],
    "interceptions":   ["Int"],
    "pass_cmp_pct":    ["Cmp%"],
    "sca":             ["SCA"],
    "gca":             ["GCA"],
}
STAT_KEYS = [
    "goals", "assists", "xg", "xa", "shots", "shots_on_target",
    "prg_passes", "prg_carries", "att_pen_touches",
    "tackles", "interceptions", "pass_cmp_pct", "sca", "gca",
]
_FBREF_POS_MAP = {"FW": "FW", "MF": "MF", "DF": "DF", "GK": "GK"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _find_col(df: pd.DataFrame, candidates: list[str]) -> str | None:
    low = {c.lower(): c for c in df.columns}
    for c in candidates:
        if c.lower() in low:
            return low[c.lower()]
    return None


def _flatten(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [
        c[1].strip() if isinstance(c, tuple) and c[1].strip() else c[0].strip()
        if isinstance(c, tuple) else str(c)
        for c in df.columns
    ]
    df = df.loc[:, ~df.columns.duplicated()]
    return df


def _safe_float(val) -> float:
    try:
        return float(val) if pd.notna(val) else 0.0
    except Exception:
        return 0.0


async def _run_in_thread(fn, *args, timeout: int = 120):
    """Run a blocking ScraperFC call in a thread with timeout."""
    loop = asyncio.get_event_loop()
    pool = _futures.ThreadPoolExecutor(max_workers=1)
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(pool, fn, *args),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        raise RuntimeError(f"Scrape timed out after {timeout}s")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


# ── FBref scraping ─────────────────────────────────────────────────────────

async def _fetch_fbref_table(fbref: FBref, stat_type: str) -> pd.DataFrame | None:
    """Fetch one FBref stat table (Big 5 Combined) as a DataFrame."""
    logger.info(f"Fetching FBref {stat_type} table...")
    try:
        result = await _run_in_thread(
            fbref.scrape_stats, SEASON_LABEL, "FBref Big 5 Combined", stat_type
        )
        if not result or "player" not in result:
            logger.warning(f"  {stat_type}: no 'player' key in result")
            return None
        df = _flatten(result["player"])
        if "Player" in df.columns:
            df = df[df["Player"].notna() & (df["Player"] != "Player")]
        logger.info(f"  {stat_type}: {len(df)} rows")
        return df
    except Exception as e:
        logger.warning(f"  {stat_type} failed: {e}")
        return None


async def fetch_all_player_stats() -> list[dict]:
    """
    Scrape FBref Big 5 Combined standard + defensive tables,
    return ALL players with ≥ MIN_MINUTES across all positions.
    """
    fbref = FBref()

    std_df  = await _fetch_fbref_table(fbref, "standard")
    if std_df is None:
        raise RuntimeError("Standard table required — cannot build cache without it")

    # Merge defensive table (adds tackles + interceptions)
    def_df = await _fetch_fbref_table(fbref, "defensive")
    if def_df is not None:
        try:
            p_std  = _find_col(std_df, ["Player"]) or "Player"
            s_std  = _find_col(std_df, ["Squad"])  or "Squad"
            p_def  = _find_col(def_df, ["Player"]) or "Player"
            s_def  = _find_col(def_df, ["Squad"])  or "Squad"
            tkl_c  = _find_col(def_df, ["Tkl"])
            int_c  = _find_col(def_df, ["Int"])
            keep   = [c for c in [p_def, s_def, tkl_c, int_c] if c and c in def_df.columns]
            slim   = def_df[keep].rename(columns={p_def: p_std, s_def: s_std})
            new_c  = [c for c in slim.columns if c not in std_df.columns or c in [p_std, s_std]]
            std_df = std_df.merge(slim[new_c], on=[p_std, s_std], how="left")
            logger.info("Defensive table merged")
        except Exception as e:
            logger.warning(f"Defensive merge failed: {e}")

    # Merge goal-shot-creation table (adds SCA/GCA)
    gsc_df = await _fetch_fbref_table(fbref, "goal and shot creation")
    if gsc_df is not None:
        try:
            p_gsc = _find_col(gsc_df, ["Player"]) or "Player"
            s_gsc = _find_col(gsc_df, ["Squad"])  or "Squad"
            sca_c = _find_col(gsc_df, ["SCA"])
            gca_c = _find_col(gsc_df, ["GCA"])
            keep  = [c for c in [p_gsc, s_gsc, sca_c, gca_c] if c and c in gsc_df.columns]
            slim  = gsc_df[keep].rename(columns={p_gsc: "Player", s_gsc: "Squad"})
            new_c = [c for c in slim.columns if c not in std_df.columns or c in ["Player","Squad"]]
            std_df = std_df.merge(slim[new_c], on=["Player","Squad"], how="left")
            logger.info("GSC table merged")
        except Exception as e:
            logger.warning(f"GSC merge failed: {e}")

    # Resolve canonical columns
    col_map = {want: _find_col(std_df, cands) for want, cands in _COL_ALIASES.items()}
    col_map = {k: v for k, v in col_map.items() if v}

    missing = {"player", "position", "minutes"} - set(col_map.keys())
    if missing:
        raise ValueError(f"Missing essential columns: {missing}")

    # Filter to ≥ MIN_MINUTES
    min_col = col_map["minutes"]
    std_df[min_col] = pd.to_numeric(std_df[min_col], errors="coerce").fillna(0)
    std_df = std_df[std_df[min_col] >= MIN_MINUTES].copy()
    logger.info(f"Players with ≥{MIN_MINUTES} min: {len(std_df)}")

    players: list[dict] = []
    for _, row in std_df.iterrows():
        minutes  = _safe_float(row.get(col_map["minutes"], 0))
        nineties = minutes / 90 if minutes > 0 else 0.001

        age_raw = row.get(col_map.get("age", "") or "Age", 0)
        if isinstance(age_raw, str) and "-" in age_raw:
            age_raw = age_raw.split("-")[0]
        try:
            age = int(float(age_raw)) if pd.notna(age_raw) else 0
        except Exception:
            age = 0

        position = str(row.get(col_map.get("position") or "Pos", "")).strip()
        if not position or position == "nan":
            continue

        raw_metrics: dict = {}
        for key in STAT_KEYS:
            src = col_map.get(key)
            if not src or src not in row.index:
                continue
            val = _safe_float(row[src])
            if key == "pass_cmp_pct":
                raw_metrics[key] = {"value": val, "per90": val}
            else:
                raw_metrics[key] = {
                    "value": round(val, 2),
                    "per90": round(val / nineties, 3),
                }

        players.append({
            "name":          str(row.get(col_map.get("player") or "Player", "")).strip(),
            "position":      position,
            "squad":         str(row.get(col_map.get("squad") or "Squad", "")).strip(),
            "league":        str(row.get(col_map.get("league") or "Comp", "")).strip(),
            "age":           age,
            "minutes":       int(minutes),
            "season_90s":    round(minutes / 90, 1),
            "raw_metrics":   raw_metrics,
            "coverage_flag": "good",
        })

    # Compute within-position percentiles
    for pos_code in ["FW", "MF", "DF", "GK"]:
        cohort = [p for p in players if pos_code in p["position"].upper()]
        for key in STAT_KEYS:
            vals = [p["raw_metrics"].get(key, {}).get("per90", 0) for p in cohort]
            arr  = np.array(vals, dtype=float)
            if arr.max() == 0:
                continue
            ranks = (arr.argsort().argsort() / max(len(arr) - 1, 1) * 100).round().astype(int)
            for p, pct in zip(cohort, ranks):
                if key in p["raw_metrics"]:
                    p["raw_metrics"][key]["percentile"] = int(pct)

    return players


# ── Capology wage scraping ─────────────────────────────────────────────────

_CAPOLOGY_LEAGUES = {
    "Premier League": "England Premier League",
    "La Liga":        "Spain La Liga",
    "Bundesliga":     "Germany Bundesliga",
    "Serie A":        "Italy Serie A",
    "Ligue 1":        "France Ligue 1",
}


def _scrape_capology_league(league_name: str) -> list[dict]:
    """Scrape one Capology league — runs in a thread (Selenium)."""
    cap = Capology()
    try:
        df = cap.scrape_league(league_name, currency="eur")
        if df is None or df.empty:
            return []
        df.columns = [str(c).lower().replace(" ", "_") for c in df.columns]
        results = []
        for _, row in df.iterrows():
            name  = str(row.get("player", "") or row.get("name", "")).strip()
            wage  = row.get("weekly_gross_base_salary_(eur)", 0) or row.get("wage", 0) or 0
            club  = str(row.get("club", "") or row.get("team", "")).strip()
            try:
                wage = int(float(str(wage).replace(",","").replace("€","").strip()))
            except Exception:
                wage = 0
            if name and wage > 0:
                results.append({"name": name, "squad": club, "wage_eur": wage})
        logger.info(f"Capology {league_name}: {len(results)} players with wages")
        return results
    except Exception as e:
        logger.warning(f"Capology {league_name} failed: {e}")
        return []


async def fetch_all_wages() -> list[dict]:
    """Fetch wages from Capology for all 5 leagues concurrently."""
    loop = asyncio.get_event_loop()
    all_wages: list[dict] = []
    for display_name, capology_name in _CAPOLOGY_LEAGUES.items():
        logger.info(f"Fetching Capology wages: {display_name}...")
        try:
            wages = await _run_in_thread(_scrape_capology_league, capology_name, timeout=120)
            for w in wages:
                w["league"] = display_name
            all_wages.extend(wages)
            time.sleep(2)   # be polite between leagues
        except Exception as e:
            logger.warning(f"  {display_name} wages failed: {e}")
    logger.info(f"Total wage records: {len(all_wages)}")
    return all_wages


# ── SQLite storage ─────────────────────────────────────────────────────────

def create_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS player_cohort (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL,
            position   TEXT NOT NULL,
            squad      TEXT,
            league     TEXT,
            age        INTEGER,
            minutes    INTEGER,
            season     TEXT NOT NULL,
            raw_data   TEXT NOT NULL,
            scraped_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_cohort_pos_season
        ON player_cohort (position, season)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS player_wages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL,
            squad      TEXT,
            league     TEXT,
            wage_eur   INTEGER,
            season     TEXT NOT NULL,
            scraped_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_wages_name
        ON player_wages (name, season)
    """)
    conn.commit()


def save_players(conn: sqlite3.Connection, players: list[dict], season: str) -> None:
    now = datetime.utcnow().isoformat()
    conn.execute("DELETE FROM player_cohort WHERE season = ?", (season,))
    conn.executemany(
        "INSERT INTO player_cohort (name, position, squad, league, age, minutes, season, raw_data, scraped_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (p["name"], p["position"], p["squad"], p["league"],
             p["age"], p["minutes"], season, json.dumps(p), now)
            for p in players if p.get("name")
        ],
    )
    conn.commit()
    logger.info(f"Saved {len(players)} players to player_cohort")


def save_wages(conn: sqlite3.Connection, wages: list[dict], season: str) -> None:
    now = datetime.utcnow().isoformat()
    conn.execute("DELETE FROM player_wages WHERE season = ?", (season,))
    conn.executemany(
        "INSERT INTO player_wages (name, squad, league, wage_eur, season, scraped_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (w["name"], w.get("squad",""), w.get("league",""), w["wage_eur"], season, now)
            for w in wages if w.get("wage_eur", 0) > 0
        ],
    )
    conn.commit()
    logger.info(f"Saved {len(wages)} wage records to player_wages")


def merge_wages_into_cohort(conn: sqlite3.Connection, season: str) -> None:
    """Join wages into the player_cohort raw_data JSON."""
    wage_lookup: dict[str, int] = {}
    rows = conn.execute(
        "SELECT name, squad, wage_eur FROM player_wages WHERE season = ?", (season,)
    ).fetchall()
    for name, squad, wage in rows:
        wage_lookup[name.lower()] = wage
        wage_lookup[f"{name.lower()}_{squad.lower()}"] = wage

    cohort_rows = conn.execute(
        "SELECT id, name, squad, raw_data FROM player_cohort WHERE season = ?", (season,)
    ).fetchall()

    updated = 0
    for row_id, name, squad, raw_data_str in cohort_rows:
        p = json.loads(raw_data_str)
        wage = (
            wage_lookup.get(f"{name.lower()}_{squad.lower()}")
            or wage_lookup.get(name.lower())
        )
        if wage and wage > 0:
            p["wage_eur"]       = wage
            p["wage_estimated"] = False
            p["wage_source"]    = "capology"
            conn.execute(
                "UPDATE player_cohort SET raw_data = ? WHERE id = ?",
                (json.dumps(p), row_id),
            )
            updated += 1

    conn.commit()
    logger.info(f"Merged real wages into {updated}/{len(cohort_rows)} player records")


# ── Main ──────────────────────────────────────────────────────────────────

async def main(skip_wages: bool = False) -> None:
    if not _SCRAPERFC_OK:
        raise RuntimeError("ScraperFC not available — Chrome/Botasaurus required")

    CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
    logger.info(f"Building cache → {CACHE_DB}")

    conn = sqlite3.connect(str(CACHE_DB))
    create_schema(conn)

    # ── Step 1: FBref stats ────────────────────────────────────────────────
    logger.info("=== Step 1: FBref player stats ===")
    players = await fetch_all_player_stats()
    logger.info(f"Total starters scraped: {len(players)}")

    pos_counts = {}
    for p in players:
        pos = p["position"].split(",")[0].strip()
        pos_counts[pos] = pos_counts.get(pos, 0) + 1
    for pos, cnt in sorted(pos_counts.items()):
        logger.info(f"  {pos}: {cnt} players")

    save_players(conn, players, SEASON_CODE)

    # ── Step 2: Capology wages ─────────────────────────────────────────────
    if not skip_wages:
        logger.info("=== Step 2: Capology wages ===")
        wages = await fetch_all_wages()
        if wages:
            save_wages(conn, wages, SEASON_CODE)
            merge_wages_into_cohort(conn, SEASON_CODE)
        else:
            logger.warning("No wage data fetched — keeping league estimates")
    else:
        logger.info("Wage scraping skipped (--no-wages)")

    conn.close()
    logger.info(f"Cache build complete: {len(players)} players in {CACHE_DB}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build FBref + Capology player cache")
    parser.add_argument("--no-wages", action="store_true", help="Skip Capology wage scraping")
    args = parser.parse_args()
    asyncio.run(main(skip_wages=args.no_wages))
