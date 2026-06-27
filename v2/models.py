"""Domain types for the agent loop.

These are plain data holders. No behaviour beyond a couple of small views used
by the controller. The point of keeping them dumb is that the controller (the
deterministic engine) owns all the logic, and the agents just produce/consume
these objects -- so the same types work for mocks now and real OpenRouter
calls later.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #

class TaskStatus(Enum):
    PENDING = "pending"          # exists in the DAG, deps not all DONE yet
    IN_PROGRESS = "in_progress"  # currently being worked
    DONE = "done"                # accepted resolution written
    FAILED = "failed"            # impasse the user chose to abandon
    CANCELLED = "cancelled"      # a dependency failed -> can never run


class Confidence(Enum):
    HIGH = "high"
    LOW = "low"


class VerdictKind(Enum):
    ACCEPT = "accept"
    REJECT = "reject"
    BLOCKED = "blocked"


class ImpasseAction(Enum):
    ABANDON = "abandon"               # mark task FAILED, cascade-cancel dependents
    ACCEPT_AS_IS = "accept_as_is"     # take the last attempt despite defects
    RETRY_WITH_GUIDANCE = "retry"     # user supplies guidance, give it one more shot


# --------------------------------------------------------------------------- #
# Task + its persisted state
# --------------------------------------------------------------------------- #

@dataclass
class RejectionEntry:
    """One rejected attempt: the output and the defects it was rejected for.
    Defects are the load-bearing signal; the verbatim attempt is optional so the
    ledger can keep all defects but only the most recent few outputs."""
    attempt: Optional[str]      # None == output dropped (kept defects only)
    defects: list[str]


@dataclass
class TaskState:
    """The persisted record for a task.

    Deliberately excludes the per-round review *discussion*, but DOES keep a
    durable rejection ledger so a worker resuming a task (even after a BLOCK or
    guided retry) still sees what was already rejected and why. State carries the
    criterion, the evolving instruction (initial context + numbered reviewer
    updates), the rejection ledger, and the final result.
    """
    acceptance_criterion: str
    initial_context: str
    context_updates: list[str] = field(default_factory=list)
    rejections: list[RejectionEntry] = field(default_factory=list)
    final_resolution: Optional[str] = None

    def add_context(self, update: str) -> int:
        """Append a numbered context update from the reviewer. Returns its number."""
        self.context_updates.append(update)
        return len(self.context_updates)

    def record_rejection(self, attempt: str, defects: list[str]) -> None:
        self.rejections.append(RejectionEntry(attempt, list(defects)))

    def rejection_brief(self, keep_outputs: int) -> list[RejectionEntry]:
        """The ledger as handed to the worker: ALL defects, but verbatim outputs
        only for the last `keep_outputs` rejections (older outputs dropped to
        avoid anchoring the worker on stale attempts). `keep_outputs` is supplied
        by the controller from ControllerConfig -- no default lives here."""
        n = len(self.rejections)
        out = []
        for i, e in enumerate(self.rejections):
            keep = i >= n - keep_outputs
            out.append(RejectionEntry(e.attempt if keep else None, e.defects))
        return out


@dataclass
class Task:
    id: str
    desc: str
    acceptance_criterion: str
    deps: list[str] = field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    retry_count: int = 0
    block_count: int = 0
    note: Optional[str] = None          # e.g. "accepted as-is at user's request"
    state: TaskState = field(init=False)

    def __post_init__(self) -> None:
        self.state = TaskState(
            acceptance_criterion=self.acceptance_criterion,
            initial_context=self.desc,
        )

    def auditor_view(self, candidate_resolution: str,
                     assumptions: Optional[list[str]] = None) -> "AuditorView":
        """What the auditor sees: criterion + the candidate resolution + the
        assumptions that resolution ships with -- blind to the *path* that
        produced it (no context updates, no reject history, no discussion). The
        worker's assumptions are intrinsic to the result (a caveat attached to the
        output), not discussion, so the auditor needs them to judge fairly."""
        return AuditorView(
            task_id=self.id,
            desc=self.desc,
            acceptance_criterion=self.acceptance_criterion,
            resolution=candidate_resolution,
            assumptions=list(assumptions or []),
        )


# --------------------------------------------------------------------------- #
# Messages between the controller and the agents
# --------------------------------------------------------------------------- #

@dataclass
class Brief:
    """Everything the worker is given for one attempt."""
    task_id: str
    desc: str
    acceptance_criterion: str
    dependency_resolutions: dict[str, str]      # dep_id -> its final_resolution
    context_updates: list[str]                  # persisted reviewer additions
    rejection_history: list[RejectionEntry] = field(default_factory=list)


@dataclass
class WorkerOutput:
    output: str
    uncertainties: list[str] = field(default_factory=list)


@dataclass
class Verdict:
    kind: VerdictKind
    confidence: Confidence = Confidence.HIGH
    defects: list[str] = field(default_factory=list)     # for REJECT
    extra_context: Optional[str] = None                  # for REJECT (persisted)
    question: Optional[str] = None                        # for BLOCKED


@dataclass
class AuditorView:
    task_id: str
    desc: str
    acceptance_criterion: str
    resolution: str
    assumptions: list[str] = field(default_factory=list)


@dataclass
class AuditResult:
    defects: list[str] = field(default_factory=list)     # empty == clean

    @property
    def clean(self) -> bool:
        return not self.defects


@dataclass
class PlanTurn:
    """One turn of the planning conversation. While `done` is False this is just
    a message to the user (tasks empty). When the reviewer sets `done`, `tasks`
    is the proposed DAG -- the artifact the user then ratifies."""
    done: bool
    message: str
    tasks: list[Task] = field(default_factory=list)


@dataclass
class Summary:
    text: str


@dataclass
class ImpasseDecision:
    action: ImpasseAction
    guidance: Optional[str] = None      # used with RETRY_WITH_GUIDANCE
