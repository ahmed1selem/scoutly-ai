import asyncio
import json
from pathlib import Path
from langchain_openai import AzureChatOpenAI
from langchain_core.prompts import PromptTemplate

# Adjust path to find utils.config if run directly
import sys
sys.path.append(str(Path(__file__).resolve().parent.parent.parent))

from backend.utils.config import settings

NARRATIVE_PROMPT = """You are a professional football tactical analyst.
Based on the following structured data about a club's tactical profile, write a cohesive, comprehensive, 200-400 word narrative summarizing their style of play.
This narrative will be used for semantic similarity search to match players to the club's philosophy.

Club Name: {club_name}
League: {league}
Primary Style: {primary_style}
Formations: {formations}
In Possession: {in_possession}
Out of Possession: {out_of_possession}
Transitions: {transitions}
Set Pieces: {set_pieces}

Focus on how they play with the ball, without the ball, and in transitions. Be descriptive but concise.
Do NOT output anything other than the narrative text.
"""

async def generate_narrative(llm, club_data: dict) -> str:
    prompt = PromptTemplate.from_template(NARRATIVE_PROMPT)
    
    club = club_data.get("club", {})
    ti = club_data.get("team_identity", {})
    form = club_data.get("formations", {})
    inp = club_data.get("in_possession", {})
    oop = club_data.get("out_of_possession", {})
    tran = club_data.get("transitions", {})
    sp = club_data.get("set_pieces", {})
    
    # Handle the primary formation depending on if it's a dict or a list
    primary_form = form.get("primary_formation", {})
    if isinstance(primary_form, dict):
        form_str = primary_form.get("value", "")
    elif isinstance(primary_form, list) and len(primary_form) > 0:
        form_str = primary_form[0].get("value", "")
    else:
        form_str = str(primary_form)
        
    chain = prompt | llm
    
    response = await chain.ainvoke({
        "club_name": club.get("club_name", "Unknown"),
        "league": club.get("league", "Unknown"),
        "primary_style": ti.get("primary_style", {}).get("value", ""),
        "formations": form_str,
        "in_possession": json.dumps(inp),
        "out_of_possession": json.dumps(oop),
        "transitions": json.dumps(tran),
        "set_pieces": json.dumps(sp)
    })
    return response.content

async def enrich_all(kb_path: Path, out_path: Path):
    with open(kb_path, "r", encoding="utf-8") as f:
        data = json.load(f)
        
    profiles = data.get("profiles", [])
    print(f"Loaded {len(profiles)} profiles.")
    
    llm = AzureChatOpenAI(
        azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
        azure_deployment=settings.AZURE_OPENAI_DEPLOYMENT_NAME,
        api_key=settings.AZURE_OPENAI_API_KEY,
        api_version=settings.AZURE_OPENAI_API_VERSION,
        temperature=0.3
    )
    
    # Process in batches to avoid rate limits
    batch_size = 5
    for i in range(0, len(profiles), batch_size):
        batch = profiles[i:i+batch_size]
        print(f"Processing batch {i//batch_size + 1}/{(len(profiles)+batch_size-1)//batch_size}...")
        
        tasks = []
        for p in batch:
            if p.get("profile_narrative"):
                async def return_existing(text): return text
                tasks.append(return_existing(p["profile_narrative"]))
            else:
                tasks.append(generate_narrative(llm, p))
                
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for p, res in zip(batch, results):
            if isinstance(res, Exception):
                print(f"  [ERROR] {p.get('club', {}).get('club_name')}: {res}")
            else:
                p["profile_narrative"] = res
                print(f"  [OK] {p.get('club', {}).get('club_name')}")
                
    data["profiles"] = profiles
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    print(f"\nSaved enriched profiles to {out_path}")

if __name__ == "__main__":
    kb_path = Path("E:/ITI/agentic/project/KB/profiles_all_top5_2025_26.json")
    out_path = Path("E:/ITI/agentic/project/KB/profiles_enriched.json")
    asyncio.run(enrich_all(kb_path, out_path))
