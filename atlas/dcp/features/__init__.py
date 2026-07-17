"""Point-in-time Feature Store (ADR-0011 step 1).

Versioned, reproducible, no-look-ahead storage for computed factor values —
the shared substrate every future factor (value, quality, growth) and any
eventual ML must read from. See store.py for the storage contract and
definitions.py for the registered features.
"""
