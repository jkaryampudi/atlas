"""F-017: the deterministic replay harness must refuse to write fixtures into a
non-disposable (production) database. Unit-level — no real DB needed."""
from __future__ import annotations

import pytest

from atlas.dcp.market_data.replay import assert_disposable_db


class _FakeScalar:
    def __init__(self, name: str) -> None:
        self._name = name

    def scalar_one(self) -> str:
        return self._name


class _FakeSession:
    def __init__(self, name: str) -> None:
        self._name = name

    def execute(self, *_a, **_k) -> _FakeScalar:
        return _FakeScalar(self._name)


@pytest.mark.parametrize("dbname", ["atlas_test", "atlas_test_xyz", "atlas_replay", "replay_box"])
def test_disposable_names_are_allowed(dbname: str) -> None:
    assert_disposable_db(_FakeSession(dbname), force=False)   # no raise


@pytest.mark.parametrize("dbname", ["atlas", "atlas_prod", "production", "postgres"])
def test_production_names_are_refused_fail_closed(dbname: str) -> None:
    with pytest.raises(SystemExit):
        assert_disposable_db(_FakeSession(dbname), force=False)


def test_force_overrides_the_guard() -> None:
    assert_disposable_db(_FakeSession("atlas"), force=True)   # deliberate override
