# Scoutly AI

Multi-agent AI football scouting platform.

## Setup

1. Copy `.env.example` to `.env` and fill in your Azure OpenAI, embedding and Tavily keys.
2. Build the player database **once** (see below).
3. Build the RAG vector store once: `python -m backend.rag.loader`
4. Run: `.\start_local.ps1`

## The player database is populated once, offline

Serving a scouting request never scrapes. All FBref/Capology traffic happens in a
single offline build that writes `data/fbref_cache.db`; the MCP server then answers
every query from SQLite in ~0.1 s and never launches Chrome.

```bash
python -m backend.data_pipeline.cache_builder
```

Useful flags:

| Flag | Effect |
| --- | --- |
| `--verify` | Report per-metric coverage and exit. Exit code 1 if anything is incomplete. |
| `--no-wages` | Skip the Capology pass (it is slow and often blocked). |
| `--fresh` | Discard staged tables and re-scrape everything. |
| `--enrich-only` | Refresh metrics for players already cached, without changing the roster. |
| `--legacy-db PATH` | Backfill metrics FBref will not serve from an older cache. |

A cold build takes 10–15 minutes: each Big-5 combined table needs ~2 minutes and
there are six of them. Every table that succeeds is checkpointed to
`data/_build_stage/`, so a Chrome crash mid-build resumes rather than starting over.

Check what you have at any time:

```bash
python -m backend.data_pipeline.cache_builder --verify
```

### Known data limitation

FBref currently serves five columns entirely empty to this scraper — `Tkl`, `Cmp%`,
`Att Pen`, `SCA` and `GCA` — while `TklW`, `Int`, `Gls`, `Ast`, `Sh` and `SoT` come
through fine. This reproduces across separate scrapes, so it is not throttling.

The builder detects all-zero columns and **drops** them rather than storing fake
zeros. Traits that depend on them become explicit qualitative gaps for the tactical
agent to judge, and `trait_coverage` reflects reality. Storing them as 0.0 would make
every player score identically on that metric while reporting full coverage.

To fill them, point `--legacy-db` at a cache built from another source.

## Runtime data policy

| Env var | Default | Effect |
| --- | --- | --- |
| `FBREF_LIVE_SCRAPE` | `false` | Allow tools to hit FBref when the cache misses. Leave off. |
| `FBREF_USE_MOCK` | `false` | Skip ScraperFC entirely, even in the builder. |

## Benchmarking the pipeline

`benchmark_pipeline.py` runs the real agent pipeline over several briefs — one per
position, across leagues — and validates each result against the constraints it
was given, rather than just checking that it produced output.

```bash
python benchmark_pipeline.py
```

| Flag | Effect |
| --- | --- |
| `--only 1,3` | Run selected briefs by id |
| `--gap 45` | Seconds between runs, to respect provider rate limits |

Each run is checked for budget sanity (weekly wage vs fee on the right scales, no
inverted range, no candidate silently over the ceiling), eligibility (nobody
already at the buying club, primary position matches the brief), scoring
consistency (`demonstrated_fit <= measurable_fit`, shortlist actually ordered by
it, contiguous ranks), and data honesty (wages not all identical, no metric
present with a null value). Output lands in `benchmarks/<timestamp>/` with
per-run JSON, the reports, and a summary table.

Exit code is non-zero if any run failed or any check failed, so it works in CI.

## Health check

`GET /api/health` reports whether the two hard dependencies are actually usable —
the Azure OpenAI deployment resolves, and the player cache is populated. Both
otherwise fail deep inside a pipeline run.
