# Demo guide

Run `.\demo.ps1`. It refuses to start unless the demo can finish — the check
that matters is the daily token budget, because when that runs out the pipeline
dies inside the first agent and the only visible symptom is an empty shortlist.

A full run takes 90–120 seconds. That is not dead air: the event stream is the
demo. Every filter the pipeline applies announces itself, so the audience watches
the shortlist being argued down from ~870 candidates to four.

---

## The line to open with

> Recruitment departments answer one question: given our budget, our system and
> our squad, who should we sign? The hard part is not finding data. It is that
> the honest answer is often "nobody you can afford", and a system that cannot
> say that will happily recommend someone it should not.

That frames the two things worth showing: it ranks the whole market, and it
admits what it cannot measure.

---

## What to show, in order

**1. Paste brief 1 (Fiorentina).** While it runs, narrate the stream.

The order matters and each step is visible:

| Step | What the audience sees |
| --- | --- |
| Financial | a budget sized for *this* club, with assumptions flagged |
| Scouter | 1,096 midfielders → dedup → position filter → ranked by fit |
| Fee/wage ceilings | named candidates excluded, with the number that excluded them |
| Tactical | deterministic fit, then the written verdict over it |
| Costing | fee amortisation and annual book cost per candidate |

**2. Land on the result.** Fiorentina's shortlist returns **Luka Modrić** and
**Vitinha**. Both genuinely moved in summer 2025 and posted 7.5+ seasons after
moving. The system was only ever shown 2024-25 data.

> It did not know these players would move. It ranked them on how well they fit
> Fiorentina, from the season before.

**3. Show the honesty.** Open a player card and point at `trait_coverage` and
the qualitative gaps. Say plainly:

> Five of the traits Fiorentina asks for can be measured from the data. The rest
> cannot, and the system says so rather than scoring them zero and pretending.
> `demonstrated_fit` counts an unmeasured requirement as unmet, which is why it
> is always at or below `measurable_fit`.

This is the strongest slide in the deck and it is a *limitation*. Lean into it.

**4. Use the review step.** Instead of picking a player, ask:

> why is the first one ranked above the second?

It routes to `ASK_QUESTION` and answers from the shortlist already in memory —
no agents re-run. Then pick a player and let it generate the PDF.

---

## The claim to make about accuracy

Do not claim it predicts transfers. Claim what was measured:

- Scored against the 2025-26 outcomes of **327 real transfers**, correlation
  between fit and match rating is **+0.300**. Ranking the same players by
  minutes played — the obvious alternative — scores **+0.016**.
- **54%** of signings that worked land in the top quartile of the cohort.
- Of the players it recommended in a live test, **24%** went on to a 7.0+ season
  at 1500+ minutes, against a **9.9%** base rate. A 2.4× lift.

If asked "so would it have found the player we signed?" — the honest answer is
that exact-name match is a harsh bar and it did not hit it in five tries. It
surfaces *a* good option, not *the* option. Say that; it is more credible than
the alternative and someone will check.

---

## If something goes wrong

**Empty shortlist.** Almost always the token budget. Check the terminal for a
429. Re-run `.\demo.ps1` — it probes every key before serving.

**A run hangs past ~3 minutes.** A provider retry is backing off. Let it finish;
the stream resumes. Do not refresh — in-flight runs live in memory and a reload
drops them.

**PDF does not download.** WeasyPrint needs GTK libraries Windows does not ship,
so it falls back to Chromium and then to HTML. The download endpoint serves
whichever was produced, so the link works either way.

**Nothing responds.** `GET /api/health` reports the two hard dependencies — the
chat provider and the player cache — with a reason for each.

---

## Questions you should expect

**"Does the model make up statistics?"** No, and this is structural rather than
a matter of prompting. The scouter LLM only ever returns names and a written
note; every number is rehydrated from the cache afterwards, so a fabricated stat
cannot survive. A name that does not match the cohort is discarded outright.

**"Why not just use market value to rank?"** Market value already encodes
reputation and the buying club's wealth. Fit is computed from what the player
did on the pitch, against what this specific club's profile asks for.

**"Where do the club profiles come from?"** Derived from measured team data, not
authored by hand — team-level metrics for all 96 Big-5 clubs, turned into a
tactical profile and a set of required traits per position.

**"What is the biggest weakness?"** Two, and say them before you are asked. The
fee is a Transfermarkt valuation discounted by contract time remaining, not a
quoted asking price — no system can know what a club would actually accept. And
a player who moved mid-season has his season split across two rows, so his
minutes are under-counted until the cache builder aggregates them.
