import asyncio
import sys
from backend.agents.planner import PlannerAgent
from backend.utils.config import make_azure_client

async def main():
    class DummyQueue:
        async def put(self, item):
            print(f"SSE: {item}")
            sys.stdout.flush()
            
    client = make_azure_client()
    planner = PlannerAgent(client=client, sse_queue=DummyQueue(), run_id="test_run")
    print("Running finalize...")
    sys.stdout.flush()
    try:
        res = await planner.run_finalize(
            query="Find me a good striker under 25 years old",
            ranked_players=[{"name": "Harry Powell", "overall_rating": 80, "wage_eur": 70000}],
            financial_decision={}
        )
        print("Done:", res)
    except Exception as e:
        print("EXCEPTION:", e)

if __name__ == "__main__":
    asyncio.run(main())
