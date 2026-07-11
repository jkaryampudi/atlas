from dataclasses import replace
from datetime import UTC, datetime

import pytest

from atlas.core.audit import ChainBuilder, ChainVerificationError, verify_chain


def _build(n: int = 5) -> ChainBuilder:
    b = ChainBuilder()
    t = datetime(2026, 7, 10, 6, 0, tzinfo=UTC)
    for i in range(n):
        b.append(event_type="signal.generated", entity_type="signal", entity_id=f"s{i}",
                 actor_type="dcp", actor_id="signal_engine",
                 payload={"score": i, "symbol": "AVGO"}, created_at=t)
    return b


def test_valid_chain_verifies():
    b = _build(5)
    assert verify_chain(b.events) == 5


def test_payload_tamper_detected():
    events = _build(5).events
    tampered = replace(events[2], payload={"score": 999, "symbol": "AVGO"})
    events[2] = tampered
    with pytest.raises(ChainVerificationError) as e:
        verify_chain(events)
    assert e.value.seq == 3 and "payload hash" in e.value.reason


def test_deletion_detected_via_link_break():
    events = _build(5).events
    del events[1]
    with pytest.raises(ChainVerificationError):
        verify_chain(events)


def test_unknown_actor_rejected():
    b = ChainBuilder()
    with pytest.raises(ValueError):
        b.append(event_type="x", entity_type="x", entity_id="x",
                 actor_type="hacker", actor_id="x", payload={},
                 created_at=datetime(2026, 7, 10, tzinfo=UTC))


def test_canonicalisation_key_order_irrelevant():
    t = datetime(2026, 7, 10, tzinfo=UTC)
    b1, b2 = ChainBuilder(), ChainBuilder()
    e1 = b1.append(event_type="t", entity_type="t", entity_id="1", actor_type="dcp",
                   actor_id="a", payload={"a": 1, "b": 2}, created_at=t)
    e2 = b2.append(event_type="t", entity_type="t", entity_id="1", actor_type="dcp",
                   actor_id="a", payload={"b": 2, "a": 1}, created_at=t)
    assert e1.payload_hash == e2.payload_hash


def test_benign_serial_gaps_tolerated():
    """Rolled-back txns consume sequence numbers; the chain must still verify."""
    from dataclasses import replace
    b = _build(3)
    events = [replace(e, seq=e.seq * 10) for e in b.events]  # 10, 20, 30
    assert verify_chain(events) == 3
