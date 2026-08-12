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

#: Transfermarkt competition codes, so the club list can be fetched directly.
#:
#: ScraperFC's get_valid_seasons/get_club_links were doing this, but they route
#: through botasaurus and fail in ways this module cannot recover from — a
#: Ligue 1 run died inside the driver with "'NoneType' object has no attribute
#: 'find'" and then paused waiting for a keypress, which is not something a data
#: build should ever do. One page per league is little enough to fetch here.
_COMP_CODES = {
    "England Premier League": "GB1",
    "Spain La Liga":          "ES1",
    "Germany Bundesliga":     "L1",
    "Italy Serie A":          "IT1",
    "France Ligue 1":         "FR1",
}


def club_links_for(league: str) -> list[str]:
    """Every club URL in a league for the target season, or [] if unreachable."""
    import cloudscraper
    from bs4 import BeautifulSoup

    code = _COMP_CODES.get(league)
    if not code:
        return []
    url = (
        f"https://www.transfermarkt.us/-/startseite/wettbewerb/{code}"
        f"/plus/?saison_id={TM_SAISON_ID}"
    )
    for attempt in range(1, 4):
        scraper = cloudscraper.CloudScraper()
        try:
            _throttle()
            resp = scraper.get(url, timeout=30)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.content, "html.parser")
                seen: list[str] = []
                for anchor in soup.find_all("a", href=True):
                    match = _CLUB_ID.search(anchor["href"])
                    if not match:
                        continue
                    href = anchor["href"]
                    if "/startseite/" not in href:
                        continue
                    full = "https://www.transfermarkt.us" + href.split("?")[0]
                    if full not in seen:
                        seen.append(full)
                if seen:
                    return seen
            logger.warning(
                "%s: club listing attempt %d/3 -> HTTP %s",
                league, attempt, resp.status_code,
            )
        except Exception as exc:
            logger.warning("%s: club listing attempt %d/3 failed (%s)",
                           league, attempt, str(exc)[:90])
        finally:
            scraper.close()
        time.sleep(5 * attempt)
    return []

#: Seconds between Transfermarkt requests. The same politeness the player
#: lookup uses; this is only ~96 pages, so it costs a few minutes.
_MIN_GAP = 2.0
_last_request = 0.0

_CLUB_ID = re.compile(r"/verein/(\d+)")
_MULT = {"k": 1_000, "m": 1_000_000, "bn": 1_000_000_000, "b": 1_000_000_000}

#: Transfermarkt is a German site and its URL slugs keep German exonyms even on
#: the .us domain: AC Milan is "ac-mailand", Roma is "as-rom", Fiorentina is
#: "ac-florenz". No folding rule bridges those, so they are spelled out.
#:
#: Order matters for Turin. Transfermarkt calls Torino "fc-turin" and Juventus
#: "juventus-turin", so a bare turin -> torino rule would rename Juventus. The
#: longer key is applied first and the two stay distinct.
_TM_EXONYMS = [
    ("juventus turin", "juventus"),
    ("inter mailand", "inter"),
    # Keep the "ac" so this reads as "ac milan" and matches the cache's spelling.
    # Reducing it to a bare "milan" left it competing with Inter for the same
    # canonical name and reconcile dropped the loser.
    ("ac mailand", "ac milan"),
    ("espanyol barcelona", "espanyol"),
    ("real betis sevilla", "real betis"),
    ("ac florenz", "fiorentina"),
    ("as rom", "roma"),
    ("fc turin", "torino"),
    ("genua cfc", "genoa"),
    ("ssc neapel", "napoli"),
    ("ogc nizza", "nice"),
    ("nizza", "nice"),
    ("mailand", "milan"),
    ("florenz", "fiorentina"),
    ("neapel", "napoli"),
    ("munchen", "munich"),
    ("saragossa", "zaragoza"),
    ("mailands", "milan"),
    ("rom", "roma"),
    ("strassburg", "strasbourg"),
    ("fc paris saint germain", "psg"),
    ("paris saint germain", "psg"),
]


def _deutsch(name: str) -> str:
    """Rewrite Transfermarkt's German club spellings into the cache's."""
    text = " ".join(str(name).replace("-", " ").lower().split())
    for german, english in _TM_EXONYMS:
        if german in text:
            return text.replace(german, english)
    return text


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
    # Transfermarkt resets connections under sustained scraping, and without a
    # retry roughly half the club pages came back empty — 50 of 96 on a run
    # where every league had listed correctly. Those clubs then fell back to the
    # generic rule for no reason other than a dropped socket.
    for attempt in range(1, 4):
        scraper = cloudscraper.CloudScraper()
        try:
            _throttle()
            resp = scraper.get(url, timeout=30)
            if resp.status_code != 200:
                logger.debug("club %s -> HTTP %s", club_id.group(1), resp.status_code)
            else:
                record = _parse_transfer_record(resp.content)
                if record:
                    record["tm_club_id"] = club_id.group(1)
                    return record
        except Exception as exc:
            logger.debug("club %s attempt %d failed: %s", club_id.group(1), attempt, exc)
        finally:
            scraper.close()
        time.sleep(2 * attempt)
    return {}


def scrape_all_clubs(skip_slugs: set[str] | None = None) -> dict[str, dict]:
    """
    Transfer records for every Big-5 club, keyed by Transfermarkt slug.

    `skip_slugs` lets a re-run fetch only what is still missing. Transfermarkt
    drops connections unpredictably, so a run gets some subset of the clubs and
    the honest way to reach all of them is to accumulate across runs rather than
    to pretend one pass will do it.
    """
    skip_slugs = skip_slugs or set()
    out: dict[str, dict] = {}
    for league in _LEAGUES:
        links = club_links_for(league)
        if not links:
            logger.warning("%s: no club listing, skipping the league", league)
            continue
        logger.info("%s: %d clubs listed", league, len(links))
        got = 0
        for link in links:
            slug = link.split("/startseite/")[0].rstrip("/").rsplit("/", 1)[-1]
            if slug in skip_slugs:
                continue
            record = scrape_club_transfers(link)
            if record:
                record["league"] = league
                record["tm_slug"] = slug
                out[slug] = record
                got += 1
        logger.info("  %s: %d new transfer records", league, got)
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

    # Carry forward transfer records already stored, so a re-run adds to what we
    # have rather than starting from nothing. Without this, a run that reached
    # four leagues would overwrite a previous run that had reached the fifth.
    existing_records: dict[str, dict] = {}
    for club, payload in conn.execute(
        "SELECT club, payload FROM club_finances WHERE season = ?", (SEASON_CODE,)
    ):
        stored = json.loads(payload)
        if stored.get("transfer_expenditure_eur") is not None:
            existing_records[club] = stored
            if club in clubs:
                clubs[club].update({
                    k: v for k, v in stored.items()
                    if k.startswith("transfer_") or k in ("tm_club_id", "tm_slug")
                })
    if existing_records:
        logger.info("carried forward %d stored transfer records", len(existing_records))
    logger.info("payroll derived for %d clubs (no scraping)", len(clubs))
    if not clubs:
        logger.error(
            "player_wages is empty — run the cache builder's wage pass first, "
            "or there is nothing to derive a wage budget from."
        )

    if not wages_only:
        from backend.data_pipeline.club_names import reconcile

        already = {v.get("tm_slug") for v in existing_records.values() if v.get("tm_slug")}
        transfers = scrape_all_clubs(skip_slugs=already)
        logger.info(
            "transfer records: %d newly scraped, %d already held",
            len(transfers), len(already),
        )

        # Transfermarkt slugs carry the corporate prefix our squad names drop —
        # "fc-arsenal" against "Arsenal", "tottenham-hotspur" against
        # "Tottenham". Folding alone matched 3 of 96. reconcile already knows to
        # discard fc/afc/vfb and to bridge the pairs no rule can, so use it
        # rather than inventing a second, worse matcher here.
        readable = {slug: _deutsch(slug) for slug in transfers}
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
