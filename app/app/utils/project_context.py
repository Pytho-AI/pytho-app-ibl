"""The active PROJECT/EVENT for the current execution context.

Everything analyst-facing is scoped per project/event. Call paths that
already thread project_id explicitly (State.project_id, fire wrappers,
bootstrap, refire endpoints) SET this contextvar at their entry point;
shared data readers (the utils.scenario proxy, scenario_clock) resolve
it implicitly so their hundreds of call sites don't each need a
project parameter. ContextVar semantics propagate through awaits within
the same task, and every fire/request runs in its own task, so
concurrent projects never bleed into each other.

Reading without an active project raises — there is no default event
and no global scenario to fall back to.
"""
from __future__ import annotations

from contextvars import ContextVar

_ACTIVE_PROJECT_ID: ContextVar[str] = ContextVar("pytho_active_project_id")


def set_active_project(project_id: str):
    """Bind the active project for this execution context. Returns the
    Token (callers may reset, though task-scoped contexts rarely need
    to). Blank raises — bind a real event."""
    pid = str(project_id or "").strip()
    if not pid or pid.lower() in ("anon", "_autodraft", "default"):
        raise ValueError(
            f"set_active_project requires a real project id (got {project_id!r})")
    return _ACTIVE_PROJECT_ID.set(pid)


def active_project() -> str:
    """The bound project id. Raises LookupError with a pointed message
    when no project is bound — the caller's entry point must
    set_active_project (or thread project_id explicitly)."""
    try:
        return _ACTIVE_PROJECT_ID.get()
    except LookupError:
        raise LookupError(
            "no active project bound in this execution context — the entry "
            "point must call project_context.set_active_project(project_id) "
            "(projects are THE scoping axis; there is no default event)"
        ) from None
