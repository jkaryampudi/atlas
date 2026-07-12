"""FX research sandbox (ADR-0008) — SEALED namespace.

EUR/USD only, research-only forever under ADR-0008. Nothing in atlas/dcp,
atlas/agents, atlas/api or atlas/ops may import from here (enforced by
tests/unit/test_boundaries_fxlab.py); this package MAY import the shared
evaluation discipline (thresholds, deflated Sharpe, trial registry) from
atlas.dcp.backtest — trials are trials, and fxlab trials count in the same
registry. No profit target exists anywhere in this package.
"""
