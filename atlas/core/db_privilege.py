"""F-019/F-020: least-privilege DB runtime guard.

The audit append-only trigger and the monotonic chain-head anchor (migration
0042) are enforced by the DATABASE — but a PostgreSQL SUPERUSER bypasses every
row trigger with ``SET session_replication_role = 'replica'`` (independently
reproduced: as the owner ``atlas`` it succeeds; as a non-superuser role it is
refused ``permission denied to set parameter``). So the triggers are only real if
the runtime connects as a NON-superuser, NON-owner role.

This module (a) reports the runtime role's privilege posture and (b) fails closed
when the deployment demands least privilege but the runtime is a superuser. It is
called at API startup and surfaced on ``/health`` so a mis-provisioned production
process (connected as the owner) refuses to operate rather than run with the
audit wall silently bypassable.

Provisioning of the least-privilege ``atlas_app`` role + its grants lives in
migration 0043 and ``ops/sql/provision_runtime_role.sql``.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

RUNTIME_ROLE = "atlas_app"          # the intended least-privilege runtime role


@dataclass(frozen=True)
class RuntimePrivilege:
    role: str
    is_superuser: bool
    can_set_replication_role: bool   # the concrete trigger-bypass capability
    least_privilege: bool            # True only when it can bypass NOTHING


def _probe_can_set_replication_role(session: Session) -> bool:
    """Directly probe the trigger-bypass capability: can this role turn triggers
    off session-wide? Runs in a SAVEPOINT and rolls back so the probe never
    actually changes replication behaviour. A superuser succeeds; a least-
    privilege role is refused with InsufficientPrivilege."""
    sp = session.begin_nested()
    try:
        session.execute(text("SET LOCAL session_replication_role = 'replica'"))
        return True
    except Exception:
        return False
    finally:
        sp.rollback()


def runtime_privilege(session: Session) -> RuntimePrivilege:
    """The runtime role's privilege posture, from the live connection."""
    row = session.execute(text(
        "SELECT current_user AS role, "
        "(SELECT rolsuper FROM pg_roles WHERE rolname = current_user) AS is_superuser"
    )).mappings().one()
    is_superuser = bool(row["is_superuser"])
    can_set = _probe_can_set_replication_role(session)
    return RuntimePrivilege(
        role=str(row["role"]), is_superuser=is_superuser,
        can_set_replication_role=can_set,
        least_privilege=not is_superuser and not can_set)


def assert_least_privilege_runtime(session: Session) -> None:
    """Fail closed when the runtime role can bypass the audit triggers. Raises
    RuntimeError naming the concrete capability; call at startup when
    ``db_require_least_privilege`` is set."""
    p = runtime_privilege(session)
    if not p.least_privilege:
        why = ("is a SUPERUSER" if p.is_superuser
               else "can SET session_replication_role")
        raise RuntimeError(
            f"DB runtime role {p.role!r} {why} — it can bypass the audit "
            "append-only + monotonic-anchor triggers (F-019/F-020). Refusing to "
            f"run. Connect as the least-privilege {RUNTIME_ROLE!r} role "
            "(migration 0043 / ops/sql/provision_runtime_role.sql).")


# --------------------------------------------------------------------------- #
# audit-wall enforcement — the DB-layer defense that does NOT depend on the
# runtime role: ENABLE ALWAYS triggers fire even under
# session_replication_role='replica', so a superuser cannot suppress them.
# --------------------------------------------------------------------------- #
_AUDIT_TRIGGERS = ("decision_events_append_only", "chain_head_guard")


def audit_triggers_always_enabled(session: Session) -> bool:
    """True iff BOTH audit guards are ENABLE ALWAYS (``tgenabled='A'``) — the
    state migration 0044 installs. When they are merely origin-enabled ('O'), a
    superuser can silence them via ``session_replication_role='replica'``."""
    rows = session.execute(text(
        "SELECT tgname, tgenabled FROM pg_trigger WHERE tgname = ANY(:names)"),
        {"names": list(_AUDIT_TRIGGERS)}).all()
    found = {r.tgname: r.tgenabled for r in rows}
    return all(found.get(t) == "A" for t in _AUDIT_TRIGGERS)


def assert_audit_wall_enforced(session: Session) -> None:
    """Fail closed UNCONDITIONALLY at startup when the audit append-only / anchor
    triggers are not ENABLE ALWAYS. This guarantees invariant-4 integrity even if
    the deployment (mis)connects as a superuser: replica mode cannot suppress an
    ENABLE ALWAYS trigger. Independent of the runtime-role check."""
    if not audit_triggers_always_enabled(session):
        raise RuntimeError(
            "audit guards decision_events_append_only / chain_head_guard are not "
            "ENABLE ALWAYS — a superuser could suppress them via "
            "session_replication_role='replica' and tamper the audit chain "
            "(F-019/F-020). Refusing to run. Apply migrations to head (0044).")
