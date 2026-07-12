"""Deep-window vendor-request chunking (pure — no DB, no vendor).

A 2010-onward backfill must never ride on one giant vendor response; chunks are
inclusive, contiguous and non-overlapping so the union of chunked fetches is
exactly the single-range fetch — no boundary day is dropped or double-fetched.
"""
from datetime import date, timedelta

import pytest

from atlas.dcp.market_data.backfill import CHUNK_DAYS, chunk_windows


def test_small_window_is_a_single_chunk():
    assert chunk_windows(date(2024, 7, 10), date(2024, 7, 15)) == [
        (date(2024, 7, 10), date(2024, 7, 15))]


def test_single_day_window():
    d = date(2024, 7, 15)
    assert chunk_windows(d, d) == [(d, d)]


def test_empty_when_start_after_end():
    assert chunk_windows(date(2024, 7, 16), date(2024, 7, 15)) == []


def test_deep_window_chunks_cover_exactly_without_overlap():
    """Full-history shape: 2010-01-01 -> 2026-07-10 (~16.5y of dailies)."""
    start, end = date(2010, 1, 1), date(2026, 7, 10)
    chunks = chunk_windows(start, end)
    assert len(chunks) == 4                       # ~6035 days / 1826 -> 4 requests
    assert chunks[0][0] == start
    assert chunks[-1][1] == end
    for lo, hi in chunks:
        assert lo <= hi
        assert (hi - lo).days + 1 <= CHUNK_DAYS   # bounded request size
    for (_, prev_hi), (next_lo, _) in zip(chunks, chunks[1:]):
        assert next_lo == prev_hi + timedelta(days=1)  # contiguous, no overlap


def test_exact_multiple_boundary():
    start = date(2020, 1, 1)
    end = start + timedelta(days=2 * 10 - 1)      # exactly two 10-day chunks
    chunks = chunk_windows(start, end, max_days=10)
    assert chunks == [(start, start + timedelta(days=9)),
                      (start + timedelta(days=10), end)]


def test_rejects_nonpositive_chunk_size():
    with pytest.raises(ValueError, match="max_days"):
        chunk_windows(date(2024, 1, 1), date(2024, 2, 1), max_days=0)
