"""
Attach a season-aligned market value to every cached player.

The cache pairs 2024-25 performance with valuations fetched today. That is not a
small discrepancy: Mohamed Salah is stored at EUR 22M, which is a fair price for
a 34-year-old in 2026 and less than half the EUR 50M he was worth at the end of
the season the system actually scouts. Erling Haaland is stored at EUR 220M
against EUR 180M. Every fee decision — the ceiling filter, the contract-aware
discount, the wage modelled from value — inherits the error.

Transfermarkt publishes the full valuation history per player, so the fix is to
read the value as it stood at the end of 2024-25 rather than the value now.

    python -m backend.data_pipeline.backfill_market_values            # cached ids only
    python -m backend.data_pipeline.backfill_market_values --resolve  # also look up missing players
    python -m backend.data_pipeline.backfill_market_values --limit 50 # try a slice first

The current value is kept. Both are stored, so a future "value them today" mode
does not need a re-scrape.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("backfill_mv")

CACHE_DB = ROOT / "data" / "fbref_cache.db"

#: The valuation is taken as it stood on the last update at or before this date.
#: 1 July 2025 is the start of the window in which a 2024-25 season would be
#: acted on, and Transfermarkt's end-of-season revaluation lands in late May.
AS_OF = datetime(2025, 7, 1)

GRAPH_URL = "https://www.transfermarkt.com/ceapi/marketValueDevelopment/graph/{}"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}
#: Same politeness gap the profile scraper uses.
_MIN_GAP = 1.5
_last_request = 0.0

_PLAYER_ID = re.compile(r"/spieler/(\d+)")


def _throttle() -> None:
    global _last_request
    gap = time.time() - _last_request
    if gap < _MIN_GAP:
        time.sleep(_MIN_GAP - gap)
    _last_request = time.time()


def value_history(player_id: str) -> list[dict]:
    """Full valuation history for one player, oldest first. [] when unavailable."""
    _throttle()
    try:
        resp = requests.get(GRAPH_URL.format(player_id), headers=_HEADERS, timeout=25)
        if resp.status_code != 200:
            return []
        return resp.json().get("list") or []
    except Exception as exc:
        logger.debug("history %s failed: %s", player_id, exc)
        return []


def value_as_of(history: list[dict], as_of: datetime = AS_OF) -> tuple[int, str] | None:
    """
    The most recent valuation at or before `as_of`.

    Returns (eur, "dd/mm/yyyy"), or None when the player had no published value
    that early — a teenager who only appears later, typically. Returning None
    rather than the earliest available point matters: the first datapoint can be
    years off, and a stale value is worse than an absent one because the fee
    gate treats an absent value as "cannot price" and keeps the candidate.
    """
    best: tuple[datetime, dict] | None = None
    for point in history:
        raw = point.get("datum_mw")
        if not raw:
            continue
        try:
            when = datetime.strptime(raw, "%d/%m/%Y")
        except ValueError:
            continue
        if when <= as_of and (best is None or when > best[0]):
            best = (when, point)
    if best is None:
        return None
    value = best[1].get("y")
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    return int(value), best[0].strftime("%d/%m/%Y")


def _player_id_from(payload: dict) -> str | None:
    match = _PLAYER_ID.search(str(payload.get("profile_url") or ""))
    return match.group(1) if match else None


def backfill(resolve_missing: bool = False, limit: int | None = None) -> int:
    conn = sqlite3.connect(str(CACHE_DB), timeout=30)
    rows = conn.execute("SELECT name_key, payload FROM player_market_value").fetchall()

    todo: list[tuple[str, dict, str]] = []
    no_id = 0
    for name_key, raw in rows:
        payload = json.loads(raw)
        player_id = _player_id_from(payload)
        if not player_id:
            no_id += 1
            continue
        todo.append((name_key, payload, player_id))

    if limit:
        todo = todo[:limit]

    logger.info(
        "%d cached rows, %d with a player id, %d without (skipped)",
        len(rows), len(todo), no_id,
    )

    updated = unchanged = missing = 0
    for i, (name_key, payload, player_id) in enumerate(todo, start=1):
        history = value_history(player_id)
        found = value_as_of(history)
        if not found:
            missing += 1
            continue
        value, when = found
        current = payload.get("market_value_eur")
        payload["market_value_asof_eur"] = value
        payload["market_value_asof_date"] = when
        payload["market_value_current_eur"] = current
        conn.execute(
            "UPDATE player_market_value SET payload = ? WHERE name_key = ?",
            (json.dumps(payload), name_key),
        )
        if current and abs(value - current) / max(current, 1) > 0.05:
            updated += 1
        else:
            unchanged += 1
        if i % 25 == 0:
            conn.commit()
            logger.info(
                "  %d/%d  (%d materially different, %d ~same, %d no value that early)",
                i, len(todo), updated, unchanged, missing,
            )
    conn.commit()

    logger.info(
        "done: %d players stamped, %d materially different from today's value, "
        "%d unchanged, %d had no published value by %s",
        updated + unchanged, updated, unchanged, missing, AS_OF.date(),
    )

    if resolve_missing:
        logger.warning(
            "--resolve is not implemented yet: it needs a Transfermarkt profile "
            "search per player, which is the slow path. %d cohort players still "
            "have no cached valuation at all.",
            conn.execute(
                "SELECT COUNT(*) FROM player_cohort WHERE season = '2425'"
            ).fetchone()[0] - len(rows),
        )

    conn.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--resolve", action="store_true",
                    help="also look up players with no cached valuation (slow)")
    ap.add_argument("--limit", type=int, help="only process the first N rows")
    args = ap.parse_args()
    return backfill(resolve_missing=args.resolve, limit=args.limit)


if __name__ == "__main__":
    sys.exit(main())
