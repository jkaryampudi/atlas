"""Per-model pricing table (desk-review 2026-07 item 3; runner.price_model).

The $10/day budget breaker is a constitutional control: pricing must never
undercount a model. Known models price at their published per-MTok rates,
the local route is $0.00 API spend, and anything unknown FAILS CLOSED at the
highest published Anthropic rate with the flag set.
"""
from __future__ import annotations

from atlas.agents.runtime.runner import (
    FAIL_CLOSED_RATE_PER_MTOK,
    MODEL_RATES_PER_MTOK,
    price_model,
)


def test_known_models_price_at_published_rates():
    assert price_model("claude-opus-4-8") == (5.0, 25.0, False)
    assert price_model("claude-opus-4-7") == (5.0, 25.0, False)
    assert price_model("claude-opus-4-6") == (5.0, 25.0, False)
    assert price_model("claude-sonnet-4-6") == (3.0, 15.0, False)
    assert price_model("claude-sonnet-5") == (3.0, 15.0, False)
    assert price_model("claude-haiku-4-5") == (1.0, 5.0, False)


def test_dated_snapshot_ids_price_like_their_alias():
    assert price_model("claude-haiku-4-5-20251001") == (1.0, 5.0, False)
    assert price_model("claude-sonnet-4-6-20251114") == (3.0, 15.0, False)


def test_opus_no_longer_billed_at_sonnet_rates():
    """The memo's defect: every model was hardcoded at Sonnet $3/$15,
    undercounting Opus. Opus input must now bill above Sonnet input."""
    opus_in, opus_out, _ = price_model("claude-opus-4-8")
    sonnet_in, sonnet_out, _ = price_model("claude-sonnet-4-6")
    assert (opus_in, opus_out) > (sonnet_in, sonnet_out)


def test_local_route_is_zero_dollars():
    assert price_model("local/qwen2.5-32b") == (0.0, 0.0, False)
    assert price_model("local/llama-3.3-70b") == (0.0, 0.0, False)


def test_unknown_models_fail_closed_at_the_highest_known_rate():
    for model in ("stub", "gpt-4o", "claude-opus-9", "qwen2.5-32b", ""):
        rate_in, rate_out, fail_closed = price_model(model)
        assert (rate_in, rate_out) == FAIL_CLOSED_RATE_PER_MTOK
        assert fail_closed is True


def test_unpriced_legacy_opus_fails_closed_at_its_true_rate():
    """claude-opus-4-1 is deliberately NOT in the table: legacy Opus was
    $15/$75 and a bare 'claude-opus-' prefix would bill it 3x low. Fail-closed
    happens to equal its true published rate — over-counting is impossible."""
    assert price_model("claude-opus-4-1") == (15.0, 75.0, True)


def test_fail_closed_rate_dominates_every_table_rate():
    """Fail-closed must be the ceiling — a new table row cheaper to miss than
    to hit would silently invert the fail-closed guarantee."""
    for _, (rate_in, rate_out) in MODEL_RATES_PER_MTOK:
        assert rate_in <= FAIL_CLOSED_RATE_PER_MTOK[0]
        assert rate_out <= FAIL_CLOSED_RATE_PER_MTOK[1]
