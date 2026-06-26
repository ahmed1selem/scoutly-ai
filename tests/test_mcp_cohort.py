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
    _flatten_cols, _find_col,
    get_position_cohort,
    get_enriched_player_stats,
    _MOCK_COHORT,
    _COHORT_CACHE,
)


@pytest.fixture(autouse=True)
def clear_cohort_cache():
    """Wipe the in-process cohort cache before every test so tests are independent."""
    _COHORT_CACHE.clear()
    
    # Force the server to think ScraperFC is OK and not in mock mode
    # so that it actually executes the dataframe processing logic
    # instead of immediately bailing to the hardcoded mock cohort.
    with patch("server._SCRAPERFC_OK", True), patch.dict("os.environ", {"FBREF_USE_MOCK": "false"}):
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

@patch("server._scrape")
def test_cohort_max_15_players(mock_scrape):
    rows = {"Player": [f"P{i}" for i in range(25)]}
    rows.update({
        "Pos":["FW"]*25, "Squad":["A"]*25, "Comp":["PL"]*25,
        "Age":["25-100"]*25, "Min":[2000-i*50 for i in range(25)],
        "Gls":[10]*25, "Ast":[5]*25, "xG":[9.0]*25, "xAG":[4.0]*25,
        "Sh":[50]*25, "SoT":[25]*25, "PrgP":[100]*25, "PrgC":[70]*25,
    })
    mock_scrape.side_effect = [{"player": pd.DataFrame(rows)}, Exception("skip")]
    result = asyncio.run(get_position_cohort("FW", "2425"))
    players = json.loads(result)
    assert len(players) <= 15
