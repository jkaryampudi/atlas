# Atlas — Definitive Strategy Rerun: Artifacts & Reproduction

The definitive rerun **refused to run** (INSUFFICIENT EVIDENCE — see
`ATLAS_DEFINITIVE_STRATEGY_RERUN.md`), so the runner produced **no strategy report,
no metrics, and no registered trial**. The artifacts below are the evidence/log
captures of the refusal and the surrounding verification, plus everything needed to
reproduce the outcome deterministically.

## 1. Reproduction identity

| Field | Value |
|---|---|
| Code commit | `d6d871f5436f341198fe05883b070e05344cc5ab` (`main`; == tree `be71d7d`) |
| Strategy | `xsmom-pit-tr` / `signals.xsmom.v1` SPEC v1.0.0 (12-1, LOOKBACK 252, SKIP 21, TOP_N 10, monthly) |
| Command | `python -m atlas.dcp.backtest.xsmom_pit_run --total-return --paths 100` |
| Deterministic clock | FrozenClock from the last stored bar = **2026-07-24** (no wall-clock) |
| Eval window | `WINDOW_START` 2012-07-01 .. 2026-07-24 (full); KILL window from 2016-01-01 |
| `IDENTITY_COVERAGE_FLOOR` | **0.9** |
| Data source | `market.price_bars_daily` source `EodhdAdapter` (2010-01-04..2026-07-24) |
| Run isolation | **disposable TEMPLATE copy `atlas_rerun`** of the production `atlas` DB; real DB never mutated |
| Runtime role | research run: owner on the disposable copy; refusal **identical** under least-privilege `atlas_app` (verified) |

## 2. Outcome (deterministic)

```
RuntimeError: F-001 identity coverage 500/666 member symbols resolved (< floor 90%)
— the instrument-identity feed is too sparse to grade the definitive panel;
refusing to run on ticker-only history. Populate market.instrument_identity first.
```

Read-only re-derivation of the coverage (same on the real `atlas` DB and via
`atlas_app`): **500 / 671 panel-candidate members resolved = 74.5%**; 171 unresolved
(73 delisted + 98 living, no ISIN); 0 of 500 resolved identities attest history.

## 3. Generated evidence artifacts (session scratchpad; SHA-256)

These are ephemeral session logs (not committed); hashes recorded for provenance.

| Artifact | SHA-256 | Contents |
|---|---|---|
| `rerun_output.txt` | `7373ed5dc0999421f5b6656bde012829f9799573c328f9a1497b271852faf9c4` | the refused definitive run (deterministic clock; RuntimeError) |
| `coverage_analysis.txt` | `5259862119ff6b9994e34ef504e3f67ec1562bfa94a0a9a00fd12751ba342bb0` | 500/671 coverage breakdown (delisted/living no-ISIN; 0 attested) |
| `data_state.txt` | `8fb679ad367fb80e02c1d12d98cb994614cc29b83198601b99a5dd0fb7e53105` | real-DB data census (bars/membership/identity/FX/dividends) |
| `pm_suite.txt` | `7a297fe7211c33d499a627dbc66d5d5b150324818684aac544be498e07727da2` | full clean-DB pytest (1871 passed / 0 skipped) |
| `priv_attacks.txt` | `fb11e8046ddb507fb711ca5c11955b086ef7f3136b02637b6104e345843bea7a` | (prior turn) atlas_app bypass-refused matrix |

Scratchpad dir:
`/private/tmp/claude-501/-Users-jayakrishnakaryampudi-Documents-atlas/e002cee0-6581-4d8d-887e-e748fcee9f13/scratchpad/`

## 4. Committed evidence documents (this turn)

| File | SHA-256 (at write time) |
|---|---|
| `REVIEW_PACKAGE/ATLAS_POST_MERGE_VERIFICATION.md` | `7227c712db16669504eab3ad78ab7c4e55f566503ab2500ff64e226b3fb2a2c3` |
| `REVIEW_PACKAGE/ATLAS_DEFINITIVE_STRATEGY_RERUN.md` | `a0166f36e87c74d343bc4fd61492a1b05d25213dd9c514cde59414bedd32c32b` |
| `REVIEW_PACKAGE/ATLAS_STRATEGY_RERUN_ARTIFACTS.md` | (this file) |
| `REVIEW_PACKAGE/ATLAS_POST_MERGE_OPERATOR_STATUS.md` | (see file) |

(Hashes recomputable with `shasum -a256`; the docs are the authoritative record.)

## 5. Exact reproduction procedure

```bash
# 1. checkout the corrected baseline
git -C atlas checkout d6d871f       # == origin/main

# 2. make a disposable copy of the production DB (research isolation)
psql -c "CREATE DATABASE atlas_rerun WITH TEMPLATE atlas"   # needs no active conns

# 3. run the definitive TR gauntlet against the copy (deterministic clock from last bar)
ATLAS_DATABASE_URL=postgresql+psycopg://atlas:...@localhost:5432/atlas_rerun \
  python -m atlas.dcp.backtest.xsmom_pit_run --total-return --paths 100
#    -> RuntimeError: F-001 identity coverage 500/666 ... < floor 90% (INSUFFICIENT EVIDENCE)

# 4. (read-only) re-derive the coverage on the production DB — deterministic count
#    500/671 = 74.5% resolved; 171 unresolved (73 delisted + 98 living, no ISIN)

# 5. drop the disposable copy
psql -c "DROP DATABASE atlas_rerun"
```

The outcome is deterministic: the coverage is a fixed count over the current data;
it will refuse identically until `market.instrument_identity` coverage reaches ≥90%
of the window's member universe (which requires the dated symbol-change /
issuer-history vendor feed for delisted/ISIN-less members).

## 6. What would change the outcome

* Procuring the **dated symbol-change / issuer-history feed** → raises identity
  coverage above 0.9 AND sets `history_complete=true` (or records issuer breaks),
  so the panel would run AND pre-membership formation would be admitted only where
  attested. Only then can a defensible definitive backtest be produced.
* No code change is warranted — the refusal is the F-001 remediation behaving
  correctly. (Lowering `IDENTITY_COVERAGE_FLOOR` to force a result would reintroduce
  the ticker-only-history bias the remediation removed and is explicitly out of
  scope.)
