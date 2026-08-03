"""
Unit tests for the updated MCP server helper functions and cohort logic.

ScraperFC/botasaurus requires a running Chrome browser, so we mock all
FBref calls and test the pure-Python logic: column flattening, column
mapping, mock-fallback output format, and downstream player structure.
"""
import pytest
import sys
import json
import asyncio
import pathlib
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "backend" / "mcp_server"))

import pandas as pd
from server import (
    _flatten_cols, _find_col, _normalise_name,
    get_position_cohort,
    get_enriched_player_stats,
    _MOCK_COHORT,
    _COHORT_CACHE,
)


@pytest.fixture(autouse=True)
def live_scrape_path():
    """
    Isolate the live-FBref branch of get_position_cohort.

    In normal operation the pre-built SQLite cache answers first and `_scrape` is
    never reached, so patching it alone tests nothing. These tests stub the cache
    lookup to miss and enable the live path explicitly.
    """
    _COHORT_CACHE.clear()
    with patch("server._SCRAPERFC_OK", True), \
         patch("server._load_cohort_from_cache", return_value=None), \
         patch.dict("os.environ", {"FBREF_USE_MOCK": "false", "FBREF_LIVE_SCRAPE": "true"}):
        yield
    _COHORT_CACHE.clear()


# ── _flatten_cols ──────────────────────────────────────────────────────────────

def test_flatten_single_level():
    df = pd.DataFrame({"Player": ["A"], "Gls": [5]})
    out = _flatten_cols(df)
    assert list(out.columns) == ["Player", "Gls"]

def test_flatten_multiindex_takes_inner_label():
    arrays = [["Performance", "Expected"], ["Gls", "xG"]]
    df = pd.DataFrame([[5, 4.2]], columns=pd.MultiIndex.from_arrays(arrays))
    out = _flatten_cols(df)
    assert "Gls" in out.columns
    assert "xG" in out.columns

def test_flatten_drops_duplicates():
    arrays = [["A", "A"], ["x", "x"]]
    df = pd.DataFrame([[1, 2]], columns=pd.MultiIndex.from_arrays(arrays))
    out = _flatten_cols(df)
    assert list(out.columns).count("x") == 1

def test_flatten_strips_whitespace():
    arrays = [["A"], ["  Gls  "]]
    df = pd.DataFrame([[5]], columns=pd.MultiIndex.from_arrays(arrays))
    out = _flatten_cols(df)
    assert "Gls" in out.columns


# ── _find_col ──────────────────────────────────────────────────────────────────

def test_find_col_case_insensitive():
    df = pd.DataFrame({"Player": [], "Gls": []})
    assert _find_col(df, ["player", "name"]) == "Player"
    assert _find_col(df, ["gls"]) == "Gls"

def test_find_col_returns_first_match():
    df = pd.DataFrame({"Player": [], "Name": []})
    # "Player" comes before "Name" in candidates → should return "Player"
    assert _find_col(df, ["Player", "Name"]) == "Player"

def test_find_col_returns_none_when_missing():
    df = pd.DataFrame({"Player": []})
    assert _find_col(df, ["NonExistent", "xyz"]) is None


# ── Mock cohort structure ──────────────────────────────────────────────────────

def test_mock_cohort_has_all_positions():
    assert "FW" in _MOCK_COHORT
    assert "MF" in _MOCK_COHORT
    assert "DF" in _MOCK_COHORT
    assert "GK" in _MOCK_COHORT

def test_mock_cohort_fw_has_required_fields():
    for player in _MOCK_COHORT["FW"]:
        assert "name" in player
        assert "minutes" in player
        assert "goals" in player


# ── get_position_cohort with mocked FBref ─────────────────────────────────────
# We patch `_scrape` so ScraperFC/botasaurus never starts.

def _make_std_df(position="FW"):
    """Minimal FBref-style standard table for one player."""
    return pd.DataFrame({
        "Player": ["Test Player"],
        "Pos":    [position],
        "Squad":  ["Test FC"],
        "Comp":   ["Premier League"],
        "Age":    ["25-100"],
        "Min":    [2250],
        "Gls":    [15],
        "Ast":    [5],
        "xG":     [12.0],
        "xAG":    [4.5],
        "Sh":     [60],
        "SoT":    [30],
        "PrgP":   [120],
        "PrgC":   [80],
    })

def _make_def_df(position="DF"):
    return pd.DataFrame({
        "Player": ["Test Player"],
        "Squad":  ["Test FC"],
        "Tkl":    [52],
        "Int":    [38],
    })

@patch("server._scrape")
def test_cohort_fw_returns_one_player(mock_scrape):
    mock_scrape.side_effect = [
        {"player": _make_std_df("FW")},   # standard table
        Exception("defensive table skip"), # defensive merge fails gracefully
    ]
    result = asyncio.run(get_position_cohort("FW", "2425"))
    players = json.loads(result)
    assert len(players) == 1
    assert players[0]["name"] == "Test Player"

@patch("server._scrape")
def test_cohort_has_raw_metrics(mock_scrape):
    mock_scrape.side_effect = [
        {"player": _make_std_df("FW")},
        Exception("skip"),
    ]
    result = asyncio.run(get_position_cohort("FW", "2425"))
    players = json.loads(result)
    assert "raw_metrics" in players[0]
    assert "goals" in players[0]["raw_metrics"]

@patch("server._scrape")
def test_cohort_defensive_merge_adds_tackles(mock_scrape):
    mock_scrape.side_effect = [
        {"player": _make_std_df("DF")},     # standard
        {"player": _make_def_df("DF")},     # defensive (merged)
    ]
    result = asyncio.run(get_position_cohort("DF", "2425"))
    players = json.loads(result)
    assert len(players) == 1
    rm = players[0]["raw_metrics"]
    assert "tackles" in rm, f"tackles missing from {list(rm.keys())}"
    assert rm["tackles"]["value"] == 52.0

@patch("server._scrape")
def test_cohort_percentiles_present(mock_scrape):
    # Need at least 2 players for percentile ranking to be meaningful
    df = pd.DataFrame({
        "Player": ["P1", "P2"],
        "Pos":    ["FW", "FW"],
        "Squad":  ["A", "B"],
        "Comp":   ["PL", "PL"],
        "Age":    ["24-100", "26-100"],
        "Min":    [2250, 1800],
        "Gls":    [15, 10],
        "Ast":    [5, 3],
        "xG":     [12.0, 8.0],
        "xAG":    [4.5, 2.5],
        "Sh":     [60, 45],
        "SoT":    [30, 22],
        "PrgP":   [120, 90],
        "PrgC":   [80, 60],
    })
    mock_scrape.side_effect = [{"player": df}, Exception("skip")]
    result = asyncio.run(get_position_cohort("FW", "2425"))
    players = json.loads(result)
    assert len(players) == 2
    # P1 scores more goals, so should have higher goals percentile
    p1 = next(p for p in players if p["name"] == "P1")
    p2 = next(p for p in players if p["name"] == "P2")
    assert p1["raw_metrics"]["goals"]["percentile"] > p2["raw_metrics"]["goals"]["percentile"]

@patch("server._scrape")
def test_cohort_fallback_on_scrape_error(mock_scrape):
    """When FBref throws, mock cohort is returned."""
    mock_scrape.side_effect = RuntimeError("FBref timeout")
    result = asyncio.run(get_position_cohort("DF", "2425"))
    players = json.loads(result)
    assert len(players) > 0  # mock cohort returned
    assert all("raw_metrics" in p for p in players)

@patch("server._scrape")
def test_cohort_league_filter_applied(mock_scrape):
    df = pd.DataFrame({
        "Player": ["P_PL", "P_LL"],
        "Pos":    ["DF",   "DF"],
        "Squad":  ["Arsenal", "Real Madrid"],
        "Comp":   ["Premier League", "La Liga"],
        "Age":    ["25-100", "27-100"],
        "Min":    [2250, 1800],
        "Gls": [0, 1], "Ast": [1, 0], "xG": [0, 0], "xAG": [0, 0],
        "Sh": [2, 1], "SoT": [1, 0], "PrgP": [100, 80], "PrgC": [40, 30],
    })
    mock_scrape.side_effect = [{"player": df}, Exception("skip")]
    result = asyncio.run(get_position_cohort("DF", "2425", league="Premier League"))
    players = json.loads(result)
    assert len(players) == 1
    assert players[0]["name"] == "P_PL"

def _big_cohort(n=25):
    rows = {"Player": [f"P{i}" for i in range(n)]}
    rows.update({
        "Pos": ["FW"]*n, "Squad": ["A"]*n, "Comp": ["PL"]*n,
        "Age": ["25-100"]*n, "Min": [3000-i*50 for i in range(n)],
        "Gls": [10]*n, "Ast": [5]*n, "xG": [9.0]*n, "xAG": [4.0]*n,
        "Sh": [50]*n, "SoT": [25]*n, "PrgP": [100]*n, "PrgC": [70]*n,
    })
    return pd.DataFrame(rows)


@patch("server._scrape")
def test_cohort_respects_limit(mock_scrape):
    mock_scrape.side_effect = [{"player": _big_cohort(25)}, Exception("skip")]
    result = asyncio.run(get_position_cohort("FW", "2425", limit=10))
    assert len(json.loads(result)) == 10


@patch("server._scrape")
def test_cohort_limit_is_capped(mock_scrape):
    """A caller asking for more than exists just gets everything, not an error."""
    mock_scrape.side_effect = [{"player": _big_cohort(25)}, Exception("skip")]
    result = asyncio.run(get_position_cohort("FW", "2425", limit=500))
    assert len(json.loads(result)) == 25


def _uniform_cohort(n):
    """n players all comfortably above any minutes floor, so only the cap bites."""
    rows = {"Player": [f"P{i}" for i in range(n)]}
    rows.update({
        "Pos": ["MF"]*n, "Squad": [f"S{i%20}" for i in range(n)], "Comp": ["PL"]*n,
        "Age": ["25-100"]*n, "Min": [900 + (i % 500) for i in range(n)],
        "Gls": [10]*n, "Ast": [5]*n, "xG": [9.0]*n, "xAG": [4.0]*n,
        "Sh": [50]*n, "SoT": [25]*n, "PrgP": [100]*n, "PrgC": [70]*n,
    })
    return pd.DataFrame(rows)


@patch("server._scrape")
def test_cap_clears_the_largest_real_position_cohort(mock_scrape):
    """
    The cap has to exceed every real cohort, because the trim is by MINUTES: any
    cap below the eligible count decides who the scouter's fit ranking is even
    allowed to see, on playing time. At 500 the scouter saw 45% of Big-5
    defenders and 34% of midfielders (890 eligible), and Valentin Rongier — the
    5th-best fit of 868 for Rennes, whom Rennes actually signed — was never in
    the cohort at all because he played 1,562 minutes.
    """
    mock_scrape.side_effect = [{"player": _uniform_cohort(890)}, Exception("skip")]
    result = asyncio.run(get_position_cohort("MF", "2425", limit=1200))
    assert len(json.loads(result)) == 890, "the full cohort must survive the cap"


@patch("server._scrape")
def test_cap_still_bounds_an_absurd_request(mock_scrape):
    """The cap is raised, not removed — it still refuses an unbounded pull."""
    mock_scrape.side_effect = [{"player": _uniform_cohort(1400)}, Exception("skip")]
    result = asyncio.run(get_position_cohort("MF", "2425", limit=99_999))
    assert len(json.loads(result)) == 1200


@patch("server._scrape")
def test_cohort_min_minutes_filter(mock_scrape):
    mock_scrape.side_effect = [{"player": _big_cohort(25)}, Exception("skip")]
    result = asyncio.run(get_position_cohort("FW", "2425", limit=100, min_minutes=2000))
    players = json.loads(result)
    assert players, "expected some players above the minutes floor"
    assert all(p["minutes"] >= 2000 for p in players)


@patch("server._scrape")
def test_cohort_min_minutes_never_empties_the_shortlist(mock_scrape):
    """An impossible floor is ignored rather than returning nothing to the LLM."""
    mock_scrape.side_effect = [{"player": _big_cohort(5)}, Exception("skip")]
    result = asyncio.run(get_position_cohort("FW", "2425", min_minutes=99_000))
    assert len(json.loads(result)) == 5


@patch("server._scrape")
def test_cohort_percentiles_span_full_cohort_not_the_trimmed_slice(mock_scrape):
    """
    Percentiles must be ranked over everyone before trimming — otherwise the
    top-N players all look average against each other.
    """
    mock_scrape.side_effect = [{"player": _big_cohort(25)}, Exception("skip")]
    result = asyncio.run(get_position_cohort("FW", "2425", limit=3))
    players = json.loads(result)
    assert len(players) == 3
    # All 25 have identical per-90 goals but different minutes, so the three
    # highest-minutes players sit at the BOTTOM of the goals-per-90 ranking.
    assert players[0]["raw_metrics"]["goals"]["percentile"] < 50


# ── name normalisation ────────────────────────────────────────────────────────

def test_normalise_name_strips_diacritics():
    assert _normalise_name("Nikola Milenković") == _normalise_name("Nikola Milenkovic")

def test_normalise_name_is_case_and_space_insensitive():
    assert _normalise_name("  ERLING   Haaland ") == "erling haaland"
