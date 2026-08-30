"""
Transfermarkt market-value and contract lookup.

A second financial source alongside Capology wages, and the only one that
supplies the two fields a signing decision actually turns on: what the player is
worth and when their contract runs out.

Plain HTTP with a browser user-agent — no Selenium, no Chrome — so it is safe to
call while serving a request. Results are cached in fbref_cache.db, so a repeated
lookup costs nothing and Transfermarkt sees one request per player per refresh
window.

Values are reported with the date Transfermarkt last updated them, because a
market value is a dated estimate, not a fact.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
import unicodedata
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger("scoutly_mcp.transfermarkt")

BASE = "https://www.transfermarkt.com"
SEARCH_URL = BASE + "/schnellsuche/ergebnis/schnellsuche?query={}"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_TIMEOUT = 20
# A cached row holds a *season-aligned* valuation — the player's value as it
# stood in mid-2025, pinned deliberately so a 2024-25 season is never priced
# with today's money. That is a historical fact; it does not go stale, and
# expiring it is wrong on its own terms.
#
# The old 14-day TTL was invisible in development, where an expired row simply
# triggers a re-fetch, but broke the deployed app completely: Transfermarkt
# answers a datacenter IP with HTTP 202 (a bot challenge), so every expired
# lookup fell through to a network call that could never succeed and returned
# None. All 1,935 cached valuations had aged past 14 days, so every shortlist
# came back with fee_eur=None and "0 of N candidates sit inside both ceilings"
# — the data was present and correct in the container the whole time.
#
# Ten years keeps the row effectively permanent while leaving the mechanism in
# place. Refreshing valuations is the backfill's job
# (backend/data_pipeline/backfill_market_values.py), not a TTL's.
_CACHE_TTL_DAYS = 3650
_MIN_REQUEST_GAP = 1.5        # seconds between requests, to stay polite
_last_request_at = 0.0


# ── helpers ───────────────────────────────────────────────────────────────────

def _fold(name: str) -> str:
    decomposed = unicodedata.normalize("NFKD", str(name))
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", stripped.lower()).split())


def _parse_market_value(text: str) -> int | None:
    """
    Turn Transfermarkt's display value into whole euros.

    Handles "€70.00m", "€850k", "-" (no value). Returns None when absent rather
    than 0, so a player with no published value is never shown as worthless.
    """
    if not text:
        return None
    cleaned = text.replace("\xa0", " ").strip()
    m = re.search(r"([\d.,]+)\s*(m|k|bn)?", cleaned, re.IGNORECASE)
    if not m:
        return None
    raw, unit = m.group(1), (m.group(2) or "").lower()
    # Transfermarkt uses "70.00m" — dot is a decimal point, comma a thousands sep.
    raw = raw.replace(",", "")
    try:
        amount = float(raw)
    except ValueError:
        return None
    multiplier = {"bn": 1_000_000_000, "m": 1_000_000, "k": 1_000}.get(unit, 1)
    value = int(amount * multiplier)
    return value if value > 0 else None


#: Transfermarkt prints these where it has no value. Passed through verbatim they
#: masquerade as data — a bare "-" in a Contract column reads as an expiry date.
_PLACEHOLDERS = {"-", "–", "—", "?", "n/a", "na", "unknown", ""}


def _clean_field(value: str | None) -> str | None:
    """Return the field text, or None when Transfermarkt printed a placeholder."""
    if value is None:
        return None
    text = str(value).strip()
    return None if text.lower() in _PLACEHOLDERS else text


def _throttle() -> None:
    global _last_request_at
    gap = time.monotonic() - _last_request_at
    if gap < _MIN_REQUEST_GAP:
        time.sleep(_MIN_REQUEST_GAP - gap)
    _last_request_at = time.monotonic()


def _session():
    import requests
    s = requests.Session()
    s.headers.update({"user-agent": _UA, "accept-language": "en-US,en;q=0.9"})
    return s


# ── cache ─────────────────────────────────────────────────────────────────────

def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS player_market_value (
            name_key    TEXT PRIMARY KEY,
            payload     TEXT NOT NULL,
            fetched_at  TEXT NOT NULL
        )
    """)
    conn.commit()


def _cache_get(db_path: str, name_key: str) -> dict | None:
    if not Path(db_path).exists():
        return None
    try:
        conn = sqlite3.connect(db_path)
        _ensure_table(conn)
        row = conn.execute(
            "SELECT payload, fetched_at FROM player_market_value WHERE name_key = ?",
            (name_key,),
        ).fetchone()
        conn.close()
    except Exception as exc:
        logger.warning(f"market-value cache read failed: {exc}")
        return None
    if not row:
        return None
    payload, fetched_at = row
    try:
        if datetime.fromisoformat(fetched_at) < datetime.utcnow() - timedelta(days=_CACHE_TTL_DAYS):
            return None
    except Exception:
        return None
    try:
        return json.loads(payload)
    except Exception:
        return None


def _cache_put(db_path: str, name_key: str, payload: dict) -> None:
    try:
        conn = sqlite3.connect(db_path)
        _ensure_table(conn)
        conn.execute(
            "INSERT OR REPLACE INTO player_market_value (name_key, payload, fetched_at) "
            "VALUES (?, ?, ?)",
            (name_key, json.dumps(payload, ensure_ascii=False), datetime.utcnow().isoformat()),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.warning(f"market-value cache write failed: {exc}")


# ── scraping ──────────────────────────────────────────────────────────────────

#: Words that carry no identifying information in a club name.
_CLUB_NOISE = {"fc", "afc", "cf", "sc", "ac", "as", "ss", "club", "de", "the", "1899",
               "united", "city", "town", "athletic", "calcio"}
_CLUB_ALIASES = {"utd": "united", "nottham": "nottingham", "wolves": "wolverhampton",
                 "spurs": "tottenham", "inter": "internazionale", "gladbach": "monchengladbach"}


def _club_key(name: str) -> set[str]:
    text = str(name).lower().replace("'", "").replace("’", "")
    cleaned = "".join(c if c.isalnum() or c.isspace() else " " for c in text)
    words = [_CLUB_ALIASES.get(w, w) for w in cleaned.split()]
    return {w for w in words if w and w not in _CLUB_NOISE}


#: Years of slack between the cached season's age and Transfermarkt's current age.
#: The cache is a 2024-25 snapshot, so a genuine match is 1-2 years younger there.
_AGE_TOLERANCE = 3

#: Every player in our cohort played at least 450 minutes in a Big-5 league, and
#: nobody who does that is valued this low. A figure under the floor means the
#: search landed on a namesake in a lower division — "Luis Díaz" resolved to one at
#: LD Alajuelense worth €550k, and "Idrissa Gueye" to one listed Without Club.
#: Both passed the name and age checks, so value plausibility is the last gate.
_MIN_PLAUSIBLE_VALUE_EUR = 1_000_000


def _verify_profile(
    profile: dict, player_name: str, squad_hint: str, expected_age: int = 0
) -> str | None:
    """
    Confirm a fetched profile is actually the player we asked about.

    Without this, a shared name resolves to whoever the search ranked first:
    "Luis Díaz" returned a different Luis Díaz at Envigado FC valued at €550k, and
    that figure drove a wage estimate and passed a budget filter as cheap. A
    valuation for the wrong player is worse than none.

    The club alone cannot decide it: our cohort is a past season, while
    Transfermarkt shows the player's club today, so anyone who transferred would
    be wrongly rejected — Marc Guéhi reads as Manchester City now, not Crystal
    Palace. Age is season-independent, so a club mismatch is tolerated when the
    age agrees. Returns None when it verifies, or a reason when it does not.
    """
    wanted_words = set(_fold(player_name).split())
    got_words = set(_fold(profile.get("name") or "").split())
    # Transfermarkt's heading often carries only the surname, so require overlap
    # rather than equality — but demand the *last* name matches.
    surname = _fold(player_name).split()[-1] if _fold(player_name) else ""
    if surname and surname not in got_words and not (wanted_words & got_words):
        return f"profile name {profile.get('name')!r} does not match {player_name!r}"

    value = profile.get("market_value_eur")
    if value is not None and value < _MIN_PLAUSIBLE_VALUE_EUR:
        return (
            f"valuation €{value:,} is too low for a Big-5 regular — "
            f"likely a namesake ({profile.get('current_club')!r})"
        )

    club_matches = None
    if squad_hint:
        hint = _club_key(squad_hint)
        actual = _club_key(profile.get("current_club") or "")
        if hint and actual:
            club_matches = bool(hint & actual)
    if club_matches:
        return None

    tm_age = profile.get("age")
    if expected_age and isinstance(tm_age, int):
        if abs(tm_age - expected_age) <= _AGE_TOLERANCE:
            return None
        return (
            f"age {tm_age} does not match the cohort's {expected_age} "
            f"(club {profile.get('current_club')!r} vs {squad_hint!r})"
        )

    if club_matches is False:
        return (
            f"club {profile.get('current_club')!r} does not match {squad_hint!r} "
            "and no age was available to confirm identity"
        )
    return None


def _find_profile_urls(session, player_name: str, squad_hint: str = "", limit: int = 4) -> list[str]:
    """Ranked candidate profile URLs, best first."""
    url = _find_profile_url(session, player_name, squad_hint, limit=limit)
    return url if isinstance(url, list) else ([url] if url else [])


def _find_profile_url(session, player_name: str, squad_hint: str = "", limit: int = 1):
    """
    Resolve a player name to a Transfermarkt profile URL.

    When the search returns several people, prefer the one whose row mentions the
    squad we already know from FBref — otherwise a common name silently resolves
    to the wrong player and every downstream figure is about someone else.
    """
    from bs4 import BeautifulSoup

    _throttle()
    resp = session.get(SEARCH_URL.format(urllib.parse.quote(player_name)), timeout=_TIMEOUT)
    if resp.status_code != 200:
        logger.warning(f"Transfermarkt search returned {resp.status_code} for '{player_name}'")
        return None

    soup = BeautifulSoup(resp.content, "html.parser")
    anchors = soup.select('a[href*="/profil/spieler/"]')
    if not anchors:
        return None

    target = _fold(player_name)
    squad_key = _fold(squad_hint)

    scored: list[tuple[int, str]] = []
    for a in anchors:
        href = a.get("href") or ""
        label = _fold(a.get_text(" ", strip=True))
        # Row text carries the club, so search it for the squad hint.
        row = a.find_parent("tr")
        row_text = _fold(row.get_text(" ", strip=True)) if row else ""

        score = 0
        if label == target:
            score += 4
        elif target in label or label in target:
            score += 2
        elif set(target.split()) & set(label.split()):
            score += 1
        if squad_key and squad_key in row_text:
            score += 3
        if score:
            scored.append((score, href))

    if not scored:
        return [] if limit > 1 else None
    scored.sort(reverse=True)
    urls = []
    for _score, href in scored:
        full = BASE + href
        if full not in urls:
            urls.append(full)
        if len(urls) >= limit:
            break
    return urls if limit > 1 else urls[0]


def _parse_profile(session, url: str) -> dict:
    from bs4 import BeautifulSoup

    _throttle()
    resp = session.get(url, timeout=_TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.content, "html.parser")

    out: dict = {"profile_url": url}

    name_tag = soup.find("h1", {"class": "data-header__headline-wrapper"})
    if name_tag:
        # The shirt number shares the heading; the name is the last line.
        out["name"] = name_tag.get_text("\n", strip=True).split("\n")[-1].strip()

    mv_tag = soup.find("a", {"class": "data-header__market-value-wrapper"})
    if mv_tag:
        mv_text = mv_tag.get_text(" ", strip=True)
        out["market_value_eur"] = _parse_market_value(mv_text.split("Last update")[0])
        if "Last update" in mv_text:
            out["market_value_updated"] = mv_text.split("Last update:")[-1].strip()

    labels = soup.select("span.info-table__content--regular")
    values = soup.select("span.info-table__content--bold")
    fields = {
        _fold(l.get_text(" ", strip=True)).rstrip(" :"): v.get_text(" ", strip=True)
        for l, v in zip(labels, values)
    }
    # "Date of birth/Age: 23/01/2002 (24)" — the age in brackets is the
    # season-independent identity check.
    dob_field = fields.get("date of birthage") or fields.get("date of birth age") or ""
    age_match = re.search(r"\((\d{2})\)", dob_field)
    if age_match:
        out["age"] = int(age_match.group(1))

    for key, dest in (
        ("contract expires", "contract_expires"),
        ("current club", "current_club"),
        ("position", "tm_position"),
        ("foot", "foot"),
        ("citizenship", "citizenship"),
    ):
        value = _clean_field(fields.get(key))
        if value:
            out[dest] = value

    return out


# ── public API ────────────────────────────────────────────────────────────────

def lookup(player_name: str, squad_hint: str = "", db_path: str = "",
           expected_age: int = 0) -> dict:
    """
    Return Transfermarkt market value and contract data for one player.

    Always returns a dict. On failure it carries `found: False` and a `note`
    rather than raising or inventing figures, so a caller can degrade cleanly.
    """
    name_key = _fold(f"{player_name}|{squad_hint}")

    if db_path:
        cached = _cache_get(db_path, name_key)
        if cached is not None:
            cached["from_cache"] = True
            return cached

    result: dict = {"query_name": player_name, "found": False, "source": "transfermarkt"}
    try:
        session = _session()
        candidates = _find_profile_urls(session, player_name, squad_hint)
        if not candidates:
            result["note"] = f"No Transfermarkt profile matched '{player_name}'"
        else:
            rejections: list[str] = []
            for url in candidates:
                profile = _parse_profile(session, url)
                reason = _verify_profile(profile, player_name, squad_hint, expected_age)
                if reason:
                    rejections.append(f"{url.rsplit('/', 1)[-1]}: {reason}")
                    continue
                result.update(profile)
                result["found"] = result.get("market_value_eur") is not None
                if not result["found"]:
                    result["note"] = "Profile found but no published market value"
                break
            else:
                # Every candidate failed verification. Returning nothing is correct:
                # a valuation for the wrong player is worse than no valuation.
                result["note"] = (
                    f"No Transfermarkt profile could be confirmed as "
                    f"'{player_name}' ({squad_hint or 'no club given'}). "
                    + "; ".join(rejections[:3])
                )
                logger.info(result["note"])
    except Exception as exc:
        result["note"] = f"Transfermarkt lookup failed: {type(exc).__name__}: {exc}"
        logger.warning(result["note"])
        return result  # transient failures are not cached

    if db_path:
        _cache_put(db_path, name_key, result)
    result["from_cache"] = False
    return result
