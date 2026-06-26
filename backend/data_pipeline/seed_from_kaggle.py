"""
Seed player cache from Kaggle dataset.

Dataset: https://www.kaggle.com/datasets/orkunaktas/all-football-players-stats-in-top-5-leagues-2425
Contains all Big-5 league players from 2024-25 FBref standard stats.

Usage:
  # Option A — Kaggle API (auto-download)
  python -m backend.data_pipeline.seed_from_kaggle

  # Option B — local CSV file you downloaded from Kaggle
  python -m backend.data_pipeline.seed_from_kaggle --csv path/to/file.csv

Requirements for Option A:
  pip install kagglehub
  Have Kaggle credentials in ~/.kaggle/kaggle.json
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

ROOT        = Path(__file__).resolve().parent.parent.parent
CACHE_DB    = ROOT / "data" / "fbref_cache.db"
SEASON_CODE = "2425"
MIN_MINUTES = 900   # true starters

KAGGLE_DATASET = "orkunaktas/all-football-players-stats-in-top-5-leagues-2425"

# ── Column name aliases (Kaggle/FBref variations) ─────────────────────────────
_COL = {
    "player":   ["Player", "player", "Name"],
    "position": ["Pos", "pos", "Position"],
    "squad":    ["Squad", "squad", "Team", "Club"],
    "league":   ["Comp", "comp", "League", "Competition"],
    "age":      ["Age", "age"],
    "minutes":  ["Min", "min", "Minutes", "Mins"],
    "nineties": ["90s", "90s_played", "Nineties"],
    "goals":    ["Gls", "gls", "Goals"],
    "assists":  ["Ast", "ast", "Assists"],
    "xg":       ["xG", "xg", "expected_goals"],
    "xa":       ["xAG", "xag", "xA", "xa"],
    "shots":    ["Sh", "sh", "Shots"],
    "sot":      ["SoT", "sot"],
    "prg_p":    ["PrgP", "prgp", "Progressive_Passes"],
    "prg_c":    ["PrgC", "prgc", "Progressive_Carries"],
}


def _find(df: pd.DataFrame, candidates: list[str]) -> str | None:
    low = {c.lower(): c for c in df.columns}
    for c in candidates:
        if c.lower() in low:
            return low[c.lower()]
    return None


def _safe(val) -> float:
    try:
        return float(val) if pd.notna(val) else 0.0
    except Exception:
        return 0.0


def _league_label(raw: str) -> str:
    """Normalise FBref competition name → display league name."""
    raw = str(raw).strip()
    MAP = {
        "eng premier league": "Premier League",
        "premier league":     "Premier League",
        "es la liga":         "La Liga",
        "la liga":            "La Liga",
        "de bundesliga":      "Bundesliga",
        "bundesliga":         "Bundesliga",
        "it serie a":         "Serie A",
        "serie a":            "Serie A",
        "fr ligue 1":         "Ligue 1",
        "ligue 1":            "Ligue 1",
    }
    key = raw.lower()
    return MAP.get(key, raw)


def _pos_code(raw: str) -> str:
    """Map fine position (FW,MF,DF,GK and combos) to broad FBref code."""
    raw = str(raw).upper().strip()
    if "GK" in raw:
        return "GK"
    if "DF" in raw:
        return "DF"
    if "MF" in raw:
        return "MF"
    if "FW" in raw:
        return "FW"
    # Fine positions
    map2 = {
        "CF": "FW", "ST": "FW", "LW": "FW", "RW": "FW", "SS": "FW",
        "AM": "MF", "CM": "MF", "DM": "MF", "LM": "MF", "RM": "MF",
        "CB": "DF", "LB": "DF", "RB": "DF", "LWB": "DF", "RWB": "DF",
    }
    for k, v in map2.items():
        if k in raw:
            return v
    return raw[:2] or "MF"


def process_csv(csv_path: str | Path) -> list[dict]:
    """Read a Kaggle/FBref CSV and return player records in cache format."""
    df = pd.read_csv(csv_path)
    logger.info(f"CSV loaded: {len(df)} rows, columns: {df.columns.tolist()}")

    # Resolve column names
    c = {k: _find(df, v) for k, v in _COL.items()}
    missing = [k for k, v in c.items() if v is None and k in ("player", "minutes")]
    if missing:
        raise ValueError(f"Cannot find essential columns in CSV: {missing}\nAvailable: {df.columns.tolist()}")

    # Convert minutes to numeric
    if c["minutes"]:
        df[c["minutes"]] = pd.to_numeric(
            df[c["minutes"]].astype(str).str.replace(",", ""), errors="coerce"
        ).fillna(0)
        df = df[df[c["minutes"]] >= MIN_MINUTES].copy()

    logger.info(f"After ≥{MIN_MINUTES} min filter: {len(df)} players")

    # Build 90s-played column if missing
    if not c["nineties"] and c["minutes"]:
        df["_90s"] = df[c["minutes"]] / 90
        c["nineties"] = "_90s"

    players: list[dict] = []
    for _, row in df.iterrows():
        name = str(row.get(c["player"], "")).strip() if c["player"] else ""
        if not name or name in ("Player", "nan", ""):
            continue

        minutes  = _safe(row.get(c["minutes"], 0)) if c["minutes"] else 0
        nineties = _safe(row.get(c["nineties"], 0)) if c["nineties"] else (minutes / 90)
        if nineties <= 0:
            nineties = max(minutes / 90, 0.001)

        pos_raw  = str(row.get(c["position"], "")).strip() if c["position"] else ""
        league   = _league_label(str(row.get(c["league"], "")).strip()) if c["league"] else ""

        age_raw = row.get(c["age"], 0) if c["age"] else 0
        if isinstance(age_raw, str) and "-" in age_raw:
            age_raw = age_raw.split("-")[0]
        try:
            age = int(float(age_raw)) if pd.notna(age_raw) else 0
        except Exception:
            age = 0

        # Build raw_metrics
        rm: dict[str, dict] = {}
        METRIC_MAP = {
            "goals":           c.get("goals"),
            "assists":         c.get("assists"),
            "xg":              c.get("xg"),
            "xa":              c.get("xa"),
            "shots":           c.get("shots"),
            "shots_on_target": c.get("sot"),
            "prg_passes":      c.get("prg_p"),
            "prg_carries":     c.get("prg_c"),
        }
        for key, col in METRIC_MAP.items():
            if not col or col not in row.index:
                continue
            val = _safe(row[col])
            rm[key] = {"value": round(val, 2), "per90": round(val / nineties, 3)}

        players.append({
            "name":          name,
            "position":      _pos_code(pos_raw),
            "squad":         str(row.get(c["squad"], "")).strip() if c["squad"] else "",
            "league":        league,
            "age":           age,
            "minutes":       int(minutes),
            "season_90s":    round(nineties, 1),
            "raw_metrics":   rm,
            "coverage_flag": "good",
        })

    logger.info(f"Built {len(players)} player records")

    # Compute within-position percentiles
    for pos_code in ["FW", "MF", "DF", "GK"]:
        cohort = [p for p in players if p["position"] == pos_code]
        if not cohort:
            continue
        for key in METRIC_MAP.keys():
            vals = [p["raw_metrics"].get(key, {}).get("per90", 0) for p in cohort]
            arr  = np.array(vals, dtype=float)
            if arr.max() == 0:
                continue
            ranks = (arr.argsort().argsort() / max(len(arr)-1, 1) * 100).round().astype(int)
            for p, pct in zip(cohort, ranks):
                if key in p["raw_metrics"]:
                    p["raw_metrics"][key]["percentile"] = int(pct)

    return players


def write_to_cache(players: list[dict]) -> None:
    CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(CACHE_DB))
    conn.execute("""CREATE TABLE IF NOT EXISTS player_cohort (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT, position TEXT, squad TEXT, league TEXT,
        age INTEGER, minutes INTEGER, season TEXT,
        raw_data TEXT, scraped_at TEXT)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pos_season ON player_cohort (position, season)")
    conn.execute("DELETE FROM player_cohort WHERE season = ?", (SEASON_CODE,))

    now = datetime.utcnow().isoformat()
    conn.executemany(
        "INSERT INTO player_cohort (name, position, squad, league, age, minutes, season, raw_data, scraped_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        [(p["name"], p["position"], p["squad"], p["league"],
          p["age"], p["minutes"], SEASON_CODE, json.dumps(p), now)
         for p in players if p.get("name")]
    )
    conn.commit()

    rows = conn.execute("SELECT position, COUNT(*) FROM player_cohort GROUP BY position").fetchall()
    conn.close()
    total = sum(cnt for _, cnt in rows)
    logger.info(f"Wrote {total} players to {CACHE_DB}")
    for pos, cnt in sorted(rows):
        logger.info(f"  {pos}: {cnt}")


def download_from_kaggle() -> Path:
    """Download the dataset via kagglehub. Requires ~/.kaggle/kaggle.json."""
    try:
        import kagglehub
    except ImportError:
        raise ImportError("Install kagglehub: pip install kagglehub")

    logger.info(f"Downloading {KAGGLE_DATASET} via Kaggle API...")
    path = kagglehub.dataset_download(KAGGLE_DATASET)
    path = Path(path)
    logger.info(f"Downloaded to: {path}")

    # Find the CSV inside the downloaded directory
    csvs = list(path.glob("**/*.csv"))
    if not csvs:
        raise FileNotFoundError(f"No CSV found in {path}")
    logger.info(f"Found CSVs: {[c.name for c in csvs]}")
    return csvs[0]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", help="Path to locally downloaded CSV file")
    args = parser.parse_args()

    if args.csv:
        csv_path = Path(args.csv)
        if not csv_path.exists():
            raise FileNotFoundError(f"CSV not found: {csv_path}")
    else:
        csv_path = download_from_kaggle()

    players = process_csv(csv_path)
    if not players:
        raise RuntimeError("No players extracted — check CSV format")

    write_to_cache(players)
    logger.info("Done. Restart the MCP server to use the new cache.")


if __name__ == "__main__":
    main()
