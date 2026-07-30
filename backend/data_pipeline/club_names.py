"""
Club-name reconciliation between Sofascore and the FBref player cache.

The two sources agree on which 96 clubs played the Big 5 in 2024-25 and disagree
on what to call almost half of them: "FC Bayern München" against "Bayern Munich",
"Olympique Lyonnais" against "Lyon", "Wolverhampton" against "Wolves". Only 54 of
96 match on a plain fold.

The FBref squad string wins as the canonical name, because that is what the
player cohort stores and what the scouter compares against when it excludes
players already at the club. A club profile filed under a name the rest of the
system never uses is a profile that never gets read.

Matching runs in three passes, most specific first, and anything still unmatched
is reported rather than guessed at — a silently mismatched club would attach one
team's tactical profile to another team's name.
"""
from __future__ import annotations

import re
import unicodedata

#: Club-type abbreviations and legal suffixes that appear on one side only.
#: Removing them lets the token-subset pass do the work: {leverkusen} is a
#: subset of {bayer, leverkusen}, so no alias is needed for that pair.
_NOISE_TOKENS = {
    "fc", "cf", "ac", "as", "sc", "sv", "ssc", "vfb", "vfl", "tsg", "rc", "rcd",
    "ud", "ca", "aj", "ogc", "fsv", "afc", "cd", "sd", "us", "ss", "acf", "ogn",
}

#: Pairs no rule can bridge — a nickname, an initialism, or a different language.
_ALIASES = {
    "wolverhampton":          "wolves",
    "wolverhampton wanderers": "wolves",
    "paris saint germain":    "psg",
    "paris saintgermain":     "psg",
    "manchester united":      "manchester utd",
    "bayern munchen":         "bayern munich",
    "borussia mgladbach":     "gladbach",
    "borussia monchengladbach": "gladbach",
    "olympique lyonnais":     "lyon",
    "stade rennais":          "rennes",
    "stade brestois":         "brest",
    "internazionale":         "inter",
    "inter milan":            "inter",
}


def fold(name: str) -> str:
    """Lowercased, diacritic-free, punctuation-free. Matches server._normalise_name."""
    decomposed = unicodedata.normalize("NFKD", str(name))
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    cleaned = re.sub(r"[^a-z0-9 ]+", " ", stripped.lower())
    return " ".join(cleaned.split())


def _tokens(name: str) -> frozenset[str]:
    """Significant tokens: no club-type noise, no bare numbers (04, 05, 1848)."""
    return frozenset(
        tok for tok in fold(name).split()
        if tok not in _NOISE_TOKENS and not tok.isdigit()
    )


def _canonical(name: str) -> str:
    folded = fold(name)
    return _ALIASES.get(folded, folded)


def match_club(name: str, candidates: dict[str, str]) -> str | None:
    """
    Resolve `name` to one of `candidates` ({folded: original}).

    Returns the original candidate string, or None when no pass is confident.
    A near-miss is never forced: an unmatched club is a build warning, not a
    profile attached to the wrong team.
    """
    target = _canonical(name)
    by_canon = {_canonical(orig): orig for orig in candidates.values()}

    # 1. exact, after folding and alias resolution
    if target in by_canon:
        return by_canon[target]

    # 2. token-subset: {marseille} vs {olympique, de, marseille}. Requires one
    #    side to contain the other entirely, so "Manchester City" and
    #    "Manchester United" cannot collapse into each other.
    target_tokens = _tokens(name)
    if target_tokens:
        subset_hits = [
            orig for orig in candidates.values()
            if (cand := _tokens(orig)) and (cand <= target_tokens or target_tokens <= cand)
        ]
        if len(subset_hits) == 1:
            return subset_hits[0]

    # 3. one token is a prefix of the other and long enough to be distinctive:
    #    "brestois" against "brest". Guarded to a single hit for the same reason.
    prefix_hits = []
    for orig in candidates.values():
        for a in target_tokens:
            for b in _tokens(orig):
                if len(a) >= 5 and len(b) >= 5 and (a.startswith(b) or b.startswith(a)):
                    prefix_hits.append(orig)
                    break
            else:
                continue
            break
    if len(set(prefix_hits)) == 1:
        return prefix_hits[0]

    return None


def reconcile(source_names: list[str], canonical_names: list[str]
              ) -> tuple[dict[str, str], list[str], list[str]]:
    """
    Map every source name onto a canonical name.

    Returns (mapping, unmatched_sources, unclaimed_canonicals). Both leftovers
    matter: an unmatched source means a club whose stats go nowhere, and an
    unclaimed canonical means a club in the player cache with no profile.
    """
    pool = {fold(n): n for n in canonical_names}
    mapping: dict[str, str] = {}
    unmatched: list[str] = []
    taken: set[str] = set()

    for name in source_names:
        hit = match_club(name, pool)
        if hit is None or hit in taken:
            unmatched.append(name)
            continue
        mapping[name] = hit
        taken.add(hit)

    unclaimed = [n for n in canonical_names if n not in taken]
    return mapping, unmatched, unclaimed
