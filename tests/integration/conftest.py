"""Make ATLAS_TEST_DATABASE_URL fully honored for pg-backed tests.

tests/conftest.py derives its connection URL from ATLAS_TEST_DATABASE_URL but
pins TEST_DB_NAME to 'atlas_test' — the name used both to CREATE the database
on demand and as the destructive-fixture guard. Concurrent agent sessions run
against their OWN throwaway test databases (e.g. atlas_test_polish) so they
never truncate each other's fixtures; this shim aligns the module's
TEST_DB_NAME with the URL's basename so _ensure_test_db creates/migrates the
right database and _assert_test_db guards the right name.

Safety is preserved, not weakened: the override only applies when the basename
still starts with 'atlas_test' — the guard can never be pointed at the dev
database or anything else. Without the env var this is byte-for-byte the old
behavior. (A nested conftest is used because tests/conftest.py is shared
infrastructure owned by other in-flight work; only pg-backed tests live under
tests/integration, so the shim loads before every fixture that needs it.)
"""
from __future__ import annotations

import tests.conftest as _base

_name = _base.URL.rsplit("/", 1)[1]
if _name != _base.TEST_DB_NAME and _name.startswith("atlas_test"):
    _base.TEST_DB_NAME = _name
