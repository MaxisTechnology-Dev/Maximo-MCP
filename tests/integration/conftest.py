"""
tests/integration/conftest.py — Shared fixtures for live-server smoke tests.

These tests connect to a real Maximo instance. Set the usual MAXIMO_URL /
MAXIMO_HOST / MAXIMO_USERNAME / MAXIMO_PASSWORD env vars (or put them in a
.env file at the project root) before running. Without those, the suite is
skipped automatically rather than failing.

Run:
    pytest tests/integration -m integration -v
or directly as a script:
    python tests/integration/test_smoke_wave1.py
"""
from __future__ import annotations

import os
import sys

import pytest
from dotenv import load_dotenv

# Add project root to sys.path so imports like `from tools import ...` work
# regardless of how pytest is invoked.
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# NOTE: do NOT mutate os.environ at module-load. pytest imports every
# conftest in the test tree during collection, so anything we do here would
# fire even for unit-only runs and clobber the dummy creds the parent
# `tests/conftest.py` set via `os.environ.setdefault`. Instead we keep all
# env-mutation inside the session-scoped autouse fixture below, which only
# runs when a test in THIS directory is about to execute.

_UNIT_TEST_DUMMY_URL = "https://test-maximo.example.com/maximo/oslc"


@pytest.fixture(scope="session", autouse=True)
def _maximo_creds_for_integration() -> None:
    """
    For integration tests only:

    1. If MAXIMO_URL is the unit-test dummy that `tests/conftest.py` seeded,
       wipe the dummy quad (URL/HOST/USERNAME/PASSWORD) so `.env` can repopulate.
    2. Load `.env` as a default — values already on the env (e.g. CI secrets,
       command-line) still win because of `override=False`.
    3. Default `CURRENT_USER_ROLE=admin` so role-gated tools (e.g.
       detect_asset_anomalies, get_compliance_dashboard) don't RBAC-deny.
    4. Skip every integration test if MAXIMO_URL still isn't set after step 2 —
       i.e. no real Maximo configured, just don't run the suite.
    """
    if os.environ.get("MAXIMO_URL") == _UNIT_TEST_DUMMY_URL:
        for k in ("MAXIMO_URL", "MAXIMO_HOST", "MAXIMO_USERNAME", "MAXIMO_PASSWORD"):
            os.environ.pop(k, None)

    load_dotenv(override=False)
    os.environ.setdefault("CURRENT_USER_ROLE", "admin")

    if not os.environ.get("MAXIMO_URL"):
        pytest.skip(
            "Integration suite skipped — set MAXIMO_URL / MAXIMO_USERNAME / "
            "MAXIMO_PASSWORD (or a .env file) to enable.",
            allow_module_level=True,
        )
