"""
Everything that can stop a demo, checked before an audience is watching.

Split out of demo.ps1 rather than inlined into it: PowerShell strips double
quotes when passing an argument to a native executable, so embedded Python
loses its string literals in ways that only show up at run time. A real file
also means the checks can be run on their own, which is what you want the
morning of a presentation.

    python demo_preflight.py           # full check
    python demo_preflight.py --quick   # skip the network probe

Exit code is 0 when the demo can proceed and 1 when it cannot, so demo.ps1 can
simply refuse to start.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

#: Output tokens a complete brief costs, measured across the benchmark runs.
TOKENS_PER_BRIEF = 28_000
#: Free-tier daily token budget, per key. Each key is its own organisation.
TOKENS_PER_KEY = 100_000
#: Below this the cache is too thin to be worth demoing.
MIN_CACHED_PLAYERS = 1_000

failures: list[str] = []
warnings: list[str] = []


def ok(msg: str) -> None:
    print(f"  [OK]   {msg}")


def warn(msg: str) -> None:
    warnings.append(msg)
    print(f"  [WARN] {msg}")


def fail(msg: str, remedy: str = "") -> None:
    failures.append(msg)
    print(f"  [FAIL] {msg}")
    if remedy:
        print(f"         {remedy}")


def check_config() -> None:
    print("\nConfiguration")
    if not (ROOT / ".env").exists():
        fail(".env not found", "Copy .env.example and fill in your keys.")
        return
    ok(".env present")

    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")

    provider = (os.getenv("LLM_PROVIDER") or "groq").strip().lower()
    model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    ok(f"provider={provider}, model={model}")

    if not os.getenv("TAVILY_API_KEY"):
        # Not fatal: the scouter catches a failed search and carries on without
        # the news line. Worth knowing before someone asks where it comes from.
        warn("no TAVILY_API_KEY - form and injury lookups will be skipped")


def check_data() -> None:
    print("\nData")
    cache = ROOT / "data" / "fbref_cache.db"
    if not cache.exists():
        fail("player cache missing",
             "python -m backend.data_pipeline.cache_builder   (10-15 min)")
        return

    try:
        conn = sqlite3.connect(str(cache))
        players = conn.execute(
            "SELECT COUNT(*) FROM player_cohort WHERE season = ?", ("2425",)
        ).fetchone()[0]
        wages = conn.execute("SELECT COUNT(*) FROM player_wages").fetchone()[0]
        by_pos = dict(conn.execute(
            "SELECT UPPER(SUBSTR(TRIM(position), 1, 2)), COUNT(*) "
            "FROM player_cohort WHERE season = ? AND minutes >= 450 "
            "GROUP BY 1", ("2425",)
        ).fetchall())
        conn.close()
    except Exception as exc:
        fail(f"player cache unreadable: {exc}", "Rebuild it with the cache_builder.")
        return

    if players < MIN_CACHED_PLAYERS:
        fail(f"only {players} players cached - too thin to demo",
             "python -m backend.data_pipeline.cache_builder")
        return

    if wages == 0:
        # Not fatal. With no Capology rows every wage is modelled from market
        # value and league, which the report labels as an estimate — but if
        # anyone asks whether the wages are real, the answer is no.
        warn(f"{players} players, but 0 cached wage rows - every wage shown "
             "will be a modelled estimate")
    else:
        ok(f"{players} players, {wages} wage rows")
    spread = ", ".join(f"{k} {v}" for k, v in sorted(by_pos.items()) if k)
    ok(f"eligible cohort (450+ min): {spread}")

    if not (ROOT / "data" / "vectorstore" / "clubs" / "index.faiss").exists():
        fail("vectorstore missing", "python -m backend.rag.loader")
    else:
        ok("club vectorstore present")


def check_budget() -> None:
    """
    The single most likely way the demo dies.

    Each key carries its own daily budget, so a spent key is survivable and an
    entirely spent set is not: the pipeline aborts inside the first agent and
    the only visible symptom is an empty shortlist.
    """
    print("\nToken budget")
    raw = os.getenv("GROQ_API_KEYS") or os.getenv("GROQ_API_KEY", "")
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    if not keys:
        fail("no GROQ_API_KEYS in .env - every agent will fail",
             "Set GROQ_API_KEYS (comma-separated to rotate).")
        return

    from openai import AsyncOpenAI

    base = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
    model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

    async def probe(key: str) -> bool:
        client = AsyncOpenAI(api_key=key, base_url=base, max_retries=0)
        try:
            await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=1,
            )
            return True
        except Exception as exc:
            text = str(exc)
            return not ("429" in text or "rate_limit" in text)

    async def probe_all() -> list[bool]:
        # wait_for has to wrap the gather from *inside* a running loop; building
        # the awaitable outside one raises before a single probe is sent.
        return await asyncio.wait_for(
            asyncio.gather(*(probe(k) for k in keys)), timeout=45
        )

    try:
        results = asyncio.run(probe_all())
    except Exception as exc:
        warn(f"could not reach the provider ({type(exc).__name__}) - check the network")
        return

    live = sum(1 for r in results if r)
    briefs = live * TOKENS_PER_KEY // TOKENS_PER_BRIEF
    if live == 0:
        fail(f"all {len(keys)} keys are rate-limited",
             "The daily budget resets on a rolling 24h window; the 429 states "
             "the exact wait.")
    elif live < len(keys):
        warn(f"{live} of {len(keys)} keys have budget left (~{briefs} briefs)")
    else:
        ok(f"{live} of {len(keys)} keys have budget left (~{briefs} briefs)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true",
                    help="skip the provider probe (offline check only)")
    args = ap.parse_args()

    print("=" * 66)
    print("  Scoutly AI - demo preflight")
    print("=" * 66)

    check_config()
    check_data()
    if args.quick:
        print("\nToken budget\n  [SKIP] --quick")
    else:
        check_budget()

    print("\n" + "=" * 66)
    if failures:
        print(f"  NOT READY - {len(failures)} blocking issue(s)")
        print("=" * 66)
        return 1
    if warnings:
        print(f"  READY - with {len(warnings)} warning(s)")
    else:
        print("  READY")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
