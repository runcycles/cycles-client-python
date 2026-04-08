"""Tests for the streaming convenience module."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from runcycles.client import AsyncCyclesClient, CyclesClient
from runcycles.config import CyclesConfig
from runcycles.context import get_cycles_context
from runcycles.exceptions import BudgetExceededError, CyclesProtocolError
from runcycles.models import Action, Amount, Decision, Subject, Unit
from runcycles.response import CyclesResponse
from runcycles.streaming import (
    AsyncStreamReservation,
    StreamReservation,
    StreamUsage,
    _build_streaming_reservation_body,
    _resolve_actual_cost,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config() -> CyclesConfig:
    return CyclesConfig(
        base_url="http://localhost:7878",
        api_key="test-key",
        tenant="acme",
        retry_enabled=False,
        retry_initial_delay=0.001,
        retry_max_delay=0.01,
    )


def _allow_response(caps: dict | None = None) -> CyclesResponse:
    body: dict = {
        "decision": "ALLOW",
        "reservation_id": "rsv_stream_test",
        "expires_at_ms": int(time.time() * 1000) + 600_000,
        "affected_scopes": ["tenant:acme"],
        "scope_path": "tenant:acme",
        "reserved": {"unit": "USD_MICROCENTS", "amount": 1000},
    }
    if caps:
        body["caps"] = caps
    return CyclesResponse.success(200, body)


def _deny_response() -> CyclesResponse:
    return CyclesResponse.success(
        200,
        {
            "decision": "DENY",
            "affected_scopes": ["tenant:acme"],
            "reason_code": "BUDGET_EXCEEDED",
        },
    )


def _commit_success() -> CyclesResponse:
    return CyclesResponse.success(
        200,
        {
            "status": "COMMITTED",
            "charged": {"unit": "USD_MICROCENTS", "amount": 500},
        },
    )


def _release_success() -> CyclesResponse:
    return CyclesResponse.success(
        200,
        {
            "status": "RELEASED",
            "released": {"unit": "USD_MICROCENTS", "amount": 1000},
        },
    )


def _make_mock_client() -> MagicMock:
    config = _make_config()
    mock = MagicMock(spec=CyclesClient)
    mock._config = config
    return mock


def _make_async_mock_client() -> MagicMock:
    config = _make_config()
    mock = MagicMock(spec=AsyncCyclesClient)
    mock._config = config
    mock.create_reservation = AsyncMock()
    mock.commit_reservation = AsyncMock()
    mock.release_reservation = AsyncMock()
    mock.extend_reservation = AsyncMock()
    return mock


def _default_subject() -> Subject:
    return Subject(tenant="acme")


def _default_action() -> Action:
    return Action(kind="llm.completion", name="gpt-4o")


def _default_estimate() -> Amount:
    return Amount(unit=Unit.USD_MICROCENTS, amount=1000)


# ---------------------------------------------------------------------------
# StreamUsage tests
# ---------------------------------------------------------------------------


class TestStreamUsage:
    def test_defaults(self) -> None:
        u = StreamUsage()
        assert u.tokens_input == 0
        assert u.tokens_output == 0
        assert u.actual_cost is None
        assert u.model_version is None
        assert u.custom == {}

    def test_add_input_tokens(self) -> None:
        u = StreamUsage()
        u.add_input_tokens(10)
        u.add_input_tokens(5)
        assert u.tokens_input == 15

    def test_add_output_tokens(self) -> None:
        u = StreamUsage()
        u.add_output_tokens(20)
        assert u.tokens_output == 20

    def test_set_actual_cost(self) -> None:
        u = StreamUsage()
        u.set_actual_cost(999)
        assert u.actual_cost == 999


# ---------------------------------------------------------------------------
# _build_streaming_reservation_body tests
# ---------------------------------------------------------------------------


class TestBuildStreamingReservationBody:
    def test_basic(self) -> None:
        body = _build_streaming_reservation_body(
            _default_subject(),
            _default_action(),
            _default_estimate(),
            ttl_ms=120_000,
            overage_policy="ALLOW_IF_AVAILABLE",
            grace_period_ms=None,
        )
        assert body["subject"]["tenant"] == "acme"
        assert body["action"]["kind"] == "llm.completion"
        assert body["estimate"]["amount"] == 1000
        assert body["ttl_ms"] == 120_000
        assert body["overage_policy"] == "ALLOW_IF_AVAILABLE"
        assert "idempotency_key" in body
        assert "grace_period_ms" not in body

    def test_with_grace_period(self) -> None:
        body = _build_streaming_reservation_body(
            _default_subject(),
            _default_action(),
            _default_estimate(),
            ttl_ms=120_000,
            overage_policy="REJECT",
            grace_period_ms=5000,
        )
        assert body["grace_period_ms"] == 5000

    def test_ttl_below_minimum_raises(self) -> None:
        with pytest.raises(ValueError, match="ttl_ms"):
            _build_streaming_reservation_body(
                _default_subject(),
                _default_action(),
                _default_estimate(),
                ttl_ms=500,
                overage_policy="ALLOW_IF_AVAILABLE",
                grace_period_ms=None,
            )

    def test_ttl_above_maximum_raises(self) -> None:
        with pytest.raises(ValueError, match="ttl_ms"):
            _build_streaming_reservation_body(
                _default_subject(),
                _default_action(),
                _default_estimate(),
                ttl_ms=86_400_001,
                overage_policy="ALLOW_IF_AVAILABLE",
                grace_period_ms=None,
            )

    def test_grace_period_above_maximum_raises(self) -> None:
        with pytest.raises(ValueError, match="grace_period_ms"):
            _build_streaming_reservation_body(
                _default_subject(),
                _default_action(),
                _default_estimate(),
                ttl_ms=120_000,
                overage_policy="ALLOW_IF_AVAILABLE",
                grace_period_ms=60_001,
            )

    def test_subject_with_no_standard_fields_raises(self) -> None:
        with pytest.raises(ValueError, match="at least one standard field"):
            _build_streaming_reservation_body(
                Subject(dimensions={"custom": "val"}),
                _default_action(),
                _default_estimate(),
                ttl_ms=120_000,
                overage_policy="ALLOW_IF_AVAILABLE",
                grace_period_ms=None,
            )


# ---------------------------------------------------------------------------
# _resolve_actual_cost tests
# ---------------------------------------------------------------------------


class TestResolveActualCost:
    def test_explicit_actual_cost(self) -> None:
        u = StreamUsage(actual_cost=777)
        assert _resolve_actual_cost(u, lambda _: 999, 1000) == 777

    def test_cost_fn(self) -> None:
        u = StreamUsage(tokens_input=100, tokens_output=50)

        def cost_fn(usage: StreamUsage) -> int:
            return usage.tokens_input * 2 + usage.tokens_output * 3

        assert _resolve_actual_cost(u, cost_fn, 1000) == 350

    def test_cost_fn_error_falls_back_to_estimate(self) -> None:
        u = StreamUsage()

        def bad_fn(_: StreamUsage) -> int:
            raise ValueError("oops")

        assert _resolve_actual_cost(u, bad_fn, 1000) == 1000

    def test_fallback_to_estimate(self) -> None:
        u = StreamUsage()
        assert _resolve_actual_cost(u, None, 500) == 500


# ---------------------------------------------------------------------------
# StreamReservation (sync) tests
# ---------------------------------------------------------------------------


class TestStreamReservation:
    def test_successful_reserve_and_commit(self) -> None:
        mock = _make_mock_client()
        mock.create_reservation.return_value = _allow_response()
        mock.commit_reservation.return_value = _commit_success()

        sr = StreamReservation(
            mock,
            subject=_default_subject(),
            action=_default_action(),
            estimate=_default_estimate(),
            ttl_ms=1000,  # disable heartbeat for test
        )

        with sr as reservation:
            assert reservation.reservation_id == "rsv_stream_test"
            assert reservation.decision == Decision.ALLOW
            reservation.usage.tokens_input = 50
            reservation.usage.tokens_output = 25

        mock.create_reservation.assert_called_once()
        mock.commit_reservation.assert_called_once()
        mock.release_reservation.assert_not_called()

    def test_exception_triggers_release(self) -> None:
        mock = _make_mock_client()
        mock.create_reservation.return_value = _allow_response()
        mock.release_reservation.return_value = _release_success()

        sr = StreamReservation(
            mock,
            subject=_default_subject(),
            action=_default_action(),
            estimate=_default_estimate(),
            ttl_ms=1000,
        )

        with pytest.raises(RuntimeError, match="stream error"):
            with sr:
                raise RuntimeError("stream error")

        mock.commit_reservation.assert_not_called()
        mock.release_reservation.assert_called_once()

    def test_deny_raises_protocol_error(self) -> None:
        mock = _make_mock_client()
        mock.create_reservation.return_value = _deny_response()

        sr = StreamReservation(
            mock,
            subject=_default_subject(),
            action=_default_action(),
            estimate=_default_estimate(),
            ttl_ms=1000,
        )

        with pytest.raises(CyclesProtocolError, match="Reservation denied"):
            with sr:
                pass

    def test_reservation_failure_raises(self) -> None:
        mock = _make_mock_client()
        mock.create_reservation.return_value = CyclesResponse.http_error(
            500,
            "Server error",
            body=None,
        )

        sr = StreamReservation(
            mock,
            subject=_default_subject(),
            action=_default_action(),
            estimate=_default_estimate(),
            ttl_ms=1000,
        )

        with pytest.raises(CyclesProtocolError, match="Failed to create reservation"):
            with sr:
                pass

    def test_missing_reservation_id_raises(self) -> None:
        mock = _make_mock_client()
        mock.create_reservation.return_value = CyclesResponse.success(
            200,
            {
                "decision": "ALLOW",
                "affected_scopes": ["tenant:acme"],
            },
        )

        sr = StreamReservation(
            mock,
            subject=_default_subject(),
            action=_default_action(),
            estimate=_default_estimate(),
            ttl_ms=1000,
        )

        with pytest.raises(CyclesProtocolError, match="reservation_id missing"):
            with sr:
                pass

    def test_cost_fn_used_for_commit(self) -> None:
        mock = _make_mock_client()
        mock.create_reservation.return_value = _allow_response()
        mock.commit_reservation.return_value = _commit_success()

        def cost_fn(usage: StreamUsage) -> int:
            return usage.tokens_input * 10 + usage.tokens_output * 20

        sr = StreamReservation(
            mock,
            subject=_default_subject(),
            action=_default_action(),
            estimate=_default_estimate(),
            ttl_ms=1000,
            cost_fn=cost_fn,
        )

        with sr as reservation:
            reservation.usage.tokens_input = 100
            reservation.usage.tokens_output = 50

        # Check the commit body had actual = 100*10 + 50*20 = 2000
        commit_call = mock.commit_reservation.call_args
        commit_body = commit_call[0][1]
        assert commit_body["actual"]["amount"] == 2000

    def test_actual_cost_overrides_cost_fn(self) -> None:
        mock = _make_mock_client()
        mock.create_reservation.return_value = _allow_response()
        mock.commit_reservation.return_value = _commit_success()

        sr = StreamReservation(
            mock,
            subject=_default_subject(),
            action=_default_action(),
            estimate=_default_estimate(),
            ttl_ms=1000,
            cost_fn=lambda _: 9999,
        )

        with sr as reservation:
            reservation.usage.set_actual_cost(42)

        commit_body = mock.commit_reservation.call_args[0][1]
        assert commit_body["actual"]["amount"] == 42

    def test_fallback_to_estimate_when_no_cost_fn(self) -> None:
        mock = _make_mock_client()
        mock.create_reservation.return_value = _allow_response()
        mock.commit_reservation.return_value = _commit_success()

        sr = StreamReservation(
            mock,
            subject=_default_subject(),
            action=_default_action(),
            estimate=_default_estimate(),
            ttl_ms=1000,
        )

        with sr:
            pass

        commit_body = mock.commit_reservation.call_args[0][1]
        assert commit_body["actual"]["amount"] == 1000  # estimate amount

    def test_caps_propagated(self) -> None:
        mock = _make_mock_client()
        mock.create_reservation.return_value = _allow_response(
            caps={"max_tokens": 512},
        )
        mock.commit_reservation.return_value = _commit_success()

        sr = StreamReservation(
            mock,
            subject=_default_subject(),
            action=_default_action(),
            estimate=_default_estimate(),
            ttl_ms=1000,
        )

        with sr as reservation:
            assert reservation.caps is not None
            assert reservation.caps.max_tokens == 512

    def test_context_set_and_cleared(self) -> None:
        mock = _make_mock_client()
        mock.create_reservation.return_value = _allow_response()
        mock.commit_reservation.return_value = _commit_success()

        sr = StreamReservation(
            mock,
            subject=_default_subject(),
            action=_default_action(),
            estimate=_default_estimate(),
            ttl_ms=1000,
        )

        ctx_inside = None
        with sr:
            ctx_inside = get_cycles_context()

        assert ctx_inside is not None
        assert ctx_inside.reservation_id == "rsv_stream_test"
        assert get_cycles_context() is None

    def test_context_cleared_on_exception(self) -> None:
        mock = _make_mock_client()
        mock.create_reservation.return_value = _allow_response()
        mock.release_reservation.return_value = _release_success()

        sr = StreamReservation(
            mock,
            subject=_default_subject(),
            action=_default_action(),
            estimate=_default_estimate(),
            ttl_ms=1000,
        )

        with pytest.raises(ValueError):
            with sr:
                raise ValueError("boom")

        assert get_cycles_context() is None

    def test_reservation_id_not_available_outside_context(self) -> None:
        mock = _make_mock_client()
        sr = StreamReservation(
            mock,
            subject=_default_subject(),
            action=_default_action(),
            estimate=_default_estimate(),
            ttl_ms=1000,
        )
        with pytest.raises(RuntimeError, match="not available outside"):
            _ = sr.reservation_id

    def test_commit_server_error_schedules_retry(self) -> None:
        mock = _make_mock_client()
        mock.create_reservation.return_value = _allow_response()
        mock.commit_reservation.return_value = CyclesResponse.http_error(
            500,
            "Server error",
            body=None,
        )

        sr = StreamReservation(
            mock,
            subject=_default_subject(),
            action=_default_action(),
            estimate=_default_estimate(),
            ttl_ms=1000,
        )

        # retry_enabled=False in config, so retry is just logged, no crash
        with sr:
            pass

        mock.commit_reservation.assert_called_once()

    def test_commit_finalized_does_not_release(self) -> None:
        mock = _make_mock_client()
        mock.create_reservation.return_value = _allow_response()
        mock.commit_reservation.return_value = CyclesResponse.http_error(
            409,
            "Finalized",
            body={"error": "RESERVATION_FINALIZED", "message": "Already committed", "request_id": "r1"},
        )

        sr = StreamReservation(
            mock,
            subject=_default_subject(),
            action=_default_action(),
            estimate=_default_estimate(),
            ttl_ms=1000,
        )

        with sr:
            pass

        mock.release_reservation.assert_not_called()

    def test_commit_expired_does_not_release(self) -> None:
        mock = _make_mock_client()
        mock.create_reservation.return_value = _allow_response()
        mock.commit_reservation.return_value = CyclesResponse.http_error(
            410,
            "Expired",
            body={"error": "RESERVATION_EXPIRED", "message": "Expired", "request_id": "r1"},
        )

        sr = StreamReservation(
            mock,
            subject=_default_subject(),
            action=_default_action(),
            estimate=_default_estimate(),
            ttl_ms=1000,
        )

        with sr:
            pass

        mock.release_reservation.assert_not_called()

    def test_commit_client_error_triggers_release(self) -> None:
        mock = _make_mock_client()
        mock.create_reservation.return_value = _allow_response()
        mock.commit_reservation.return_value = CyclesResponse.http_error(
            400,
            "Bad request",
            body={"error": "VALIDATION_ERROR", "message": "Bad", "request_id": "r1"},
        )
        mock.release_reservation.return_value = _release_success()

        sr = StreamReservation(
            mock,
            subject=_default_subject(),
            action=_default_action(),
            estimate=_default_estimate(),
            ttl_ms=1000,
        )

        with sr:
            pass

        mock.release_reservation.assert_called_once()

    def test_commit_exception_schedules_retry(self) -> None:
        mock = _make_mock_client()
        mock.create_reservation.return_value = _allow_response()
        mock.commit_reservation.side_effect = Exception("network down")

        sr = StreamReservation(
            mock,
            subject=_default_subject(),
            action=_default_action(),
            estimate=_default_estimate(),
            ttl_ms=1000,
        )

        # Should not raise; logs and attempts retry (disabled in test config)
        with sr:
            pass

    def test_metadata_passed_to_commit(self) -> None:
        mock = _make_mock_client()
        mock.create_reservation.return_value = _allow_response()
        mock.commit_reservation.return_value = _commit_success()

        sr = StreamReservation(
            mock,
            subject=_default_subject(),
            action=_default_action(),
            estimate=_default_estimate(),
            ttl_ms=1000,
            metadata={"source": "test"},
        )

        with sr:
            pass

        commit_body = mock.commit_reservation.call_args[0][1]
        assert commit_body["metadata"] == {"source": "test"}

    def test_metrics_include_tokens(self) -> None:
        mock = _make_mock_client()
        mock.create_reservation.return_value = _allow_response()
        mock.commit_reservation.return_value = _commit_success()

        sr = StreamReservation(
            mock,
            subject=_default_subject(),
            action=_default_action(),
            estimate=_default_estimate(),
            ttl_ms=1000,
        )

        with sr as reservation:
            reservation.usage.tokens_input = 100
            reservation.usage.tokens_output = 50
            reservation.usage.model_version = "gpt-4o-2024"

        commit_body = mock.commit_reservation.call_args[0][1]
        assert commit_body["metrics"]["tokens_input"] == 100
        assert commit_body["metrics"]["tokens_output"] == 50
        assert commit_body["metrics"]["model_version"] == "gpt-4o-2024"

    def test_heartbeat_starts_and_stops(self) -> None:
        mock = _make_mock_client()
        mock.create_reservation.return_value = _allow_response()
        mock.commit_reservation.return_value = _commit_success()
        mock.extend_reservation.return_value = CyclesResponse.success(
            200,
            {
                "status": "EXTENDED",
                "expires_at_ms": int(time.time() * 1000) + 600_000,
            },
        )

        sr = StreamReservation(
            mock,
            subject=_default_subject(),
            action=_default_action(),
            estimate=_default_estimate(),
            ttl_ms=2000,  # heartbeat every 1s
        )

        with sr:
            # Wait long enough for at least one heartbeat
            time.sleep(1.2)

        mock.extend_reservation.assert_called()


# ---------------------------------------------------------------------------
# AsyncStreamReservation tests
# ---------------------------------------------------------------------------


class TestAsyncStreamReservation:
    @pytest.mark.asyncio
    async def test_successful_reserve_and_commit(self) -> None:
        mock = _make_async_mock_client()
        mock.create_reservation.return_value = _allow_response()
        mock.commit_reservation.return_value = _commit_success()

        asr = AsyncStreamReservation(
            mock,
            subject=_default_subject(),
            action=_default_action(),
            estimate=_default_estimate(),
            ttl_ms=1000,
        )

        async with asr as reservation:
            assert reservation.reservation_id == "rsv_stream_test"
            reservation.usage.tokens_input = 50

        mock.create_reservation.assert_called_once()
        mock.commit_reservation.assert_called_once()
        mock.release_reservation.assert_not_called()

    @pytest.mark.asyncio
    async def test_exception_triggers_release(self) -> None:
        mock = _make_async_mock_client()
        mock.create_reservation.return_value = _allow_response()
        mock.release_reservation.return_value = _release_success()

        asr = AsyncStreamReservation(
            mock,
            subject=_default_subject(),
            action=_default_action(),
            estimate=_default_estimate(),
            ttl_ms=1000,
        )

        with pytest.raises(RuntimeError, match="async stream error"):
            async with asr:
                raise RuntimeError("async stream error")

        mock.commit_reservation.assert_not_called()
        mock.release_reservation.assert_called_once()

    @pytest.mark.asyncio
    async def test_deny_raises(self) -> None:
        mock = _make_async_mock_client()
        mock.create_reservation.return_value = _deny_response()

        asr = AsyncStreamReservation(
            mock,
            subject=_default_subject(),
            action=_default_action(),
            estimate=_default_estimate(),
            ttl_ms=1000,
        )

        with pytest.raises(CyclesProtocolError, match="Reservation denied"):
            async with asr:
                pass

    @pytest.mark.asyncio
    async def test_cost_fn_used(self) -> None:
        mock = _make_async_mock_client()
        mock.create_reservation.return_value = _allow_response()
        mock.commit_reservation.return_value = _commit_success()

        asr = AsyncStreamReservation(
            mock,
            subject=_default_subject(),
            action=_default_action(),
            estimate=_default_estimate(),
            ttl_ms=1000,
            cost_fn=lambda u: u.tokens_input * 5,
        )

        async with asr as reservation:
            reservation.usage.tokens_input = 200

        commit_body = mock.commit_reservation.call_args[0][1]
        assert commit_body["actual"]["amount"] == 1000

    @pytest.mark.asyncio
    async def test_context_set_and_cleared(self) -> None:
        mock = _make_async_mock_client()
        mock.create_reservation.return_value = _allow_response()
        mock.commit_reservation.return_value = _commit_success()

        asr = AsyncStreamReservation(
            mock,
            subject=_default_subject(),
            action=_default_action(),
            estimate=_default_estimate(),
            ttl_ms=1000,
        )

        ctx_inside = None
        async with asr:
            ctx_inside = get_cycles_context()

        assert ctx_inside is not None
        assert ctx_inside.reservation_id == "rsv_stream_test"
        assert get_cycles_context() is None

    @pytest.mark.asyncio
    async def test_reservation_id_not_available_outside(self) -> None:
        mock = _make_async_mock_client()
        asr = AsyncStreamReservation(
            mock,
            subject=_default_subject(),
            action=_default_action(),
            estimate=_default_estimate(),
            ttl_ms=1000,
        )
        with pytest.raises(RuntimeError, match="not available outside"):
            _ = asr.reservation_id

    @pytest.mark.asyncio
    async def test_commit_server_error_schedules_retry(self) -> None:
        mock = _make_async_mock_client()
        mock.create_reservation.return_value = _allow_response()
        mock.commit_reservation.return_value = CyclesResponse.http_error(
            500,
            "Server error",
            body=None,
        )

        asr = AsyncStreamReservation(
            mock,
            subject=_default_subject(),
            action=_default_action(),
            estimate=_default_estimate(),
            ttl_ms=1000,
        )

        async with asr:
            pass

        mock.commit_reservation.assert_called_once()

    @pytest.mark.asyncio
    async def test_commit_client_error_triggers_release(self) -> None:
        mock = _make_async_mock_client()
        mock.create_reservation.return_value = _allow_response()
        mock.commit_reservation.return_value = CyclesResponse.http_error(
            400,
            "Bad request",
            body={"error": "VALIDATION_ERROR", "message": "Bad", "request_id": "r1"},
        )
        mock.release_reservation.return_value = _release_success()

        asr = AsyncStreamReservation(
            mock,
            subject=_default_subject(),
            action=_default_action(),
            estimate=_default_estimate(),
            ttl_ms=1000,
        )

        async with asr:
            pass

        mock.release_reservation.assert_called_once()

    @pytest.mark.asyncio
    async def test_missing_reservation_id_raises(self) -> None:
        mock = _make_async_mock_client()
        mock.create_reservation.return_value = CyclesResponse.success(
            200,
            {
                "decision": "ALLOW",
                "affected_scopes": ["tenant:acme"],
            },
        )

        asr = AsyncStreamReservation(
            mock,
            subject=_default_subject(),
            action=_default_action(),
            estimate=_default_estimate(),
            ttl_ms=1000,
        )

        with pytest.raises(CyclesProtocolError, match="reservation_id missing"):
            async with asr:
                pass

    @pytest.mark.asyncio
    async def test_reservation_failure_raises(self) -> None:
        mock = _make_async_mock_client()
        mock.create_reservation.return_value = CyclesResponse.http_error(
            500,
            "Server error",
            body=None,
        )

        asr = AsyncStreamReservation(
            mock,
            subject=_default_subject(),
            action=_default_action(),
            estimate=_default_estimate(),
            ttl_ms=1000,
        )

        with pytest.raises(CyclesProtocolError, match="Failed to create reservation"):
            async with asr:
                pass


# ---------------------------------------------------------------------------
# Client convenience method tests
# ---------------------------------------------------------------------------


class TestClientStreamReservation:
    def test_sync_client_returns_stream_reservation(self) -> None:
        config = _make_config()
        mock_http = MagicMock()
        with patch("runcycles.client.httpx.Client", return_value=mock_http):
            client = CyclesClient(config)
            sr = client.stream_reservation(
                action=_default_action(),
                estimate=_default_estimate(),
            )
            assert isinstance(sr, StreamReservation)

    def test_sync_client_uses_config_subject(self) -> None:
        config = CyclesConfig(
            base_url="http://localhost:7878",
            api_key="test",
            tenant="acme",
            workspace="prod",
        )
        mock_http = MagicMock()
        with patch("runcycles.client.httpx.Client", return_value=mock_http):
            client = CyclesClient(config)
            sr = client.stream_reservation(
                action=_default_action(),
                estimate=_default_estimate(),
            )
            assert sr._subject.tenant == "acme"
            assert sr._subject.workspace == "prod"

    def test_sync_client_explicit_subject_overrides(self) -> None:
        config = _make_config()
        mock_http = MagicMock()
        with patch("runcycles.client.httpx.Client", return_value=mock_http):
            client = CyclesClient(config)
            custom_subject = Subject(tenant="other")
            sr = client.stream_reservation(
                subject=custom_subject,
                action=_default_action(),
                estimate=_default_estimate(),
            )
            assert sr._subject.tenant == "other"

    def test_async_client_returns_async_stream_reservation(self) -> None:
        config = _make_config()
        mock_http = MagicMock()
        with patch("runcycles.client.httpx.AsyncClient", return_value=mock_http):
            client = AsyncCyclesClient(config)
            asr = client.stream_reservation(
                action=_default_action(),
                estimate=_default_estimate(),
            )
            assert isinstance(asr, AsyncStreamReservation)


# ---------------------------------------------------------------------------
# Budget-exceeded (typed exception) test
# ---------------------------------------------------------------------------


class TestStreamReservationEdgeCases:
    def test_unrecognized_commit_response(self) -> None:
        mock = _make_mock_client()
        mock.create_reservation.return_value = _allow_response()
        # Status 302 is neither client error, server error, nor transport error
        mock.commit_reservation.return_value = CyclesResponse.http_error(
            302,
            "Redirect",
            body=None,
        )

        sr = StreamReservation(
            mock,
            subject=_default_subject(),
            action=_default_action(),
            estimate=_default_estimate(),
            ttl_ms=1000,
        )

        with sr:
            pass

        mock.release_reservation.assert_not_called()

    def test_release_failure_logged(self) -> None:
        mock = _make_mock_client()
        mock.create_reservation.return_value = _allow_response()
        mock.release_reservation.return_value = CyclesResponse.http_error(
            500,
            "Server error",
            body=None,
        )

        sr = StreamReservation(
            mock,
            subject=_default_subject(),
            action=_default_action(),
            estimate=_default_estimate(),
            ttl_ms=1000,
        )

        with pytest.raises(ValueError):
            with sr:
                raise ValueError("boom")

        mock.release_reservation.assert_called_once()

    def test_release_exception_logged(self) -> None:
        mock = _make_mock_client()
        mock.create_reservation.return_value = _allow_response()
        mock.release_reservation.side_effect = Exception("network down")

        sr = StreamReservation(
            mock,
            subject=_default_subject(),
            action=_default_action(),
            estimate=_default_estimate(),
            ttl_ms=1000,
        )

        with pytest.raises(ValueError):
            with sr:
                raise ValueError("boom")

    def test_commit_idempotency_mismatch_does_not_release(self) -> None:
        mock = _make_mock_client()
        mock.create_reservation.return_value = _allow_response()
        mock.commit_reservation.return_value = CyclesResponse.http_error(
            409,
            "Idempotency mismatch",
            body={"error": "IDEMPOTENCY_MISMATCH", "message": "Mismatch", "request_id": "r1"},
        )

        sr = StreamReservation(
            mock,
            subject=_default_subject(),
            action=_default_action(),
            estimate=_default_estimate(),
            ttl_ms=1000,
        )

        with sr:
            pass

        mock.release_reservation.assert_not_called()

    def test_ctx_metrics_respected(self) -> None:
        """If user sets ctx.metrics during streaming, those should be used instead of StreamUsage."""
        mock = _make_mock_client()
        mock.create_reservation.return_value = _allow_response()
        mock.commit_reservation.return_value = _commit_success()

        from runcycles.models import CyclesMetrics

        sr = StreamReservation(
            mock,
            subject=_default_subject(),
            action=_default_action(),
            estimate=_default_estimate(),
            ttl_ms=1000,
        )

        with sr:
            ctx = get_cycles_context()
            assert ctx is not None
            ctx.metrics = CyclesMetrics(tokens_input=999, tokens_output=888, model_version="custom")

        commit_body = mock.commit_reservation.call_args[0][1]
        assert commit_body["metrics"]["tokens_input"] == 999
        assert commit_body["metrics"]["tokens_output"] == 888
        assert commit_body["metrics"]["model_version"] == "custom"


class TestAsyncStreamReservationEdgeCases:
    @pytest.mark.asyncio
    async def test_commit_finalized_does_not_release(self) -> None:
        mock = _make_async_mock_client()
        mock.create_reservation.return_value = _allow_response()
        mock.commit_reservation.return_value = CyclesResponse.http_error(
            409,
            "Finalized",
            body={"error": "RESERVATION_FINALIZED", "message": "Done", "request_id": "r1"},
        )

        asr = AsyncStreamReservation(
            mock,
            subject=_default_subject(),
            action=_default_action(),
            estimate=_default_estimate(),
            ttl_ms=1000,
        )

        async with asr:
            pass

        mock.release_reservation.assert_not_called()

    @pytest.mark.asyncio
    async def test_commit_expired_does_not_release(self) -> None:
        mock = _make_async_mock_client()
        mock.create_reservation.return_value = _allow_response()
        mock.commit_reservation.return_value = CyclesResponse.http_error(
            410,
            "Expired",
            body={"error": "RESERVATION_EXPIRED", "message": "Expired", "request_id": "r1"},
        )

        asr = AsyncStreamReservation(
            mock,
            subject=_default_subject(),
            action=_default_action(),
            estimate=_default_estimate(),
            ttl_ms=1000,
        )

        async with asr:
            pass

        mock.release_reservation.assert_not_called()

    @pytest.mark.asyncio
    async def test_unrecognized_commit_response(self) -> None:
        mock = _make_async_mock_client()
        mock.create_reservation.return_value = _allow_response()
        mock.commit_reservation.return_value = CyclesResponse.http_error(
            302,
            "Redirect",
            body=None,
        )

        asr = AsyncStreamReservation(
            mock,
            subject=_default_subject(),
            action=_default_action(),
            estimate=_default_estimate(),
            ttl_ms=1000,
        )

        async with asr:
            pass

        mock.release_reservation.assert_not_called()

    @pytest.mark.asyncio
    async def test_commit_exception_schedules_retry(self) -> None:
        mock = _make_async_mock_client()
        mock.create_reservation.return_value = _allow_response()
        mock.commit_reservation.side_effect = Exception("network down")

        asr = AsyncStreamReservation(
            mock,
            subject=_default_subject(),
            action=_default_action(),
            estimate=_default_estimate(),
            ttl_ms=1000,
        )

        async with asr:
            pass

    @pytest.mark.asyncio
    async def test_commit_idempotency_mismatch_does_not_release(self) -> None:
        mock = _make_async_mock_client()
        mock.create_reservation.return_value = _allow_response()
        mock.commit_reservation.return_value = CyclesResponse.http_error(
            409,
            "Idempotency mismatch",
            body={"error": "IDEMPOTENCY_MISMATCH", "message": "Mismatch", "request_id": "r1"},
        )

        asr = AsyncStreamReservation(
            mock,
            subject=_default_subject(),
            action=_default_action(),
            estimate=_default_estimate(),
            ttl_ms=1000,
        )

        async with asr:
            pass

        mock.release_reservation.assert_not_called()

    @pytest.mark.asyncio
    async def test_release_failure_logged(self) -> None:
        mock = _make_async_mock_client()
        mock.create_reservation.return_value = _allow_response()
        mock.release_reservation.return_value = CyclesResponse.http_error(
            500,
            "Server error",
            body=None,
        )

        asr = AsyncStreamReservation(
            mock,
            subject=_default_subject(),
            action=_default_action(),
            estimate=_default_estimate(),
            ttl_ms=1000,
        )

        with pytest.raises(ValueError):
            async with asr:
                raise ValueError("boom")

    @pytest.mark.asyncio
    async def test_release_exception_logged(self) -> None:
        mock = _make_async_mock_client()
        mock.create_reservation.return_value = _allow_response()
        mock.release_reservation.side_effect = Exception("network down")

        asr = AsyncStreamReservation(
            mock,
            subject=_default_subject(),
            action=_default_action(),
            estimate=_default_estimate(),
            ttl_ms=1000,
        )

        with pytest.raises(ValueError):
            async with asr:
                raise ValueError("boom")

    @pytest.mark.asyncio
    async def test_heartbeat_starts_and_stops(self) -> None:
        mock = _make_async_mock_client()
        mock.create_reservation.return_value = _allow_response()
        mock.commit_reservation.return_value = _commit_success()
        mock.extend_reservation.return_value = CyclesResponse.success(
            200,
            {
                "status": "EXTENDED",
                "expires_at_ms": int(time.time() * 1000) + 600_000,
            },
        )

        asr = AsyncStreamReservation(
            mock,
            subject=_default_subject(),
            action=_default_action(),
            estimate=_default_estimate(),
            ttl_ms=2000,
        )

        async with asr:
            await asyncio.sleep(1.2)

        mock.extend_reservation.assert_called()

    @pytest.mark.asyncio
    async def test_metadata_passed_to_commit(self) -> None:
        mock = _make_async_mock_client()
        mock.create_reservation.return_value = _allow_response()
        mock.commit_reservation.return_value = _commit_success()

        asr = AsyncStreamReservation(
            mock,
            subject=_default_subject(),
            action=_default_action(),
            estimate=_default_estimate(),
            ttl_ms=1000,
            metadata={"key": "val"},
        )

        async with asr:
            pass

        commit_body = mock.commit_reservation.call_args[0][1]
        assert commit_body["metadata"] == {"key": "val"}

    @pytest.mark.asyncio
    async def test_caps_propagated(self) -> None:
        mock = _make_async_mock_client()
        mock.create_reservation.return_value = _allow_response(
            caps={"max_tokens": 256},
        )
        mock.commit_reservation.return_value = _commit_success()

        asr = AsyncStreamReservation(
            mock,
            subject=_default_subject(),
            action=_default_action(),
            estimate=_default_estimate(),
            ttl_ms=1000,
        )

        async with asr as reservation:
            assert reservation.caps is not None
            assert reservation.caps.max_tokens == 256


class TestBudgetExceeded:
    def test_budget_exceeded_raises_typed_error(self) -> None:
        mock = _make_mock_client()
        mock.create_reservation.return_value = CyclesResponse.http_error(
            409,
            "Budget exceeded",
            body={
                "error": "BUDGET_EXCEEDED",
                "message": "No budget",
                "request_id": "req-1",
            },
        )

        sr = StreamReservation(
            mock,
            subject=_default_subject(),
            action=_default_action(),
            estimate=_default_estimate(),
            ttl_ms=1000,
        )

        with pytest.raises(BudgetExceededError):
            with sr:
                pass
