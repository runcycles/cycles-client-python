"""Configuration for the Cycles client."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class CyclesConfig:
    """Configuration for Cycles client instances."""

    base_url: str
    api_key: str

    # Default subject fields (used when not specified per-request)
    tenant: str | None = None
    workspace: str | None = None
    app: str | None = None
    workflow: str | None = None
    agent: str | None = None
    toolset: str | None = None

    # HTTP settings
    connect_timeout: float = 2.0
    read_timeout: float = 5.0

    # Retry settings
    retry_enabled: bool = True
    retry_max_attempts: int = 5
    retry_initial_delay: float = 0.5
    retry_multiplier: float = 2.0
    retry_max_delay: float = 30.0

    @classmethod
    def from_env(cls, prefix: str = "CYCLES_") -> CyclesConfig:
        """Create config from environment variables.

        Reads: CYCLES_BASE_URL, CYCLES_API_KEY, CYCLES_TENANT, CYCLES_WORKSPACE,
        CYCLES_APP, CYCLES_WORKFLOW, CYCLES_AGENT, CYCLES_TOOLSET,
        CYCLES_CONNECT_TIMEOUT, CYCLES_READ_TIMEOUT, CYCLES_RETRY_ENABLED,
        CYCLES_RETRY_MAX_ATTEMPTS, CYCLES_RETRY_INITIAL_DELAY,
        CYCLES_RETRY_MULTIPLIER, CYCLES_RETRY_MAX_DELAY.
        """
        base_url = os.environ.get(f"{prefix}BASE_URL", "")
        api_key = os.environ.get(f"{prefix}API_KEY", "")

        if not base_url:
            raise ValueError(f"{prefix}BASE_URL environment variable is required")
        if not api_key:
            raise ValueError(f"{prefix}API_KEY environment variable is required")

        return cls(
            base_url=base_url,
            api_key=api_key,
            tenant=os.environ.get(f"{prefix}TENANT"),
            workspace=os.environ.get(f"{prefix}WORKSPACE"),
            app=os.environ.get(f"{prefix}APP"),
            workflow=os.environ.get(f"{prefix}WORKFLOW"),
            agent=os.environ.get(f"{prefix}AGENT"),
            toolset=os.environ.get(f"{prefix}TOOLSET"),
            connect_timeout=float(os.environ.get(f"{prefix}CONNECT_TIMEOUT", "2.0")),
            read_timeout=float(os.environ.get(f"{prefix}READ_TIMEOUT", "5.0")),
            retry_enabled=os.environ.get(f"{prefix}RETRY_ENABLED", "true").lower() == "true",
            retry_max_attempts=int(os.environ.get(f"{prefix}RETRY_MAX_ATTEMPTS", "5")),
            retry_initial_delay=float(os.environ.get(f"{prefix}RETRY_INITIAL_DELAY", "0.5")),
            retry_multiplier=float(os.environ.get(f"{prefix}RETRY_MULTIPLIER", "2.0")),
            retry_max_delay=float(os.environ.get(f"{prefix}RETRY_MAX_DELAY", "30.0")),
        )
