"""P0/P0.1 (ADR-0018): NO docker-compose service may publish its port on all
interfaces. Docker publishes `"PORT:PORT"` and `"0.0.0.0:PORT:PORT"` on every
host interface; only a `"127.0.0.1:PORT:PORT"` host-interface prefix binds to
loopback. The API (8000), PostgreSQL (5432), and Redis (6379) must all be
loopback-bound — the unauthenticated API and the local-only DB must never be
reachable off-host.

Parsed textually (no YAML dependency in this environment). This is a STATIC
config-parse test: it proves the compose file declares loopback bindings, NOT
that a running container actually refuses off-host connections. In an
environment with Docker, RUNTIME SOCKET PROBING remains required — bring the
stack up and confirm each port answers on 127.0.0.1 but not on the host's LAN
address (this test cannot and does not substitute for that)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker-compose.yml"


def _service_block(text: str, name: str) -> str:
    """The lines of the `<name>:` service block (2-space-indented service under
    `services:`), up to the next 2-space top-level key."""
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines)
                 if re.match(rf"^  {re.escape(name)}:\s*$", ln))
    out = []
    for ln in lines[start + 1:]:
        if re.match(r"^  \S", ln):            # next service / top-level key
            break
        out.append(ln)
    return "\n".join(out)


@pytest.mark.parametrize("service, port", [
    ("api", 8000), ("db", 5432), ("redis", 6379)])
def test_published_port_is_bound_to_loopback_not_all_interfaces(service, port):
    text = COMPOSE.read_text()
    block = _service_block(text, service)
    mappings = re.findall(rf'["\']?([0-9.:]*?:?{port}:{port})["\']?', block)
    assert mappings, f"{service} must publish port {port} (mapping not found)"
    for m in mappings:
        assert m.startswith("127.0.0.1:"), (
            f"{service} port {port} mapping {m!r} publishes on all interfaces — "
            f"it must be host-interface-bound (127.0.0.1:{port}:{port})")
    # explicit negatives: no all-interfaces form remains in the block
    assert f'"{port}:{port}"' not in block and f"'{port}:{port}'" not in block
    assert f"0.0.0.0:{port}:{port}" not in block


def test_no_service_binds_a_bare_all_interfaces_port():
    """Belt-and-suspenders across the whole file: no `"NNNN:NNNN"` bare mapping
    (all-interfaces) survives anywhere in the compose services."""
    text = COMPOSE.read_text()
    bare = re.findall(r'"\d{2,5}:\d{2,5}"', text)
    assert bare == [], f"bare all-interfaces port mapping(s) present: {bare}"
