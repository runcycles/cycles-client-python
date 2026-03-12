"""Tests for CyclesResponse."""

from runcycles.response import CyclesResponse


class TestCyclesResponse:
    def test_success(self) -> None:
        r = CyclesResponse.success(200, {"decision": "ALLOW"})
        assert r.is_success
        assert not r.is_client_error
        assert not r.is_server_error
        assert not r.is_transport_error
        assert r.get_body_attribute("decision") == "ALLOW"

    def test_http_error(self) -> None:
        r = CyclesResponse.http_error(409, "Budget exceeded", {"error": "BUDGET_EXCEEDED"})
        assert not r.is_success
        assert r.is_client_error
        assert not r.is_server_error
        assert r.error_message == "Budget exceeded"

    def test_server_error(self) -> None:
        r = CyclesResponse.http_error(500, "Internal error")
        assert r.is_server_error
        assert not r.is_client_error

    def test_transport_error(self) -> None:
        r = CyclesResponse.transport_error(ConnectionError("Connection refused"))
        assert r.is_transport_error
        assert r.status == -1
        assert "Connection refused" in (r.error_message or "")

    def test_get_error_response(self) -> None:
        r = CyclesResponse.http_error(
            409,
            "Budget exceeded",
            {"error": "BUDGET_EXCEEDED", "message": "Insufficient budget", "request_id": "req-1"},
        )
        err = r.get_error_response()
        assert err is not None
        assert err.error == "BUDGET_EXCEEDED"
        assert err.message == "Insufficient budget"

    def test_get_body_attribute_missing(self) -> None:
        r = CyclesResponse.success(200, {"foo": "bar"})
        assert r.get_body_attribute("missing") is None

    def test_get_body_attribute_no_body(self) -> None:
        r = CyclesResponse.transport_error(Exception("fail"))
        assert r.get_body_attribute("anything") is None
