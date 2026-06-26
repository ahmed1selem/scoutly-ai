import asyncio
import json
from openai import AsyncAzureOpenAI
from backend.agents.planner import PlannerAgent
from backend.utils.config import make_azure_client

class PrintQueue:
    async def put(self, item):
        print("QUEUE:", item)
        
    async def get(self):
        await asyncio.sleep(1)
        return None

async def run_local():
    print("Initializing...")
    client = make_azure_client()
    queue = PrintQueue()
    
    planner = PlannerAgent(client=client, sse_queue=queue, run_id="local_run")
    
    print("Running draft...")
    result = await planner.run_draft("Find me a strong CB for Liverpool.")
    print("Draft Result:", json.dumps(result, indent=2))

if __name__ == "__main__":
    asyncio.run(run_local())
