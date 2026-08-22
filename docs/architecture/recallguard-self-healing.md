# RecallGuard: how GapRadar's self-healing reliability works

> "AI proposes. RecallGuard proves." — `backend/app/recallguard/service.py`

RecallGuard is GapRadar's reliability layer for its Bright Data scrapers
("collectors"). It watches every collection run, decides whether the data
coming back can be trusted, asks Bright Data's AI to repair a broken
scraper when that's the right fix, and — this is the part that makes it
*self-healing* rather than just *self-reporting* — refuses to call anything
recovered until a brand-new, independently collected run proves it.

This document explains the mechanism end to end: what it watches, how it
diagnoses a failure, how the autonomous repair loop works, and exactly what
evidence has to exist before an incident is allowed to close.

---

## 1. The core principle: execution success ≠ data trust

A Bright Data collector run can finish with `status = SUCCEEDED` and still
be lying. The historical bug that motivated this design: a scraper reading
a 1-10 severity score off a page and returning `60` instead — the run
"succeeded," HTTP-wise, but every record was wrong.

RecallGuard formalizes the distinction:

| Concept | Owned by | Answers |
|---|---|---|
| `CollectorRun.status` | the collection orchestrator | "Did the scrape execute?" |
| `ReliabilityIncident` / `ReliabilityState` | RecallGuard | "Can this collector's data be trusted *right now*?" |

A run's own status is **never rewritten** by RecallGuard. A technically
successful run that fails reliability checks stays `SUCCEEDED` in the
database — RecallGuard just also opens an incident and, downstream, refuses
to let anything treat that run's data as trusted.

---

## 2. Detection: five deterministic checks, no LLM

Every terminal run (`SUCCEEDED` or `FAILED`) is evaluated by
`evaluate_collector_run` (`app/recallguard/service.py`), which runs a fixed
set of pure functions from `app/recallguard/detection.py`:

1. **execution** — did the run reach `SUCCEEDED`?
2. **transport_payload** — is the dataset a well-formed JSON array?
3. **source_contract** — does every record satisfy the Fix My Itch schema
   (field ranges, required fields, no extra fields)?
4. **ingestion** — did GapRadar's own persistence succeed?
5. **completeness** *(only for a `SUCCEEDED` run)* — compared against a
   *baseline*, never a fixed number: the largest record count this
   collector has ever produced. Growth always passes. Only a drop to zero,
   or (if configured) a drop beyond a policy threshold, fails.

These are plain functions over metadata the orchestrator already wrote to
the run (`raw_metadata.orchestration`) — no database writes, no provider
calls, no LLM, and critically **no repair of the source data**. A
`tam_score` of `60` stays `60` in the evidence forever; the wrong value
*is* the finding.

If every check passes, nothing is opened (a clean run never closes an
incident either — see §5). If any check fails, `diagnose()` classifies the
failure:

| Failure classification | Meaning | Recommended action |
|---|---|---|
| `OUTAGE` | Provider couldn't execute the collection at all (trigger/collection/timeout stage) | `RETRY` |
| `EXTRACTION_DRIFT` | Collection ran, but returned something malformed or incomplete — this is what a broken selector looks like | `REQUEST_HEAL` |
| `UNKNOWN` | GapRadar's own ingestion failed — never the scraper's fault | `INVESTIGATE` |

Only `EXTRACTION_DRIFT` / `REQUEST_HEAL` is eligible for autonomous repair.
An outage isn't a scraper problem, so healing is refused for it (§6
explains how an outage still gets to close on its own).

A `ReliabilityIncident` row is opened (or, if one is already active for
that collector, updated with a new "occurrence" in its evidence JSON) —
**at most one active incident per collector**, so a repeatedly failing
scraper accumulates evidence instead of spawning duplicate rows.

---

## 3. The incident lifecycle (state machine)

```
DEGRADED ──start_healing──► HEALING ──register_repair_candidate──► VALIDATING
                                                                       │
                                                          begin_validation
                                                                       │
                                                                       ▼
                                                                  VERIFYING
                                                                   │      │
                                                        verify_recovery   │
                                                          passes │        │ fails
                                                                 ▼        ▼
                                                            RECOVERED  DEGRADED
                                                            (terminal)  (next attempt)

  3 failed attempts, or an unrepairable diagnosis ──► MANUAL_REVIEW (a human owns it)
```

Enforced invariants (`app/recallguard/service.py`):

- **A repair can never declare itself successful.** Registering a
  candidate, or even having Bright Data approve and deploy it, only moves
  the incident to `VALIDATING` — a deliberately non-committal status.
- **Only an independent, fresh run can move an incident to `RECOVERED`.**
  `verify_recovery` requires a *new* `CollectorRun` that: belongs to the
  same collector, is not the run that detected the incident, has never
  been used to verify this incident before, started **after** the repair
  candidate was registered, executed `SUCCEEDED`, and passes every one of
  the five checks above (re-run fresh, not trusted from an earlier
  evaluation).
- **Capped autonomous repair.** `MAX_AUTONOMOUS_REPAIR_ATTEMPTS = 3`. A
  fourth attempt is refused outright and the incident is escalated to
  `MANUAL_REVIEW` for a human.
- **Source data is never modified.** Offending field values are preserved
  verbatim in the incident's evidence as proof, never "corrected" to look
  plausible.

---

## 4. The autonomous repair loop, step by step

`app/recallguard/healing.py` is the module that actually talks to Bright
Data. RecallGuard's service module only tracks state — nothing in
`service.py` ever contacts the provider. The full loop, from
`resume_or_execute_healing_attempt`:

```
DEGRADED
  └─ ask Bright Data: is a repair already running for this collector?
       ├─ yes → resume_healing        (no new attempt consumed)
       └─ no  → start_healing         (consumes 1 of 3 attempts)
                  └─ POST refactor_template (the self-heal trigger)
  └─ poll progress until AWAITING_APPROVAL or a terminal state
  └─ register_repair_candidate         (HEALING → VALIDATING; "a candidate
                                         exists," not "it works")
  └─ candidate preflight (see §4.2)
       ├─ FAIL → reject the candidate → back to DEGRADED
       │          (attempt spent; escalates to MANUAL_REVIEW only once
       │           the 3-attempt budget is gone)
       └─ PASS → approve the candidate → Bright Data deploys the repair
  └─ run a brand-new production collection through the SAME orchestrator
     used for every normal collection (no second collection code path)
  └─ verify_recovery on that fresh run
       ├─ FAIL → back to DEGRADED (next attempt, up to 3 total)
       └─ PASS → RECOVERED, with a structured RecoveryProof persisted
```

### 4.1 Why "resume" exists — and why attempt 1 stays attempt 1

Bright Data keeps repairing after GapRadar stops watching — after a local
poll times out, the process exits, or a deployment restarts. If GapRadar
trusted only its own record, a restarted process would see `DEGRADED` and
trigger a *second* repair on top of a live one.

So before doing anything, `resume_or_execute_healing_attempt` always asks
the provider first: `GET /dca/collectors/{id}/refactor_template/progress`.

- `RUNNING` / `AWAITING_APPROVAL` → **resume**. No trigger call, no attempt
  consumed — the candidate path picks up exactly where it left off.
- `UNKNOWN` or unreadable → **refuse**. Fail closed: an unrecognized status
  might be hiding a live repair, so nothing is written and nothing is
  triggered.
- `DONE` / `FAILED` / no job at all → nothing is in flight, so a genuinely
  new attempt may start, budget rules applying as normal.

This is also why a local timeout is deliberately **not** treated as a
provider failure (`HealingOutcome.LOCAL_TIMEOUT` vs.
`HealingOutcome.PROVIDER_FAILED`): GapRadar running out of patience says
nothing about whether Bright Data's repair is actually broken.

### 4.2 Candidate preflight — evidence, not a verdict

Before a repair candidate is ever approved, its `preview_result` (a sample
of what the repaired scraper *would* return) is run through the exact same
strict source-contract validation production data faces
(`app/integrations/brightdata/fix_my_itch.py:validate_dataset`).

- **No preview at all → rejected.** RecallGuard never approves a repair it
  hasn't seen; policy defaults to `require_preview_for_approval = True`.
- **Any preview record still violates the contract → rejected**, with the
  first violation's field, index, and reason recorded as evidence.
- **Every preview record passes → approved**, and Bright Data deploys the
  repair.

Crucially: **passing preflight is not recovery.** It is evidence used to
decide whether the repair is worth deploying at all. The only thing that
can prove recovery is §4.3.

### 4.3 The only path to RECOVERED

After approval, RecallGuard runs a real, full production collection
through `run_fix_my_itch_collection` — the identical orchestrator, trigger
contract, and atomic-ingestion path every ordinary collection uses. There
is no separate "verification collection" code path. That fresh
`CollectorRun` is then judged by `verify_recovery`, which re-runs every
reliability check against it from scratch.

- Fails → incident returns to `DEGRADED`; the failed run id and its failed
  checks are preserved, and the next invocation can try again within the
  attempt budget.
- Passes → the incident moves to `RECOVERED`, with a `RecoveryProof`
  containing the detection run id, the verification run id, the repair
  attempt number, and every check's result — structured, auditable proof
  a human (or the frontend) can inspect.

### 4.4 Deterministic repair prompts — no LLM writing them either

The instruction sent to Bright Data's self-healing AI
(`app/recallguard/prompts.py:build_heal_prompt`) is assembled purely from
the incident's own recorded evidence — which checks failed, what the
contract expects, what wrong values were actually observed. The same
incident always produces the same prompt. Three deliberate rules:

1. Tell the scraper to **extract what the page displays**, never to
   "rescale" — the historical bug was reading the wrong element, not a
   units mismatch.
2. **Keep the output schema.** A repair that fixes values but renames or
   drops fields breaks every downstream consumer.
3. Describe the failure that is **open right now**, not the one that first
   opened the incident — because a repair can fix the bug it was asked
   about and introduce a new one (this literally happened once: a fix for
   `tam_score` also shipped `categories.slice(0, 1)`, truncating coverage).

---

## 5. Outages get a different — but equally strict — recovery path

An `OUTAGE` (the provider couldn't run the collection at all) is **not**
sent to the healer — there's no scraper bug to repair. Instead,
`verify_retry_recovery` (`app/recallguard/service.py`) treats the *next
successful independent retry* as the only proof such an incident can ever
receive.

It anchors independence on the **detection timestamp** instead of a repair
candidate (an outage never has one), and it is exactly as strict:

- same collector, not the detection run itself, never used to verify this
  incident before,
- started strictly *after* the incident was detected,
- executed `SUCCEEDED`,
- and passes every reliability check — re-evaluated fresh, not assumed.

Anything that doesn't qualify returns `None` and the incident is left
completely untouched — this is the *normal*, expected answer on every
passing run while an outage is unresolved, not an error.

---

## 6. Where this plugs into the rest of GapRadar

```
app.pipeline.service.evaluate_and_heal   (the one orchestration seam)
  ├─ 1. run_fix_my_itch_collection        → produces a CollectorRun
  ├─ 2. evaluate_collector_run            → RecallGuard judges it
  ├─ 3. if passed & an incident was open  → offer this run to
  │                                          verify_retry_recovery
  │                                          (outage-only path)
  └─ 4. if failed & diagnosis repairable  → resume_or_execute_healing_attempt
                                             (the full loop in §4)
```

This single function is called from both the on-demand pipeline API and
`app/jobs/daily_refresh.py`, the production cron that refreshes GapRadar's
market dataset once per business day. **`DEGRADED` is a successful exit
code (`0`) for that cron on purpose** — RecallGuard catching bad data *is*
the system working, not a crash, and treating it as a failure would make a
real outage indistinguishable from a real, correctly-caught detection.

Only data from a currently-`HEALTHY` collector — meaning no active
incident of any kind — is ever marked `trusted` for downstream use
(`_trusted_run_id` in `pipeline/service.py`). A `DEGRADED`, `HEALING`, or
`MANUAL_REVIEW` collector's data is never surfaced as trustworthy, no
matter how good it looks.

### API surface

`GET /reliability` and `GET /reliability/incidents[/{id}]`
(`app/api/v1/routes/reliability.py`) are **read-only** — approving,
rejecting, or recovering a real production incident only ever happens
through the pipeline/healing lifecycle described above, never through a
direct API call. The one exception, `POST /reliability/demo/*`, drives a
deterministic fixture replay (`app/recallguard/demo.py`, scoped to an
isolated, permanently-disabled demo collector) through these exact same
service functions, purely so the self-healing story can be narrated
without needing a live, currently-broken Bright Data scraper on hand.

---

## 7. Worked example: one full incident, start to finish

1. **Healthy.** Collector has no active incident; `ReliabilityState.HEALTHY`.
2. **Drift detected.** A run comes back with 20 records against a
   100-record baseline. `check_completeness` fails →
   `FailureClassification.EXTRACTION_DRIFT`, `RecommendedAction.REQUEST_HEAL`.
   A `ReliabilityIncident` opens: `DEGRADED`, attempt count `0`.
3. **Healing attempt 1.** `start_healing` → `HEALING`, `repair_attempts = 1`.
   Bright Data is triggered with a deterministic prompt built from the
   observed evidence.
4. **Bad repair rejected.** The candidate's preview still returns a
   truncated dataset. Preflight rejects it → back to `DEGRADED`. Attempt 1
   is spent, 2 remain.
5. **Healing attempt 2.** `start_healing` again → `repair_attempts = 2`.
   This candidate's preview satisfies every check → approved and deployed.
6. **Verification.** A brand-new production collection runs. It returns
   100 records, passes every check. `verify_recovery` → `RECOVERED`, with
   a `RecoveryProof` recording detection run, verification run, attempt
   number `2`, and every passing check.
7. **Back to healthy.** `collector_reliability_state` now reports
   `HEALTHY` again — not because the incident vanished, but because it's
   closed and there's nothing else active.

This exact sequence (minus real provider calls) is what
`app/recallguard/demo.py` replays deterministically for demonstration —
using the *same* `service.py` lifecycle functions a real incident would
use, not a parallel simulation.
