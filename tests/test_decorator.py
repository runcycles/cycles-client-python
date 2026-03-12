"""Tests for the @cycles decorator."""

import pytest

from runcycles.client import AsyncCyclesClient, CyclesClient
from runcycles.config import CyclesConfig
from runcycles.context import get_cycles_context
from runcycles.decorator import cycles, set_default_client
from runcycles.exceptions import BudgetExceededError, CyclesProtocolError


@pytest.fixture
def config() -> CyclesConfig:
    return CyclesConfig(base_url="http://localhost:7878", api_key="test-key", tenant="acme")


class TestCyclesDecoratorSync:
    def test_basic_lifecycle(self, config: CyclesConfig, httpx_mock) -> None:  # type: ignore[no-untyped-def]
        # Mock reservation creation
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:7878/v1/reservations",
            json={
                "decision": "ALLOW",
                "reservation_id": "res_dec_1",
                "expires_at_ms": 9999999999,
                "affected_scopes": ["tenant:acme"],
                "scope_path": "tenant:acme",
                "reserved": {"unit": "USD_MICROCENTS", "amount": 1000},
            },
            status_code=200,
        )
        # Mock commit
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:7878/v1/reservations/res_dec_1/commit",
            json={"status": "COMMITTED", "charged": {"unit": "USD_MICROCENTS", "amount": 1000}},
            status_code=200,
        )

        client = CyclesClient(config)

        @cycles(estimate=1000, client=client)
        def my_func(x: int) -> str:
            ctx = get_cycles_context()
            assert ctx is not None
            assert ctx.reservation_id == "res_dec_1"
            return f"result-{x}"

        result = my_func(42)
        assert result == "result-42"
        client.close()

    def test_callable_estimate_and_actual(self, config: CyclesConfig, httpx_mock) -> None:  # type: ignore[no-untyped-def]
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:7878/v1/reservations",
            json={"decision": "ALLOW", "reservation_id": "res_dec_2", "expires_at_ms": 9999999999},
            status_code=200,
        )
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:7878/v1/reservations/res_dec_2/commit",
            json={"status": "COMMITTED"},
            status_code=200,
        )

        client = CyclesClient(config)

        @cycles(
            estimate=lambda x: x * 10,
            actual=lambda result: len(result) * 5,
            client=client,
        )
        def compute(x: int) -> str:
            return "hello"

        result = compute(100)
        assert result == "hello"
        client.close()

    def test_denied_raises(self, config: CyclesConfig, httpx_mock) -> None:  # type: ignore[no-untyped-def]
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:7878/v1/reservations",
            json={"error": "BUDGET_EXCEEDED", "message": "No budget"},
            status_code=409,
        )

        client = CyclesClient(config)

        @cycles(estimate=1000, client=client)
        def guarded() -> str:
            return "should not run"

        with pytest.raises(CyclesProtocolError):
            guarded()
        client.close()

    def test_function_exception_triggers_release(self, config: CyclesConfig, httpx_mock) -> None:  # type: ignore[no-untyped-def]
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:7878/v1/reservations",
            json={"decision": "ALLOW", "reservation_id": "res_dec_3", "expires_at_ms": 9999999999},
            status_code=200,
        )
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:7878/v1/reservations/res_dec_3/release",
            json={"status": "RELEASED"},
            status_code=200,
        )

        client = CyclesClient(config)

        @cycles(estimate=1000, client=client)
        def failing_func() -> str:
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            failing_func()
        client.close()

    def test_context_cleared_after_call(self, config: CyclesConfig, httpx_mock) -> None:  # type: ignore[no-untyped-def]
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:7878/v1/reservations",
            json={"decision": "ALLOW", "reservation_id": "res_dec_4", "expires_at_ms": 9999999999},
            status_code=200,
        )
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:7878/v1/reservations/res_dec_4/commit",
            json={"status": "COMMITTED"},
            status_code=200,
        )

        client = CyclesClient(config)

        @cycles(estimate=1000, client=client)
        def func() -> str:
            assert get_cycles_context() is not None
            return "ok"

        func()
        assert get_cycles_context() is None
        client.close()


@pytest.mark.asyncio
class TestCyclesDecoratorAsync:
    async def test_basic_async_lifecycle(self, config: CyclesConfig, httpx_mock) -> None:  # type: ignore[no-untyped-def]
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:7878/v1/reservations",
            json={"decision": "ALLOW", "reservation_id": "res_async_1", "expires_at_ms": 9999999999},
            status_code=200,
        )
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:7878/v1/reservations/res_async_1/commit",
            json={"status": "COMMITTED"},
            status_code=200,
        )

        client = AsyncCyclesClient(config)

        @cycles(estimate=1000, client=client)
        async def async_func(x: int) -> str:
            ctx = get_cycles_context()
            assert ctx is not None
            return f"async-{x}"

        result = await async_func(42)
        assert result == "async-42"
        await client.aclose()
