# Deploying Scoutly

Target is Fly.io. The repo also carries a `render.yaml`, which does not work —
see [Why not Render](#why-not-render) at the bottom.

Everything below was verified against this repo on 2026-08-11.

---

## 0. What actually gets deployed

One container running two processes, started by `start.sh`:

```
                    fly.io machine (1 shared CPU, 1GB)
  ┌────────────────────────────────────────────────────────┐
  │  uvicorn  backend.main:app      :8000   <-- public      │
  │      |                                                  │
  │      | MCP over SSE, localhost only                     │
  │      v                                                  │
  │  FastMCP  backend/mcp_server    :8001   <-- internal    │
  │      |                                                  │
  │      v                                                  │
  │  data/fbref_cache.db  (7.7 MB)   baked into the image   │
  │  data/vectorstore     (3.2 MB)   baked into the image   │
  └────────────────────────────────────────────────────────┘
```

The player cache and the club vector store ship **inside the image**. Nothing is
scraped at request time, so there is no Chrome in the container and no volume to
provision. That also means: **rebuilding the cache means redeploying.**

PDF rendering works better in the container than on Windows — the image installs
WeasyPrint's GTK libraries, so reports render natively rather than falling back.

---

## 1. Prerequisites

| Thing | Check | If missing |
| --- | --- | --- |
| flyctl | `fly version` | `iwr https://fly.io/install.ps1 -useb \| iex` |
| Fly account | `fly auth whoami` | `fly auth login` (opens a browser) |
| Payment card on file | — | Fly requires one even on the free allowance |
| A working local run | `python demo_preflight.py` | fix what it reports first |

Docker is **not** required locally — every command below uses `--remote-only`,
which builds on Fly's builders. That matters here because Docker Desktop on this
machine holds port 8000.

---

## 2. Pre-deploy checklist

Run this first. If it fails locally it will fail in the container, and it is far
cheaper to find out here.

```bash
python demo_preflight.py
```

Then confirm the two data files that get baked in actually exist:

```bash
ls -la data/fbref_cache.db data/vectorstore/clubs/index.faiss
```

If the vector store is missing:

```bash
python -m backend.rag.loader
```

If the player cache is missing (10-15 minutes, needs Chrome):

```bash
python -m backend.data_pipeline.cache_builder
```

---

## 3. Deploy

### The scripted path

```bash
.\deploy.ps1
```

It logs in, creates the app, reads the secrets out of `.env`, refuses to
continue if the configured provider has no key, and deploys.

### The manual path

**3.1 — Log in.**

```bash
fly auth login
```

**3.2 — Create the app.** The name in `fly.toml` is `scoutly-ai`, which is
global across all of Fly and may already be taken by someone else. If creation
fails with a name conflict, pick another and change `app = ` in `fly.toml` to
match.

```bash
fly apps create scoutly-ai --org personal
```

**3.3 — Set the secrets.** These are the ones that matter:

| Secret | Required | Notes |
| --- | --- | --- |
| `GROQ_API_KEYS` | **yes** | Comma-separated. Keys from different Groq orgs have independent daily budgets. |
| `TAVILY_API_KEY` | no | Without it, form and injury lookups are skipped silently. |
| `LANGCHAIN_API_KEY` | no | Only if you want tracing. |
| `AZURE_OPENAI_API_KEY` | only if `LLM_PROVIDER=azure` | Not needed on the default path. |

```bash
fly secrets set GROQ_API_KEYS="gsk_one,gsk_two,gsk_three" TAVILY_API_KEY="tvly-..." --app scoutly-ai
```

`LLM_PROVIDER`, `GROQ_MODEL`, `MCP_SERVER_PORT`, `FBREF_USE_MOCK` and
`FBREF_LIVE_SCRAPE` are already set in `fly.toml` under `[env]` — they are not
secret and do not need setting here.

**3.4 — Deploy.**

```bash
fly deploy --remote-only --app scoutly-ai
```

First build takes 3-5 minutes. The image is large (~1.5GB) because of torch,
faiss and pandas.

---

## 4. Verify

Health first — it reports the two hard dependencies, and it is the fastest way
to tell a config problem from a code problem:

```bash
curl https://scoutly-ai.fly.dev/api/health
```

Expect `"status":"ok"` with both checks true:

```json
{"status":"ok","checks":{"groq":{"ok":true,...},"player_cache":{"ok":true,"detail":"1982 players, ..."}}}
```

Then the page, then an actual run — a deploy can serve a healthy page and still
fail on the first agent:

```bash
fly logs --app scoutly-ai
```

Paste a brief in the UI and watch the stream. A run takes 90-120 seconds.

---

## 5. Failure modes, in the order they actually happen

**`status: degraded`, groq `ok: false`, "no API key configured".**
The secret is missing or misnamed. `fly secrets list --app scoutly-ai` shows
names (not values). Setting a secret restarts the machine.

**Health is fine, but every run returns an empty shortlist.**
Two candidates. Either the daily token budget is spent — check `fly logs` for
`429 ... tokens per day` — or the MCP server is not reachable from the backend.
For the latter, confirm `MCP_SERVER_PORT` in `fly.toml` and the port the scouter
dials still agree; they are both derived from that one variable now, but this
was broken once and is worth checking first.

**Machine restarts in a loop, or an unexplained 500 mid-run.**
Out of memory. `fly.toml` asks for 1GB for a reason: the backend loads FAISS,
pandas and the vector store before it can answer a health check, and ranking an
890-player cohort peaks well above rest usage. Do not lower it. Confirm with
`fly status --app scoutly-ai`.

**First request after idle takes ~30 seconds.**
Expected. `auto_stop_machines = "stop"` with `min_machines_running = 0` scales to
zero, so a cold request pays for boot plus FAISS load. The health check has a 90s
grace period for the same reason. Set `min_machines_running = 1` if a demo cannot
absorb that — it costs more.

**PDF download 404s.**
It should not in the container, since WeasyPrint's libraries are installed. The
download endpoint serves `.pdf` or `.html`, whichever the generator produced.

---

## 6. Cost, and the thing to watch

A `shared-cpu-1x` with 1GB, scaled to zero, is a few dollars for a few days —
usually inside Fly's free allowance if it spends most of its time stopped.

The real exposure is not compute. **A public URL means anyone who finds it burns
your Groq daily quota**, and that quota is what the demo depends on. For anything
beyond a short private demo, put it behind something. The cheapest option is to
stop the machine when you are not showing it:

```bash
fly scale count 0 --app scoutly-ai
```

```bash
fly scale count 1 --app scoutly-ai
```

---

## 7. Updating and tearing down

Redeploy after any code or cache change:

```bash
fly deploy --remote-only --app scoutly-ai
```

Remove it entirely:

```bash
fly apps destroy scoutly-ai
```

---

## Why not Render

`render.yaml` is present but broken in two ways: its `dockerCommand` calls
`bash ./start_render.sh`, a file that does not exist in this repo, and its
`envVars` wire only the Azure variables while the app defaults to Groq. Its free
tier is also 512MB, which is below what this image needs. Fixing it is possible;
nothing currently depends on it.
