- `[x]` **M0: Scaffold (Steal Infra + Setup)**
  - `[x]` Cloned existing repo to temporary directory
  - `[x]` Created project structure in `E:\ITI\agentic\project\Scoutly`
  - `[x]` Copied existing codebase (`backend`, `frontend`, `configs`, `prompts`)
  - `[x]` Setup `.env` and `pyproject.toml`
  - `[x]` Install dependencies via `pip install -e .` (Running)

- `[x]` **M1: Club Profiles → ChromaDB**
  - `[x]` Create `backend/schemas/club_profile.py`
  - `[x]` Create `backend/data_pipeline/enrich_profiles.py`
  - `[x]` Create `backend/rag/vectorstore.py`
  - `[x]` Create `backend/rag/loader.py`
  - `[x]` Run enrichment pipeline to generate `profile_narrative` for 96 clubs
  - `[x]` Load enriched profiles and generic skills into ChromaDB

- `[x]` **M2: Data Source (FBref MCP)**
  - `[x]` Find or create FBref MCP server
  - `[x]` Ensure it supports required stats and data points

- `[x]` **M3: Scoutly Fit Engine (Prompt & Schema)**
  - `[x]` Update prompts for `tactical`, `analysis`, and `scouter`
  - `[x]` Implement robust structured parsing for agent output

- `[x]` **Trait Token Harmonization**
  - `[x]` Map 59 KB traits to locked schema tokens as defined in project documentation

- `[x]` **M4: RAG Integration**
  - `[x]` Hook up `vectorstore.py` inside `backend/agents/scouter.py` and `tactical.py`
  - `[x]` Test end-to-end extraction from ChromaDB into the prompt context

- `[ ]` **M5: Human-in-the-Loop (Web Search & Debate)**
  - `[ ]` Setup Tavily MCP server or integrate Tavily into the agent's toolset
  - `[ ]` Implement the pause-for-debate flow

- `[ ]` **M6: Verification & Testing**
  - `[ ]` Run end-to-end tests for a specific club's recruitment search
