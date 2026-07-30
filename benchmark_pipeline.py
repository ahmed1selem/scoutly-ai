"""
End-to-end pipeline benchmark.

Runs the real agent pipeline over several briefs and checks each result against
the constraints it was given — budget, eligibility, ranking consistency — rather
than just asserting it produced something.

Usage:
    python benchmark_pipeline.py                 # all briefs
    python benchmark_pipeline.py --only 1,3      # selected briefs
    python benchmark_pipeline.py --gap 45        # seconds between runs

Requires the MCP server on port 8005 and a working chat provider. Writes
benchmarks/<stamp>/ with per-run JSON, the reports, and a summary table.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from backend.agents.planner import PlannerAgent          # noqa: E402
from backend.agents.scouter import _same_club             # noqa: E402
from backend.utils.config import make_llm_client, settings  # noqa: E402

# ── Brief sets ────────────────────────────────────────────────────────────────
# "top" exercises the elite clubs; "mid" is the harder test — mid-table sides
# have real budget ceilings, so the fee and wage filters actually bite instead of
# waving everyone through, and the shortlist has to find value rather than fame.
ELITE_BRIEFS = [
    {"id": 1, "club": "Liverpool",       "position": "DF",
     "query": "Find a strong centre-back for Liverpool under 27"},
    {"id": 2, "club": "Arsenal",         "position": "FW",
     "query": "Find a clinical striker for Arsenal who can lead the line"},
    {"id": 3, "club": "Manchester United", "position": "MF",
     "query": "Find a ball-winning midfielder for Manchester United"},
    {"id": 4, "club": "Chelsea",         "position": "GK",
     "query": "Find a goalkeeper for Chelsea comfortable playing out from the back"},
    {"id": 5, "club": "Bayern Munich",   "position": "DF",
     "query": "Find a young attacking left-back for Bayern Munich on a modest budget"},
]

MID_TABLE_BRIEFS = [
    {"id": 1, "club": "Brentford", "position": "DF",
     "query": "Find a centre-back for Brentford who is comfortable in possession, "
              "budget up to 15m and 60k a week"},
    {"id": 2, "club": "Torino", "position": "FW",
     "query": "Find a striker for Torino who can lead the line on a tight budget"},
    {"id": 3, "club": "Mainz 05", "position": "MF",
     "query": "Find a hard-working central midfielder for Mainz 05, "
              "no more than 12m and 40k per week"},
    {"id": 4, "club": "Rayo Vallecano", "position": "GK",
     "query": "Find a goalkeeper for Rayo Vallecano who is good with his feet, "
              "cheap or free transfer preferred"},
    {"id": 5, "club": "RC Lens", "position": "DF",
     "query": "Find an attacking full-back for RC Lens under 25 on a modest budget"},
    {"id": 6, "club": "Bologna", "position": "FW",
     "query": "Find a winger for Bologna with a fee under 20m"},
]

BRIEF_SETS = {"top": ELITE_BRIEFS, "mid": MID_TABLE_BRIEFS}
BRIEFS = ELITE_BRIEFS


# ── Validation ────────────────────────────────────────────────────────────────

def validate(brief: dict, draft: dict, final: dict) -> list[dict]:
    """Check a completed run against its own brief. Returns a list of checks."""
    checks: list[dict] = []

    def ck(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    players = draft.get("ranked_players") or []
    fin = draft.get("financial_decision") or {}
    smin = fin.get("salary_min")
    smax = fin.get("salary_max")
    vmax = fin.get("value_max")

    # ── budget sanity ─────────────────────────────────────────────────────────
    ck("financial: salary_max present", isinstance(smax, (int, float)) and smax > 0,
       f"salary_max={smax}")
    if isinstance(smin, (int, float)) and isinstance(smax, (int, float)):
        ck("financial: salary_min <= salary_max", smin <= smax, f"{smin} <= {smax}")
    ck("financial: salary_max plausible for a weekly wage",
       isinstance(smax, (int, float)) and 5_000 <= smax <= 1_000_000,
       f"salary_max={smax}")
    ck("financial: value_max present", isinstance(vmax, (int, float)) and vmax > 0,
       f"value_max={vmax}")
    ck("financial: fee and wage are on different scales",
       not (isinstance(vmax, (int, float)) and isinstance(smax, (int, float)))
       or vmax > smax * 10,
       f"value_max={vmax} salary_max={smax}")
    ck("financial: budget attributed to the target club",
       fin.get("team_name") == brief["club"], f"team_name={fin.get('team_name')!r}")
    ck("financial: budget basis is stated",
       bool(fin.get("budget_basis")), f"basis={fin.get('budget_basis')!r}")

    # An empty shortlist is the correct answer to an over-constrained brief, so
    # what matters is whether the pipeline can account for it. Brentford asking
    # for a EUR 15M centre-back on EUR 60k/wk legitimately yields nobody; a silent
    # zero with no explanation would be a failure.
    funnel = draft.get("selection_funnel") or {}
    if players:
        ck("shortlist non-empty", True, f"{len(players)} players")
    else:
        excluded = {k: v for k, v in funnel.items()
                    if k not in ("cohort", "shortlisted") and v}
        ck("empty shortlist is explained by the selection funnel",
           bool(funnel.get("cohort")) and bool(excluded),
           f"funnel={funnel}")
        return checks

    # ── budget adherence, per player ──────────────────────────────────────────
    if isinstance(smax, (int, float)):
        over = [p for p in players
                if p.get("wage_eur") and p["wage_eur"] > smax and not p.get("over_budget")]
        ck("budget: no candidate silently exceeds salary_max", not over,
           "; ".join(f"{p['name']} €{p['wage_eur']:,} > €{smax:,}" for p in over))
    if isinstance(vmax, (int, float)):
        # The fee ceiling is a hard filter, not a warning label. Recommending a
        # player the club cannot buy is not a useful option to weigh.
        over_v = [p for p in players if p.get("value_eur") and p["value_eur"] > vmax]
        ck("budget: no candidate exceeds the fee ceiling", not over_v,
           "; ".join(f"{p['name']} €{p['value_eur']/1e6:.0f}M > €{vmax/1e6:.0f}M"
                     for p in over_v))
        ck("budget: every candidate is costed against both ceilings",
           all(p.get("economic_verdict") for p in players),
           f"{sum(1 for p in players if p.get('economic_verdict'))}/{len(players)}")

    # ── eligibility ───────────────────────────────────────────────────────────
    own = [p["name"] for p in players if _same_club(p.get("squad", ""), brief["club"])]
    ck("eligibility: no player already at the target club", not own, ", ".join(own))

    wrong_pos = [f"{p['name']}({p.get('position')})" for p in players
                 if p.get("position", "").split(",")[0].strip().upper() != brief["position"]]
    ck(f"eligibility: primary position is {brief['position']}", not wrong_pos,
       ", ".join(wrong_pos))

    # ── scoring consistency ───────────────────────────────────────────────────
    both = [p for p in players
            if p.get("demonstrated_fit") is not None and p.get("measurable_fit") is not None]
    bad = [p["name"] for p in both if p["demonstrated_fit"] > p["measurable_fit"] + 0.05]
    ck("scoring: demonstrated_fit <= measurable_fit", not bad, ", ".join(bad))

    # A shortlist where nothing could be measured is not a ranking. The GK and MF
    # briefs produced exactly this — every candidate at coverage 0.0, ordered
    # arbitrarily — and every other check still passed.
    covs = [p.get("trait_coverage") or 0 for p in players]
    ck("scoring: at least one candidate has measurable trait coverage",
       any(c > 0 for c in covs), f"coverages={covs}")

    fits = [p.get("demonstrated_fit") for p in players if p.get("demonstrated_fit") is not None]
    ck("scoring: shortlist ordered by demonstrated_fit",
       fits == sorted(fits, reverse=True), str(fits))

    ranks = [p.get("rank") for p in players]
    ck("scoring: ranks are 1..n with no gaps", ranks == list(range(1, len(players) + 1)),
       str(ranks))

    for p in players:
        cov = p.get("trait_coverage")
        conf = p.get("confidence")
        if cov is not None and conf is not None:
            # Coverage multiplies confidence, so confidence can never exceed it
            # by much; a high confidence on thin coverage is the old bug.
            ck(f"scoring: {p['name']} confidence consistent with coverage",
               conf <= cov + 0.35, f"conf={conf} cov={cov}")
            break

    # ── data honesty ──────────────────────────────────────────────────────────
    wages = {p.get("wage_eur") for p in players if p.get("wage_eur")}
    ck("data: wages are not all identical", len(wages) > 1 or len(players) == 1,
       f"{len(wages)} distinct across {len(players)}")
    unpriced = [p["name"] for p in players if not p.get("wage_eur")]
    ck("data: every candidate has a wage figure", not unpriced, ", ".join(unpriced))
    fabricated = [p["name"] for p in players
                  for k, v in (p.get("raw_metrics") or {}).items()
                  if v.get("per90") is None]
    ck("data: no metric present with a null value", not fabricated, ", ".join(fabricated))

    # ── report ────────────────────────────────────────────────────────────────
    path = Path(final.get("pdf_path", ""))
    ck("report: file written", path.exists(), str(path))
    if path.exists():
        ck("report: non-trivial size", path.stat().st_size > 20_000,
           f"{path.stat().st_size:,} bytes")
        ck("report: rendered as PDF", final.get("pdf_backend") in ("weasyprint", "chromium"),
           f"backend={final.get('pdf_backend')}")
    return checks


# ── One run ───────────────────────────────────────────────────────────────────

async def run_brief(brief: dict, outdir: Path) -> dict:
    run_id = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue()
    events: list[dict] = []
    stop = asyncio.Event()

    async def pump() -> None:
        while not stop.is_set() or not queue.empty():
            try:
                ev = await asyncio.wait_for(queue.get(), timeout=0.4)
            except asyncio.TimeoutError:
                continue
            ev.setdefault("timestamp", time.time())
            events.append(ev)

    task = asyncio.create_task(pump())
    planner = PlannerAgent(client=make_llm_client(), sse_queue=queue, run_id=run_id)

    record: dict = {
        "brief": brief, "run_id": run_id, "ok": False,
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }
    t0 = time.time()
    draft: dict = {}
    final: dict = {}
    try:
        draft = await planner.run_draft(query=brief["query"])
        record["draft_seconds"] = round(time.time() - t0, 1)
        t1 = time.time()
        final = await planner.run_finalize(
            query=brief["query"],
            ranked_players=draft.get("ranked_players", []),
            financial_decision=draft.get("financial_decision", {}),
            selection_funnel=draft.get("selection_funnel", {}),
        )
        record["finalize_seconds"] = round(time.time() - t1, 1)
        record["ok"] = True
    except Exception as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        stop.set()
        await task

    record["total_seconds"] = round(time.time() - t0, 1)

    # Per-agent wall time, from first to last event each agent emitted.
    spans: dict[str, list[float]] = defaultdict(list)
    for ev in events:
        spans[ev.get("agent", "?")].append(ev["timestamp"])
    record["agent_seconds"] = {
        a: round(max(ts) - min(ts), 1) for a, ts in spans.items() if len(ts) > 1
    }
    record["event_count"] = len(events)
    record["errors_emitted"] = [
        {"agent": e.get("agent"), "message": e.get("message"),
         "error": str(e.get("error") or e.get("detail") or "")[:300]}
        for e in events if e.get("type") in ("error", "agent_error", "pipeline_error")
    ]

    players = draft.get("ranked_players") or []
    record["player_count"] = len(players)
    record["shortlist"] = [
        {k: p.get(k) for k in (
            "rank", "name", "squad", "position", "age", "minutes",
            "demonstrated_fit", "measurable_fit", "tactical_fit_score",
            "trait_coverage", "confidence", "wage_eur", "wage_low", "wage_high",
            "wage_estimated", "wage_source", "value_eur", "value_as_of",
            "contract_expires", "used_position_key", "economic_verdict",
            "within_fee_budget", "within_wage_budget")}
        for p in players
    ]
    record["financial_decision"] = draft.get("financial_decision", {})
    record["selection_funnel"] = draft.get("selection_funnel", {})
    record["report"] = {
        "path": final.get("pdf_path", ""),
        "backend": final.get("pdf_backend", ""),
        "bytes": Path(final["pdf_path"]).stat().st_size
                 if final.get("pdf_path") and Path(final["pdf_path"]).exists() else 0,
    }
    record["checks"] = validate(brief, draft, final)
    record["checks_passed"] = sum(1 for c in record["checks"] if c["ok"])
    record["checks_failed"] = sum(1 for c in record["checks"] if not c["ok"])

    (outdir / f"run_{brief['id']:02d}.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return record


# ── Reporting ─────────────────────────────────────────────────────────────────

def summarise(records: list[dict], outdir: Path) -> str:
    lines: list[str] = []
    w = lines.append

    w("=" * 100)
    w(f"PIPELINE BENCHMARK — {len(records)} runs — provider={settings.LLM_PROVIDER} "
      f"model={settings.OPENAI_MODEL}")
    w("=" * 100)

    w("")
    w(f"{'#':<3}{'club':<20}{'pos':<5}{'players':>8}{'draft':>8}{'final':>8}"
      f"{'total':>8}{'checks':>10}{'report':>10}")
    w("-" * 100)
    for r in records:
        b = r["brief"]
        status = "FAILED" if not r["ok"] else f"{r['checks_passed']}/{r['checks_passed']+r['checks_failed']}"
        w(f"{b['id']:<3}{b['club']:<20}{b['position']:<5}{r.get('player_count',0):>8}"
          f"{r.get('draft_seconds','-'):>8}{r.get('finalize_seconds','-'):>8}"
          f"{r.get('total_seconds','-'):>8}{status:>10}"
          f"{(r['report']['backend'] or '-'):>10}")

    ok = [r for r in records if r["ok"]]
    if ok:
        totals = [r["total_seconds"] for r in ok]
        w("")
        w(f"timing  min {min(totals):.0f}s   median {statistics.median(totals):.0f}s   "
          f"max {max(totals):.0f}s   mean {statistics.mean(totals):.0f}s")
        stage: dict[str, list[float]] = defaultdict(list)
        for r in ok:
            for a, s in r.get("agent_seconds", {}).items():
                stage[a].append(s)
        w("per-agent median seconds: " + "  ".join(
            f"{a}={statistics.median(v):.0f}" for a, v in sorted(stage.items())))

    w("")
    w("BUDGET & CORRECTNESS")
    w("-" * 100)
    by_check: dict[str, list[bool]] = defaultdict(list)
    for r in records:
        for c in r["checks"]:
            by_check[c["check"]].append(c["ok"])
    for name, results in sorted(by_check.items()):
        passed = sum(results)
        flag = "OK  " if passed == len(results) else "FAIL"
        w(f"  {flag}  {name:<62} {passed}/{len(results)}")

    failures = [(r["brief"]["id"], c) for r in records for c in r["checks"] if not c["ok"]]
    if failures:
        w("")
        w("FAILED CHECKS IN DETAIL")
        w("-" * 100)
        for rid, c in failures:
            w(f"  run {rid}: {c['check']}")
            if c["detail"]:
                w(f"           {c['detail']}")

    errs = [(r["brief"]["id"], e) for r in records for e in r.get("errors_emitted", [])]
    if errs:
        w("")
        w("AGENT ERRORS EMITTED")
        w("-" * 100)
        for rid, e in errs:
            w(f"  run {rid} [{e['agent']}] {e['message']}")
            if e["error"]:
                w(f"           {e['error'][:180]}")

    w("")
    w("SHORTLISTS")
    w("-" * 100)
    for r in records:
        b = r["brief"]
        w(f"  {b['id']}. {b['club']} — {b['query']}")
        fin = r.get("financial_decision", {})
        w(f"     budget: wage <= EUR {fin.get('salary_max', 0):,}/wk   "
          f"value <= EUR {(fin.get('value_max') or 0)/1e6:.0f}M   "
          f"[{fin.get('budget_basis','basis not stated')}]")
        for flag in (fin.get("risk_flags") or [])[:3]:
            w(f"       ! {flag}")
        if not r["shortlist"]:
            w("     (no candidates)")
        for p in r["shortlist"]:
            val = f"EUR {p['value_eur']/1e6:.0f}M" if p.get("value_eur") else "-"
            w(f"     #{p.get('rank','?')} {(p.get('name') or '')[:24]:<26}"
              f"{(p.get('squad') or '')[:18]:<20}"
              f"demo={p.get('demonstrated_fit')!s:<6} meas={p.get('measurable_fit')!s:<6} "
              f"cov={p.get('trait_coverage')!s:<6} "
              f"wage=EUR {(p.get('wage_eur') or 0):,}{'est' if p.get('wage_estimated') else '   '} "
              f"val={val}")
        w("")

    text = "\n".join(lines)
    (outdir / "summary.txt").write_text(text, encoding="utf-8")
    (outdir / "runs.json").write_text(
        json.dumps(records, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return text


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", choices=sorted(BRIEF_SETS), default="top",
                    help="which brief set to run: 'top' (elite clubs) or "
                         "'mid' (mid-table clubs, tighter budgets)")
    ap.add_argument("--only", help="comma-separated brief ids")
    ap.add_argument("--gap", type=int, default=30,
                    help="seconds to wait between runs, to respect provider rate limits")
    args = ap.parse_args()

    briefs = BRIEF_SETS[args.set]
    if args.only:
        wanted = {int(x) for x in args.only.split(",")}
        briefs = [b for b in briefs if b["id"] in wanted]

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    outdir = ROOT / "benchmarks" / f"{stamp}_{args.set}"
    outdir.mkdir(parents=True, exist_ok=True)
    print(f"benchmark -> {outdir}\n")

    records = []
    for i, brief in enumerate(briefs):
        print(f"[{i+1}/{len(briefs)}] {brief['club']} — {brief['query']}")
        rec = await run_brief(brief, outdir)
        flag = "ok" if rec["ok"] else f"FAILED ({rec.get('error','')[:80]})"
        print(f"        {flag}  {rec.get('player_count',0)} players  "
              f"{rec.get('total_seconds','-')}s  "
              f"checks {rec['checks_passed']}/{rec['checks_passed']+rec['checks_failed']}\n")
        records.append(rec)
        if args.gap and i < len(briefs) - 1:
            print(f"        pausing {args.gap}s for provider rate limits\n")
            await asyncio.sleep(args.gap)

    print(summarise(records, outdir))
    return 0 if all(r["ok"] and r["checks_failed"] == 0 for r in records) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
