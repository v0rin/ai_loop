# Agent-loop — Design Notes

Purpose of this file: capture the *why* behind the design so a cold start (a new
chat, or you months from now) doesn't lose the reasoning. The code and its
comments carry the *what*; this carries the decisions, the alternatives we
rejected, and the gaps we know about. If something here contradicts the code, the
code wins and this file is stale — but the rationale should still tell you what
the code was *trying* to do.

The source of truth for state is the files, not any chat history. On a cold
start, re-upload the current files.

---

## 1. The one idea everything else serves

**Every check in this system is only as good as (a) the task's acceptance
criterion and (b) the judgment of the role reading it. No structural mechanism
manufactures rigor the prompts/models don't supply.** A vague criterion gets
rubber-stamped by the reviewer *and* the auditor — two checks, zero protection.
So the highest-leverage artifact in the whole pipeline is the acceptance
criterion, and the highest-leverage prompt is the planning prompt that forces
criteria to be falsifiable PASS/FAIL tests. When output quality is mushy, suspect
the criteria first.

---

## 2. Core architecture

Three roles + a deterministic controller. (We started with four roles and an
explicit auditor-as-second-opinion; collapsed to three.)

- **Reviewer** (strong model): plans the DAG *with the user*, judges each worker
  output, runs coherence + synthesis at the end. Planner and reviewer are the
  same model in different phases — merging them removed a handoff and a
  self-approval step.
- **Worker** (cheap model): executes one task with only what it's given.
- **Auditor** (medium model): an *independent* check — discretionary per task,
  mandatory on the final summary.
- **Controller** (plain Python, `controller.py`): owns ALL control flow —
  readiness, retry counting, budget, status transitions, every user consult.
  Models only ever return *judgments*. Reason: models can't reliably count to 3,
  stop at a dollar ceiling, or transition a state machine. Keep those in code.

**Rejected:** a 4th "auditor" role that was just a second LLM opinion on the same
inputs the reviewer already saw. That's redundant — a second opinion is not
verification. The auditor only earns its slot by being *independent* (see §5).

---

## 3. The central seam (the most important structural decision)

Every model role is:

```
build_prompt(context) -> str   →   transport.call(prompt) -> raw_text   →   parse(raw) -> typed
```

**Only `transport.call` is mocked.** Prompt construction, the schema, parsing,
validation, and re-prompt-on-failure are REAL code that runs identically whether
the string came from a human typing or from OpenRouter.

Why this matters: the riskiest integration point is "does the model's text parse
into the object the controller branches on." Earlier mocks hand-fabricated typed
`Verdict`/`WorkerOutput` objects, which bypassed exactly that seam — so "testing"
tested our own Python, not the thing that breaks with real models. That was
theater. The current design makes the parse path real even in tests
(`ScriptedTransport` returns canned *wire strings*, not typed objects).

Transports (`model_io.py`, `llm_transport.py`):
- `HumanModel` — you type the reply (guided assembly, or raw mode to test bad input).
- `ScriptedTransport` — canned wire strings, deterministic tests.
- `OpenRouterTransport` — real models. Swapping to live = this one class.

`transport.call` is **pure transport**: role + prompt in, raw string out. It must
not know about parsing or role logic, or the clean swap is lost. (The mock
`HumanModel` is allowed to be fancier — guided entry — because that cleverness
dies with the mock.)

---

## 4. The wire protocol

Format: `[$KEY]content[$/KEY]` (`wire.py`) — asymmetric open/close, XML/HTML style.

- **Chosen over JSON.** Objects are flat (only the task list nests), the delimiter
  is unlikely to appear in natural content, and a model can emit it as easily as
  JSON without the reasoning penalty strict-JSON imposes. It is the real wire
  format, not a throwaway for testing.
- **Open/close are asymmetric** (`[$KEY]`…`[$/KEY]`, note the slash). Started as
  symmetric `[$KEY]...[$KEY]`; switched after a real model produced a stray
  opener inside a nested task block and the symmetric parser could only say "not
  closed" without localizing which of many identical tags broke. Asymmetric tags
  are (a) what models produce reliably (heavy XML/HTML training), (b) locally
  diagnosable — the error names the key and which side is missing, and (c) more
  robust: a stray *opener* after a complete block is simply ignored rather than
  fatal. `tasks()` also checks opener-count vs parsed-count to flag a missing
  `[$/TASK]`.
- **All declared blocks must be present; `none` is the explicit empty value.**
  (Your deliberate override of my "absent-when-empty" proposal.) Forces the model
  to make a conscious decision (e.g. "no defects" → `none`) and makes an omitted
  block an unambiguous error rather than a maybe-they-meant-empty.
- **Strict parse + exactly ONE re-prompt + then impasse.** Strict parsing is the
  honesty mechanism. A lenient parser that "tries hard to extract a verdict from
  whatever the model said" hides the exact failures we built strictness to
  expose. The re-prompt is the *only* tolerance mechanism, and it is **corrective**:
  it echoes the model's prior reply verbatim and names the exact parse error, so
  the model edits a near-miss rather than regenerating blind (and likely slipping
  somewhere new). Second failure → `ModelProtocolError` → controller treats it as
  an impasse.
- The task list uses nested `[$TASK]...[$/TASK]` blocks, each holding
  `[$ID][$DESC][$DEPS][$CRITERION]`. The format spec shows a concrete filled-in
  task template (the cheapest fix for nesting mistakes).
- The **block list in each prompt is generated from the schema** (`model_io.format_spec`),
  NOT hand-written in `prompts.py`. Reason: if it were prose it could drift from
  what the parser accepts, silently. Only the header sentence is prose
  (`prompts.FORMAT_SPEC_HEADER`).

---

## 5. Roles and their contracts

### Reviewer — planning (`plan`)
Conversational. Emits `STATUS=CONTINUE|DONE`, `MESSAGE`, `TASKS`. (We killed an
earlier `MODE=ASK|PLAN` toggle — conversations don't want to be state machines.)

**Load-bearing ordering: the artifact is pinned BEFORE acceptance.** The reviewer
proposes the structured DAG (DONE), and the user ratifies *that parsed artifact* —
never the prose. Reason: a model can drift between the prose a user approved and
the serialized DAG it emits afterward; if you accept the prose and serialize
after, the contract locks against something that isn't the contract.

- Invalid proposed DAG (cycle / unknown dep / duplicate id) → fed back to the
  reviewer **without** bothering the user (it's a model error, not a plan to
  ratify). `_validate_tasks` checks without mutating state.
- User rejection → reason fed back into the conversation, loop continues.

### Worker — execute
Sees ONLY: task desc, criterion, dependency resolutions, context updates, and the
rejection ledger. Must not invent missing context — record gaps in `ASSUMPTIONS`
instead. Surfacing a gap honestly is rewarded; hiding it is the failure.

### Reviewer — judge
ACCEPT / REJECT / BLOCKED, against the criterion ONLY.
- **CONFIDENCE (HIGH/LOW) is the auditor trigger.** LOW → auditor runs. The prompt
  explicitly says don't default to HIGH to dodge the check. This instruction is
  fragile under real models — **watch whether models actually set LOW when they
  can't verify**; if they reflexively pick HIGH, the entire auditor layer goes
  dormant.
- REJECT carries enumerated, fixable defects (not "improve quality") + optional
  `CONTEXT` (persisted). Vague rejection makes the worker re-roll instead of fix.
- **BLOCKED ≠ REJECT.** Added because accept/reject can't express "only the user
  can resolve this." It does NOT burn a retry (separate `block_count`) — missing
  info is not a quality failure.

### Auditor — independent check
- **Blind to the *discussion* by construction** (sees criterion + resolution +
  the result's stated assumptions — but NOT the reject history, context updates,
  or the reviewer's reasoning). The per-task auditor view is built from the
  *candidate* output (it runs pre-accept); the summary auditor is built from
  state. Assumptions are included because they're intrinsic to the result (a
  caveat the output ships with), not discussion. This blindness is the whole
  point: it's not re-running
  the reviewer's judgment on the same inputs, it's checking resolution-satisfies-
  criterion *unanchored* by the back-and-forth that might have rationalized a weak
  result over rounds.
- **Discretionary per task** (only on reviewer LOW confidence), **mandatory on the
  summary.** A check that fires every task and rarely overturns is just a tax;
  gate it by stakes. The summary audit is the auditor's strongest use (one
  summary, bounded cost, maximal stakes).
- **Reviewer keeps final say** (`reconsider`). Auditor is advisory.
- Its value still rides entirely on criterion quality — a weak criterion gets
  rubber-stamped twice. (See §1.)

### Reviewer — coherence + synthesize + revise
Per-task acceptance ≠ global coherence. Coherence pass spawns internal patch
tasks (no user consult — they serve the agreed scope). Summary is checked by the
mandatory auditor against task states.

---

## 6. Key mechanisms

### Rejection ledger (`TaskState.rejections`, durable)
- **ALL defects kept forever; only verbatim OUTPUTS are capped** (`keep_rejection_outputs`,
  default 2, in `ControllerConfig`). Defects are the signal; old outputs are
  anchoring-risk for the worker and token cost. The fix to "give the worker all
  attempts" was really "give it the full *defect* history."
- **Replayed on EVERY dispatch**, including after a BLOCK or guided retry. The
  original bug was that `prior/defects` were loop-local and wiped on those
  transitions — a worker resumed a task with amnesia. Same class as any
  "transient state dropped at a boundary" bug; watch for it elsewhere.
- **Judge and worker share the same brief/ledger.** The judge needs the full
  defect history for regression detection (a fix that reintroduces an old defect).
  It deliberately sees the worker's output window too — "the judge sees what the
  worker saw" is worth more than narrowing it. Coupled to `keep_rejection_outputs`
  on purpose; if you widen the worker's window the judge's widens too.

### Task state (`TaskState`)
`{criterion, initial_context, context_updates[], rejections[], final_resolution}`.
Excludes the worker's intermediate outputs and the per-round discussion — those
are transient. `context_updates` persist (durable knowledge added by the
reviewer, e.g. a user's answer to a BLOCKED question, numbered).

### DAG, not a priority queue
Tasks have dependencies, not just priority. A worker sees only its task + its
dependencies' resolutions. The `final_resolution`s are the shared artifact store.

### Budget (`BudgetController`)
- **Charging lives in the transport** (one charge per actual call, so re-prompts
  count). `guard()` before a paid call, `record(cost=...)` after (real dollars
  from `usage.cost`); `charge()` is the mock's pre-charge-and-raise.
- Real runs: ceiling + costs are DOLLARS. Mock runs: abstract units (5/3/1).
- Hard ceiling HALTS (raises `BudgetHalt`, caught at `run()` top → partial state),
  not just reports.

### Config knobs (all in `ControllerConfig`)
`max_retries=3`, `max_blocks=2`, `max_coherence_rounds=2`, `max_plan_rounds=8`,
`keep_rejection_outputs=2`. One source of truth; no hidden defaults in method
signatures. `keep_rejection_outputs` is the anchoring-vs-context knob to tune
against real models (1–3).

---

## 7. Deferred: rescoping (designed, NOT built)

We worked out the model but deferred building it until real use clarifies what's
wanted. Captured so it isn't re-derived from scratch:

- **Patches ≠ rescopes.** A patch *adds* a node (original stays DONE, contract
  grows, in-scope). A rescope *mutates* a node's criterion/desc (can un-complete a
  DONE task, invalidate dependents' inputs, change topology — destructive,
  changes the contract). They move opposite ways through the DAG.
- **BLOCKED ≠ rescope.** BLOCKED adds context; the criterion never moves. Don't
  overload BLOCKED to carry rescopes.
- **Tiered fix:** a trivial in-place rescope (leaf, not yet done, deps unchanged)
  could be a lightweight *ratified* amendment; a structural rescope goes back
  through the planning handshake (the only thing that knows how to validate a DAG
  and get user sign-off). Match the mechanism to the blast radius.
- **Per-task rescope cap** (e.g. 1, then consult): repeated rescopes mean the spec
  was wrong, which is the user's to re-ratify. The real runaway guard is the
  budget, not the count.
- **Open question, unresolved:** when a rescope lands on a task with DONE
  dependents, auto-invalidate the subtree or require user approval of the cascade?
  This decides whether "1 rescope" is cheap or secretly expensive.
- **Current code:** `UserChannel.approve_scope_change` exists but is **NOT wired
  into the execution loop** — there is no `RESCOPE` verdict, so the reviewer has
  no way to raise a mid-run rescope. Only the internal patch-task path
  (coherence) is reachable. Scope-change consult is designed, not built.

---

## 8. Known gaps / open issues (honest list)

1. **Auditor + worker assumptions — RESOLVED.** The per-task auditor now receives
   the candidate output's assumptions in its `AuditorView` and the prompt tells it
   how to weigh them (an assumption the criterion forbids → defect; one it's silent
   on → fine). Done at the *candidate* (pre-accept) level, since that's where the
   per-task auditor runs — no state persistence was added (nothing reads it yet;
   the summary auditor deliberately still gets no per-task assumptions, as those
   are noise there). If a future consumer (summary auditor, coherence) needs them,
   that's when persisting `accepted_assumptions` to `TaskState` earns its place.
2. **Rescoping not wired** (see §7).
3. **`ScriptedUser.accept` is a static bool** — scripted tests can't replay
   reject-then-accept during planning (that branch was verified via `play.py`).
4. **No parallelism.** DAG executes sequentially. Concurrency brings partial-
   failure and budget-race semantics; deferred until the flow is proven.
5. **`usage.cost` may not be populated** by the SDK. `OpenRouterTransport` falls
   back to `max_cost_per_call`, so spend could be a ceiling estimate, not actual.
   Verify against a real response.
6. **Prompts are untuned first-version hypotheses.** Likely need worked examples
   (few-shot) for judge/auditor if zero-shot output wobbles. They're somewhat
   long; the judge fires most often, so it's the place to trim once you know which
   instructions models actually need.
7. **Parsers do light semantic checks** (REJECT must have defects, BLOCKED must
   have a question) — slightly beyond pure format validation, a deliberate
   line-crossing. Revisit if you want parsers format-only.
8. **Single user message to the model** (no system/user split). First lever to
   pull if real models format sloppily: move the format spec / role framing into a
   system message.
9. **Web search is ON for ALL roles (interim).** `OpenRouterTransport` attaches
   OpenRouter's `web` plugin to every call (`web_search=True`) as a plain dict
   `{"id":"web","max_results":N}` — the SDK's `plugins` param accepts dicts
   (`ChatRequestPluginTypedDict`), confirmed from the `chat.send` signature, so no
   guessed plugin class. `step_count` is raised to `max_tool_steps` (4); note
   `stop_server_tools_when` governs the *server-tool agent loop*, and the `web`
   plugin is likely auto-search-and-inject (not that loop), so the raise may be
   unnecessary for web — harmless either way. `max_cost_per_call` stays the hard
   guard. Use `search_test.py` to confirm search actually engages (it dumps the
   message annotations/citations). **Known tension:** the cleaner design is
   *per-task* search capability (planner marks which tasks need it) — global
   search lets the *auditor* re-research instead of checking the fixed criterion,
   eroding its independence. Prompts unchanged; models search only if they choose.

---

## 9. Values baked into the design

- Strict parse + bounded retry, never silent coercion.
- Surface uncertainty, don't hide it (worker assumptions, honest confidence).
- The controller decides; models judge. Keep counting/budget/state in code.
- Independence is what makes a second check worth its cost (the auditor's
  blindness), not a second opinion on the same inputs.
- Pin the artifact before ratifying it.

---

## 10. File map

| File | Role |
|---|---|
| `models.py` | Dumb data types (Task, TaskState, Brief, Verdict, PlanTurn, …) |
| `wire.py` | Build + strict-parse the `[$KEY]` format |
| `model_io.py` | Schema (FieldSpec/Kind), console helpers, Transport base, HumanModel, ScriptedTransport |
| `agents.py` | Roles as build_prompt + parse over a transport; user channels |
| `prompts.py` | All prompt prose (the substantive content to tune) |
| `controller.py` | The deterministic engine: budget, tracer, the 3-phase loop |
| `llm_transport.py` | `OpenRouterTransport` (real models; supervised stepping) |
| `play.py` | Interactive run — you are the models + the user (HumanModel) |
| `play_llm.py` | Real-LLM run — you are the user; models are real (supervised) |
| `demo.py` | Deterministic scenarios over ScriptedTransport |

Flow: PLAN (converse → ratify DAG) → EXECUTE (dispatch ready tasks; judge;
optional audit; act) → SYNTHESIZE (coherence + patches; summary; mandatory audit).
