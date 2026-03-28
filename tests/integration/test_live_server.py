"""
Integration tests against a live Cycles server.
Skipped unless CYCLES_BASE_URL is set.
"""
import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("CYCLES_BASE_URL"),
    reason="CYCLES_BASE_URL not set -- skipping live server tests"
)

# These will be fleshed out once the nightly workflow is configured with secrets


def test_health_check():
    """Server responds to health endpoint."""
    import urllib.request
    base = os.environ["CYCLES_BASE_URL"]
    req = urllib.request.Request(f"{base}/actuator/health")
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200


def test_reservation_lifecycle():
    """Create, commit, and verify a reservation."""
    # TODO: implement once API key provisioning is set up
    pass


def test_decide_endpoint():
    """POST /v1/decide returns a valid decision."""
    # TODO: implement
    pass


def test_balance_query():
    """GET /v1/balances returns balance data."""
    # TODO: implement
    pass
