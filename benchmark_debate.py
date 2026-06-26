import asyncio
import httpx
import json
import time
import sys

BASE_URL = "http://localhost:8000"

async def read_sse(response: httpx.Response):
    """Yields parsed JSON events from an SSE stream."""
    async for line in response.aiter_lines():
        line = line.strip()
        if line.startswith("data: "):
            data_str = line[6:]
            if data_str:
                yield json.loads(data_str)

async def test_debate_flow():
    print("=== Scoutly HITL Debate Benchmark ===")
    
    # Check health
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{BASE_URL}/api/health")
            resp.raise_for_status()
            print("[+] API Health Check: Passed")
    except Exception as e:
        print(f"[-] API Health Check: Failed. Is the server running? {e}")
        return

    async with httpx.AsyncClient(timeout=1200.0) as client:
        # 1. Start Scouting Run
        print("\n[1] Starting Scouting Run...")
        query = "Find me a strong CB for Liverpool."
        req = client.build_request("POST", f"{BASE_URL}/api/scout", json={"query": query})
        
        run_id = None
        draft_ready = False
        
        try:
            response = await client.send(req, stream=True)
            async for event in read_sse(response):
                print(f"  -> Event: {event.get('type')}")
                if event.get("type") == "connected":
                    run_id = event.get("run_id")
                elif event.get("type") == "draft_ready":
                    draft_ready = True
                    print(f"  -> Draft Ready! {len(event.get('ranked_players', []))} players shortlisted.")
                elif event.get("type") == "awaiting_review":
                    print("  -> Stream paused. Awaiting human review.")
                    break
                elif event.get("type") == "pipeline_error":
                    print(f"[-] Error in pipeline: {event.get('error')}")
                    return
        except Exception as e:
            print(f"[-] Initial scout run failed: {e}")
            return
            
        if not run_id or not draft_ready:
            print("[-] Run ID or draft not received.")
            return
            
        print(f"[+] Drafting complete. Run ID: {run_id}")

        # 2. Check State Endpoint
        print("\n[2] Checking State Endpoint...")
        resp = await client.get(f"{BASE_URL}/api/scout/{run_id}")
        state = resp.json()
        print(f"  -> State Status: {state.get('status')}")
        assert state.get("status") == "awaiting_review", "State is not awaiting_review"

        # 3. Send a Directive
        print("\n[3] Sending a Directive to Debate...")
        directive = "Exclude players over 30 years old."
        req2 = client.build_request("POST", f"{BASE_URL}/api/scout/{run_id}/directive", json={"message": directive})
        
        revision_ready = False
        try:
            response2 = await client.send(req2, stream=True)
            async for event in read_sse(response2):
                print(f"  -> Event: {event.get('type')}")
                if event.get("type") == "revision_ready":
                    revision_ready = True
                elif event.get("type") == "awaiting_review":
                    print("  -> Stream paused. Awaiting human review again.")
                    break
        except Exception as e:
            print(f"[-] Directive run failed: {e}")
            return
            
        if not revision_ready:
            print("[-] Revision not ready.")
            return

        # 4. Select Player (Approve Draft via Intent)
        print("\n[4] Approving Draft & Finalizing via Intent Router...")
        selection_msg = "Let's go with the first player on the list."
        req3 = client.build_request("POST", f"{BASE_URL}/api/scout/{run_id}/directive", json={"message": selection_msg})
        
        pdf_ready = False
        try:
            response3 = await client.send(req3, stream=True)
            async for event in read_sse(response3):
                print(f"  -> Event: {event.get('type')}")
                if event.get("type") == "final_ready":
                    pdf_ready = True
                    print(f"  -> PDF Generated: {event.get('pdf_filename')}")
                elif event.get("stream_end") or event.get("type") == "stream_end":
                    break
        except Exception as e:
            print(f"[-] Approval run failed: {e}")
            return
            
        if pdf_ready:
            print("\n[+] Benchmark test completed successfully! The full Debate state machine works.")
        else:
            print("\n[-] Benchmark failed. PDF not generated.")

if __name__ == "__main__":
    asyncio.run(test_debate_flow())
