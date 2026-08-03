"""
Agent-level prediction test: would the pipeline have recommended the player the
club actually signed?

backtest_transfers.py answers this for the deterministic core alone — it ranks a
player against the buying club's profile and reports where they land. That
leaves the half of the system made of agents untested: the LLM still has to pick
3-5 names out of the ~20 it is shown, and a good ranking is worth nothing if the
selection step then ignores it.

So this runs the real pipeline. Each brief is a transfer that actually happened
in summer 2025 and worked (1500+ minutes at 7.0+ in 2025-26), phrased as the
brief that club would plausibly have written, with a budget matching the fee
they really paid. The only question asked is whether the signing comes back in
the shortlist.

Every brief here is one the fit engine already ranks inside the top 30 that the
scouter prices and shows, which is deliberate: it isolates the agent layer. A
miss means the model discarded a candidate it was shown, not that retrieval
never surfaced them.

    python predict_real_signings.py            # all briefs
    python predict_real_signings.py --only 1,3
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Player names carry accents and the Windows console is cp1252. Without this the
# run dies on the first "Fagioli"/"Cásseres" it prints, after the tokens have
# already been spent.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from backend.agents.planner import PlannerAgent   # noqa: E402
from backend.utils.config import make_llm_client  # noqa: E402

#: Real summer-2025 transfers that succeeded. `signed` is the name exactly as
#: the 2024-25 cache spells it, because that is what a shortlist match is against.
BRIEFS = [
    {
        "id": 1,
        "club": "Bayern Munich",
        "signed": "Jonathan Tah",
        "from": "Leverkusen",
        "fee": "free transfer",
        "query": "Find a commanding centre-back for Bayern Munich who is strong in "
                 "the air and comfortable defending a high line, fee up to 25m and "
                 "wages up to 200k a week",
    },
    {
        "id": 2,
        "club": "Rennes",
        "signed": "Valentin Rongier",
        "from": "Marseille",
        "fee": "~EUR 3M",
        "query": "Find a hard-working central midfielder for Rennes who can control "
                 "tempo and break up play, fee under 10m and up to 80k per week",
    },
    {
        "id": 3,
        "club": "Fiorentina",
        "signed": "Nicolò Fagioli",
        "from": "Juventus",
        "fee": "~EUR 13.5M",
        "query": "Find a technical central midfielder for Fiorentina who is secure "
                 "in possession and progresses the ball, fee up to 20m and 90k a week",
    },
    {
        "id": 4,
        "club": "Real Madrid",
        "signed": "Dean Huijsen",
        "from": "Bournemouth",
        "fee": "~EUR 60M",
        "query": "Find a young ball-playing centre-back for Real Madrid, under 23, "
                 "comfortable building from the back, fee up to 70m and wages up to "
                 "250k a week",
    },
    {
        "id": 5,
        "club": "Barcelona",
        "signed": "Joan García",
        "from": "Espanyol",
        "fee": "~EUR 25M",
        "query": "Find a goalkeeper for Barcelona who is a reliable shot-stopper and "
                 "comfortable sweeping behind a high line, fee up to 30m and 150k a week",
    },
]


def _norm(name: str) -> str:
    """Fold accents so a shortlist match does not hinge on encoding."""
    import unicodedata
    return "".join(
        c for c in unicodedata.normalize("NFKD", str(name).lower())
        if not unicodedata.combining(c)
    ).strip()


async def run_one(brief: dict) -> dict:
    queue: asyncio.Queue = asyncio.Queue()
    events: list[dict] = []
    stop = asyncio.Event()

    async def pump() -> None:
        while not stop.is_set() or not queue.empty():
            try:
                events.append(await asyncio.wait_for(queue.get(), timeout=0.4))
            except asyncio.TimeoutError:
                continue

    pumping = asyncio.create_task(pump())
    planner = PlannerAgent(
        client=make_llm_client(), sse_queue=queue, run_id=str(uuid.uuid4())
    )

    rec: dict = {"brief": brief, "ok": False}
    t0 = time.time()
    try:
        draft = await planner.run_draft(query=brief["query"])
        rec["ok"] = True
        players = draft.get("ranked_players") or []
        rec["shortlist"] = [
            {
                "rank": p.get("rank"),
                "name": p.get("name"),
                "squad": p.get("squad"),
                "demonstrated_fit": p.get("demonstrated_fit"),
                "wage_eur": p.get("wage_eur"),
                "value_eur": p.get("value_eur"),
            }
            for p in players
        ]
        rec["funnel"] = draft.get("selection_funnel", {})
        rec["budget"] = {
            k: draft.get("financial_decision", {}).get(k)
            for k in ("salary_max", "value_max", "budget_basis")
        }
        target = _norm(brief["signed"])
        hit = next(
            (p for p in rec["shortlist"] if _norm(p["name"]) == target), None
        )
        rec["found"] = hit is not None
        rec["found_at_rank"] = hit["rank"] if hit else None
    except Exception as exc:
        rec["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        stop.set()
        await pumping

    rec["seconds"] = round(time.time() - t0, 1)
    # Keep the whole narration. A miss has to be attributable — a ceiling, a
    # position filter, or the model passing over someone it was shown — and
    # only the scouter's own progress lines distinguish those.
    rec["trace"] = [
        e.get("message", "") for e in events if e.get("type") == "progress"
    ]
    target = _norm(brief["signed"])
    rec["target_mentioned_in_trace"] = [
        m for m in rec["trace"] if target.split()[-1] in _norm(m)
    ]
    return rec


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma-separated brief ids")
    ap.add_argument("--gap", type=int, default=30,
                    help="seconds between runs, to respect provider rate limits")
    args = ap.parse_args()

    briefs = BRIEFS
    if args.only:
        wanted = {int(x) for x in args.only.split(",")}
        briefs = [b for b in briefs if b["id"] in wanted]

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    outdir = ROOT / "benchmarks" / f"{stamp}_predict"
    outdir.mkdir(parents=True, exist_ok=True)

    records = []
    for i, b in enumerate(briefs):
        print(f"[{i+1}/{len(briefs)}] {b['club']} — actually signed {b['signed']}")
        rec = await run_one(b)
        if rec["ok"]:
            verdict = (f"FOUND at rank {rec['found_at_rank']}" if rec["found"]
                       else "missed")
            names = ", ".join(p["name"] for p in rec["shortlist"]) or "(empty)"
            print(f"        {verdict}  [{names}]  {rec['seconds']}s\n")
        else:
            print(f"        FAILED {rec.get('error','')[:120]}\n")
        records.append(rec)
        # Written after every brief, not at the end. A crash on the third run
        # previously discarded two completed runs' worth of tokens.
        (outdir / "runs.json").write_text(
            json.dumps(records, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8")
        if args.gap and i < len(briefs) - 1:
            await asyncio.sleep(args.gap)

    lines = ["=" * 92,
             "AGENT-LEVEL PREDICTION TEST — real summer-2025 signings",
             "=" * 92, ""]
    ok = [r for r in records if r["ok"]]
    hits = [r for r in ok if r["found"]]
    for r in records:
        b = r["brief"]
        if not r["ok"]:
            lines.append(f"  {b['club']:<16} {b['signed']:<20} FAILED  {r.get('error','')[:60]}")
            continue
        mark = f"HIT  rank {r['found_at_rank']}" if r["found"] else "MISS"
        lines.append(f"  {b['club']:<16} {b['signed']:<20} {mark}")
        lines.append(f"      shortlist: " +
                     ", ".join(f"{p['name']}" for p in r["shortlist"]))
    lines += ["",
              f"  {len(hits)}/{len(ok)} completed briefs returned the player the club "
              f"actually signed.",
              ""]
    text = "\n".join(lines)
    print(text)
    (outdir / "summary.txt").write_text(text, encoding="utf-8")
    (outdir / "runs.json").write_text(
        json.dumps(records, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"-> {outdir}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
