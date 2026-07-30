"""
Build club tactical profiles from measured team data — offline, run once.

Replaces the authored knowledge base. The profiles that shipped before had 96
clubs sharing 13 distinct trait-requirement blocks, so any two possession sides
produced an identical fit score for the same player; 192 of their 306 evidence
entries were the same two boilerplate sentences, and only 18 carried a source
URL. Nothing in them traced to a number.

Here every label is produced by a rule that fired on a measured percentile, and
the number that fired it is written into the evidence block. A club that is
average at everything gets "balanced" with low confidence, which is a finding
rather than a fallback.

Output keeps the club_tactical_profile_v1 shape, so ClubVectorStore, loader.py,
fit_engine and the agents need no changes.

    python -m backend.data_pipeline.club_source          # fetch the stats first
    python -m backend.data_pipeline.club_profile_builder # then build
    python -m backend.data_pipeline.club_profile_builder --show Bologna
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sqlite3
from datetime import date
from pathlib import Path

from .club_indicators import ALL_INDICATORS, compute_indicators, rank_all
from .club_names import fold, reconcile
from .club_rules import BASELINE, POSITIONS, RULES, score_styles
from . import club_source

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
CACHE_DB = ROOT / "data" / "fbref_cache.db"
PAYLOADS = ROOT / "data" / "vectorstore" / "club_payloads.json"
SEASON = "2024/25"
SEASON_SLUG = "2024_25"
SCHEMA_VERSION = "club_tactical_profile_v1"

#: Three-letter country codes for the club_id, matching the old file's format.
_COUNTRY_CODE = {
    "England": "eng", "Spain": "esp", "Italy": "ita",
    "Germany": "ger", "France": "fra",
}

#: A style needs this much signal before it is called primary. Below it the club
#: is genuinely unremarkable and saying so beats inventing an identity.
_PRIMARY_STYLE_FLOOR = 0.12
_SECONDARY_STYLE_FLOOR = 0.20

#: Requirement lists longer than this dilute the fit score — every extra trait
#: is another thing the player must demonstrate. Four come from the positional
#: baseline, leaving up to five slots for what the club's own numbers ask for.
_MAX_TRAITS_PER_POSITION = 9


def _club_id(name: str, country: str) -> str:
    code = _COUNTRY_CODE.get(country, "eur")
    return f"{fold(name).replace(' ', '_')}_{code}_{SEASON_SLUG}"


def _cache_squads() -> list[str]:
    """Club names exactly as the player cohort stores them."""
    if not CACHE_DB.exists():
        return []
    conn = sqlite3.connect(str(CACHE_DB))
    try:
        return sorted({row[0] for row in conn.execute(
            "SELECT DISTINCT squad FROM player_cohort WHERE squad IS NOT NULL"
        ) if row[0]})
    finally:
        conn.close()


def _fired_rules(ranks: dict[str, int], values: dict[str, float]) -> list[dict]:
    """Every rule whose indicator sits in the tail it watches, with its number."""
    fired = []
    for rule in RULES:
        pct = ranks.get(rule.indicator)
        if not rule.fires(pct):
            continue
        value = values.get(rule.indicator)
        if value is None:
            continue
        fired.append({
            "rule": rule.key,
            "indicator": rule.indicator,
            "value": value,
            "percentile": pct,
            "reason": rule.describe(value, pct),
            "traits": rule.traits,
            "poor_fit": rule.poor_fit,
        })
    return fired


def _requirements(fired: list[dict]) -> tuple[dict[str, list[str]], dict[str, dict[str, str]]]:
    """
    Per-position trait lists, plus why each trait is there.

    Traits are ordered by how strong the evidence for them was, so truncating to
    the cap drops the weakest justification rather than an arbitrary one.
    """
    scored: dict[str, dict[str, int]] = {pos: {} for pos in POSITIONS}
    because: dict[str, dict[str, str]] = {pos: {} for pos in POSITIONS}

    for pos, traits in BASELINE.items():
        for trait in traits:
            # Baseline sits above any rule-added trait: it is what the position
            # requires at any club, so it must survive truncation.
            scored[pos][trait] = 1000
            because[pos][trait] = "required of the position at any club"

    for entry in fired:
        # A rule deep in the tail is better evidence than one that just cleared
        # the threshold, so its traits should survive truncation first.
        weight = int(entry["percentile"] if entry["percentile"] >= 50
                     else 100 - entry["percentile"])
        for pos, traits in entry["traits"].items():
            for trait in traits:
                if scored[pos].get(trait, 0) < weight:
                    scored[pos][trait] = weight
                    because[pos][trait] = entry["reason"]

    out: dict[str, list[str]] = {}
    reasons: dict[str, dict[str, str]] = {}
    for pos in POSITIONS:
        ranked = sorted(scored[pos].items(), key=lambda kv: -kv[1])[:_MAX_TRAITS_PER_POSITION]
        out[pos] = [trait for trait, _ in ranked]
        reasons[pos] = {trait: because[pos][trait] for trait, _ in ranked}
    return out, reasons


def _style_value(value: str, score: float, evidence_id: str) -> dict:
    return {
        "value": value,
        # Style scores run 0-1 over the top two fifths of the distribution, so a
        # club at the extreme scores near 1. Reported confidence is deliberately
        # capped below certainty: this is a label over a proxy, not a fact.
        "confidence": round(min(0.5 + score * 0.45, 0.92), 2),
        "evidence_ids": [evidence_id],
    }


def _evidence(club_id: str, values: dict[str, float], ranks: dict[str, int],
              fired: list[dict], season: str) -> list[dict]:
    """
    Evidence that can be checked.

    One entry lists every indicator with its value and percentile; one lists the
    rules that fired and what fired them. Both name the endpoint they came from.
    """
    measured = ", ".join(
        f"{ALL_INDICATORS[key].label} {values[key]:g}{ALL_INDICATORS[key].unit}"
        f" (p{ranks.get(key, '?')})"
        for key in sorted(values)
        if key in ALL_INDICATORS
    )
    return [
        {
            "evidence_id": f"{club_id}_ev_sofascore_team_stats",
            "source_type": "team_stats",
            "source_url": "https://api.sofascore.com/api/v1/team/{id}"
                          "/unique-tournament/{league}/season/{season}/statistics/overall",
            "date": date.today().isoformat(),
            "reliability": "measured",
            "text_summary": (
                f"Sofascore {season} season totals for this club. Percentiles are "
                f"ranks within the 96 Big-5 clubs. {measured}."
            ),
        },
        {
            "evidence_id": f"{club_id}_ev_derived_labels",
            "source_type": "derivation",
            "source_url": None,
            "date": date.today().isoformat(),
            "reliability": "derived",
            "text_summary": (
                "Labels and trait requirements were derived by club_rules.py from "
                "the percentiles above. Rules that fired: "
                + ("; ".join(f"{e['rule']} — {e['reason']}" for e in fired)
                   if fired else "none; this club sits mid-table on every indicator")
                + "."
            ),
        },
    ]


def _narrative(club_name: str, styles: list[tuple[str, float]],
               fired: list[dict], values: dict[str, float]) -> str:
    """
    Prose for the FAISS embedding, assembled from the measured facts only.

    The old narrative was written from the labels by an LLM and read as though
    someone had watched the team. This one says what was measured, which is what
    a semantic search over these documents should actually match on.
    """
    primary = styles[0][0].replace("_", " ") if styles else "balanced"
    bits = [
        f"{club_name} measured as a {primary} side in the {SEASON} Big 5.",
        f"They averaged {values.get('possession_pct', 0):.1f}% possession, "
        f"allowed {values.get('ppda', 0):.1f} opponent passes per defensive action, "
        f"and played {values.get('directness_pct', 0):.1f}% of their passes long.",
    ]
    if fired:
        bits.append("Distinctive against the rest of the Big 5: "
                    + "; ".join(e["reason"] for e in fired) + ".")
    else:
        bits.append("No indicator placed them in the tails of the Big 5 "
                    "distribution — a genuinely average profile.")
    return " ".join(bits)


def build_profile(club_name: str, stats: dict, values: dict[str, float],
                  ranks: dict[str, int]) -> dict:
    """Assemble one club_tactical_profile_v1 record."""
    country = stats.get("_country", "")
    league = stats.get("_league", "")
    club_id = _club_id(club_name, country)

    fired = _fired_rules(ranks, values)
    styles = [(name, score) for name, score in score_styles(ranks)
              if score >= _PRIMARY_STYLE_FLOOR]
    ev_id = f"{club_id}_ev_sofascore_team_stats"

    if styles:
        primary = _style_value(styles[0][0], styles[0][1], ev_id)
        secondary = [_style_value(name, score, ev_id)
                     for name, score in styles[1:4] if score >= _SECONDARY_STYLE_FLOOR]
    else:
        primary = {"value": "balanced", "confidence": 0.5, "evidence_ids": [ev_id]}
        secondary = []

    traits, reasons = _requirements(fired)
    poor_fit = sorted({t for entry in fired for t in entry["poor_fit"]})

    def labelled(*rule_keys: str) -> list[dict]:
        """
        Style entries for the rules in this section that actually fired.

        Confidence tracks how far into the tail the indicator sat: a club at the
        99th percentile for pressing is a surer label than one at the 70th.
        """
        entries = []
        for entry in fired:
            if entry["rule"] not in rule_keys:
                continue
            pct = entry["percentile"]
            extremity = abs(pct - 50) / 50          # 0 at the median, 1 at either end
            entries.append({
                "value": entry["rule"],
                "confidence": round(min(0.5 + extremity * 0.4, 0.9), 2),
                "evidence_ids": [f"{club_id}_ev_derived_labels"],
            })
        return entries

    # Measured coverage: how much of the indicator set this club actually has.
    coverage = len(values) / len(ALL_INDICATORS)

    profile = {
        "schema_version": SCHEMA_VERSION,
        "club": {
            "club_id": club_id,
            "club_name": club_name,
            "country": country,
            "league": league,
            "season": SEASON,
            "league_source_urls": ["https://www.sofascore.com/"],
        },
        "profile_scope": {
            "matches_analyzed": int(stats.get("matches") or 0),
            "date_range": {"season": SEASON},
            "last_updated": date.today().isoformat(),
        },
        "data_quality": {
            "coverage_level": ("high" if coverage >= 0.9 else
                               "medium" if coverage >= 0.6 else "low"),
            "source_count": 1,
            # Earned, not asserted. Full indicator coverage is necessary but not
            # sufficient: these are labels over proxies, so the ceiling is 0.85
            # rather than 1.0, and a club where no rule fired has little more
            # than the positional baseline to offer, so it scores lower.
            "confidence_overall": round(
                0.85 * coverage * (0.6 + 0.4 * min(len(fired) / 4, 1.0)), 2
            ),
            "notes": [
                f"Derived from Sofascore {SEASON} team season statistics by "
                "club_profile_builder. Every label traces to a measured "
                "indicator and its percentile among the 96 Big-5 clubs.",
                "Style names are labels over measured proxies, not event-data "
                "classifications. Defensive line height is inferred from "
                "opponents caught offside.",
                "Trait requirements are produced by documented threshold rules "
                "in club_rules.py; the rule and the number that fired it are in "
                "the evidence block.",
            ],
        },
        "team_identity": {"primary_style": primary, "secondary_styles": secondary},
        # No source we have covers formation without scraping every match lineup,
        # and nothing scores on it. Left explicitly unknown rather than guessed.
        "formations": {
            "primary_formation": {"value": "unknown", "confidence": 0.0,
                                  "evidence_ids": []},
            "secondary_formations": [],
        },
        "in_possession": {
            "build_up_style": labelled("possession_side", "short_build_up", "direct"),
            "final_third_creation": labelled("cross_heavy", "box_focused", "dribble_led"),
        },
        "out_of_possession": {
            "defensive_block": labelled("high_line", "deep_line", "low_possession_side"),
            "pressing_trigger": labelled("pins_opponents_back"),
            "pressing_intensity": labelled("high_press"),
        },
        "transitions": {
            "attacking_transition": labelled("transition_threat"),
            "defensive_transition": labelled("defensively_exposed"),
        },
        "set_pieces": {
            "attacking": labelled("set_piece_reliant"),
            "defensive": labelled("aerially_dominant", "aerially_weak"),
        },
        "recruitment_profile": {
            "ideal_player_traits": traits,
            "poor_fit_traits": poor_fit,
        },
        "evidence": _evidence(club_id, values, ranks, fired, SEASON),
        "profile_narrative": _narrative(club_name, styles, fired, values),
        # Not part of the schema the agents read — kept so a human can audit any
        # requirement back to the number that produced it.
        "measured_indicators": {
            key: {"value": values[key], "percentile": ranks.get(key),
                  "formula": ALL_INDICATORS[key].formula}
            for key in sorted(values) if key in ALL_INDICATORS
        },
        "trait_rationale": reasons,
    }
    return profile


def build(write: bool = True) -> dict[str, dict]:
    """Build every club profile. Returns {club_id: profile}."""
    stats_by_source = club_source.load_staged()
    if not stats_by_source:
        raise SystemExit(
            "No staged club stats. Run: python -m backend.data_pipeline.club_source"
        )

    # Canonical names come from the player cohort, because that is what the
    # scouter compares against and what a user's query names.
    canonical = _cache_squads()
    source_names = [s["_team_name"] for s in stats_by_source.values()]
    if canonical:
        mapping, unmatched, unclaimed = reconcile(source_names, canonical)
        for name in unmatched:
            logger.warning(f"no cache squad matches Sofascore club {name!r} — "
                           f"keeping the Sofascore name")
        for name in unclaimed:
            logger.warning(f"cache squad {name!r} has no club profile")
    else:
        logger.warning("player cache unavailable — using Sofascore club names")
        mapping = {}

    values = {key: compute_indicators(stats) for key, stats in stats_by_source.items()}
    ranks = rank_all(values)

    profiles: dict[str, dict] = {}
    for key, stats in stats_by_source.items():
        source_name = stats["_team_name"]
        club_name = mapping.get(source_name, source_name)
        profile = build_profile(club_name, stats, values[key], ranks[key])
        profiles[profile["club"]["club_id"]] = profile

    if write:
        if PAYLOADS.exists():
            backup = PAYLOADS.with_suffix(".authored.json")
            if not backup.exists():
                shutil.copy2(PAYLOADS, backup)
                logger.info(f"Kept the authored profiles at {backup.name}")
        PAYLOADS.parent.mkdir(parents=True, exist_ok=True)
        with open(PAYLOADS, "w", encoding="utf-8") as fh:
            json.dump(profiles, fh, ensure_ascii=False)
        logger.info(f"Wrote {len(profiles)} profiles to {PAYLOADS}")

    return profiles


def _summarise(profiles: dict[str, dict]) -> None:
    from collections import Counter
    styles = Counter(p["team_identity"]["primary_style"]["value"] for p in profiles.values())
    blocks = Counter(
        json.dumps(p["recruitment_profile"]["ideal_player_traits"], sort_keys=True)
        for p in profiles.values()
    )
    conf = [p["data_quality"]["confidence_overall"] for p in profiles.values()]
    print(f"\n{len(profiles)} profiles")
    print(f"  distinct requirement blocks: {len(blocks)} "
          f"(largest shared by {blocks.most_common(1)[0][1]} clubs)")
    print(f"  confidence: min {min(conf):.2f}  mean {sum(conf) / len(conf):.2f}  "
          f"max {max(conf):.2f}")
    print("  primary styles: " + ", ".join(f"{k} {v}" for k, v in styles.most_common()))


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="build without writing")
    ap.add_argument("--show", metavar="CLUB", help="print one club's profile")
    args = ap.parse_args()

    profiles = build(write=not (args.dry_run or args.show))

    if args.show:
        key = fold(args.show)
        hit = next((p for p in profiles.values()
                    if fold(p["club"]["club_name"]) == key), None) or \
              next((p for p in profiles.values()
                    if key in fold(p["club"]["club_name"])), None)
        if not hit:
            print(f"No club matching {args.show!r}")
            return 1
        print(json.dumps(hit, ensure_ascii=False, indent=1))
        return 0

    _summarise(profiles)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
