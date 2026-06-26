"""
FBref Cache Builder — weekly player data refresh.

Fetches all position cohorts (FW / MF / DF / GK) from FBref using requests + pandas
(no Chrome required) and writes them to data/fbref_cache.db.

Usage:
    python -m backend.data_pipeline.cache_builder            # all positions
    python -m backend.data_pipeline.cache_builder --pos FW   # one position

Run by GitHub Actions on a weekly schedule; output committed to the repo so the
deployed container always has fresh data without needing Chrome.
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import requests

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

ROOT      = Path(__file__).resolve().parent.parent.parent
CACHE_DB  = ROOT / "data" / "fbref_cache.db"
SEASON    = "2024-2025"
SEASON_CODE = "2425"

POSITIONS = ["FW", "MF", "DF", "GK"]

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://fbref.com/en/comps/Big5/",
    "DNT": "1",
    "Connection": "keep-alive",
}

# FBref HTML table IDs to parse (Big 5 combined)
_FBREF_URLS = {
    "standard":   f"https://fbref.com/en/comps/Big5/{SEASON}/stats/players/{SEASON}-Big-5-European-Leagues-Stats",
    "defensive":  f"https://fbref.com/en/comps/Big5/{SEASON}/defense/players/{SEASON}-Big-5-European-Leagues-Defense-Stats",
    "passing":    f"https://fbref.com/en/comps/Big5/{SEASON}/passing/players/{SEASON}-Big-5-European-Leagues-Passing-Stats",
    "possession": f"https://fbref.com/en/comps/Big5/{SEASON}/possession/players/{SEASON}-Big-5-European-Leagues-Possession-Stats",
    "gsc":        f"https://fbref.com/en/comps/Big5/{SEASON}/gca/players/{SEASON}-Big-5-European-Leagues-Goal-And-Shot-Creation-Stats",
}

# Canonical column aliases  (ordered: first match wins)
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


# ── HTML → DataFrame ──────────────────────────────────────────────────────────

def _fetch_table(url: str, retries: int = 3) -> pd.DataFrame | None:
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=30)
            if resp.status_code == 429:
                logger.warning(f"Rate limited on {url} — sleeping 60s")
                time.sleep(60)
                continue
            resp.raise_for_status()
            tables = pd.read_html(resp.text, header=[0, 1])
            if not tables:
                logger.warning(f"No tables found at {url}")
                return None
            df = tables[0].copy()
            # Flatten MultiIndex columns
            df.columns = [
                c[1].strip() if isinstance(c, tuple) and c[1].strip() else c[0].strip()
                if isinstance(c, tuple) else str(c)
                for c in df.columns
            ]
            df = df.loc[:, ~df.columns.duplicated()]
            # Drop "Unnamed" and squad-level sub-header rows
            if "Player" in df.columns:
                df = df[df["Player"].notna() & (df["Player"] != "Player")]
            return df
        except Exception as exc:
            logger.warning(f"Attempt {attempt + 1}/{retries} failed for {url}: {exc}")
            time.sleep(5 * (attempt + 1))
    return None


def _find_col(df: pd.DataFrame, candidates: list[str]) -> str | None:
    low = {c.lower(): c for c in df.columns}
    for c in candidates:
        if c.lower() in low:
            return low[c.lower()]
    return None


def _safe_float(val) -> float:
    try:
        return float(val) if pd.notna(val) else 0.0
    except Exception:
        return 0.0


# ── Core fetch function ────────────────────────────────────────────────────────

def fetch_all_tables() -> dict[str, pd.DataFrame]:
    """Download all FBref stat tables; return {table_name: DataFrame}."""
    tables: dict[str, pd.DataFrame] = {}
    for name, url in _FBREF_URLS.items():
        logger.info(f"Fetching {name} table from {url}")
        df = _fetch_table(url)
        if df is not None:
            tables[name] = df
            logger.info(f"  → {len(df)} rows")
        else:
            logger.warning(f"  → FAILED (skipping {name})")
        time.sleep(3)  # be polite to FBref
    return tables


def build_player_records(tables: dict[str, pd.DataFrame]) -> list[dict]:
    """Merge tables and build the same format as get_position_cohort returns."""
    std = tables.get("standard")
    if std is None:
        raise ValueError("Standard table is required but missing")

    # Start with standard table, merge others on (Player, Squad)
    merge_base = std.copy()
    for name in ("defensive", "passing", "possession", "gsc"):
        t = tables.get(name)
        if t is None:
            continue
        p_col = _find_col(t, ["Player"]) or "Player"
        s_col = _find_col(t, ["Squad"])   or "Squad"
        keep_cols = [p_col, s_col] + [
            c for c in t.columns
            if c not in ("Player", "Squad", "Rk", "Matches", "Nation", "Born",
                         "Pos", "Age", "Comp", "Min", "90s", "MP", "Starts", "Subs")
            and c not in merge_base.columns
        ]
        keep_cols = list(dict.fromkeys(keep_cols))  # deduplicate
        slim = t[[c for c in keep_cols if c in t.columns]].copy()
        slim = slim.rename(columns={p_col: "Player", s_col: "Squad"})
        merge_base = merge_base.merge(slim, on=["Player", "Squad"], how="left")

    df = merge_base.copy()

    # Resolve canonical column names
    col_map: dict[str, str | None] = {
        want: _find_col(df, cands)
        for want, cands in _COL_ALIASES.items()
    }

    required = {"player", "position", "minutes"}
    missing  = required - {k for k, v in col_map.items() if v}
    if missing:
        raise ValueError(f"Missing essential columns: {missing}  (found: {list(col_map.keys())})")

    # Convert minutes to numeric
    min_col = col_map["minutes"]
    df[min_col] = pd.to_numeric(df[min_col], errors="coerce").fillna(0)

    # Drop rows with < 450 minutes
    df = df[df[min_col] >= 450].copy()

    players: list[dict] = []
    for _, row in df.iterrows():
        minutes  = _safe_float(row.get(min_col, 0))
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
            "coverage_flag": "good" if minutes >= 900 else "low",
        })

    # Compute within-full-cohort percentiles per stat
    for key in STAT_KEYS:
        vals = [p["raw_metrics"].get(key, {}).get("per90", 0) for p in players]
        arr  = np.array(vals, dtype=float)
        if arr.max() == 0:
            continue
        ranks = (arr.argsort().argsort() / max(len(arr) - 1, 1) * 100).round().astype(int)
        for p, pct in zip(players, ranks):
            if key in p["raw_metrics"]:
                p["raw_metrics"][key]["percentile"] = int(pct)

    return players


# ── SQLite storage ─────────────────────────────────────────────────────────────

def create_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS player_cohort (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL,
            position    TEXT    NOT NULL,
            squad       TEXT,
            league      TEXT,
            age         INTEGER,
            minutes     INTEGER,
            season      TEXT    NOT NULL,
            raw_data    TEXT    NOT NULL,
            scraped_at  TEXT    NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_cohort_pos_season
        ON player_cohort (position, season)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_cohort_league
        ON player_cohort (league, position, season)
    """)
    conn.commit()


def save_players(conn: sqlite3.Connection, players: list[dict], season: str) -> None:
    now = datetime.utcnow().isoformat()
    conn.execute("DELETE FROM player_cohort WHERE season = ?", (season,))
    conn.executemany(
        """INSERT INTO player_cohort (name, position, squad, league, age, minutes, season, raw_data, scraped_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                p["name"], p["position"], p["squad"], p["league"],
                p["age"], p["minutes"], season, json.dumps(p), now,
            )
            for p in players
            if p.get("name")
        ],
    )
    conn.commit()
    logger.info(f"Saved {len(players)} players to cache for season {season}")


# ── Entry point ────────────────────────────────────────────────────────────────

def main(positions: list[str] | None = None) -> None:
    CACHE_DB.parent.mkdir(parents=True, exist_ok=True)

    logger.info(f"Starting FBref cache build → {CACHE_DB}")
    logger.info(f"Season: {SEASON}")

    try:
        tables = fetch_all_tables()
        if not tables:
            raise RuntimeError("No tables fetched from FBref — aborting")

        all_players = build_player_records(tables)
        logger.info(f"Built {len(all_players)} player records total")

        # Filter to requested positions if specified
        if positions:
            pos_upper = [p.upper() for p in positions]
            all_players = [
                p for p in all_players
                if any(code in p["position"].upper() for code in pos_upper)
            ]
            logger.info(f"Filtered to {positions}: {len(all_players)} records")

        conn = sqlite3.connect(str(CACHE_DB))
        try:
            create_schema(conn)
            save_players(conn, all_players, SEASON_CODE)
        finally:
            conn.close()

        logger.info(f"Cache build complete: {CACHE_DB}")

    except Exception as exc:
        logger.error(f"Cache build FAILED: {exc}")
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build FBref player cache")
    parser.add_argument("--pos", nargs="*", help="Positions to fetch (FW MF DF GK)")
    args = parser.parse_args()
    main(positions=args.pos)
