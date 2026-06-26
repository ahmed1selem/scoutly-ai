import asyncio
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backend.mcp_server.server import get_fbref_player_stats, search_fbref_players, get_position_cohort

async def main():
    print("Testing get_fbref_player_stats for Saka...")
    try:
        res = await get_fbref_player_stats("Saka")
        print("Result:", str(res)[:500])
    except Exception as e:
        print("Error:", e)

    print("\nTesting search_fbref_players for FW age 20-22...")
    try:
        res = await search_fbref_players(position="FW", min_age=20, max_age=22, limit=2)
        print("Result:", str(res)[:500])
    except Exception as e:
        print("Error:", e)

    print("\nTesting get_position_cohort for GK...")
    try:
        res = await get_position_cohort(position="GK")
        print("Result:", str(res)[:500])
    except Exception as e:
        print("Error:", e)

if __name__ == "__main__":
    asyncio.run(main())
