"""The wire format shared by every model role: `[$KEY]content[$/KEY]`.

Open/close are asymmetric (XML/HTML style) so a missing or stray tag is locally
detectable and the parse error can name which key and which side is missing —
and because the matched-pair form is something models produce reliably. Every
block a role declares MUST be present; "none" is the explicit empty value, so an
omitted block is an error, not an ambiguous empty. Parsing is strict: the
re-prompt loop (in agents.py) is the only tolerance mechanism, never silent
coercion here.

This module is pure: build strings, parse strings. No I/O, no model awareness.
"""
from __future__ import annotations

import re

from models import Task


class ParseError(Exception):
    """A reply did not conform to the declared blocks. Triggers one re-prompt."""


def block(key: str, content: str) -> str:
    return f"[${key}]{content}[$/{key}]"


def _between(raw: str, key: str) -> str:
    open_t, close_t = f"[${key}]", f"[$/{key}]"
    i = raw.find(open_t)
    if i == -1:
        raise ParseError(f"missing block: no opening [${key}] tag found")
    j = raw.find(close_t, i + len(open_t))
    if j == -1:
        raise ParseError(f"block [${key}] has an opening tag but no closing [$/{key}]")
    return raw[i + len(open_t):j]


def text(raw: str, key: str, *, allow_none: bool = False) -> str:
    v = _between(raw, key).strip()
    if not v:
        raise ParseError(f"[${key}] is empty (write 'none' if intentional)")
    if allow_none and v.lower() == "none":
        return ""
    return v


def items(raw: str, key: str) -> list[str]:
    """A list field: one item per line, or the single word 'none'."""
    v = _between(raw, key).strip()
    if not v:
        raise ParseError(f"[${key}] is empty (write 'none' if there are none)")
    if v.lower() == "none":
        return []
    return [ln.strip() for ln in v.splitlines() if ln.strip()]


def choice(raw: str, key: str, options: tuple[str, ...]) -> str:
    v = _between(raw, key).strip().upper()
    if v not in options:
        raise ParseError(f"[${key}] must be one of {options}, got {v!r}")
    return v


_TASK_RE = re.compile(r"\[\$TASK\](.*?)\[\$/TASK\]", re.DOTALL)


def _deps(seg: str) -> list[str]:
    v = _between(seg, "DEPS").strip()
    if v.lower() == "none":
        return []
    return [p.strip() for p in re.split(r"[,\n]", v) if p.strip()]


def tasks(raw: str, key: str) -> list[Task]:
    """A task-list field: repeated [$TASK]...[$/TASK] blocks, or 'none'.
    Each task block holds [$ID][$DESC][$DEPS][$CRITERION] (each open/close)."""
    body = _between(raw, key).strip()
    if body.lower() == "none":
        return []
    out: list[Task] = []
    for m in _TASK_RE.finditer(body):
        seg = m.group(1)
        out.append(Task(
            id=text(seg, "ID"),
            desc=text(seg, "DESC"),
            acceptance_criterion=text(seg, "CRITERION"),
            deps=_deps(seg),
        ))
    if not out:
        raise ParseError(f"[${key}] contained no complete [$TASK]...[$/TASK] blocks "
                         f"(write 'none' for empty)")
    openers = body.count("[$TASK]")
    if openers != len(out):
        raise ParseError(f"[${key}]: {openers} [$TASK] opener(s) but only {len(out)} "
                         f"complete [$TASK]...[$/TASK] block(s) -- a closing [$/TASK] "
                         f"is missing or a [$TASK] is stray")
    return out


def task_block(t: Task) -> str:
    """Build the wire form of a single task (used by guided mode and tests)."""
    deps = ", ".join(t.deps) if t.deps else "none"
    inner = (block("ID", t.id) + block("DESC", t.desc)
             + block("DEPS", deps) + block("CRITERION", t.acceptance_criterion))
    return block("TASK", inner)
