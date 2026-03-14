"""Tests for CyclesContext."""

from runcycles.context import CyclesContext, _clear_context, _set_context, get_cycles_context
from runcycles.models import Caps, CyclesMetrics, Decision


class TestCyclesContext:
    def test_get_returns_none_by_default(self) -> None:
        _clear_context()
        assert get_cycles_context() is None

    def test_set_and_get(self) -> None:
        ctx = CyclesContext(
            reservation_id="res_123",
            estimate=1000,
            decision=Decision.ALLOW,
        )
        _set_context(ctx)
        try:
            got = get_cycles_context()
            assert got is not None
            assert got.reservation_id == "res_123"
            assert got.estimate == 1000
            assert got.decision == Decision.ALLOW
        finally:
            _clear_context()

    def test_clear(self) -> None:
        ctx = CyclesContext(reservation_id="res_123", estimate=1000, decision=Decision.ALLOW)
        _set_context(ctx)
        _clear_context()
        assert get_cycles_context() is None

    def test_has_caps(self) -> None:
        ctx = CyclesContext(
            reservation_id="res_123",
            estimate=1000,
            decision=Decision.ALLOW_WITH_CAPS,
            caps=Caps(max_tokens=500),
        )
        assert ctx.has_caps()

    def test_no_caps(self) -> None:
        ctx = CyclesContext(reservation_id="res_123", estimate=1000, decision=Decision.ALLOW)
        assert not ctx.has_caps()

    def test_writable_metrics(self) -> None:
        ctx = CyclesContext(reservation_id="res_123", estimate=1000, decision=Decision.ALLOW)
        ctx.metrics = CyclesMetrics(tokens_input=100, tokens_output=50)
        assert ctx.metrics.tokens_input == 100

    def test_writable_metadata(self) -> None:
        ctx = CyclesContext(reservation_id="res_123", estimate=1000, decision=Decision.ALLOW)
        ctx.commit_metadata = {"batch_id": "b123"}
        assert ctx.commit_metadata["batch_id"] == "b123"

    def test_update_expires(self) -> None:
        ctx = CyclesContext(
            reservation_id="res_123",
            estimate=1000,
            decision=Decision.ALLOW,
            expires_at_ms=1000,
        )
        ctx.update_expires_at_ms(2000)
        assert ctx.expires_at_ms == 2000
