import asyncio
import json
from mcp.client.sse import sse_client
from mcp import ClientSession

async def main():
    async with sse_client("http://localhost:8001/sse") as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.call_tool("search_fbref_players", {"position": "CB", "limit": 15})
            print("CB Result:", result.content)
            
            result2 = await session.call_tool("search_fbref_players", {"position": "CF", "limit": 15})
            print("CF Result:", result2.content)

asyncio.run(main())
