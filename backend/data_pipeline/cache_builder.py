"""
One-time cache builder — the ONLY place that scrapes.

Everything the app serves at request time comes out of data/fbref_cache.db.
This script is what puts it there. Run it once (and again when you want fresh
numbers); the MCP server then answers every query from SQLite in ~0.1 s without
ever launching Chrome.

What it builds:
  player_cohort   — every Big-5 player with ≥ MIN_MINUTES, carrying all 14 metrics
                    the trait engine consumes, each with value / per90 / percentile
  player_wages    — real weekly wages from Capology per Big-5 league

FBref tables merged (a full Big-5 combined table takes ~2 min each, so a cold
build is roughly 10-15 minutes):
  standard                 goals assists xg xa shots shots_on_target prg_passes prg_carries
  defensive                tackles interceptions
  passing                  pass_cmp_pct
  possession               att_pen_touches
  goal and shot creation   sca gca

Run:
  python -m backend.data_pipeline.cache_builder                 # full build
  python -m backend.data_pipeline.cache_builder --no-wages      # skip Capology
  python -m backend.data_pipeline.cache_builder --enrich-only   # keep existing
                                                                # players, just
                                                                # backfill metrics
  python -m backend.data_pipeline.cache_builder --verify        # report coverage
"""
from __future__ import annotations

import argparse
import asyncio
import concurrent.futures as _futures
import json
import logging
import sqlite3
import statistics
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

ROOT         = Path(__file__).resolve().parent.parent.parent
CACHE_DB     = ROOT / "data" / "fbref_cache.db"
STAGE_DIR    = ROOT / "data" / "_build_stage"   # per-table scrape checkpoints
# Source for the metrics FBref will not serve (see _LEGACY_ONLY_METRICS).
DEFAULT_LEGACY_DB = ROOT / "data" / "fbref_cache.db.bak"
SEASON_CODE  = "2425"
SEASON_LABEL = "2024-2025"
#: Capology labels the same season "2024-25" where FBref wants "2024-2025".
#: Passing FBref's label was rejected outright — "2024-2025 is not a valid year"
#: — which is one of the reasons no wage has ever landed in the cache. Derived
#: from SEASON_CODE so the two cannot drift apart when the season rolls over.
CAPOLOGY_SEASON = f"20{SEASON_CODE[:2]}-{SEASON_CODE[2:]}"
MIN_MINUTES  = 450   # matches the default floor in get_position_cohort

# A Big-5 combined table takes 110-140 s. 60 s (the old value) times out on a
# perfectly healthy response and looks like FBref blocking us.
SCRAPE_TIMEOUT = 300

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
    FBref = Capology = None
    _SCRAPERFC_OK = False

# ── Canonical metric → FBref column ──────────────────────────────────────────
_COL_ALIASES: dict[str, list[str]] = {
    "player":          ["Player"],
    "position":        ["Pos"],
    "squad":           ["Squad"],
    "league":          ["Comp"],
    "age":             ["Age"],
    "minutes":         ["Min"],
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
    # From Sofascore — these are what make goalkeeper and midfielder briefs
    # scorable, so they must be ranked like any other metric. Percentages are
    # listed too: percentile ranks are still useful for the *_pct fields even
    # though the trait engine reads their raw value.
    "duels_won_pct", "ground_duels_won_pct", "aerial_won_pct", "tackles_won_pct",
    "dribble_success_pct", "clearances", "key_passes", "big_chances_created",
    "saves", "possession_won_att_third",
]
# Percentages must not be divided by 90s.
_PCT_KEYS = {"pass_cmp_pct"}

# Which extra table supplies which metric. Keys are ScraperFC stat_type names.
_MERGE_TABLES: list[tuple[str, list[str]]] = [
    ("defensive",              ["Tkl", "Int"]),
    ("passing",                ["Cmp%"]),
    ("possession",             ["Att Pen"]),
    ("goal and shot creation", ["SCA", "GCA"]),
    ("shooting",               ["Sh", "SoT"]),
]

# FBref does not serve its expected-goals or progression columns to this scraper
# — xG, xAG, PrgP and PrgC are absent from every table it returns. They are
# backfilled from a legacy cache instead (--legacy-db), matched on player name.
_LEGACY_ONLY_METRICS = ["xg", "xa", "prg_passes", "prg_carries"]

_CAPOLOGY_LEAGUES = {
    "Premier League": "England Premier League",
    "La Liga":        "Spain La Liga",
    "Bundesliga":     "Germany Bundesliga",
    "Serie A":        "Italy Serie A",
    "Ligue 1":        "France Ligue 1",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _find_col(df: pd.DataFrame, candidates: list[str]) -> str | None:
    low = {c.lower(): c for c in df.columns}
    for c in candidates:
        if c.lower() in low:
            return low[c.lower()]
    return None


def _flatten(df: pd.DataFrame) -> pd.DataFrame:
    """
    Flatten ScraperFC MultiIndex columns to single-level names, keeping the
    first of any duplicate. FBref repeats short labels across column groups —
    'passing' carries four 'Cmp%' columns (total/short/medium/long) and
    'defensive' carries two 'Tkl' columns (tackles/challenges). The first
    occurrence is the totals column in both cases, which is what we want.
    """
    df = df.copy()
    df.columns = [
        c[1].strip() if isinstance(c, tuple) and len(c) > 1 and c[1].strip()
        else (c[0].strip() if isinstance(c, tuple) else str(c))
        for c in df.columns
    ]
    df = df.loc[:, ~df.columns.duplicated()]
    return df


def _safe_float(val) -> float:
    try:
        return float(val) if pd.notna(val) else 0.0
    except Exception:
        return 0.0


def _normalise_name(name: str) -> str:
    """Diacritic-free lowercase key — must match server.py's _normalise_name."""
    decomposed = unicodedata.normalize("NFKD", str(name))
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(stripped.lower().split())


async def _run_in_thread(fn, *args, timeout: int = SCRAPE_TIMEOUT):
    """Run a blocking ScraperFC call in a thread with a hard timeout."""
    loop = asyncio.get_event_loop()
    pool = _futures.ThreadPoolExecutor(max_workers=1)
    try:
        return await asyncio.wait_for(loop.run_in_executor(pool, fn, *args), timeout=timeout)
    except asyncio.TimeoutError:
        raise RuntimeError(f"Scrape timed out after {timeout}s")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _kill_stale_chrome() -> None:
    """
    Reap Chrome instances left over from a previous botasaurus run.

    A timed-out scrape is abandoned mid-flight (the worker thread is not
    cancellable), so its browser survives and holds the debugging port. The next
    attempt then fails with 'target machine actively refused it'. Only browsers
    launched from botasaurus's own profile directory are touched.
    """
    try:
        import psutil
    except ImportError:
        return

    killed = 0
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            if not (proc.info["name"] or "").lower().startswith("chrome"):
                continue
            cmdline = " ".join(proc.info["cmdline"] or [])
            if "botasaurus" not in cmdline.lower():
                continue
            proc.kill()
            killed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    if killed:
        logger.info(f"  reaped {killed} stale botasaurus Chrome process(es)")


def _drop_empty_metrics(players: list[dict]) -> list[str]:
    """
    Remove metrics that are zero for every player, and return their names.

    FBref serves the Big-5 tables with most data cells blank — only the repeated
    header rows carry text — so a column can parse cleanly and still be entirely
    empty. Observed consistently across separate scrapes for Tkl, Cmp%, Att Pen,
    SCA and GCA, while TklW, Int, Gls, Ast, Sh and SoT come through fine.

    Storing the empties as 0.0 is worse than storing nothing: the trait engine
    would score every player identically on that metric and the fit engine would
    report full coverage for a trait nothing was measured on. Dropping them turns
    a silent wrong answer into an honest coverage gap the LLM is told to reason
    about qualitatively.
    """
    dropped: list[str] = []
    for key in STAT_KEYS:
        present = [p for p in players if key in p["raw_metrics"]]
        if not present:
            continue
        if any(p["raw_metrics"][key].get("value") for p in present):
            continue
        for p in present:
            del p["raw_metrics"][key]
        dropped.append(key)

    if dropped:
        logger.warning(
            f"Dropped all-zero metrics (FBref returned empty columns): {dropped}. "
            "Traits needing them become qualitative gaps rather than fake zeros. "
            "Supply them via --legacy-db if you have another source."
        )
    return dropped


def _compute_percentiles(players: list[dict]) -> None:
    """Rank every metric within each broad position group, in place."""
    for pos_code in ["FW", "MF", "DF", "GK"]:
        cohort = [p for p in players if pos_code in p["position"].upper()]
        if not cohort:
            continue
        for key in STAT_KEYS:
            vals = [p["raw_metrics"].get(key, {}).get("per90", 0) for p in cohort]
            arr = np.array(vals, dtype=float)
            if arr.max() == 0:
                continue
            ranks = (arr.argsort().argsort() / max(len(arr) - 1, 1) * 100).round().astype(int)
            for p, pct in zip(cohort, ranks):
                if key in p["raw_metrics"]:
                    p["raw_metrics"][key]["percentile"] = int(pct)


# ── FBref scraping ───────────────────────────────────────────────────────────

def _stage_club_tables(stat_type: str, result: dict) -> None:
    """
    Persist the squad-for and squad-against tables that came back with the
    player table. club_profile_builder reads these; nothing else does, so a
    failure here must never fail the player build.
    """
    slug = stat_type.replace(" ", "_")
    for side in ("squad", "opponent"):
        table = result.get(side)
        if table is None or getattr(table, "empty", True):
            continue
        try:
            _flatten(table).to_pickle(STAGE_DIR / f"{SEASON_CODE}_club_{side}_{slug}.pkl")
        except Exception as exc:
            logger.warning(f"  {stat_type}: could not stage {side} table ({exc})")


async def _fetch_table(fbref, stat_type: str, attempts: int = 3) -> pd.DataFrame | None:
    """
    Fetch one FBref stat table (Big 5 Combined) as a flat DataFrame.

    Each successful table is staged to disk, so re-running the builder after a
    Chrome crash resumes instead of refetching the tables that already worked.
    Botasaurus's headless Chrome is flaky enough that a cold 5-table build fails
    outright more often than not without this.
    """
    staged = STAGE_DIR / f"{SEASON_CODE}_{stat_type.replace(' ', '_')}.pkl"
    if staged.exists():
        df = pd.read_pickle(staged)
        logger.info(f"  {stat_type}: {len(df)} rows (staged, skipping fetch)")
        return df

    for attempt in range(1, attempts + 1):
        logger.info(
            f"Fetching FBref '{stat_type}' table "
            f"(attempt {attempt}/{attempts}, up to {SCRAPE_TIMEOUT}s)..."
        )
        _kill_stale_chrome()
        t0 = time.time()
        try:
            result = await _run_in_thread(
                fbref.scrape_stats, SEASON_LABEL, "FBref Big 5 Combined", stat_type
            )
            if not result or "player" not in result:
                raise ValueError("no 'player' table in result")
            df = _flatten(result["player"])
            if "Player" in df.columns:
                df = df[df["Player"].notna() & (df["Player"] != "Player")]
            if df.empty:
                raise ValueError("empty player table")
            STAGE_DIR.mkdir(parents=True, exist_ok=True)
            df.to_pickle(staged)
            # scrape_stats fetches squad-for and squad-against alongside the
            # player table on the same page load. Discarding them meant the club
            # profiles had no measured basis while their data sat in memory.
            _stage_club_tables(stat_type, result)
            logger.info(f"  {stat_type}: {len(df)} rows in {time.time() - t0:.0f}s (staged)")
            return df
        except Exception as e:
            logger.warning(f"  {stat_type} attempt {attempt} failed after "
                           f"{time.time() - t0:.0f}s: {e}")
            if attempt < attempts:
                await asyncio.sleep(10)

    logger.error(f"  {stat_type}: giving up after {attempts} attempts")
    return None


def _merge_table(base: pd.DataFrame, extra: pd.DataFrame, wanted: list[str]) -> pd.DataFrame:
    """Left-join `wanted` columns from `extra` onto `base`, keyed on Player+Squad."""
    p_base = _find_col(base, ["Player"]) or "Player"
    s_base = _find_col(base, ["Squad"]) or "Squad"
    p_ex = _find_col(extra, ["Player"])
    s_ex = _find_col(extra, ["Squad"])
    if not p_ex or not s_ex:
        logger.warning("  merge skipped — no Player/Squad key in extra table")
        return base

    cols = [c for c in (_find_col(extra, [w]) for w in wanted) if c]
    if not cols:
        logger.warning(f"  merge skipped — none of {wanted} present")
        return base

    slim = extra[[p_ex, s_ex] + cols].rename(columns={p_ex: p_base, s_ex: s_base})
    slim = slim.drop_duplicates(subset=[p_base, s_base])
    # Don't clobber a column the base table already supplies.
    keep = [c for c in cols if c not in base.columns]
    if not keep:
        logger.info(f"  merge no-op — {cols} already present in base")
        return base
    merged = base.merge(slim[[p_base, s_base] + keep], on=[p_base, s_base], how="left")
    logger.info(f"  merged {keep}")
    return merged


# Categories whose squad-for / squad-against tables the club profiles need.
# Kept separate from _MERGE_TABLES because these are wanted for the team-level
# indicators, not for any player metric:
#   possession            touches by zone, the territory signal
#   defensive             tackles by third, the press-height signal
#   advanced goalkeeping  GK #OPA and AvgDist — a direct read on line height
#   passing / pass types  directness, crosses, through balls, corners
#   goal and shot creation  SCA/GCA by type — set-piece vs take-on creation
#   misc                  aerials, fouls, offsides won
_CLUB_TABLES = [
    "possession", "defensive", "advanced goalkeeping",
    "passing", "pass types", "goal and shot creation", "misc",
]


async def fetch_club_tables(force: bool = False) -> int:
    """
    Stage the FBref squad-for / squad-against tables for the club profiles.

    Separate from the player build on purpose: _fetch_table short-circuits on a
    staged player pickle, so a cache that is already built would never reach the
    squad tables. This walks the categories the club indicators need and stages
    both sides of each.
    """
    fbref = FBref()
    staged = 0
    for stat_type in _CLUB_TABLES:
        slug = stat_type.replace(" ", "_")
        target = STAGE_DIR / f"{SEASON_CODE}_club_squad_{slug}.pkl"
        if target.exists() and not force:
            logger.info(f"  {stat_type}: club tables already staged, skipping")
            staged += 1
            continue

        logger.info(f"Fetching FBref club tables for '{stat_type}'...")
        _kill_stale_chrome()
        t0 = time.time()
        try:
            result = await _run_in_thread(
                fbref.scrape_stats, SEASON_LABEL, "FBref Big 5 Combined", stat_type
            )
            STAGE_DIR.mkdir(parents=True, exist_ok=True)
            _stage_club_tables(stat_type, result or {})
            if target.exists():
                staged += 1
                logger.info(f"  {stat_type}: staged in {time.time() - t0:.0f}s")
            else:
                logger.warning(f"  {stat_type}: no squad table returned")
        except Exception as exc:
            logger.warning(f"  {stat_type} failed after {time.time() - t0:.0f}s: {exc}")

    logger.info(f"Club tables staged for {staged}/{len(_CLUB_TABLES)} categories")
    return staged


async def fetch_all_player_stats() -> list[dict]:
    """Scrape and merge every FBref table into one player record list."""
    fbref = FBref()

    std_df = await _fetch_table(fbref, "standard")
    if std_df is None:
        raise RuntimeError("Standard table is required — cannot build the cache without it")

    for stat_type, wanted in _MERGE_TABLES:
        extra = await _fetch_table(fbref, stat_type)
        if extra is None:
            logger.error(f"'{stat_type}' unavailable — {wanted} will be missing from the cache")
            continue
        std_df = _merge_table(std_df, extra, wanted)

    col_map = {want: _find_col(std_df, cands) for want, cands in _COL_ALIASES.items()}
    col_map = {k: v for k, v in col_map.items() if v}

    missing_essential = {"player", "position", "minutes"} - set(col_map)
    if missing_essential:
        raise ValueError(f"Missing essential columns: {missing_essential}")

    absent = [k for k in STAT_KEYS if k not in col_map]
    if absent:
        logger.warning(f"Metrics unavailable in the scraped tables: {absent}")

    min_col = col_map["minutes"]
    std_df[min_col] = pd.to_numeric(
        std_df[min_col].astype(str).str.replace(",", ""), errors="coerce"
    ).fillna(0)
    std_df = std_df[std_df[min_col] >= MIN_MINUTES].copy()
    logger.info(f"Players with >= {MIN_MINUTES} min: {len(std_df)}")

    players: list[dict] = []
    for _, row in std_df.iterrows():
        name = str(row.get(col_map["player"], "")).strip()
        position = str(row.get(col_map["position"], "")).strip()
        if not name or name == "nan" or not position or position == "nan":
            continue

        minutes = _safe_float(row.get(min_col, 0))
        nineties = minutes / 90 if minutes > 0 else 0.001

        age_raw = row.get(col_map.get("age", "Age"), 0)
        if isinstance(age_raw, str) and "-" in age_raw:
            age_raw = age_raw.split("-")[0]
        try:
            age = int(float(age_raw)) if pd.notna(age_raw) else 0
        except Exception:
            age = 0

        raw_metrics: dict = {}
        for key in STAT_KEYS:
            src = col_map.get(key)
            if not src or src not in row.index:
                continue
            val = _safe_float(row[src])
            raw_metrics[key] = (
                {"value": val, "per90": val} if key in _PCT_KEYS
                else {"value": round(val, 2), "per90": round(val / nineties, 3)}
            )

        players.append({
            "name":          name,
            "position":      position,
            "squad":         str(row.get(col_map.get("squad", "Squad"), "")).strip(),
            "league":        str(row.get(col_map.get("league", "Comp"), "")).strip(),
            "age":           age,
            "minutes":       int(minutes),
            "season_90s":    round(nineties, 1),
            "raw_metrics":   raw_metrics,
            "coverage_flag": "good" if minutes >= 900 else "low",
        })

    # Percentiles are computed by the caller, after the legacy backfill has
    # added its metrics — otherwise those metrics would have no ranks.
    return players


# ── Capology wage scraping ───────────────────────────────────────────────────

def _scrape_capology_league(league_name: str) -> list[dict]:
    """
    Scrape one Capology league — runs in a worker thread.

    Capology drives Selenium and sits behind Cloudflare, so this fails more often
    than it succeeds. That is tolerable: merge_wages_into_cohort stamps a
    league-tier estimate on anyone it cannot price, so no player is ever left at
    €0 (which used to make the financial agent treat them as free).
    """
    cap = Capology()
    try:
        df = cap.scrape_salaries(CAPOLOGY_SEASON, league_name, "eur")
        if df is None or df.empty:
            return []
        # Capology returns a two-level column index, so a flattened name reads
        # "('est._fixed_salary',_'gross_fixed_p/w\n(eur)')" rather than "wage".
        # Matching those by equality against "player" found nothing and the
        # whole league was discarded with "no wage/name column" — after the
        # scrape had already succeeded. Match on substrings instead.
        df.columns = [
            str(c).lower().replace("\n", " ").replace(" ", "_") for c in df.columns
        ]

        def _find(*needles: str, exclude: tuple[str, ...] = ()) -> str | None:
            for col in df.columns:
                if any(x in col for x in exclude):
                    continue
                if all(n in col for n in needles):
                    return col
            return None

        # Weekly gross is the figure the rest of the pipeline assumes; "p/w" is
        # how Capology spells it. Take it ahead of any yearly column, and never
        # fall back to one — a p/y figure silently read as weekly would put
        # every player 52x over their real wage.
        wage_col = (
            _find("gross", "p/w")
            or _find("weekly", "gross")
            or _find("salary", "p/w")
            or _find("wage", exclude=("p/y", "bonus", "total"))
        )
        name_col = _find("player") or _find("name")
        club_col = _find("club") or _find("team") or _find("squad")
        if not wage_col or not name_col:
            logger.warning(f"Capology {league_name}: no wage/name column in {list(df.columns)}")
            return []

        results = []
        for _, row in df.iterrows():
            name = str(row.get(name_col, "")).strip()
            try:
                wage = int(float(
                    str(row.get(wage_col, 0)).replace(",", "").replace("€", "").strip() or 0
                ))
            except Exception:
                wage = 0
            if name and wage > 0:
                results.append({
                    "name": name,
                    "squad": str(row.get(club_col, "")).strip() if club_col else "",
                    "wage_eur": wage,
                })
        logger.info(f"Capology {league_name}: {len(results)} players with wages")
        return results
    except Exception as e:
        logger.warning(f"Capology {league_name} failed: {e}")
        return []


#: Attempts per league before giving up on it.
#:
#: Clearing Cloudflare is probabilistic, not deterministic — the same request
#: that is served the interstitial once goes straight through on the next try.
#: A single attempt per league therefore loses whole leagues to a coin flip,
#: which is indistinguishable from the site being permanently blocked.
_CAPOLOGY_ATTEMPTS = 3


async def fetch_all_wages() -> list[dict]:
    """Fetch wages from Capology for all five leagues, sequentially and politely."""
    all_wages: list[dict] = []
    for display_name, capology_name in _CAPOLOGY_LEAGUES.items():
        for attempt in range(1, _CAPOLOGY_ATTEMPTS + 1):
            logger.info(
                f"Fetching Capology wages: {display_name} "
                f"(attempt {attempt}/{_CAPOLOGY_ATTEMPTS})..."
            )
            try:
                wages = await _run_in_thread(_scrape_capology_league, capology_name)
            except Exception as e:
                logger.warning(f"  {display_name} wages failed: {e}")
                wages = []
            if wages:
                for w in wages:
                    w["league"] = display_name
                all_wages.extend(wages)
                logger.info(f"  {display_name}: {len(wages)} wage rows")
                break
            if attempt < _CAPOLOGY_ATTEMPTS:
                # A slightly longer pause than the polite one; the challenge is
                # more likely to pass once the previous session has aged out.
                await asyncio.sleep(10)
        else:
            logger.warning(
                f"  {display_name}: no wages after {_CAPOLOGY_ATTEMPTS} attempts"
            )
        await asyncio.sleep(2)
    logger.info(f"Total wage records: {len(all_wages)}")
    return all_wages


# ── SQLite storage ───────────────────────────────────────────────────────────

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
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cohort_pos_season ON player_cohort (position, season)"
    )
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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_wages_name ON player_wages (name, season)")
    conn.commit()


def save_players(conn: sqlite3.Connection, players: list[dict], season: str) -> None:
    now = datetime.utcnow().isoformat()
    conn.execute("DELETE FROM player_cohort WHERE season = ?", (season,))
    conn.executemany(
        "INSERT INTO player_cohort (name, position, squad, league, age, minutes, season, raw_data, scraped_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (p["name"], p["position"], p["squad"], p["league"],
             p["age"], p["minutes"], season, json.dumps(p, ensure_ascii=False), now)
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
            (w["name"], w.get("squad", ""), w.get("league", ""), w["wage_eur"], season, now)
            for w in wages if w.get("wage_eur", 0) > 0
        ],
    )
    conn.commit()
    logger.info(f"Saved {len(wages)} wage records to player_wages")


def merge_wages_into_cohort(conn: sqlite3.Connection, season: str) -> None:
    """
    Stamp a wage onto every cached player: the real Capology figure where we have
    one, otherwise the median for that league. Nobody is left at 0, which is what
    used to make the financial agent treat unknown players as free.
    """
    wage_rows = conn.execute(
        "SELECT name, squad, league, wage_eur FROM player_wages WHERE season = ?", (season,)
    ).fetchall()

    by_name_squad: dict[str, int] = {}
    by_name: dict[str, int] = {}
    by_league: dict[str, list[int]] = {}
    for name, squad, league, wage in wage_rows:
        if not wage:
            continue
        key = _normalise_name(name)
        by_name.setdefault(key, wage)
        by_name_squad[f"{key}|{_normalise_name(squad)}"] = wage
        by_league.setdefault((league or "").lower(), []).append(wage)

    league_median = {lg: int(statistics.median(ws)) for lg, ws in by_league.items() if ws}

    cohort_rows = conn.execute(
        "SELECT id, name, squad, league, minutes, age, raw_data FROM player_cohort WHERE season = ?",
        (season,),
    ).fetchall()

    real = median = estimated = 0
    for row_id, name, squad, league, minutes, age, raw in cohort_rows:
        p = json.loads(raw)
        key = _normalise_name(name)
        wage = by_name_squad.get(f"{key}|{_normalise_name(squad or '')}") or by_name.get(key)

        if wage:
            p.update(wage_eur=int(wage), wage_estimated=False, wage_source="capology")
            real += 1
        elif league_median.get((league or "").lower()):
            p.update(
                wage_eur=league_median[(league or "").lower()],
                wage_estimated=True,
                wage_source="league_median",
            )
            median += 1
        else:
            p.update(
                wage_eur=_fallback_wage(league, int(minutes or 0), int(age or 0)),
                wage_estimated=True,
                wage_source="league_tier",
            )
            estimated += 1

        conn.execute(
            "UPDATE player_cohort SET raw_data = ? WHERE id = ?",
            (json.dumps(p, ensure_ascii=False), row_id),
        )

    conn.commit()
    logger.info(
        f"Wages stamped: {real} real, {median} league-median, {estimated} tier-estimated "
        f"(of {len(cohort_rows)})"
    )


def enrich_from_sofascore(players: list[dict], season_label: str = "24/25") -> list[str]:
    """
    Fill the metrics FBref will not serve, from Sofascore, in place.

    Adds tackles and pass completion (empty from FBref for every player) plus
    duels, aerial duels, clearances and saves, which FBref never offered at all.
    Those last four are what make midfielder and goalkeeper briefs scorable —
    before this every candidate for those positions came out at zero coverage.

    Returns the metric names it actually populated.
    """
    from .sofascore_source import fetch_big5, _fold, PERCENT_FIELDS

    try:
        source = fetch_big5(season_label)
    except Exception as exc:
        logger.warning(f"Sofascore enrichment unavailable ({exc}) — skipping")
        return []
    if not source:
        return []

    added: dict[str, int] = {}
    matched = 0
    for p in players:
        record = source.get(_fold(p["name"]))
        if not record:
            continue
        matched += 1
        nineties = max(float(p.get("minutes") or 0) / 90, 0.1)
        for key, value in record.items():
            if key in ("minutes", "squad", "league"):
                continue
            # Never overwrite a metric FBref actually supplied.
            existing = p["raw_metrics"].get(key)
            if existing and existing.get("value"):
                continue
            is_pct = key in PERCENT_FIELDS
            p["raw_metrics"][key] = {
                "value": round(value, 2),
                "per90": round(value, 2) if is_pct else round(value / nineties, 3),
                "source": "sofascore",
            }
            added[key] = added.get(key, 0) + 1

    logger.info(
        f"Sofascore matched {matched}/{len(players)} cached players; "
        f"populated {sorted(added)}"
    )
    return sorted(added)


def backfill_from_legacy(players: list[dict], legacy_db: Path, season: str) -> None:
    """
    Copy metrics FBref will not serve out of an older cache, in place.

    ScraperFC's FBref parse returns no xG, xAG, PrgP or PrgC columns in any
    table, so those four have to come from elsewhere. A previous cache built
    from the Kaggle 2024-25 export carries exactly them. Matching is on the
    accent-folded name, which is stable across both sources.
    """
    if not legacy_db.exists():
        logger.warning(f"No legacy cache at {legacy_db} — skipping backfill of "
                       f"{_LEGACY_ONLY_METRICS}")
        return

    conn = sqlite3.connect(str(legacy_db))
    try:
        rows = conn.execute(
            "SELECT name, raw_data FROM player_cohort WHERE season = ?", (season,)
        ).fetchall()
    except Exception as exc:
        logger.warning(f"Legacy cache unreadable ({exc}) — skipping backfill")
        return
    finally:
        conn.close()

    legacy = {_normalise_name(name): json.loads(raw) for name, raw in rows}
    logger.info(f"Legacy cache: {len(legacy)} players available for backfill")

    filled = 0
    for p in players:
        source = legacy.get(_normalise_name(p["name"]))
        if not source:
            continue
        source_metrics = source.get("raw_metrics", {})
        added = False
        for key in _LEGACY_ONLY_METRICS:
            if key in p["raw_metrics"] or key not in source_metrics:
                continue
            entry = dict(source_metrics[key])
            entry.pop("percentile", None)   # recomputed against this cohort
            entry["source"] = "legacy_cache"
            p["raw_metrics"][key] = entry
            added = True
        filled += added

    missing = len(players) - filled
    logger.info(
        f"Backfilled {_LEGACY_ONLY_METRICS} for {filled}/{len(players)} players"
        + (f"; {missing} unmatched" if missing else "")
    )


def _fallback_wage(league: str, minutes: int, age: int = 0) -> int:
    """
    Modelled weekly wage, shared with the MCP server's `get_player_salary`.

    Deliberately the same function rather than a second copy: the builder used to
    have its own three-bucket table, so every Premier League starter was stamped
    at a flat EUR 55,000 in the cache. The server's improved estimator never got
    a chance to run, because the scouter skips the lookup once a player already
    carries a wage — so the stale figure won and every shortlist showed the same
    number for every candidate.
    """
    from server import _estimate_wage  # backend/mcp_server is on sys.path

    return _estimate_wage(league, minutes, age)


# ── Verification ─────────────────────────────────────────────────────────────

def verify(season: str = SEASON_CODE) -> bool:
    """Report per-metric coverage. Returns True when every metric is fully populated."""
    if not CACHE_DB.exists():
        logger.error(f"No cache at {CACHE_DB} — run the builder first")
        return False

    conn = sqlite3.connect(str(CACHE_DB))
    rows = conn.execute(
        "SELECT raw_data FROM player_cohort WHERE season = ?", (season,)
    ).fetchall()
    wage_count = conn.execute(
        "SELECT COUNT(*) FROM player_wages WHERE season = ?", (season,)
    ).fetchone()[0]
    conn.close()

    total = len(rows)
    if not total:
        logger.error(f"player_cohort is empty for season {season}")
        return False

    present = {k: 0 for k in STAT_KEYS}
    nonzero = {k: 0 for k in STAT_KEYS}
    with_pct = {k: 0 for k in STAT_KEYS}
    priced = 0
    for (raw,) in rows:
        p = json.loads(raw)
        for k, v in p.get("raw_metrics", {}).items():
            if k in present:
                present[k] += 1
                if v.get("value"):
                    nonzero[k] += 1
                if v.get("percentile") is not None:
                    with_pct[k] += 1
        if p.get("wage_eur"):
            priced += 1

    logger.info(f"Cache verification — {total} players, season {season}")
    complete = True
    for k in STAT_KEYS:
        pct = present[k] / total * 100
        # A column that is present but zero for everyone is not coverage — it is
        # the failure mode that made "measurable_fit" a single-metric ranking.
        ok = pct > 99 and nonzero[k] > 0
        complete = complete and ok
        logger.info(
            f"  {'OK  ' if ok else 'GAP '} {k:<18} {present[k]:>5}/{total} ({pct:5.1f}%)  "
            f"nonzero: {nonzero[k]:>5}  percentiles: {with_pct[k]:>5}"
        )
    logger.info(f"  wages on players: {priced}/{total};  player_wages rows: {wage_count}")
    return complete


# ── Main ─────────────────────────────────────────────────────────────────────

async def main(
    skip_wages: bool = False,
    enrich_only: bool = False,
    legacy_db: Path | None = None,
    skip_sofascore: bool = False,
) -> None:
    if not _SCRAPERFC_OK:
        raise RuntimeError("ScraperFC not available — Chrome/Botasaurus required")

    CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
    logger.info(f"Building cache -> {CACHE_DB}")

    conn = sqlite3.connect(str(CACHE_DB))
    create_schema(conn)

    logger.info("=== Step 1: FBref player stats ===")
    players = await fetch_all_player_stats()
    logger.info(f"Total players: {len(players)}")

    logger.info("=== Step 1b: Backfill metrics FBref does not serve ===")
    _drop_empty_metrics(players)
    backfill_from_legacy(players, legacy_db or DEFAULT_LEGACY_DB, SEASON_CODE)
    if not skip_sofascore:
        logger.info("=== Step 1c: Sofascore enrichment ===")
        enrich_from_sofascore(players)
    _compute_percentiles(players)

    pos_counts: dict[str, int] = {}
    for p in players:
        pos = p["position"].split(",")[0].strip()
        pos_counts[pos] = pos_counts.get(pos, 0) + 1
    for pos, cnt in sorted(pos_counts.items()):
        logger.info(f"  {pos}: {cnt}")

    if enrich_only:
        # Keep whoever is already cached; only refresh their metrics.
        existing = {
            _normalise_name(n)
            for (n,) in conn.execute(
                "SELECT name FROM player_cohort WHERE season = ?", (SEASON_CODE,)
            )
        }
        if existing:
            before = len(players)
            players = [p for p in players if _normalise_name(p["name"]) in existing]
            logger.info(f"--enrich-only: kept {len(players)} of {before} scraped players")

    save_players(conn, players, SEASON_CODE)

    if not skip_wages:
        logger.info("=== Step 2: Capology wages ===")
        wages = await fetch_all_wages()
        if wages:
            save_wages(conn, wages, SEASON_CODE)
        else:
            logger.warning("No wage data fetched — falling back to league estimates")
    else:
        logger.info("Wage scraping skipped (--no-wages)")

    # Always stamp a wage, even with no Capology data, so nothing is left at 0.
    merge_wages_into_cohort(conn, SEASON_CODE)
    conn.close()

    logger.info("=== Verification ===")
    verify(SEASON_CODE)
    logger.info(f"Cache build complete: {len(players)} players in {CACHE_DB}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build the FBref + Capology player cache (run once)")
    parser.add_argument("--no-wages", action="store_true", help="Skip Capology wage scraping")
    parser.add_argument("--enrich-only", action="store_true",
                        help="Only refresh metrics for players already in the cache")
    parser.add_argument("--verify", action="store_true",
                        help="Report cache coverage and exit without scraping")
    parser.add_argument("--fresh", action="store_true",
                        help="Discard staged tables and re-scrape everything from FBref")
    parser.add_argument("--no-sofascore", action="store_true",
                        help="Skip the Sofascore pass that fills tackles, pass %%, "
                             "duels and saves")
    parser.add_argument("--sofascore-only", action="store_true",
                        help="Only run the Sofascore enrichment against the existing "
                             "cache (no FBref scraping)")
    parser.add_argument("--restamp-wages", action="store_true",
                        help="Recompute modelled wages on the existing cache and exit "
                             "(no scraping)")
    parser.add_argument("--club-tables", action="store_true",
                        help="Stage only the FBref squad/opponent tables used by "
                             "club_profile_builder, then exit")
    parser.add_argument("--legacy-db", type=Path, default=DEFAULT_LEGACY_DB,
                        help=f"Cache to backfill {_LEGACY_ONLY_METRICS} from "
                             f"(default: {DEFAULT_LEGACY_DB.name})")
    args = parser.parse_args()

    if args.verify:
        sys.exit(0 if verify() else 1)

    if args.club_tables:
        count = asyncio.run(fetch_club_tables(force=args.fresh))
        sys.exit(0 if count else 1)

    if args.sofascore_only:
        conn = sqlite3.connect(str(CACHE_DB))
        create_schema(conn)
        rows = conn.execute(
            "SELECT id, raw_data FROM player_cohort WHERE season = ?", (SEASON_CODE,)
        ).fetchall()
        cached = [(rid, json.loads(raw)) for rid, raw in rows]
        players = [p for _, p in cached]
        logger.info(f"Enriching {len(players)} cached players from Sofascore")
        enrich_from_sofascore(players)
        _compute_percentiles(players)
        for (rid, _), p in zip(cached, players):
            conn.execute("UPDATE player_cohort SET raw_data = ? WHERE id = ?",
                         (json.dumps(p, ensure_ascii=False), rid))
        conn.commit()
        conn.close()
        verify(SEASON_CODE)
        sys.exit(0)

    if args.restamp_wages:
        conn = sqlite3.connect(str(CACHE_DB))
        create_schema(conn)
        merge_wages_into_cohort(conn, SEASON_CODE)
        conn.close()
        verify(SEASON_CODE)
        sys.exit(0)

    if args.fresh and STAGE_DIR.exists():
        for f in STAGE_DIR.glob("*.pkl"):
            f.unlink()
        logger.info("Cleared staged tables (--fresh)")

    asyncio.run(main(
        skip_wages=args.no_wages,
        enrich_only=args.enrich_only,
        legacy_db=args.legacy_db,
        skip_sofascore=args.no_sofascore,
    ))
