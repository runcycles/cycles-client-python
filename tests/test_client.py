"""Tests for CyclesClient and AsyncCyclesClient."""


import httpx
import pytest

from runcycles.client import AsyncCyclesClient, CyclesClient
from runcycles.config import CyclesConfig
from runcycles.models import (
    Action,
    Amount,
    CommitRequest,
    DecisionRequest,
    EventCreateRequest,
    ReleaseRequest,
    ReservationCreateRequest,
    ReservationExtendRequest,
    Subject,
    Unit,
)


@pytest.fixture
def config() -> CyclesConfig:
    return CyclesConfig(base_url="http://localhost:7878", api_key="test-key")


class TestCyclesClientSync:
    def test_create_reservation_success(self, config: CyclesConfig, httpx_mock) -> None:  # type: ignore[no-untyped-def]
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:7878/v1/reservations",
            json={"decision": "ALLOW", "reservation_id": "res_123"},
            status_code=200,
        )

        with CyclesClient(config) as client:
            response = client.create_reservation(ReservationCreateRequest(
                idempotency_key="req-001",
                subject=Subject(tenant="acme"),
                action=Action(kind="llm.completion", name="gpt-4"),
                estimate=Amount(unit=Unit.USD_MICROCENTS, amount=1000),
            ))

        assert response.is_success
        assert response.get_body_attribute("decision") == "ALLOW"
        assert response.get_body_attribute("reservation_id") == "res_123"

    def test_create_reservation_denied(self, config: CyclesConfig, httpx_mock) -> None:  # type: ignore[no-untyped-def]
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:7878/v1/reservations",
            json={"error": "BUDGET_EXCEEDED", "message": "Insufficient budget"},
            status_code=409,
        )

        with CyclesClient(config) as client:
            response = client.create_reservation({"idempotency_key": "req-002", "subject": {"tenant": "acme"}})

        assert not response.is_success
        assert response.is_client_error
        assert response.status == 409

    def test_commit_reservation(self, config: CyclesConfig, httpx_mock) -> None:  # type: ignore[no-untyped-def]
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:7878/v1/reservations/res_123/commit",
            json={"status": "COMMITTED", "charged": {"unit": "USD_MICROCENTS", "amount": 800}},
            status_code=200,
        )

        with CyclesClient(config) as client:
            response = client.commit_reservation("res_123", CommitRequest(
                idempotency_key="commit-001",
                actual=Amount(unit=Unit.USD_MICROCENTS, amount=800),
            ))

        assert response.is_success
        assert response.get_body_attribute("status") == "COMMITTED"

    def test_release_reservation(self, config: CyclesConfig, httpx_mock) -> None:  # type: ignore[no-untyped-def]
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:7878/v1/reservations/res_123/release",
            json={"status": "RELEASED"},
            status_code=200,
        )

        with CyclesClient(config) as client:
            response = client.release_reservation("res_123", ReleaseRequest(
                idempotency_key="rel-001",
                reason="cancelled",
            ))

        assert response.is_success

    def test_extend_reservation(self, config: CyclesConfig, httpx_mock) -> None:  # type: ignore[no-untyped-def]
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:7878/v1/reservations/res_123/extend",
            json={"status": "ACTIVE", "expires_at_ms": 9999999},
            status_code=200,
        )

        with CyclesClient(config) as client:
            response = client.extend_reservation("res_123", ReservationExtendRequest(
                idempotency_key="ext-001",
                extend_by_ms=60000,
            ))

        assert response.is_success

    def test_decide(self, config: CyclesConfig, httpx_mock) -> None:  # type: ignore[no-untyped-def]
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:7878/v1/decide",
            json={"decision": "ALLOW"},
            status_code=200,
        )

        with CyclesClient(config) as client:
            response = client.decide(DecisionRequest(
                idempotency_key="dec-001",
                subject=Subject(tenant="acme"),
                action=Action(kind="llm.completion", name="gpt-4"),
                estimate=Amount(unit=Unit.USD_MICROCENTS, amount=1000),
            ))

        assert response.is_success

    def test_get_balances(self, config: CyclesConfig, httpx_mock) -> None:  # type: ignore[no-untyped-def]
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:7878/v1/balances?tenant=acme",
            json={"balances": []},
            status_code=200,
        )

        with CyclesClient(config) as client:
            response = client.get_balances(tenant="acme")

        assert response.is_success

    def test_list_reservations(self, config: CyclesConfig, httpx_mock) -> None:  # type: ignore[no-untyped-def]
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:7878/v1/reservations?tenant=acme",
            json={"reservations": [], "has_more": False},
            status_code=200,
        )

        with CyclesClient(config) as client:
            response = client.list_reservations(tenant="acme")

        assert response.is_success

    def test_list_reservations_with_from_to_window(self, config: CyclesConfig, httpx_mock) -> None:  # type: ignore[no-untyped-def]
        """`from`/`to` ISO-8601 window filter passthrough (cycles-protocol v0.1.25 revision 2026-05-21).

        `from` is a Python reserved keyword so callers must use the dict-unpack form
        `**{"from": ..., "to": ...}` rather than a direct kwarg. The client forwards
        the params byte-exactly to the URL query string."""
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:7878/v1/reservations?tenant=acme&from=2026-05-21T00%3A00%3A00Z&to=2026-05-22T00%3A00%3A00Z",
            json={"reservations": [], "has_more": False},
            status_code=200,
        )

        with CyclesClient(config) as client:
            response = client.list_reservations(
                **{
                    "tenant": "acme",
                    "from": "2026-05-21T00:00:00Z",
                    "to": "2026-05-22T00:00:00Z",
                }
            )

        assert response.is_success

    def test_list_reservations_with_expires_and_finalized_windows(self, config: CyclesConfig, httpx_mock) -> None:  # type: ignore[no-untyped-def]
        """`expires_*`/`finalized_*` ISO-8601 window-filter passthrough (cycles-protocol v0.1.25 revision 2026-05-22).

        Unlike `from`, these param names aren't Python reserved keywords, so callers
        can use direct kwargs. Test pins all four params on the wire."""
        httpx_mock.add_response(
            method="GET",
            url=(
                "http://localhost:7878/v1/reservations?tenant=acme"
                "&expires_from=2026-05-22T00%3A00%3A00Z"
                "&expires_to=2026-05-23T00%3A00%3A00Z"
                "&finalized_from=2026-05-15T00%3A00%3A00Z"
                "&finalized_to=2026-05-22T00%3A00%3A00Z"
            ),
            json={"reservations": [], "has_more": False},
            status_code=200,
        )

        with CyclesClient(config) as client:
            response = client.list_reservations(
                tenant="acme",
                expires_from="2026-05-22T00:00:00Z",
                expires_to="2026-05-23T00:00:00Z",
                finalized_from="2026-05-15T00:00:00Z",
                finalized_to="2026-05-22T00:00:00Z",
            )

        assert response.is_success

    def test_get_reservation(self, config: CyclesConfig, httpx_mock) -> None:  # type: ignore[no-untyped-def]
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:7878/v1/reservations/res_123",
            json={"reservation_id": "res_123", "status": "ACTIVE"},
            status_code=200,
        )

        with CyclesClient(config) as client:
            response = client.get_reservation("res_123")

        assert response.is_success

    def test_create_event(self, config: CyclesConfig, httpx_mock) -> None:  # type: ignore[no-untyped-def]
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:7878/v1/events",
            json={"status": "APPLIED", "event_id": "evt_123"},
            status_code=201,
        )

        with CyclesClient(config) as client:
            response = client.create_event(EventCreateRequest(
                idempotency_key="evt-001",
                subject=Subject(tenant="acme"),
                action=Action(kind="llm.completion", name="gpt-4"),
                actual=Amount(unit=Unit.USD_MICROCENTS, amount=500),
            ))

        assert response.is_success

    def test_transport_error(self, config: CyclesConfig, httpx_mock) -> None:  # type: ignore[no-untyped-def]
        httpx_mock.add_exception(httpx.ConnectError("Connection refused"))

        with CyclesClient(config) as client:
            response = client.create_reservation({"idempotency_key": "req-err"})

        assert response.is_transport_error
        assert response.status == -1

    def test_idempotency_header_set(self, config: CyclesConfig, httpx_mock) -> None:  # type: ignore[no-untyped-def]
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:7878/v1/reservations",
            json={"decision": "ALLOW"},
            status_code=200,
        )

        with CyclesClient(config) as client:
            client.create_reservation({"idempotency_key": "my-key", "subject": {"tenant": "acme"}})

        request = httpx_mock.get_request()
        assert request is not None
        assert request.headers.get("X-Idempotency-Key") == "my-key"
        assert request.headers.get("X-Cycles-API-Key") == "test-key"

    def test_get_balances_requires_filter(self, config: CyclesConfig) -> None:
        with CyclesClient(config) as client:
            with pytest.raises(ValueError, match="at least one subject filter"):
                client.get_balances()

    def test_get_balances_non_subject_params_rejected(self, config: CyclesConfig) -> None:
        with CyclesClient(config) as client:
            with pytest.raises(ValueError, match="at least one subject filter"):
                client.get_balances(limit="10")

    def test_response_headers_extracted(self, config: CyclesConfig, httpx_mock) -> None:  # type: ignore[no-untyped-def]
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:7878/v1/reservations",
            json={"decision": "ALLOW"},
            status_code=200,
            headers={"X-Request-Id": "req-abc", "X-RateLimit-Remaining": "42"},
        )

        with CyclesClient(config) as client:
            response = client.create_reservation({"idempotency_key": "hdr-test", "subject": {"tenant": "acme"}})

        assert response.request_id == "req-abc"
        assert response.rate_limit_remaining == 42

    def test_retry_after_header_on_429_limit_exceeded(self, config: CyclesConfig, httpx_mock) -> None:  # type: ignore[no-untyped-def]
        # Runtime spec v0.1.25.12: 429 rate-limit responses carry
        # error=LIMIT_EXCEEDED plus the Retry-After header (seconds).
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:7878/v1/reservations",
            json={"error": "LIMIT_EXCEEDED", "message": "Rate limited", "request_id": "req-rl"},
            status_code=429,
            headers={"Retry-After": "3", "X-RateLimit-Reset": "1700000000"},
        )

        with CyclesClient(config) as client:
            response = client.create_reservation({"idempotency_key": "rl-test", "subject": {"tenant": "acme"}})

        assert response.status == 429
        assert response.retry_after_ms_header == 3000
        assert response.rate_limit_reset == 1700000000
        error = response.get_error_response()
        assert error is not None
        assert error.error_code is not None
        assert error.error_code.value == "LIMIT_EXCEEDED"

    def test_dict_body(self, config: CyclesConfig, httpx_mock) -> None:  # type: ignore[no-untyped-def]
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:7878/v1/reservations",
            json={"decision": "ALLOW"},
            status_code=200,
        )

        with CyclesClient(config) as client:
            response = client.create_reservation({
                "idempotency_key": "raw-dict",
                "subject": {"tenant": "acme"},
                "estimate": {"unit": "USD_MICROCENTS", "amount": 1000},
            })

        assert response.is_success


class TestCyclesClientSyncEdgeCases:
    def test_unsupported_body_type_raises(self, config: CyclesConfig) -> None:
        with CyclesClient(config) as client:
            with pytest.raises(TypeError, match="Unsupported body type"):
                client.create_reservation("not-a-dict-or-model")  # type: ignore[arg-type]

    def test_get_transport_error(self, config: CyclesConfig, httpx_mock) -> None:  # type: ignore[no-untyped-def]
        httpx_mock.add_exception(httpx.ConnectError("Connection refused"))

        with CyclesClient(config) as client:
            response = client.list_reservations(tenant="acme")

        assert response.is_transport_error
        assert response.status == -1

    def test_json_parse_failure(self, config: CyclesConfig, httpx_mock) -> None:  # type: ignore[no-untyped-def]
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:7878/v1/reservations",
            content=b"not-json",
            status_code=200,
            headers={"content-type": "text/plain"},
        )

        with CyclesClient(config) as client:
            response = client.create_reservation({"idempotency_key": "bad-json"})

        assert response.is_success
        assert response.body == {}

    def test_json_parse_failure_error_response(self, config: CyclesConfig, httpx_mock) -> None:  # type: ignore[no-untyped-def]
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:7878/v1/reservations",
            content=b"internal error",
            status_code=500,
            headers={"content-type": "text/plain"},
        )

        with CyclesClient(config) as client:
            response = client.create_reservation({"idempotency_key": "bad-json-err"})

        assert response.is_server_error


@pytest.mark.asyncio
class TestAsyncCyclesClient:
    async def test_create_reservation_success(self, config: CyclesConfig, httpx_mock) -> None:  # type: ignore[no-untyped-def]
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:7878/v1/reservations",
            json={"decision": "ALLOW", "reservation_id": "res_456"},
            status_code=200,
        )

        async with AsyncCyclesClient(config) as client:
            response = await client.create_reservation(ReservationCreateRequest(
                idempotency_key="async-001",
                subject=Subject(tenant="acme"),
                action=Action(kind="llm.completion", name="gpt-4"),
                estimate=Amount(unit=Unit.USD_MICROCENTS, amount=1000),
            ))

        assert response.is_success
        assert response.get_body_attribute("reservation_id") == "res_456"

    async def test_commit_reservation(self, config: CyclesConfig, httpx_mock) -> None:  # type: ignore[no-untyped-def]
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:7878/v1/reservations/res_a1/commit",
            json={"status": "COMMITTED", "charged": {"unit": "USD_MICROCENTS", "amount": 800}},
            status_code=200,
        )

        async with AsyncCyclesClient(config) as client:
            response = await client.commit_reservation("res_a1", CommitRequest(
                idempotency_key="ac-001",
                actual=Amount(unit=Unit.USD_MICROCENTS, amount=800),
            ))

        assert response.is_success
        assert response.get_body_attribute("status") == "COMMITTED"

    async def test_release_reservation(self, config: CyclesConfig, httpx_mock) -> None:  # type: ignore[no-untyped-def]
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:7878/v1/reservations/res_a1/release",
            json={"status": "RELEASED", "released": {"unit": "USD_MICROCENTS", "amount": 1000}},
            status_code=200,
        )

        async with AsyncCyclesClient(config) as client:
            response = await client.release_reservation("res_a1", ReleaseRequest(
                idempotency_key="ar-001", reason="done",
            ))

        assert response.is_success

    async def test_extend_reservation(self, config: CyclesConfig, httpx_mock) -> None:  # type: ignore[no-untyped-def]
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:7878/v1/reservations/res_a1/extend",
            json={"status": "ACTIVE", "expires_at_ms": 9999999},
            status_code=200,
        )

        async with AsyncCyclesClient(config) as client:
            response = await client.extend_reservation("res_a1", ReservationExtendRequest(
                idempotency_key="ae-001", extend_by_ms=60000,
            ))

        assert response.is_success

    async def test_decide(self, config: CyclesConfig, httpx_mock) -> None:  # type: ignore[no-untyped-def]
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:7878/v1/decide",
            json={"decision": "ALLOW"},
            status_code=200,
        )

        async with AsyncCyclesClient(config) as client:
            response = await client.decide(DecisionRequest(
                idempotency_key="ad-001",
                subject=Subject(tenant="acme"),
                action=Action(kind="llm.completion", name="gpt-4"),
                estimate=Amount(unit=Unit.USD_MICROCENTS, amount=1000),
            ))

        assert response.is_success

    async def test_list_reservations(self, config: CyclesConfig, httpx_mock) -> None:  # type: ignore[no-untyped-def]
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:7878/v1/reservations?tenant=acme",
            json={"reservations": [], "has_more": False},
            status_code=200,
        )

        async with AsyncCyclesClient(config) as client:
            response = await client.list_reservations(tenant="acme")

        assert response.is_success

    async def test_list_reservations_with_from_to_window(self, config: CyclesConfig, httpx_mock) -> None:  # type: ignore[no-untyped-def]
        """Async parity for the `from`/`to` window-filter passthrough.

        See sync sibling for the reserved-keyword caveat on `from`."""
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:7878/v1/reservations?tenant=acme&from=2026-05-21T00%3A00%3A00Z&to=2026-05-22T00%3A00%3A00Z",
            json={"reservations": [], "has_more": False},
            status_code=200,
        )

        async with AsyncCyclesClient(config) as client:
            response = await client.list_reservations(
                **{
                    "tenant": "acme",
                    "from": "2026-05-21T00:00:00Z",
                    "to": "2026-05-22T00:00:00Z",
                }
            )

        assert response.is_success

    async def test_list_reservations_with_expires_and_finalized_windows(self, config: CyclesConfig, httpx_mock) -> None:  # type: ignore[no-untyped-def]
        """Async parity for the `expires_*`/`finalized_*` window-filter passthrough.

        See sync sibling for the reserved-keyword note on `from`; the new
        v0.1.25.22 params use plain kwargs."""
        httpx_mock.add_response(
            method="GET",
            url=(
                "http://localhost:7878/v1/reservations?tenant=acme"
                "&expires_from=2026-05-22T00%3A00%3A00Z"
                "&expires_to=2026-05-23T00%3A00%3A00Z"
                "&finalized_from=2026-05-15T00%3A00%3A00Z"
                "&finalized_to=2026-05-22T00%3A00%3A00Z"
            ),
            json={"reservations": [], "has_more": False},
            status_code=200,
        )

        async with AsyncCyclesClient(config) as client:
            response = await client.list_reservations(
                tenant="acme",
                expires_from="2026-05-22T00:00:00Z",
                expires_to="2026-05-23T00:00:00Z",
                finalized_from="2026-05-15T00:00:00Z",
                finalized_to="2026-05-22T00:00:00Z",
            )

        assert response.is_success

    async def test_get_reservation(self, config: CyclesConfig, httpx_mock) -> None:  # type: ignore[no-untyped-def]
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:7878/v1/reservations/res_a1",
            json={"reservation_id": "res_a1", "status": "ACTIVE"},
            status_code=200,
        )

        async with AsyncCyclesClient(config) as client:
            response = await client.get_reservation("res_a1")

        assert response.is_success

    async def test_get_balances(self, config: CyclesConfig, httpx_mock) -> None:  # type: ignore[no-untyped-def]
        httpx_mock.add_response(
            method="GET",
            url="http://localhost:7878/v1/balances?tenant=acme",
            json={"balances": []},
            status_code=200,
        )

        async with AsyncCyclesClient(config) as client:
            response = await client.get_balances(tenant="acme")

        assert response.is_success

    async def test_get_balances_requires_filter(self, config: CyclesConfig) -> None:
        async with AsyncCyclesClient(config) as client:
            with pytest.raises(ValueError, match="at least one subject filter"):
                await client.get_balances()

    async def test_create_event(self, config: CyclesConfig, httpx_mock) -> None:  # type: ignore[no-untyped-def]
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:7878/v1/events",
            json={"status": "APPLIED", "event_id": "evt_a1"},
            status_code=201,
        )

        async with AsyncCyclesClient(config) as client:
            response = await client.create_event(EventCreateRequest(
                idempotency_key="aevt-001",
                subject=Subject(tenant="acme"),
                action=Action(kind="llm.completion", name="gpt-4"),
                actual=Amount(unit=Unit.USD_MICROCENTS, amount=500),
            ))

        assert response.is_success

    async def test_transport_error(self, config: CyclesConfig, httpx_mock) -> None:  # type: ignore[no-untyped-def]
        httpx_mock.add_exception(httpx.ConnectError("Connection refused"))

        async with AsyncCyclesClient(config) as client:
            response = await client.create_reservation({"idempotency_key": "async-err"})

        assert response.is_transport_error

    async def test_get_transport_error(self, config: CyclesConfig, httpx_mock) -> None:  # type: ignore[no-untyped-def]
        httpx_mock.add_exception(httpx.ConnectError("Connection refused"))

        async with AsyncCyclesClient(config) as client:
            response = await client.list_reservations(tenant="acme")

        assert response.is_transport_error

    async def test_json_parse_failure(self, config: CyclesConfig, httpx_mock) -> None:  # type: ignore[no-untyped-def]
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:7878/v1/reservations",
            content=b"not-json",
            status_code=200,
            headers={"content-type": "text/plain"},
        )

        async with AsyncCyclesClient(config) as client:
            response = await client.create_reservation({"idempotency_key": "bad"})

        assert response.is_success
        assert response.body == {}

    async def test_idempotency_header_set(self, config: CyclesConfig, httpx_mock) -> None:  # type: ignore[no-untyped-def]
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:7878/v1/reservations",
            json={"decision": "ALLOW"},
            status_code=200,
        )

        async with AsyncCyclesClient(config) as client:
            await client.create_reservation({"idempotency_key": "async-key", "subject": {"tenant": "acme"}})

        request = httpx_mock.get_request()
        assert request is not None
        assert request.headers.get("X-Idempotency-Key") == "async-key"

    async def test_error_response_with_body(self, config: CyclesConfig, httpx_mock) -> None:  # type: ignore[no-untyped-def]
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:7878/v1/reservations",
            json={"error": "BUDGET_EXCEEDED", "message": "No budget"},
            status_code=409,
        )

        async with AsyncCyclesClient(config) as client:
            response = await client.create_reservation({"idempotency_key": "err-async"})

        assert response.is_client_error
        assert response.status == 409

    async def test_error_response_no_body(self, config: CyclesConfig, httpx_mock) -> None:  # type: ignore[no-untyped-def]
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:7878/v1/reservations",
            content=b"not-json",
            status_code=500,
            headers={"content-type": "text/plain"},
        )

        async with AsyncCyclesClient(config) as client:
            response = await client.create_reservation({"idempotency_key": "err-no-body"})

        assert response.is_server_error

    async def test_response_headers_extracted(self, config: CyclesConfig, httpx_mock) -> None:  # type: ignore[no-untyped-def]
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:7878/v1/reservations",
            json={"decision": "ALLOW"},
            status_code=200,
            headers={"X-Request-Id": "async-req-id", "X-Cycles-Tenant": "acme"},
        )

        async with AsyncCyclesClient(config) as client:
            response = await client.create_reservation({"idempotency_key": "hdr-async"})

        assert response.request_id == "async-req-id"
        assert response.cycles_tenant == "acme"
