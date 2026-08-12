"""
Real per-club finances: what each club actually pays, and actually spends.

Until now every club's budget was one generic YAML scaled by an LLM's sense of
that club's reputation, and `budget_basis` said so out loud — "scaled from
generic default rules, not the club's audited budget". Brentford's ceiling and
Real Madrid's came from the same file.

Two real sources replace that, and neither is a guess:

  Wage capacity comes from the club's own payroll, aggregated from the Capology
  wage rows already in the cache. Nothing is scraped for this — 96 of 96 Big-5
  clubs are covered by data we hold, from Real Madrid at EUR 5.53M/week down to
  Holstein Kiel at EUR 244k. That is a 23x spread the generic file could not
  express at all.

  Transfer capacity comes from what the club spent in the 2024-25 window,
  read off Transfermarkt's own transfer record. Manchester City: EUR 243.00m
  out, EUR 141.70m in, balance EUR -101.30m.

    python -m backend.data_pipeline.club_finances            # build the table
    python -m backend.data_pipeline.club_finances --wages-only   # skip scraping
    python -m backend.data_pipeline.club_finances --show Arsenal

Both figures are historical fact for the season being scouted, not a forecast.
What a club spent last summer is evidence of what it can spend, not a promise —
callers should present it as the former.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend" / "mcp_server"))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("club_finances")

CACHE_DB = ROOT / "data" / "fbref_cache.db"
SEASON_CODE = "2425"
#: Transfermarkt's id for the 2024-25 season.
TM_SAISON_ID = "2024"

_LEAGUES = [
    "England Premier League",
    "Spain La Liga",
    "Germany Bundesliga",
    "Italy Serie A",
    "France Ligue 1",
]

#: Seconds between Transfermarkt requests. The same politeness the player
#: lookup uses; this is only ~96 pages, so it costs a few minutes.
_MIN_GAP = 2.0
_last_request = 0.0

_CLUB_ID = re.compile(r"/verein/(\d+)")
_MULT = {"k": 1_000, "m": 1_000_000, "bn": 1_000_000_000, "b": 1_000_000_000}


def _throttle() -> None:
    global _last_request
    gap = time.time() - _last_request
    if gap < _MIN_GAP:
        time.sleep(_MIN_GAP - gap)
    _last_request = time.time()


def _money(amount: str, unit: str | None) -> int | None:
    """Turn Transfermarkt's "141.70 m" into whole euros."""
    try:
        value = float(str(amount).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return int(value * _MULT.get((unit or "").strip().lower(), 1))


# ── Wage capacity, from data already held ────────────────────────────────────

def payroll_by_club(conn: sqlite3.Connection) -> dict[str, dict]:
    """
    Weekly payroll per club, from the cached Capology wages.

    `top_earner` matters as much as the total: it is the strongest evidence of
    what a club will pay one individual, which is exactly the number the wage
    ceiling is trying to be.
    """
    rows = conn.execute(
        "SELECT squad, league, COUNT(*), SUM(wage_eur), MAX(wage_eur) "
        "FROM player_wages WHERE season = ? AND squad <> '' "
        "GROUP BY squad, league",
        (SEASON_CODE,),
    ).fetchall()
    return {
        squad: {
            "club": squad,
            "league": league,
            "squad_size": n,
            "weekly_wage_bill_eur": int(total or 0),
            "top_earner_weekly_eur": int(top or 0),
            "median_weekly_eur": None,  # filled below; needs the raw rows
        }
        for squad, league, n, total, top in rows
    }


def _add_medians(conn: sqlite3.Connection, clubs: dict[str, dict]) -> None:
    for squad, info in clubs.items():
        wages = [
            w for (w,) in conn.execute(
                "SELECT wage_eur FROM player_wages "
                "WHERE season = ? AND squad = ? AND wage_eur > 0 ORDER BY wage_eur",
                (SEASON_CODE, squad),
            )
        ]
        if wages:
            info["median_weekly_eur"] = wages[len(wages) // 2]


# ── Transfer capacity, from Transfermarkt ────────────────────────────────────

def _parse_transfer_record(html: bytes) -> dict:
    """
    Pull the "Transfer record" summary out of a club's transfers page.

    The box reads:
      Transfer record 24/25 Arrivals/Departures Fee Income 12 EUR 141.70 m
      Expenditure 15 EUR 243.00 m Overall balance EUR -101.30 m
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    out: dict = {}
    patterns = {
        "transfer_income_eur":      r"Income\s+\d+\s*€\s*([\d.,]+)\s*(bn|b|m|k)?",
        "transfer_expenditure_eur": r"Expenditure\s+\d+\s*€\s*([\d.,]+)\s*(bn|b|m|k)?",
        "transfer_balance_eur":     r"balance\s*€\s*(-?[\d.,]+)\s*(bn|b|m|k)?",
    }
    for field, pattern in patterns.items():
        match = re.search(pattern, text, re.I)
        if match:
            out[field] = _money(match.group(1), match.group(2))
    return out


def scrape_club_transfers(club_url: str) -> dict:
    """Transfer income and expenditure for one club in the target season."""
    import cloudscraper

    club_id = _CLUB_ID.search(club_url)
    if not club_id:
        return {}
    url = (
        f"https://www.transfermarkt.us/x/transfers/verein/{club_id.group(1)}"
        f"/saison_id/{TM_SAISON_ID}"
    )
    scraper = cloudscraper.CloudScraper()
    try:
        _throttle()
        resp = scraper.get(url, timeout=30)
        if resp.status_code != 200:
            logger.debug("club %s -> HTTP %s", club_id.group(1), resp.status_code)
            return {}
        record = _parse_transfer_record(resp.content)
        record["tm_club_id"] = club_id.group(1)
        return record
    except Exception as exc:
        logger.debug("club %s failed: %s", club_id.group(1), exc)
        return {}
    finally:
        scraper.close()


def scrape_all_clubs() -> dict[str, dict]:
    """Transfer records for every Big-5 club, keyed by Transfermarkt club name."""
    from ScraperFC.transfermarkt import Transfermarkt

    tm = Transfermarkt()
    out: dict[str, dict] = {}
    for league in _LEAGUES:
        try:
            seasons = tm.get_valid_seasons(league)
        except Exception as exc:
            logger.warning("%s: cannot list seasons (%s)", league, exc)
            continue
        # Keys look like "24/25"; fall back to whatever maps to our saison id.
        key = next(
            (k for k, v in seasons.items() if str(v) == TM_SAISON_ID),
            None,
        )
        if key is None:
            logger.warning("%s: no season maps to saison_id %s", league, TM_SAISON_ID)
            continue
        try:
            links = tm.get_club_links(key, league)
        except Exception as exc:
            logger.warning("%s: cannot list clubs (%s)", league, exc)
            continue
        logger.info("%s: %d clubs", league, len(links))
        for link in links:
            name = link.split("/startseite/")[0].rstrip("/").rsplit("/", 1)[-1]
            record = scrape_club_transfers(link)
            if record:
                record["league"] = league
                record["tm_slug"] = name
                out[name] = record
        logger.info("  %s: %d clubs with a transfer record", league, len(out))
    return out


# ── Storage ──────────────────────────────────────────────────────────────────

def create_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS club_finances (
            club        TEXT NOT NULL,
            league      TEXT,
            season      TEXT NOT NULL,
            payload     TEXT NOT NULL,
            scraped_at  TEXT NOT NULL,
            PRIMARY KEY (club, season)
        )
    """)
    conn.commit()


def save(conn: sqlite3.Connection, clubs: dict[str, dict]) -> None:
    from datetime import datetime

    now = datetime.utcnow().isoformat()
    conn.executemany(
        "INSERT OR REPLACE INTO club_finances (club, league, season, payload, scraped_at) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            (name, info.get("league", ""), SEASON_CODE, json.dumps(info), now)
            for name, info in clubs.items()
        ],
    )
    conn.commit()
    logger.info("saved %d clubs to club_finances", len(clubs))


def load(conn: sqlite3.Connection, club: str) -> dict | None:
    """Finances for one club, matched leniently on name."""
    from backend.data_pipeline.club_names import fold

    target = fold(club)
    for name, payload in conn.execute(
        "SELECT club, payload FROM club_finances WHERE season = ?", (SEASON_CODE,)
    ):
        if fold(name) == target:
            return json.loads(payload)
    return None


def build(wages_only: bool = False) -> int:
    conn = sqlite3.connect(str(CACHE_DB), timeout=30)
    create_schema(conn)

    clubs = payroll_by_club(conn)
    _add_medians(conn, clubs)
    logger.info("payroll derived for %d clubs (no scraping)", len(clubs))
    if not clubs:
        logger.error(
            "player_wages is empty — run the cache builder's wage pass first, "
            "or there is nothing to derive a wage budget from."
        )

    if not wages_only:
        from backend.data_pipeline.club_names import reconcile

        transfers = scrape_all_clubs()
        logger.info("transfer records scraped for %d clubs", len(transfers))

        # Transfermarkt slugs carry the corporate prefix our squad names drop —
        # "fc-arsenal" against "Arsenal", "tottenham-hotspur" against
        # "Tottenham". Folding alone matched 3 of 96. reconcile already knows to
        # discard fc/afc/vfb and to bridge the pairs no rule can, so use it
        # rather than inventing a second, worse matcher here.
        readable = {slug: slug.replace("-", " ") for slug in transfers}
        mapping, unmatched, unclaimed = reconcile(
            list(readable.values()), list(clubs)
        )
        by_name = {readable[slug]: record for slug, record in transfers.items()}

        matched = 0
        for source_name, squad in mapping.items():
            record = by_name.get(source_name)
            if not record:
                continue
            clubs[squad].update({
                k: v for k, v in record.items()
                if k.startswith("transfer_") or k == "tm_club_id"
            })
            matched += 1

        logger.info(
            "matched a transfer record to %d of %d clubs", matched, len(clubs)
        )
        if unmatched:
            logger.warning("no squad matched: %s", ", ".join(sorted(unmatched)[:8]))
        # Named explicitly: a club with no transfer record silently falls back
        # to the generic rule, and that is exactly the situation this module
        # exists to make visible.
        missing = [c for c in clubs if "transfer_expenditure_eur" not in clubs[c]]
        if missing:
            logger.warning(
                "%d clubs have payroll but no transfer record: %s",
                len(missing), ", ".join(sorted(missing)[:8]),
            )

    save(conn, clubs)
    conn.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wages-only", action="store_true",
                    help="derive payroll from cached wages, skip Transfermarkt")
    ap.add_argument("--show", help="print one club's finances and exit")
    args = ap.parse_args()

    if args.show:
        conn = sqlite3.connect(str(CACHE_DB), timeout=30)
        create_schema(conn)
        info = load(conn, args.show)
        conn.close()
        print(json.dumps(info, indent=2) if info else f"no finances stored for {args.show!r}")
        return 0 if info else 1

    return build(wages_only=args.wages_only)


if __name__ == "__main__":
    sys.exit(main())
