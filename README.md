# runcycles

Python client for the [Cycles](https://runcycles.io) budget-management protocol.

## Installation

```bash
pip install runcycles
```

## Quick Start

### Decorator-based (recommended)

```python
from runcycles import CyclesClient, CyclesConfig, cycles, get_cycles_context, CyclesMetrics

config = CyclesConfig(
    base_url="http://localhost:7878",
    api_key="your-api-key",
    tenant="acme",
)
client = CyclesClient(config)

@cycles(
    estimate=lambda prompt, tokens: tokens * 10,
    actual=lambda result: len(result) * 5,
    action_kind="llm.completion",
    action_name="gpt-4",
    client=client,
)
def call_llm(prompt: str, tokens: int) -> str:
    # Access the reservation context inside the guarded function
    ctx = get_cycles_context()
    if ctx and ctx.has_caps():
        tokens = min(tokens, ctx.caps.max_tokens or tokens)

    result = f"Response to: {prompt}"

    # Report metrics (included in the commit)
    if ctx:
        ctx.metrics = CyclesMetrics(tokens_input=tokens, tokens_output=len(result))

    return result

result = call_llm("Hello", tokens=100)
```

### Programmatic client

```python
from runcycles import (
    CyclesClient, CyclesConfig, ReservationCreateRequest,
    CommitRequest, Subject, Action, Amount, Unit, CyclesMetrics,
)

config = CyclesConfig(base_url="http://localhost:7878", api_key="your-api-key")

with CyclesClient(config) as client:
    # 1. Reserve budget
    response = client.create_reservation(ReservationCreateRequest(
        subject=Subject(tenant="acme", agent="support-bot"),
        action=Action(kind="llm.completion", name="gpt-4"),
        estimate=Amount(unit=Unit.USD_MICROCENTS, amount=500_000),
        ttl_ms=30_000,
    ))

    if response.is_success:
        reservation_id = response.get_body_attribute("reservation_id")

        # 2. Do work ...

        # 3. Commit actual usage
        client.commit_reservation(reservation_id, CommitRequest(
            actual=Amount(unit=Unit.USD_MICROCENTS, amount=420_000),
            metrics=CyclesMetrics(tokens_input=1200, tokens_output=800),
        ))
```

### Async support

```python
from runcycles import AsyncCyclesClient, CyclesConfig, cycles

config = CyclesConfig(base_url="http://localhost:7878", api_key="your-api-key")
client = AsyncCyclesClient(config)

@cycles(estimate=1000, client=client)
async def call_llm(prompt: str) -> str:
    return f"Response to: {prompt}"

# In an async context:
result = await call_llm("Hello")
```

## Configuration

### From environment variables

```python
from runcycles import CyclesConfig

config = CyclesConfig.from_env()
# Reads: CYCLES_BASE_URL, CYCLES_API_KEY, CYCLES_TENANT, etc.
```

### All options

```python
CyclesConfig(
    base_url="http://localhost:7878",
    api_key="your-api-key",
    tenant="acme",
    workspace="prod",
    app="chat",
    connect_timeout=2.0,
    read_timeout=5.0,
    retry_enabled=True,
    retry_max_attempts=5,
    retry_initial_delay=0.5,
    retry_multiplier=2.0,
    retry_max_delay=30.0,
)
```

## Features

- **Decorator-based**: `@cycles` wraps functions with automatic reserve/execute/commit lifecycle
- **Programmatic client**: Full control via `CyclesClient` / `AsyncCyclesClient`
- **Sync + async**: Both synchronous and asyncio-based APIs
- **Automatic heartbeat**: TTL extension at half-interval keeps reservations alive
- **Commit retry**: Failed commits are retried with exponential backoff
- **Context access**: `get_cycles_context()` provides reservation details inside guarded functions
- **Pydantic models**: Typed request/response models with validation
- **Environment config**: `CyclesConfig.from_env()` for 12-factor apps

## Requirements

- Python 3.10+
- httpx
- pydantic >= 2.0
