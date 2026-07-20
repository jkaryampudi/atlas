"""Per-family catalog modules (Research Factory).

Each family lives in its OWN module so that widening the catalog with a new
family never touches another family's hashed source: a member's code_sha
covers exactly the files its math lives in (its family module + the imported
math modules), and adding families/quality.py tomorrow cannot invalidate the
momentum or low-vol pins. The aggregation (RANKABLE_FEATURES, the ADR-0016
FEATURE_LINEAGE binding, the import-time guard) stays in factory/features.py,
which is deliberately NOT hashed into any member — it assembles dicts and
contains no math.

The one-time cost of this structure was a reviewed re-pin of the momentum
variants (their construction moved here from factory/features.py, changing
their code_sha with the math untouched) — executed with
atlas/tools/repin_features.py, audited, byte-identity re-proven by the
equivalence tests.
"""
