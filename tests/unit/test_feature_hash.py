"""Pure hash pins for the feature store (no DB): the dataset_version recipe
and the code_sha pin behave deterministically and detect every drift they
exist to detect."""
from __future__ import annotations

from atlas.dcp.features.definitions import MOMENTUM_12_1, SUE_FOS
from atlas.dcp.features.store import FeatureDefinition, dataset_version_for


def _feature(tmp_path, name="hash_probe", version="1.0.0", files=("a.py",)):
    paths = []
    for f in files:
        p = tmp_path / f
        if not p.exists():
            p.write_text(f"# {f}\n")
        paths.append(p)
    return FeatureDefinition(
        name=name, version=version, market="US", spec={},
        code_paths=tuple(paths),
        compute=lambda db, sym, iid, sessions: {},
        input_extent=lambda db, syms, end: {"symbols": {}})


EXTENT = {"symbols": {"AAA": {"bars": {"min": "2024-01-02",
                                       "max": "2025-05-30", "rows": 353}}}}


def test_dataset_version_is_deterministic_and_key_order_free(tmp_path):
    feat = _feature(tmp_path)
    v1 = dataset_version_for(feat, EXTENT)
    reordered = {"symbols": {"AAA": {"bars": {"rows": 353,
                                              "max": "2025-05-30",
                                              "min": "2024-01-02"}}}}
    assert dataset_version_for(feat, reordered) == v1
    assert len(v1) == 64 and int(v1, 16) >= 0          # full sha256 hex


def test_dataset_version_changes_with_extent_and_identity(tmp_path):
    feat = _feature(tmp_path)
    v1 = dataset_version_for(feat, EXTENT)
    grown = {"symbols": {"AAA": {"bars": {"min": "2024-01-02",
                                          "max": "2025-05-30", "rows": 354}}}}
    assert dataset_version_for(feat, grown) != v1       # new data, new vintage
    other = _feature(tmp_path, name="hash_probe_2")
    assert dataset_version_for(other, EXTENT) != v1     # feature in the hash
    bumped = _feature(tmp_path, version="1.0.1")
    assert dataset_version_for(bumped, EXTENT) != v1    # version in the hash


def test_code_sha_pins_file_bytes_and_order(tmp_path):
    feat = _feature(tmp_path, files=("a.py", "b.py"))
    sha = feat.code_sha()
    assert sha == feat.code_sha()                       # deterministic
    swapped = _feature(tmp_path, files=("b.py", "a.py"))
    assert swapped.code_sha() != sha                    # order is declared
    (tmp_path / "a.py").write_text("# a.py drifted\n")
    assert feat.code_sha() != sha                       # any byte counts


def test_registered_definitions_pin_their_signal_sources():
    """The v1 definitions hash the modules the math is imported from — the
    prompts-are-code pin the equivalence tests rely on."""
    momentum_files = {p.name for p in MOMENTUM_12_1.code_paths}
    assert momentum_files == {"momentum.py", "v1.py", "adjustment.py"}
    sue_files = {p.name for p in SUE_FOS.code_paths}
    assert sue_files == {"sue.py", "v1.py"}
    for feat in (MOMENTUM_12_1, SUE_FOS):
        assert all(p.exists() for p in feat.code_paths)
        assert len(feat.code_sha()) == 64
