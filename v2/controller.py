"""The deterministic control layer.

Everything here is plain Python -- no model decides control flow. The
controller owns: DAG readiness, retry counting, the budget ceiling, all status
transitions, and every point where the user is consulted. The agents are only
asked for *judgements* (plan, accept/reject, audit, synthesize).

Three phases:
  0. Plan      -- reviewer (+ user) produce and ratify the DAG (the contract)
  1. Execute   -- dispatch ready tasks; judge; optional audit; act on verdict
  2. Synthesize-- coherence pass + patch tasks; summary; mandatory summary audit
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from agents import Auditor, ModelProtocolError, Reviewer, UserChannel, Worker
from models import (
    AuditorView, Brief, ImpasseAction, Summary, Task, TaskStatus,
    Verdict, VerdictKind, WorkerOutput, Confidence,
)


# --------------------------------------------------------------------------- #
# Budget
# --------------------------------------------------------------------------- #

class BudgetHalt(Exception):
    """Raised when the hard ceiling is crossed. Caught at the top of run()."""


@dataclass
class BudgetController:
    """Tracks spend and enforces a hard ceiling.

    Two usage patterns:
      - mock transports call `charge(role)`  -> record fixed role_cost, raise if over
        (pre-charge: the ceiling is checked the instant the cost lands).
      - real transports call `guard()` before the call and `record(role, cost=...)`
        after, passing the actual dollar cost from usage. The ceiling is enforced
        by `guard()` on the *next* call, so a real reply is never discarded.
    With real cost, `ceiling` and `role_cost` are dollars; with mocks they're
    abstract units.
    """
    ceiling: float
    report_every: int = 5
    role_cost: dict[str, float] = field(default_factory=lambda: {
        "reviewer": 5.0,   # strong
        "auditor": 3.0,    # medium
        "worker": 1.0,     # cheap
    })
    spent: float = 0.0
    calls: int = 0
    _tracer: Optional["Tracer"] = None

    def guard(self) -> None:
        """Raise if already at/over the ceiling. Call BEFORE doing paid work."""
        if self.spent >= self.ceiling:
            if self._tracer:
                self._tracer.event("BUDGET", f"ceiling {self.ceiling:.2f} reached "
                                             f"at {self.spent:.2f} -- HALT")
            raise BudgetHalt()

    def record(self, role: str, label: str = "", cost: Optional[float] = None) -> None:
        """Add a call's cost (actual dollars if given, else fixed role_cost)."""
        amount = self.role_cost.get(role, 1.0) if cost is None else cost
        self.spent += amount
        self.calls += 1
        if self._tracer:
            self._tracer.event("charge", f"{role} {('('+label+')') if label else ''}"
                                         f"  +{amount:.3f}  total={self.spent:.3f}")
        if self.calls % self.report_every == 0:
            self.report()

    def charge(self, role: str, label: str = "", cost: Optional[float] = None) -> None:
        """Pre-charge then raise if over (used by the mock transports)."""
        self.record(role, label, cost)
        if self.spent >= self.ceiling:
            if self._tracer:
                self._tracer.event("BUDGET", f"ceiling {self.ceiling:.2f} crossed "
                                             f"at {self.spent:.2f} -- HALT")
            raise BudgetHalt()

    def report(self) -> None:
        if self._tracer:
            self._tracer.event("budget", f"{self.calls} calls, "
                                         f"{self.spent:.2f}/{self.ceiling:.2f} spent")


# --------------------------------------------------------------------------- #
# Trace / logging
# --------------------------------------------------------------------------- #

class Tracer:
    """Dead-simple indented event log so the process is legible without real
    agents. Swap for real logging later."""
    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.depth = 0

    def phase(self, name: str) -> None:
        if self.enabled:
            print(f"\n{'='*70}\n  PHASE: {name}\n{'='*70}")

    def task(self, msg: str) -> None:
        if self.enabled:
            print(f"\n-- {msg}")

    def event(self, kind: str, msg: str = "") -> None:
        if self.enabled:
            print(f"   [{kind}] {msg}")


# --------------------------------------------------------------------------- #
# Controller
# --------------------------------------------------------------------------- #

@dataclass
class ControllerConfig:
    max_retries: int = 3          # REJECT bounces before impasse escalation
    max_blocks: int = 2           # BLOCKED rounds before treating as impasse
    max_coherence_rounds: int = 2 # synthesis patch-task loops
    max_plan_rounds: int = 8      # clarify <-> reviewer rounds before forcing a DAG
    keep_rejection_outputs: int = 2  # verbatim attempts kept in the worker's ledger;
    #                                  older rejections keep their defects only.
    #                                  Tune 1-3 with real models: higher = more
    #                                  context but more risk of anchoring on bad attempts.


@dataclass
class RunResult:
    tasks: list[Task]
    summary: Optional[Summary]
    halted: bool
    spent: float


class Controller:
    def __init__(self, *, reviewer: Reviewer, worker: Worker, auditor: Auditor,
                 user: UserChannel, budget: BudgetController,
                 config: ControllerConfig = ControllerConfig(),
                 tracer: Optional[Tracer] = None):
        self.reviewer = reviewer
        self.worker = worker
        self.auditor = auditor
        self.user = user
        self.budget = budget
        self.cfg = config
        self.tracer = tracer or Tracer()
        self.budget._tracer = self.tracer
        self.tasks: dict[str, Task] = {}
        self.order: list[str] = []          # stable processing order
        self.summary: Optional[Summary] = None

    # ----- public entry point ------------------------------------------- #

    def run(self, request: str) -> RunResult:
        halted = False
        try:
            if not self._phase_plan(request):
                return self._result(halted=False)
            self._phase_execute()
            self._phase_synthesize()
        except BudgetHalt:
            halted = True
        return self._result(halted)

    def _result(self, halted: bool) -> RunResult:
        return RunResult(list(self.tasks.values()), self.summary,
                         halted, self.budget.spent)

    # ----- phase 0: plan ------------------------------------------------- #

    def _phase_plan(self, request: str) -> bool:
        self.tracer.phase("PLAN")
        transcript: list[tuple[str, str]] = []   # (speaker, text), full running exchange
        rounds = 0
        while True:
            rounds += 1
            if rounds > self.cfg.max_plan_rounds:
                self.tracer.event("plan", "max plan rounds reached -- aborting")
                return False
            try:
                turn = self.reviewer.plan(request, transcript)
            except ModelProtocolError as e:
                self.tracer.event("PROTOCOL", f"planning aborted: {e}")
                return False

            if not turn.done:
                self.tracer.event("plan", f"reviewer: {turn.message}")
                reply = self.user.respond_to_planner(turn.message)
                transcript.append(("reviewer", turn.message))
                transcript.append(("user", reply))
                continue

            # DONE: the structured DAG is the artifact. Validate, then ratify IT.
            self.tracer.event("plan", f"reviewer proposes {len(turn.tasks)} task(s): "
                                      f"{[t.id for t in turn.tasks]}")
            try:
                self._validate_tasks(turn.tasks)
            except ValueError as e:
                self.tracer.event("plan", f"proposed DAG invalid: {e} -- feeding back")
                transcript.append(("reviewer", turn.message))
                transcript.append(("user", f"(that plan is structurally invalid: {e}; fix it)"))
                continue

            reason = self.user.accept_plan(turn.tasks)
            if not reason:
                self._install_dag(turn.tasks)
                self.tracer.event("plan", "user accepted -- DAG is the contract")
                return True
            self.tracer.event("plan", f"user rejected: {reason}")
            transcript.append(("reviewer", turn.message + f" [proposed {len(turn.tasks)} tasks]"))
            transcript.append(("user", f"(rejected the proposed plan) {reason}"))

    def _install_dag(self, tasks: list[Task]) -> None:
        self._validate_tasks(tasks)
        self.tasks = {t.id: t for t in tasks}
        self.order = [t.id for t in tasks]

    def _validate_tasks(self, tasks: list[Task]) -> None:
        """Validate a proposed task list (missing deps, cycles) WITHOUT installing.
        Raises ValueError so planning can feed the problem back to the reviewer."""
        index = {t.id: t for t in tasks}
        if len(index) != len(tasks):
            raise ValueError("duplicate task ids")
        for t in tasks:
            for d in t.deps:
                if d not in index:
                    raise ValueError(f"task {t.id} depends on unknown task {d}")
        WHITE, GREY, BLACK = 0, 1, 2
        color = {tid: WHITE for tid in index}

        def visit(tid: str) -> None:
            color[tid] = GREY
            for d in index[tid].deps:
                if color[d] == GREY:
                    raise ValueError(f"dependency cycle involving {tid} -> {d}")
                if color[d] == WHITE:
                    visit(d)
            color[tid] = BLACK

        for tid in index:
            if color[tid] == WHITE:
                visit(tid)

    # ----- phase 1: execute --------------------------------------------- #

    def _phase_execute(self) -> None:
        self.tracer.phase("EXECUTE")
        while True:
            self._cascade_cancellations()
            tid = self._next_ready_task()
            if tid is None:
                break
            self._run_task(self.tasks[tid])

    def _cascade_cancellations(self) -> None:
        """A PENDING task whose any dependency FAILED/CANCELLED can never run."""
        changed = True
        while changed:
            changed = False
            for t in self.tasks.values():
                if t.status is not TaskStatus.PENDING:
                    continue
                if any(self.tasks[d].status in (TaskStatus.FAILED, TaskStatus.CANCELLED)
                       for d in t.deps):
                    t.status = TaskStatus.CANCELLED
                    t.note = "cancelled: a dependency did not complete"
                    self.tracer.event("cancel", f"{t.id} (dependency failed)")
                    changed = True

    def _next_ready_task(self) -> Optional[str]:
        for tid in self.order:                      # stable order == priority order
            t = self.tasks[tid]
            if t.status is TaskStatus.PENDING and all(
                self.tasks[d].status is TaskStatus.DONE for d in t.deps
            ):
                return tid
        return None

    def _build_brief(self, task: Task) -> Brief:
        dep_res = {d: self.tasks[d].state.final_resolution or "" for d in task.deps}
        return Brief(
            task_id=task.id,
            desc=task.desc,
            acceptance_criterion=task.acceptance_criterion,
            dependency_resolutions=dep_res,
            context_updates=list(task.state.context_updates),
            rejection_history=task.state.rejection_brief(self.cfg.keep_rejection_outputs),
        )

    def _run_task(self, task: Task) -> None:
        task.status = TaskStatus.IN_PROGRESS
        self.tracer.task(f"TASK {task.id}: {task.desc}")
        self.tracer.event("criterion", task.acceptance_criterion)

        while True:
            brief = self._build_brief(task)        # always reflects the durable ledger
            try:
                output = self.worker.execute(brief)
                self.tracer.event("worker", self._preview(output.output)
                                  + (f"  | uncertain: {output.uncertainties}"
                                     if output.uncertainties else ""))

                verdict = self.reviewer.judge(brief, output)
                self.tracer.event("verdict", self._fmt_verdict(verdict))

                if verdict.kind is VerdictKind.ACCEPT:
                    verdict = self._maybe_audit(task, brief, output, verdict)
            except ModelProtocolError as e:
                self.tracer.event("PROTOCOL", str(e))
                if not self._handle_impasse(task, [f"unparseable model output: {e}"],
                                            WorkerOutput("(no valid output)")):
                    return
                continue

            if verdict.kind is VerdictKind.BLOCKED:
                if not self._handle_blocked(task, verdict):
                    return                       # escalated to impasse -> task done/failed
                continue                         # ledger persists across the block

            if verdict.kind is VerdictKind.ACCEPT:
                self._accept(task, output)
                return

            if verdict.kind is VerdictKind.REJECT:
                if verdict.extra_context:
                    n = task.state.add_context(verdict.extra_context)
                    self.tracer.event("context+", f"#{n}: {verdict.extra_context}")
                task.state.record_rejection(output.output, verdict.defects)
                task.retry_count += 1
                self.tracer.event("retry", f"{task.retry_count}/{self.cfg.max_retries}")
                if task.retry_count >= self.cfg.max_retries:
                    if not self._handle_impasse(task, verdict.defects, output):
                        return                   # FAILED / accepted-as-is -> terminal
                continue                         # guided retry -> ledger persists

    def _maybe_audit(self, task: Task, brief: Brief, output: WorkerOutput,
                     verdict: Verdict) -> Verdict:
        """Discretionary auditor: only when the reviewer's confidence is low.
        Auditor sees the compressed view (criterion + candidate), blind to the
        discussion. Reviewer keeps final say."""
        if verdict.confidence is not Confidence.LOW:
            return verdict
        view = task.auditor_view(output.output, output.uncertainties)
        result = self.auditor.audit_task(view)
        if result.clean:
            self.tracer.event("auditor", "clean")
            return verdict
        self.tracer.event("auditor", f"defects: {result.defects}")
        final = self.reviewer.reconsider(brief, output, result.defects)
        self.tracer.event("reconsider", self._fmt_verdict(final))
        return final

    def _handle_blocked(self, task: Task, verdict: Verdict) -> bool:
        """Get the user's answer, fold it into context, re-dispatch. BLOCKED does
        NOT count toward the retry cap, but is bounded by max_blocks. Returns
        True to continue working, False if it was escalated/terminal."""
        task.block_count += 1
        if task.block_count > self.cfg.max_blocks:
            self.tracer.event("blocked", "exceeded block budget -> treat as impasse")
            return self._handle_impasse(task, [f"repeatedly blocked: {verdict.question}"],
                                        WorkerOutput("(no resolution)"))
        ans = self.user.answer_blocked(task.id, verdict.question or "")
        n = task.state.add_context(f"[user] {ans}")
        self.tracer.event("blocked", f"asked user; answer folded as context #{n}")
        return True

    def _handle_impasse(self, task: Task, defects: list[str],
                        last: WorkerOutput) -> bool:
        """Retry cap (or block cap) hit. Report and ask the user how to proceed.
        Returns True only if the user wants one more guided attempt."""
        self.tracer.event("IMPASSE", f"{task.id} after {task.retry_count} retries; "
                                     f"defects: {defects}")
        decision = self.user.resolve_impasse(task.id, defects)
        if decision.action is ImpasseAction.ABANDON:
            task.status = TaskStatus.FAILED
            task.note = "abandoned by user at impasse"
            self.tracer.event("impasse", "user abandoned -> FAILED")
            return False
        if decision.action is ImpasseAction.ACCEPT_AS_IS:
            task.state.final_resolution = last.output
            task.status = TaskStatus.DONE
            task.note = "accepted as-is at user's request (known defects)"
            self.tracer.event("impasse", "user accepted as-is -> DONE")
            return False
        # RETRY_WITH_GUIDANCE
        n = task.state.add_context(f"[user guidance] {decision.guidance}")
        task.retry_count = 0
        self.tracer.event("impasse", f"user guidance folded as context #{n}; retrying")
        return True

    def _accept(self, task: Task, output: WorkerOutput) -> None:
        task.state.final_resolution = output.output
        task.status = TaskStatus.DONE
        self.tracer.event("ACCEPT", f"{task.id} DONE")

    # ----- phase 2: synthesize ------------------------------------------ #

    def _phase_synthesize(self) -> None:
        self.tracer.phase("SYNTHESIZE")
        done = [t for t in self.tasks.values() if t.status is TaskStatus.DONE]
        if not done:
            self.tracer.event("synthesis", "no completed tasks -- nothing to synthesize")
            return
        try:
            self._synthesize_body()
        except ModelProtocolError as e:
            self.tracer.event("PROTOCOL", f"synthesis aborted: {e}")

    def _synthesize_body(self) -> None:
        for round_no in range(self.cfg.max_coherence_rounds):
            resolutions = self._done_resolutions()
            patches = self.reviewer.coherence_pass(resolutions)
            if not patches:
                self.tracer.event("coherence", f"round {round_no+1}: coherent")
                break
            self.tracer.event("coherence",
                              f"round {round_no+1}: {len(patches)} patch task(s) "
                              "(internal, no consult)")
            for p in patches:
                self.tasks[p.id] = p
                self.order.append(p.id)
            self._phase_execute_patches(patches)

        # summary + MANDATORY summary audit (against task states only)
        resolutions = self._done_resolutions()
        summary = self.reviewer.synthesize(resolutions)
        self.tracer.event("summary", summary.text)

        views = [self.tasks[tid].auditor_view(res)
                 for tid, res in resolutions.items()]
        audit = self.auditor.audit_summary(summary, views)
        if audit.clean:
            self.tracer.event("auditor", "summary clean")
        else:
            self.tracer.event("auditor", f"summary defects: {audit.defects}")
            summary = self.reviewer.revise_summary(summary, audit.defects, resolutions)
            self.tracer.event("summary", f"revised: {summary.text}")
        self.summary = summary

    def _phase_execute_patches(self, patches: list[Task]) -> None:
        for p in patches:
            self._cascade_cancellations()
            if p.status is TaskStatus.PENDING and all(
                self.tasks[d].status is TaskStatus.DONE for d in p.deps
            ):
                self._run_task(p)

    def _done_resolutions(self) -> dict[str, str]:
        return {t.id: t.state.final_resolution or ""
                for t in self.tasks.values() if t.status is TaskStatus.DONE}

    # ----- formatting --------------------------------------------------- #

    @staticmethod
    def _preview(s: str, n: int = 80) -> str:
        """One-line preview for the process log (the judge view shows it in full)."""
        first = (s or "").strip().splitlines()[0] if (s or "").strip() else ""
        extra = (s or "").strip().count("\n")
        clipped = first[:n] + ("…" if len(first) > n else "")
        return clipped + (f"  (+{extra} more line(s))" if extra else "")

    @staticmethod
    def _fmt_verdict(v: Verdict) -> str:
        if v.kind is VerdictKind.ACCEPT:
            return f"ACCEPT (confidence={v.confidence.value})"
        if v.kind is VerdictKind.REJECT:
            extra = f" +context" if v.extra_context else ""
            return f"REJECT defects={v.defects}{extra}"
        return f"BLOCKED q={v.question!r}"
