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

# The unit-test conftest seeds dummy creds via `os.environ.setdefault` so
# unit tests can import `core.settings` cleanly. Those dummies must NOT win
# over either real values from `.env` or values passed on the command line.
#
# Strategy: if the MAXIMO_URL we currently see is the unit-test dummy,
# wipe it (and the matching dummy creds) so the upcoming `load_dotenv` can
# populate them from the real `.env`. Then call `load_dotenv(override=False)`
# so anything the operator explicitly set on the command line still wins.
_UNIT_TEST_DUMMY_URL = "https://test-maximo.example.com/maximo/oslc"
if os.environ.get("MAXIMO_URL") == _UNIT_TEST_DUMMY_URL:
    for k in ("MAXIMO_URL", "MAXIMO_HOST", "MAXIMO_USERNAME", "MAXIMO_PASSWORD"):
        os.environ.pop(k, None)

load_dotenv(override=False)

# Smoke tests legitimately need admin role (e.g. detect_asset_anomalies
# requires supervisor+). Default the env-based identity to admin unless the
# operator explicitly set something else.
os.environ.setdefault("CURRENT_USER_ROLE", "admin")


@pytest.fixture(scope="session", autouse=True)
def _skip_if_no_maximo_creds() -> None:
    """Skip every test in this directory unless MAXIMO_URL is configured."""
    if not os.environ.get("MAXIMO_URL"):
        pytest.skip(
            "Integration suite skipped — set MAXIMO_URL / MAXIMO_USERNAME / "
            "MAXIMO_PASSWORD (or a .env file) to enable.",
            allow_module_level=True,
        )
