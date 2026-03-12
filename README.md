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
        idempotency_key="req-001",
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
            idempotency_key="commit-001",
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

### Default client / config

Instead of passing `client=` to every `@cycles` decorator, set a module-level default:

```python
from runcycles import CyclesConfig, set_default_config, set_default_client, CyclesClient, cycles

# Option 1: Set a config (client created lazily)
set_default_config(CyclesConfig(base_url="http://localhost:7878", api_key="your-key", tenant="acme"))

# Option 2: Set an explicit client
set_default_client(CyclesClient(CyclesConfig(base_url="http://localhost:7878", api_key="your-key")))

# Now @cycles works without client=
@cycles(estimate=1000)
def my_func() -> str:
    return "hello"
```

## Error handling

```python
from runcycles import (
    CyclesClient, CyclesConfig, ReservationCreateRequest,
    Subject, Action, Amount, Unit,
    BudgetExceededError, OverdraftLimitExceededError,
    CyclesProtocolError, CyclesTransportError,
)

config = CyclesConfig(base_url="http://localhost:7878", api_key="your-key")

with CyclesClient(config) as client:
    try:
        response = client.create_reservation(ReservationCreateRequest(
            idempotency_key="req-002",
            subject=Subject(tenant="acme"),
            action=Action(kind="llm.completion", name="gpt-4"),
            estimate=Amount(unit=Unit.USD_MICROCENTS, amount=500_000),
        ))

        if not response.is_success:
            print(f"Error {response.status_code}: {response.error_message}")

    except CyclesTransportError as e:
        # Network-level failure (DNS, connection refused, timeout)
        print(f"Transport error: {e}, cause: {e.cause}")
```

With the `@cycles` decorator, protocol errors are raised as typed exceptions:

```python
from runcycles import cycles, BudgetExceededError, CyclesProtocolError

@cycles(estimate=1000, client=client)
def guarded_func() -> str:
    return "result"

try:
    guarded_func()
except BudgetExceededError:
    print("Budget exhausted — degrade or queue")
except CyclesProtocolError as e:
    if e.is_retryable() and e.retry_after_ms:
        print(f"Retry after {e.retry_after_ms}ms")
    print(f"Protocol error: {e}, code: {e.error_code}")
```

Exception hierarchy:

| Exception | When |
|---|---|
| `CyclesError` | Base for all Cycles errors |
| `CyclesProtocolError` | Server returned a protocol-level error |
| `BudgetExceededError` | Budget insufficient for the reservation |
| `OverdraftLimitExceededError` | Debt exceeds the overdraft limit |
| `DebtOutstandingError` | Outstanding debt blocks new reservations |
| `ReservationExpiredError` | Operating on an expired reservation |
| `ReservationFinalizedError` | Operating on an already-committed/released reservation |
| `CyclesTransportError` | Network-level failure (connection, DNS, timeout) |

## Preflight checks (decide)

Check whether a reservation *would* be allowed without creating one:

```python
from runcycles import DecisionRequest, Subject, Action, Amount, Unit

response = client.decide(DecisionRequest(
    idempotency_key="decide-001",
    subject=Subject(tenant="acme"),
    action=Action(kind="llm.completion", name="gpt-4"),
    estimate=Amount(unit=Unit.USD_MICROCENTS, amount=500_000),
))

if response.is_success:
    decision = response.get_body_attribute("decision")  # "ALLOW" or "DENY"
    print(f"Decision: {decision}")
```

## Events (direct debit)

Record usage without a reservation — useful for post-hoc accounting:

```python
from runcycles import EventCreateRequest, Subject, Action, Amount, Unit

response = client.create_event(EventCreateRequest(
    idempotency_key="evt-001",
    subject=Subject(tenant="acme"),
    action=Action(kind="api.call", name="geocode"),
    actual=Amount(unit=Unit.USD_MICROCENTS, amount=1_500),
))
```

## Querying balances

```python
response = client.get_balances(tenant="acme")
if response.is_success:
    print(response.body)
```

## Dry run (shadow mode)

Evaluate a reservation without persisting it. The `@cycles` decorator supports `dry_run=True`:

```python
@cycles(estimate=1000, dry_run=True, client=client)
def shadow_func() -> str:
    return "result"
```

In dry-run mode, the server evaluates the reservation and returns a decision, but no budget is held or consumed.

## Overage policies

Control what happens when actual usage exceeds the estimate at commit time:

```python
from runcycles import CommitOveragePolicy

# REJECT (default) — commit fails if actual > estimate
# ALLOW_IF_AVAILABLE — commit succeeds if budget is available for the overage
# ALLOW_WITH_OVERDRAFT — commit always succeeds, may create debt

@cycles(estimate=1000, overage_policy="ALLOW_WITH_OVERDRAFT", client=client)
def overdraft_func() -> str:
    return "result"
```

## Features

- **Decorator-based**: `@cycles` wraps functions with automatic reserve/execute/commit lifecycle
- **Programmatic client**: Full control via `CyclesClient` / `AsyncCyclesClient`
- **Sync + async**: Both synchronous and asyncio-based APIs
- **Automatic heartbeat**: TTL extension at half-interval keeps reservations alive
- **Commit retry**: Failed commits are retried with exponential backoff
- **Context access**: `get_cycles_context()` provides reservation details inside guarded functions
- **Typed exceptions**: `BudgetExceededError`, `OverdraftLimitExceededError`, etc. for precise error handling
- **Pydantic models**: Typed request/response models with validation
- **Environment config**: `CyclesConfig.from_env()` for 12-factor apps

## Requirements

- Python 3.10+
- httpx
- pydantic >= 2.0
