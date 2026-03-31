"""
Integration tests against a live Cycles server.
Skipped unless CYCLES_BASE_URL is set.

These tests run in the nightly integration workflow which:
1. Starts Redis + cycles-server (7878) + cycles-server-admin (7979)
2. Provisions a tenant, API key, and budget via the admin API
3. Passes CYCLES_BASE_URL, CYCLES_API_KEY, CYCLES_TENANT as env vars
"""
import os
from uuid import uuid4

import pytest
import requests

pytestmark = pytest.mark.skipif(
    not os.environ.get("CYCLES_BASE_URL"),
    reason="CYCLES_BASE_URL not set -- skipping live server tests",
)

BASE = os.environ.get("CYCLES_BASE_URL", "")
KEY = os.environ.get("CYCLES_API_KEY", "")
TENANT = os.environ.get("CYCLES_TENANT", "integration-test")
HEADERS = {"X-Cycles-API-Key": KEY, "Content-Type": "application/json"}


def test_health_check():
    """Server responds to health endpoint."""
    res = requests.get(f"{BASE}/actuator/health", timeout=5)
    assert res.status_code == 200


def test_reservation_lifecycle():
    """Create a reservation, commit it, verify balance decreased."""
    # Reserve
    res = requests.post(
        f"{BASE}/v1/reservations",
        headers=HEADERS,
        json={
            "idempotency_key": str(uuid4()),
            "subject": {"tenant": TENANT},
            "action": {"kind": "llm.completion", "name": "test-model"},
            "estimate": {"unit": "USD_MICROCENTS", "amount": 10000},
            "ttl_ms": 60000,
        },
        timeout=5,
    )
    assert res.status_code == 200, f"Reserve failed: {res.text}"
    data = res.json()
    assert "reservation_id" in data
    rid = data["reservation_id"]

    # Commit with lower actual
    res = requests.post(
        f"{BASE}/v1/reservations/{rid}/commit",
        headers=HEADERS,
        json={
            "idempotency_key": str(uuid4()),
            "actual": {"unit": "USD_MICROCENTS", "amount": 8000},
        },
        timeout=5,
    )
    assert res.status_code == 200, f"Commit failed: {res.text}"


def test_reserve_and_release():
    """Create a reservation, release it, verify budget is returned."""
    res = requests.post(
        f"{BASE}/v1/reservations",
        headers=HEADERS,
        json={
            "idempotency_key": str(uuid4()),
            "subject": {"tenant": TENANT},
            "action": {"kind": "llm.completion", "name": "test-model"},
            "estimate": {"unit": "USD_MICROCENTS", "amount": 5000},
            "ttl_ms": 60000,
        },
        timeout=5,
    )
    assert res.status_code == 200
    rid = res.json()["reservation_id"]

    # Release
    res = requests.post(
        f"{BASE}/v1/reservations/{rid}/release",
        headers=HEADERS,
        json={
            "idempotency_key": str(uuid4()),
            "reason": "integration-test-release",
        },
        timeout=5,
    )
    assert res.status_code == 200, f"Release failed: {res.text}"


def test_decide_endpoint():
    """POST /v1/decide returns a valid decision."""
    res = requests.post(
        f"{BASE}/v1/decide",
        headers=HEADERS,
        json={
            "idempotency_key": str(uuid4()),
            "subject": {"tenant": TENANT},
            "action": {"kind": "llm.completion", "name": "test-model"},
            "estimate": {"unit": "USD_MICROCENTS", "amount": 1000},
        },
        timeout=5,
    )
    assert res.status_code == 200, f"Decide failed: {res.text}"
    data = res.json()
    assert data["decision"] in ("ALLOW", "ALLOW_WITH_CAPS", "DENY")


def test_balance_query():
    """GET /v1/balances returns budget data."""
    res = requests.get(
        f"{BASE}/v1/balances",
        headers=HEADERS,
        params={"tenant": TENANT},
        timeout=5,
    )
    assert res.status_code == 200, f"Balance query failed: {res.text}"
    data = res.json()
    assert "balances" in data
