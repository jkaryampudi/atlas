"""Operator alerting: a failure must find the Principal, not wait to be found.

Transport is a plain ntfy-style webhook (POST body = message, Title header):
set ATLAS_ALERT_URL to e.g. https://ntfy.sh/<your-private-topic> and install
the ntfy app, or point it at any webhook receiver. With no URL configured,
alerts degrade to stderr — visible in the launchd log, never lost silently,
never an exception: alerting failures must not take the pipeline down with
them (the pipeline's own exit code is the ground truth the scheduler sees).
"""
from __future__ import annotations

import os
import sys

import httpx

_TIMEOUT = 10.0


def notify(title: str, message: str, *, priority: str = "default") -> bool:
    """Best-effort push to the operator. Returns True only on confirmed
    delivery; NEVER raises."""
    url = os.environ.get("ATLAS_ALERT_URL", "").strip()
    line = f"[atlas-alert] {title}: {message}"
    if not url:
        print(line + " (ATLAS_ALERT_URL unset — stderr only)", file=sys.stderr)
        return False
    try:
        r = httpx.post(url, content=message.encode(),
                       headers={"Title": title, "Priority": priority},
                       timeout=_TIMEOUT)
        if r.status_code // 100 == 2:
            return True
        print(f"{line} (webhook {r.status_code})", file=sys.stderr)
        return False
    except Exception as e:  # noqa: BLE001 — alerting must never crash the caller
        print(f"{line} (webhook unreachable: {e})", file=sys.stderr)
        return False
