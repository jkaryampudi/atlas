"""Make ATLAS_TEST_DATABASE_URL fully honored for pg-backed constitution tests.

Same shim as tests/integration/conftest.py (see its docstring for the full
rationale): tests/conftest.py derives URL from ATLAS_TEST_DATABASE_URL but
pins TEST_DB_NAME to 'atlas_test'; concurrent agent sessions run against
their OWN throwaway test databases and this aligns the create/migrate/guard
name with the URL's basename. Constitution tests are pg-backed (clean_audit),
so they need the shim when this directory is run in isolation — in a full
suite run the integration copy already applied the identical mutation, and
re-applying is a no-op. Without the env var this is byte-for-byte the old
behavior, and the override never leaves the 'atlas_test*' namespace.
"""
from __future__ import annotations

import tests.conftest as _base

_name = _base.URL.rsplit("/", 1)[1]
if _name != _base.TEST_DB_NAME and _name.startswith("atlas_test"):
    _base.TEST_DB_NAME = _name
