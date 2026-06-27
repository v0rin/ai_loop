"""Transport layer -- the ONLY thing mocked.

A model call is: a role + a prompt string in, a raw reply string out. Charging
the budget happens here (one charge per call, so re-prompts are counted), then
`_produce` returns the raw text. That's the whole seam:

    real:    _produce -> OpenRouter(prompt)              # ignores schema
    mock:    _produce -> a human types it (guided/raw)
    scripted:_produce -> next canned wire string         # deterministic tests

`schema` is passed through but only the mock uses it (to offer guided entry).
The real transport ignores it, so swapping in OpenRouter changes nothing else.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

import wire
import prompts
from models import Task


# --------------------------------------------------------------------------- #
# Schema: how a role declares the blocks it expects back
# --------------------------------------------------------------------------- #

class Kind(Enum):
    TEXT = "text"        # single value
    BLOB = "blob"        # free-form, possibly multi-line (e.g. worker output)
    LIST = "list"        # one item per line, or 'none'
    CHOICE = "choice"    # one of options
    TASKS = "tasks"      # repeated task blocks, or 'none'


@dataclass
class FieldSpec:
    key: str
    kind: Kind
    prompt: str
    options: tuple[str, ...] = ()
    allow_none: bool = False


def format_spec(schema: list[FieldSpec]) -> str:
    """The format instructions appended to every prompt -- teaches the wire form.
    The header prose lives in prompts.py; the per-field lines are generated from
    the schema so they can never drift from what the parser accepts."""
    lines = [prompts.FORMAT_SPEC_HEADER]
    for f in schema:
        if f.kind is Kind.CHOICE:
            lines.append(f"[${f.key}] one of: {', '.join(f.options)} [$/{f.key}]")
        elif f.kind is Kind.LIST:
            lines.append(f"[${f.key}] one item per line, or none [$/{f.key}]")
        elif f.kind is Kind.TASKS:
            lines.append(
                f"[${f.key}]\n"
                f"  [$TASK][$ID]t1[$/ID][$DESC]...[$/DESC]"
                f"[$DEPS]none[$/DEPS][$CRITERION]a checkable PASS/FAIL[$/CRITERION][$/TASK]\n"
                f"  (repeat [$TASK]...[$/TASK] once per task; DEPS is comma-separated ids "
                f"or none), or write none\n"
                f"[$/{f.key}]")
        else:
            tail = ", or none" if f.allow_none else ""
            lines.append(f"[${f.key}] ...{tail} [$/{f.key}]")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Console helpers (shared by HumanModel and the human user channel)
# --------------------------------------------------------------------------- #

def c_line(prompt: str, *, default: str | None = None, allow_empty: bool = False) -> str:
    hint = f" [{default}]" if default is not None else ""
    while True:
        ans = input(f"  {prompt}{hint}\n  > ").strip()
        if ans:
            return ans
        if default is not None:
            return default
        if allow_empty:
            return ""
        print("    (a value is required)")


def c_lines(prompt: str) -> list[str]:
    print(f"  {prompt}")
    print("    (one per line; blank line to finish; nothing = none)")
    out: list[str] = []
    while True:
        ln = input("  - ").strip()
        if not ln:
            break
        out.append(ln)
    return out


def c_multiline(prompt: str) -> str:
    print(f"  {prompt}")
    print("    (one or more lines; finish with a single '.' on its own line)")
    out: list[str] = []
    while True:
        ln = input()
        if ln.strip() == ".":
            break
        out.append(ln)
    return "\n".join(out)


def c_choice(prompt: str, options: dict[str, str]) -> str:
    print(f"  {prompt}")
    for k, v in options.items():
        print(f"     [{k}] {v}")
    keys = "/".join(options)
    while True:
        ans = input(f"  ({keys}) > ").strip().lower()
        if ans in options:
            return ans
        print(f"    (pick one of: {keys})")


def c_yesno(prompt: str, *, default: bool = True) -> bool:
    d = "y" if default else "n"
    while True:
        ans = input(f"  {prompt} (y/n) [{d}]\n  > ").strip().lower()
        if not ans:
            return default
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        print("    (y or n)")


# --------------------------------------------------------------------------- #
# Transport base: charges budget, then produces raw text
# --------------------------------------------------------------------------- #

class Transport:
    def __init__(self, budget):
        self.budget = budget

    def call(self, role: str, prompt: str, schema: list[FieldSpec], hint: str = "") -> str:
        self.budget.charge(role, hint)        # may raise BudgetHalt
        return self._produce(role, prompt, schema)

    def _produce(self, role: str, prompt: str, schema: list[FieldSpec]) -> str:
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# HumanModel: you type the model's reply (guided assembly OR raw wire text)
# --------------------------------------------------------------------------- #

class HumanModel(Transport):
    def _produce(self, role: str, prompt: str, schema: list[FieldSpec]) -> str:
        print(f"\n  ====== MODEL CALL · role={role.upper()} ======")
        print("  " + prompt.replace("\n", "\n  "))
        mode = c_choice("Reply how?", {
            "g": "guided  (answer fields; I assemble the wire reply)",
            "r": "raw     (type the literal [$KEY] reply yourself -- use to test bad input)",
        })
        if mode == "r":
            return c_multiline("paste/type the raw reply")
        return "\n".join(self._gather(f) for f in schema)

    def _gather(self, f: FieldSpec) -> str:
        if f.kind is Kind.TEXT:
            v = c_line(f.prompt, default=("none" if f.allow_none else None))
            return wire.block(f.key, v)
        if f.kind is Kind.BLOB:
            v = c_multiline(f.prompt)
            return wire.block(f.key, v or ("none" if f.allow_none else v))
        if f.kind is Kind.LIST:
            its = c_lines(f.prompt)
            return wire.block(f.key, "\n".join(its) if its else "none")
        if f.kind is Kind.CHOICE:
            opts = {o.lower(): o for o in f.options}
            return wire.block(f.key, c_choice(f.prompt, opts).upper())
        if f.kind is Kind.TASKS:
            return wire.block(f.key, self._gather_tasks(f.prompt))
        raise ValueError(f"unknown kind {f.kind}")

    def _gather_tasks(self, prompt: str) -> str:
        print(f"  {prompt}")
        if not c_yesno("add a task?", default=True):
            return "none"
        blocks = []
        while True:
            t = Task(
                id=c_line("task id"),
                desc=c_line("description"),
                acceptance_criterion=c_line("acceptance criterion (checkable PASS/FAIL)"),
                deps=[d.strip() for d in c_line("dependency ids (comma-separated, or blank)",
                                                allow_empty=True).split(",") if d.strip()],
            )
            blocks.append(wire.task_block(t))
            if not c_yesno("add another task?", default=False):
                break
        return "\n".join(blocks)


# --------------------------------------------------------------------------- #
# ScriptedTransport: deterministic tests -- returns canned wire strings in order
# --------------------------------------------------------------------------- #

class ScriptedTransport(Transport):
    """Per-role FIFO queues of raw wire replies. Exercises the REAL build/parse
    path (unlike the old typed mocks) while staying fully deterministic."""
    def __init__(self, budget, replies: dict[str, list[str]]):
        super().__init__(budget)
        self.replies = {r: list(q) for r, q in replies.items()}

    def _produce(self, role: str, prompt: str, schema: list[FieldSpec]) -> str:
        q = self.replies.get(role)
        if not q:
            raise AssertionError(f"ScriptedTransport: no scripted reply left for role {role!r}")
        return q.pop(0)
