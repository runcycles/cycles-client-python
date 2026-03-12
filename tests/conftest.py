"""Shared test fixtures."""

import pytest


@pytest.fixture(autouse=True)
def _reset_default_client() -> None:
    """Reset the module-level default client before each test."""
    import runcycles.decorator as dec
    dec._default_client = None
    dec._default_config = None
