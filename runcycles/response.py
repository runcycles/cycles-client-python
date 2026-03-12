"""Uniform response wrapper for all Cycles API calls."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from runcycles.models import ErrorResponse


@dataclass
class CyclesResponse:
    """Wraps a Cycles API response, distinguishing success, HTTP errors, and transport errors."""

    status: int
    body: dict[str, Any] | None = None
    error_message: str | None = None
    _is_transport_error: bool = field(default=False, repr=False)
    transport_exception: Exception | None = field(default=None, repr=False)

    @classmethod
    def success(cls, status: int, body: dict[str, Any]) -> CyclesResponse:
        return cls(status=status, body=body)

    @classmethod
    def http_error(cls, status: int, error_message: str, body: dict[str, Any] | None = None) -> CyclesResponse:
        return cls(status=status, body=body, error_message=error_message)

    @classmethod
    def transport_error(cls, ex: Exception) -> CyclesResponse:
        return cls(
            status=-1,
            error_message=str(ex),
            _is_transport_error=True,
            transport_exception=ex,
        )

    @property
    def is_success(self) -> bool:
        return 200 <= self.status < 300

    @property
    def is_client_error(self) -> bool:
        return 400 <= self.status < 500

    @property
    def is_server_error(self) -> bool:
        return 500 <= self.status < 600

    @property
    def is_transport_error(self) -> bool:
        return self._is_transport_error

    def get_body_attribute(self, key: str) -> Any:
        if self.body and key in self.body:
            return self.body[key]
        return None

    def get_error_response(self) -> ErrorResponse | None:
        if self.body and isinstance(self.body, dict):
            try:
                return ErrorResponse.model_validate(self.body)
            except Exception:
                return None
        return None
