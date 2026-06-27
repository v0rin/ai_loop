"""The agent layer: roles as build_prompt + strict parse over a shared transport.

Each model role method now does the real harness work:
    build_prompt(context) -> str        # context rendered + format spec appended
    transport.call(...)   -> raw_text   # the ONLY mocked step
    parse(raw_text)       -> typed obj  # strict; ParseError on any deviation

On ParseError the base issues ONE format-emphasizing re-prompt, re-parses, and
if it still fails raises ModelProtocolError (the controller treats that as an
impasse). Guided mode in HumanModel always assembles valid wire text, so to
exercise the re-prompt path you reply in RAW mode with something malformed.

The user channel is NOT a model -- it stays a plain human interface (or scripted
for tests); it is never swapped for an LLM.
"""
from __future__ import annotations

from typing import Protocol

import wire
import prompts
from model_io import (
    FieldSpec, Kind, Transport, format_spec, c_line, c_lines, c_multiline, c_yesno,
)
from models import (
    AuditorView, AuditResult, Brief, Confidence, ImpasseAction, ImpasseDecision,
    PlanTurn, RejectionEntry, Summary, Task, Verdict, VerdictKind, WorkerOutput,
)


class ModelProtocolError(Exception):
    """A role's reply could not be parsed even after one re-prompt."""


# --------------------------------------------------------------------------- #
# Protocols (unchanged signatures -- the controller is untouched by this layer)
# --------------------------------------------------------------------------- #

class Reviewer(Protocol):
    def plan(self, request: str, transcript) -> PlanTurn: ...
    def judge(self, brief: Brief, output: WorkerOutput) -> Verdict: ...
    def reconsider(self, brief: Brief, output: WorkerOutput, auditor_defects) -> Verdict: ...
    def coherence_pass(self, resolutions) -> list[Task]: ...
    def synthesize(self, resolutions) -> Summary: ...
    def revise_summary(self, summary, auditor_defects, resolutions) -> Summary: ...


class Worker(Protocol):
    def execute(self, brief: Brief) -> WorkerOutput: ...


class Auditor(Protocol):
    def audit_task(self, view: AuditorView) -> AuditResult: ...
    def audit_summary(self, summary: Summary, views) -> AuditResult: ...


class UserChannel(Protocol):
    def respond_to_planner(self, message: str) -> str: ...
    def accept_plan(self, tasks) -> str: ...      # "" == accepted; else rejection reason
    def answer_blocked(self, task_id, question) -> str: ...
    def resolve_impasse(self, task_id, defects) -> ImpasseDecision: ...
    def approve_scope_change(self, description) -> bool: ...


# --------------------------------------------------------------------------- #
# Prompt-rendering helpers (kept deliberately brief)
# --------------------------------------------------------------------------- #

def _render_ledger(history: list[RejectionEntry]) -> str:
    if not history:
        return "  (none -- first attempt)"
    rows = []
    for i, e in enumerate(history, 1):
        out = e.attempt if e.attempt is not None else "(older output dropped)"
        rows.append(f"  #{i} rejected for {e.defects}; was: {out}")
    return "\n".join(rows)


def _render_brief(brief: Brief) -> str:
    return (
        f"TASK {brief.task_id}: {brief.desc}\n"
        f"ACCEPTANCE CRITERION: {brief.acceptance_criterion}\n"
        f"DEPENDENCY OUTPUTS: {brief.dependency_resolutions or '(none)'}\n"
        f"CONTEXT UPDATES: {brief.context_updates or '(none)'}\n"
        f"REJECTION LEDGER (all defects; recent outputs):\n{_render_ledger(brief.rejection_history)}"
    )


def _prompt(body: str, schema: list[FieldSpec]) -> str:
    return prompts.PIPELINE_PREAMBLE + "\n\n" + body + "\n\n" + format_spec(schema)


_REPROMPT = (
    "\n\n--- YOUR PREVIOUS REPLY COULD NOT BE PARSED ---\n"
    "The exact problem: {err}\n\n"
    "Your previous reply, verbatim:\n{prev}\n\n"
    "Fix ONLY that problem and resend the corrected reply. Every block must be "
    "wrapped in a matching opening [$KEY] and closing [$/KEY] tag, each required "
    "block present exactly once. Output only the corrected reply, nothing else."
)


# --------------------------------------------------------------------------- #
# Base: invoke = call -> parse -> (one re-prompt) -> parse -> raise
# --------------------------------------------------------------------------- #

class _ModelAgent:
    ROLE = "model"

    def __init__(self, transport: Transport):
        self.transport = transport

    def _invoke(self, prompt, schema, parse_fn, hint, what):
        raw = self.transport.call(self.ROLE, prompt, schema, hint)
        try:
            return parse_fn(raw)
        except wire.ParseError as e1:
            raw2 = self.transport.call(
                self.ROLE, prompt + _REPROMPT.format(err=e1, prev=raw), schema, hint + "*")
            try:
                return parse_fn(raw2)
            except wire.ParseError as e2:
                raise ModelProtocolError(f"{what}: {e2}")


# --------------------------------------------------------------------------- #
# Worker
# --------------------------------------------------------------------------- #

class ModelWorker(_ModelAgent):
    ROLE = "worker"
    SCHEMA = [
        FieldSpec("OUTPUT", Kind.BLOB, "your output (must satisfy the criterion)"),
        FieldSpec("ASSUMPTIONS", Kind.LIST, "assumptions / things you couldn't verify"),
    ]

    def execute(self, brief: Brief) -> WorkerOutput:
        body = prompts.WORKER_EXECUTE + "\n\n" + _render_brief(brief)
        prompt = _prompt(body, self.SCHEMA)

        def parse(raw):
            return WorkerOutput(wire.text(raw, "OUTPUT"), wire.items(raw, "ASSUMPTIONS"))

        return self._invoke(prompt, self.SCHEMA, parse, f"exec:{brief.task_id}", "worker.execute")


# --------------------------------------------------------------------------- #
# Reviewer
# --------------------------------------------------------------------------- #

class ModelReviewer(_ModelAgent):
    ROLE = "reviewer"

    PLAN_SCHEMA = [
        FieldSpec("STATUS", Kind.CHOICE, "CONTINUE to keep talking to the user, "
                  "DONE to propose the final plan", ("CONTINUE", "DONE")),
        FieldSpec("MESSAGE", Kind.TEXT, "your message to the user "
                  "(a question, or the plan summary when DONE)"),
        FieldSpec("TASKS", Kind.TASKS, "the final DAG when DONE (else none)"),
    ]
    JUDGE_SCHEMA = [
        FieldSpec("VERDICT", Kind.CHOICE, "verdict", ("ACCEPT", "REJECT", "BLOCKED")),
        FieldSpec("CONFIDENCE", Kind.CHOICE, "confidence (LOW -> auditor checks)", ("HIGH", "LOW")),
        FieldSpec("DEFECTS", Kind.LIST, "defects if REJECT (specific, fixable)"),
        FieldSpec("CONTEXT", Kind.TEXT, "extra context the worker lacked", allow_none=True),
        FieldSpec("QUESTION", Kind.TEXT, "the question if BLOCKED", allow_none=True),
    ]
    RECON_SCHEMA = [
        FieldSpec("VERDICT", Kind.CHOICE, "final verdict (you have final say)", ("ACCEPT", "REJECT")),
        FieldSpec("DEFECTS", Kind.LIST, "defects if REJECT"),
        FieldSpec("CONTEXT", Kind.TEXT, "extra context", allow_none=True),
    ]
    COHERE_SCHEMA = [FieldSpec("PATCHES", Kind.TASKS, "internal patch tasks (or none)")]
    SUMMARY_SCHEMA = [FieldSpec("SUMMARY", Kind.TEXT, "final summary + conclusions")]

    def plan(self, request: str, transcript) -> PlanTurn:
        convo = "\n".join(f"  {who.upper()}: {txt}" for who, txt in transcript) or "  (none yet)"
        body = (prompts.REVIEWER_PLAN
                + f"\n\nUSER REQUEST: {request}\nCONVERSATION:\n{convo}")
        prompt = _prompt(body, self.PLAN_SCHEMA)

        def parse(raw):
            status = wire.choice(raw, "STATUS", ("CONTINUE", "DONE"))
            message = wire.text(raw, "MESSAGE")
            if status == "DONE":
                ts = wire.tasks(raw, "TASKS")
                if not ts:
                    raise wire.ParseError("STATUS DONE but TASKS is none")
                return PlanTurn(True, message, ts)
            return PlanTurn(False, message, [])

        return self._invoke(prompt, self.PLAN_SCHEMA, parse, "plan", "reviewer.plan")

    def judge(self, brief: Brief, output: WorkerOutput) -> Verdict:
        body = (prompts.REVIEWER_JUDGE + "\n\n" + _render_brief(brief)
                + f"\n\nWORKER OUTPUT:\n{output.output}"
                + f"\nWORKER UNCERTAINTIES: {output.uncertainties or '(none)'}")
        prompt = _prompt(body, self.JUDGE_SCHEMA)

        def parse(raw):
            v = wire.choice(raw, "VERDICT", ("ACCEPT", "REJECT", "BLOCKED"))
            if v == "ACCEPT":
                conf = wire.choice(raw, "CONFIDENCE", ("HIGH", "LOW"))
                return Verdict(VerdictKind.ACCEPT,
                               Confidence.LOW if conf == "LOW" else Confidence.HIGH)
            if v == "REJECT":
                defects = wire.items(raw, "DEFECTS")
                if not defects:
                    raise wire.ParseError("REJECT requires at least one defect")
                return Verdict(VerdictKind.REJECT, defects=defects,
                               extra_context=wire.text(raw, "CONTEXT", allow_none=True) or None)
            q = wire.text(raw, "QUESTION", allow_none=True)
            if not q:
                raise wire.ParseError("BLOCKED requires a QUESTION")
            return Verdict(VerdictKind.BLOCKED, question=q)

        return self._invoke(prompt, self.JUDGE_SCHEMA, parse, f"judge:{brief.task_id}",
                            "reviewer.judge")

    def reconsider(self, brief: Brief, output: WorkerOutput, auditor_defects) -> Verdict:
        body = (prompts.REVIEWER_RECONSIDER + "\n\n" + _render_brief(brief)
                + f"\n\nWORKER OUTPUT:\n{output.output}"
                + f"\nAUDITOR DEFECTS: {auditor_defects}")
        prompt = _prompt(body, self.RECON_SCHEMA)

        def parse(raw):
            v = wire.choice(raw, "VERDICT", ("ACCEPT", "REJECT"))
            if v == "ACCEPT":
                return Verdict(VerdictKind.ACCEPT)
            defects = wire.items(raw, "DEFECTS")
            if not defects:
                raise wire.ParseError("REJECT requires at least one defect")
            return Verdict(VerdictKind.REJECT, defects=defects,
                           extra_context=wire.text(raw, "CONTEXT", allow_none=True) or None)

        return self._invoke(prompt, self.RECON_SCHEMA, parse, f"recon:{brief.task_id}",
                            "reviewer.reconsider")

    def coherence_pass(self, resolutions) -> list[Task]:
        body = prompts.REVIEWER_COHERENCE + f"\n\nRESOLUTIONS: {resolutions}"
        prompt = _prompt(body, self.COHERE_SCHEMA)
        return self._invoke(prompt, self.COHERE_SCHEMA,
                            lambda raw: wire.tasks(raw, "PATCHES"),
                            "coherence", "reviewer.coherence_pass")

    def synthesize(self, resolutions) -> Summary:
        body = prompts.REVIEWER_SYNTHESIZE + f"\n\nRESOLUTIONS: {resolutions}"
        prompt = _prompt(body, self.SUMMARY_SCHEMA)
        return self._invoke(prompt, self.SUMMARY_SCHEMA,
                            lambda raw: Summary(wire.text(raw, "SUMMARY")),
                            "synthesize", "reviewer.synthesize")

    def revise_summary(self, summary, auditor_defects, resolutions) -> Summary:
        body = (prompts.REVIEWER_REVISE
                + f"\n\nCURRENT SUMMARY: {summary.text}\nAUDITOR DEFECTS: {auditor_defects}\n"
                f"RESOLUTIONS: {resolutions}")
        prompt = _prompt(body, self.SUMMARY_SCHEMA)
        return self._invoke(prompt, self.SUMMARY_SCHEMA,
                            lambda raw: Summary(wire.text(raw, "SUMMARY")),
                            "revise", "reviewer.revise_summary")


# --------------------------------------------------------------------------- #
# Auditor
# --------------------------------------------------------------------------- #

class ModelAuditor(_ModelAgent):
    ROLE = "auditor"
    SCHEMA = [FieldSpec("DEFECTS", Kind.LIST, "defects, or none if it passes")]

    def audit_task(self, view: AuditorView) -> AuditResult:
        body = (prompts.AUDITOR_TASK
                + f"\n\nTASK {view.task_id}: {view.desc}\nCRITERION: {view.acceptance_criterion}\n"
                f"RESOLUTION:\n{view.resolution}"
                f"\n\nASSUMPTIONS THE RESOLUTION RELIES ON: {view.assumptions or '(none stated)'}")
        prompt = _prompt(body, self.SCHEMA)
        return self._invoke(prompt, self.SCHEMA,
                            lambda raw: AuditResult(wire.items(raw, "DEFECTS")),
                            f"audit:{view.task_id}", "auditor.audit_task")

    def audit_summary(self, summary: Summary, views) -> AuditResult:
        states = "\n".join(f"  {v.task_id}: [{v.acceptance_criterion}] -> {v.resolution}"
                           for v in views)
        body = (prompts.AUDITOR_SUMMARY
                + f"\n\nSUMMARY:\n{summary.text}\n\nTASK STATES:\n{states}")
        prompt = _prompt(body, self.SCHEMA)
        return self._invoke(prompt, self.SCHEMA,
                            lambda raw: AuditResult(wire.items(raw, "DEFECTS")),
                            "audit-summary", "auditor.audit_summary")


# --------------------------------------------------------------------------- #
# User channels (human or scripted -- never a model)
# --------------------------------------------------------------------------- #

class InteractiveUser:
    def respond_to_planner(self, message) -> str:
        print("\n  ===== USER · reviewer says =====")
        print("  " + message.replace("\n", "\n  "))
        return c_multiline("your reply (you can paste multiple lines)")

    def accept_plan(self, tasks) -> str:
        print("\n  ===== USER · proposed DAG (becomes the contract) =====")
        for t in tasks:
            print(f"     {t.id}: {t.desc}")
            print(f"         criterion: {t.acceptance_criterion}   deps: {t.deps or '[]'}")
        if c_yesno("Accept this plan?", default=True):
            return ""
        return c_multiline("what should change? (you can paste multiple lines)")

    def answer_blocked(self, task_id, question) -> str:
        print(f"\n  ===== USER · task {task_id} blocked: {question}")
        return c_line("your answer")

    def resolve_impasse(self, task_id, defects) -> ImpasseDecision:
        print(f"\n  ===== USER · impasse on {task_id}: {defects}")
        a = c_line("[a]bandon / [k]eep as-is / [g]uide", default="a").lower()[:1]
        if a == "k":
            return ImpasseDecision(ImpasseAction.ACCEPT_AS_IS)
        if a == "g":
            return ImpasseDecision(ImpasseAction.RETRY_WITH_GUIDANCE, guidance=c_line("guidance"))
        return ImpasseDecision(ImpasseAction.ABANDON)

    def approve_scope_change(self, description) -> bool:
        print(f"\n  ===== USER · scope change: {description}")
        return c_yesno("Approve?", default=False)


class ScriptedUser:
    """Deterministic user for tests."""
    def __init__(self, *, planner_replies=None, accept=True, reject_reason="",
                 blocked=None, impasse=None, scope=False):
        self.planner_replies = list(planner_replies or [])
        self.accept = accept
        self.reject_reason = reject_reason
        self.blocked = dict(blocked or {})
        self.impasse = dict(impasse or {})
        self.scope = scope

    def respond_to_planner(self, message):
        return self.planner_replies.pop(0) if self.planner_replies else "(ok, proceed)"

    def accept_plan(self, tasks):
        return "" if self.accept else (self.reject_reason or "needs changes")

    def answer_blocked(self, task_id, question):
        q = self.blocked.get(task_id)
        if isinstance(q, list):
            return q.pop(0) if q else f"(answer: {question})"
        return q or f"(answer: {question})"

    def resolve_impasse(self, task_id, defects):
        return self.impasse.get(task_id, ImpasseDecision(ImpasseAction.ABANDON))

    def approve_scope_change(self, description):
        return self.scope
