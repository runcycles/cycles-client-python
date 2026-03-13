"""Tests for the @cycles decorator."""

import pytest

from runcycles.client import AsyncCyclesClient, CyclesClient
from runcycles.config import CyclesConfig
from runcycles.context import get_cycles_context
from runcycles.decorator import cycles, set_default_client, set_default_config
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
            json={"decision": "ALLOW", "reservation_id": "res_dec_2", "expires_at_ms": 9999999999, "affected_scopes": ["tenant:acme"]},
            status_code=200,
        )
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:7878/v1/reservations/res_dec_2/commit",
            json={"status": "COMMITTED", "charged": {"unit": "USD_MICROCENTS", "amount": 500}},
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
            json={"decision": "ALLOW", "reservation_id": "res_dec_3", "expires_at_ms": 9999999999, "affected_scopes": ["tenant:acme"]},
            status_code=200,
        )
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:7878/v1/reservations/res_dec_3/release",
            json={"status": "RELEASED", "released": {"unit": "USD_MICROCENTS", "amount": 1000}},
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
            json={"decision": "ALLOW", "reservation_id": "res_dec_4", "expires_at_ms": 9999999999, "affected_scopes": ["tenant:acme"]},
            status_code=200,
        )
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:7878/v1/reservations/res_dec_4/commit",
            json={"status": "COMMITTED", "charged": {"unit": "USD_MICROCENTS", "amount": 1000}},
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
            json={"decision": "ALLOW", "reservation_id": "res_async_1", "expires_at_ms": 9999999999, "affected_scopes": ["tenant:acme"]},
            status_code=200,
        )
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:7878/v1/reservations/res_async_1/commit",
            json={"status": "COMMITTED", "charged": {"unit": "USD_MICROCENTS", "amount": 1000}},
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


class TestDefaultClientConfig:
    def test_set_default_client(self, config: CyclesConfig, httpx_mock) -> None:  # type: ignore[no-untyped-def]
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:7878/v1/reservations",
            json={"decision": "ALLOW", "reservation_id": "res_def_1", "expires_at_ms": 9999999999, "affected_scopes": ["tenant:acme"]},
            status_code=200,
        )
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:7878/v1/reservations/res_def_1/commit",
            json={"status": "COMMITTED", "charged": {"unit": "USD_MICROCENTS", "amount": 1000}},
            status_code=200,
        )

        client = CyclesClient(config)
        set_default_client(client)

        @cycles(estimate=1000)
        def func() -> str:
            return "ok"

        result = func()
        assert result == "ok"
        client.close()

    def test_set_default_config_creates_client_lazily(self, config: CyclesConfig, httpx_mock) -> None:  # type: ignore[no-untyped-def]
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:7878/v1/reservations",
            json={"decision": "ALLOW", "reservation_id": "res_lazy_1", "expires_at_ms": 9999999999, "affected_scopes": ["tenant:acme"]},
            status_code=200,
        )
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:7878/v1/reservations/res_lazy_1/commit",
            json={"status": "COMMITTED", "charged": {"unit": "USD_MICROCENTS", "amount": 1000}},
            status_code=200,
        )

        set_default_config(config)

        @cycles(estimate=1000)
        def func() -> str:
            return "lazy"

        result = func()
        assert result == "lazy"

    def test_no_client_raises(self) -> None:
        @cycles(estimate=1000)
        def func() -> str:
            return "nope"

        with pytest.raises(ValueError, match="No Cycles client available"):
            func()

    def test_sync_func_with_async_client_raises(self, config: CyclesConfig) -> None:
        async_client = AsyncCyclesClient(config)
        set_default_client(async_client)

        @cycles(estimate=1000)
        def func() -> str:
            return "nope"

        with pytest.raises(TypeError, match="Sync function requires a CyclesClient"):
            func()

    @pytest.mark.asyncio
    async def test_set_default_config_creates_async_client_lazily(self, config: CyclesConfig, httpx_mock) -> None:  # type: ignore[no-untyped-def]
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:7878/v1/reservations",
            json={"decision": "ALLOW", "reservation_id": "res_lazy_a1", "expires_at_ms": 9999999999, "affected_scopes": ["tenant:acme"]},
            status_code=200,
        )
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:7878/v1/reservations/res_lazy_a1/commit",
            json={"status": "COMMITTED", "charged": {"unit": "USD_MICROCENTS", "amount": 1000}},
            status_code=200,
        )

        set_default_config(config)

        @cycles(estimate=1000)
        async def func() -> str:
            return "async-lazy"

        result = await func()
        assert result == "async-lazy"

    @pytest.mark.asyncio
    async def test_async_func_with_sync_client_raises(self, config: CyclesConfig) -> None:
        sync_client = CyclesClient(config)
        set_default_client(sync_client)

        @cycles(estimate=1000)
        async def func() -> str:
            return "nope"

        with pytest.raises(TypeError, match="Async function requires an AsyncCyclesClient"):
            await func()
