"""The CLOSED rankable-feature catalog (Research Factory).

RANKABLE_FEATURES is the closed set a RecipeSpec may name as its
rank_feature. Families live in their OWN modules under factory/families/ —
momentum (the phase-1 grid) and low-vol (the 2026-07 widening) — so that
adding a family never touches another family's hashed source: a member's
code_sha covers exactly its family module plus the imported math modules.
THIS aggregator is deliberately NOT hashed into any member: it assembles
dicts and contains no math; the parameterizations live in the family files.

WIDENING THE CATALOG IS A REVIEWED CHANGE: a new family lands as a new
families/<name>.py declaring BOTH its members and their ADR-0016 lineage
(families/*.FAMILY_LINEAGE — inside the hashed source, so a lineage rename
trips the family's pins; the import-time guard below refuses a member whose
family declared no line). register_feature's pin discipline applies per
member; a reviewed change that moves a member's hashed source is executed
against the registry with atlas/tools/repin_features.py (audited,
value-verified, spec-identical refactors only).

Re-exports (family_member_name, MOMENTUM_GRID) keep the phase-1 import
surface stable for the equivalence tests.
"""
from __future__ import annotations

from typing import Final

import atlas.dcp.factory.families.low_vol as _low_vol
import atlas.dcp.factory.families.momentum as _momentum_family
from atlas.dcp.factory.families.low_vol import low_vol_members
from atlas.dcp.factory.families.momentum import (  # noqa: F401 — re-exports
    MOMENTUM_GRID,
    family_member_name,
    momentum_members,
)
from atlas.dcp.features.store import FeatureDefinition

_FAMILIES = ((_momentum_family, momentum_members),
             (_low_vol, low_vol_members))


def _build_catalog() -> dict[str, FeatureDefinition]:
    catalog: dict[str, FeatureDefinition] = {}
    for _module, members_fn in _FAMILIES:
        for name, definition in members_fn().items():
            if definition.name != name:  # pragma: no cover — structural guard
                raise RuntimeError(
                    f"catalog key {name!r} does not match its definition's "
                    f"name {definition.name!r} — a mislabeled member would be "
                    f"registered and ranked under the wrong identity")
            if name in catalog:  # pragma: no cover — structural guard
                raise RuntimeError(f"catalog name collision: {name!r}")
            catalog[name] = definition
    return catalog


RANKABLE_FEATURES: Final[dict[str, FeatureDefinition]] = _build_catalog()


def _build_lineage() -> dict[str, str]:
    """ADR-0016 LINEAGE BINDING, merged from the FAMILY modules — each family
    declares its members' lineage INSIDE its own hashed source
    (families/*.FAMILY_LINEAGE), so renaming a line changes that family's
    bytes, trips every member's code_sha, and register_feature refuses until
    the change is reviewed and repinned. The deflation count is never
    spec-chosen and never resettable from this unhashed aggregator."""
    merged: dict[str, str] = {}
    for module, _members_fn in _FAMILIES:
        for name, lineage in module.FAMILY_LINEAGE.items():
            if name in merged:  # pragma: no cover — structural guard
                raise RuntimeError(f"lineage declared twice for {name!r}")
            merged[name] = lineage
    return merged


FEATURE_LINEAGE: Final[dict[str, str]] = _build_lineage()
if set(FEATURE_LINEAGE) != set(RANKABLE_FEATURES):  # pragma: no cover — guard
    raise RuntimeError(
        "FEATURE_LINEAGE and RANKABLE_FEATURES disagree: every catalog "
        "member must declare its ADR-0016 lineage in its family module's "
        "FAMILY_LINEAGE, in the same reviewed diff that adds the member "
        f"(catalog {sorted(RANKABLE_FEATURES)}, "
        f"lineages declared for {sorted(FEATURE_LINEAGE)})")


def get_rank_feature(name: str) -> FeatureDefinition:
    try:
        return RANKABLE_FEATURES[name]
    except KeyError:
        raise KeyError(
            f"unknown rank feature {name!r} — the catalog is the closed "
            f"set {sorted(RANKABLE_FEATURES)}; widening it is a reviewed "
            "change (a new factory/families/ module + its lineage "
            "declaration in atlas/dcp/factory/features.py)") from None
