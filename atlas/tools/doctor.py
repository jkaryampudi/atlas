"""atlas doctor — one command that checks the whole local environment and says
exactly what's wrong and how to fix it. Run: python -m atlas.tools.doctor"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys


def _ok(label: str) -> None:
    print(f"  ✓ {label}")


def _bad(label: str, fix: str) -> None:
    print(f"  ✗ {label}\n    → fix: {fix}")


def main() -> int:
    problems = 0
    print("atlas doctor\n")

    if sys.version_info >= (3, 12):
        _ok(f"python {sys.version.split()[0]}")
    else:
        _bad(f"python {sys.version.split()[0]} too old", "use Python >= 3.12")
        problems += 1

    if os.environ.get("VIRTUAL_ENV"):
        _ok("virtualenv active")
    else:
        _bad("virtualenv not active", "source .venv/bin/activate")
        problems += 1

    if shutil.which("docker"):
        r = subprocess.run(["docker", "ps", "--format", "{{.Names}}"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            _bad("docker daemon not running", "open Docker Desktop and wait for the whale")
            problems += 1
        elif "atlas-db-1" in r.stdout:
            _ok("atlas-db-1 container up")
        else:
            _bad("atlas-db-1 not running", "docker compose up -d db redis")
            problems += 1
    else:
        _bad("docker not installed", "install Docker Desktop")
        problems += 1

    url = os.environ.get("ATLAS_DATABASE_URL", "")
    if url:
        _ok("ATLAS_DATABASE_URL set")
        try:
            from sqlalchemy import create_engine, text
            with create_engine(url).connect() as c:
                n = c.execute(text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema IN ('market','risk','trading','audit',"
                    "'learning','quant','research')")).scalar()
            if n and n >= 16:
                _ok(f"postgres reachable, {n} atlas tables (migrations applied)")
            else:
                _bad(f"postgres reachable but only {n} tables", "alembic upgrade head")
                problems += 1
        except Exception as e:
            _bad(f"postgres unreachable ({type(e).__name__})",
                 "docker compose up -d db && wait for healthy, check the URL")
            problems += 1
    else:
        _bad("ATLAS_DATABASE_URL not set",
             'export ATLAS_DATABASE_URL="postgresql+psycopg://atlas:'
             'atlas_local_only@localhost:5432/atlas"')
        problems += 1

    if os.environ.get("ATLAS_EODHD_API_KEY") or os.path.exists(".env"):
        _ok("EODHD key present (or .env exists)")
    else:
        print("  · EODHD key not set (fine for fixtures; needed for real backfill)")

    print(f"\n{'all clear — run pytest' if problems == 0 else f'{problems} problem(s) above'}")
    return 0 if problems == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
