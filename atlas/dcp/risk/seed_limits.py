"""Load limit set v1 (ADR-0001) into risk.limit_sets. Idempotent."""
from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session


def seed_limit_set(session: Session, path: Path) -> None:
    doc = json.loads(path.read_text())
    session.execute(text(
        "INSERT INTO risk.limit_sets (version, mode, limits, effective_from, created_by, "
        " confirmation_a, confirmation_b) "
        "VALUES (:v, :m, CAST(:l AS jsonb), :ef, :cb, now() - interval '2 hours', now()) "
        "ON CONFLICT (version) DO NOTHING"),
        {"v": doc["version"], "m": doc["mode"], "l": json.dumps(doc["limits"]),
         "ef": doc["effective_from"], "cb": doc["created_by"]})
