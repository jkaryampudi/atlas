"""P0 objective 7d (ADR-0018): the docker-compose `api` service must NOT publish
its unauthenticated port on all interfaces. Docker publishes `"8000:8000"` and
`"0.0.0.0:8000:8000"` on every host interface; only a `"127.0.0.1:8000:8000"`
host-interface prefix binds the API to loopback. Until authentication exists
(deliberately out of P0 scope), loopback-only publishing is the safety boundary.

Parsed textually (no YAML dependency in this environment) — the assertion is on
the api service's ports mapping for container port 8000."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker-compose.yml"


def _api_service_block(text: str) -> str:
    """The lines of the `api:` service block (2-space-indented service under
    `services:`), up to the next 2-space top-level key."""
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if re.match(r"^  api:\s*$", ln))
    out = []
    for ln in lines[start + 1:]:
        if re.match(r"^  \S", ln):            # next service / top-level key
            break
        out.append(ln)
    return "\n".join(out)


def test_api_port_8000_is_bound_to_loopback_not_all_interfaces():
    text = COMPOSE.read_text()
    api = _api_service_block(text)
    # every published mapping for container port 8000 in the api service
    mappings = re.findall(r'["\']?([0-9.:]*?:?8000:8000)["\']?', api)
    assert mappings, "api service must publish port 8000 (mapping not found)"
    for m in mappings:
        assert m.startswith("127.0.0.1:"), (
            f"api port 8000 mapping {m!r} publishes on all interfaces — it must "
            "be host-interface-bound to 127.0.0.1 (127.0.0.1:8000:8000)")
    # explicit negative: no all-interfaces form remains in the api block
    assert '"8000:8000"' not in api and "'8000:8000'" not in api
    assert "0.0.0.0:8000:8000" not in api
