[![PyPI](https://img.shields.io/pypi/v/runcycles)](https://pypi.org/project/runcycles/)
[![PyPI Downloads](https://img.shields.io/pypi/dm/runcycles)](https://pypi.org/project/runcycles/)
[![CI](https://github.com/runcycles/cycles-client-python/actions/workflows/ci.yml/badge.svg)](https://github.com/runcycles/cycles-client-python/actions)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue)](LICENSE)
[![Coverage](https://img.shields.io/badge/coverage-100%25-brightgreen)](https://github.com/runcycles/cycles-client-python/actions)

# Cycles Python Client

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

> **Need an API key?** API keys are created via the Cycles Admin Server (port 7979). See the [deployment guide](https://runcycles.io/quickstart/deploying-the-full-cycles-stack#step-3-create-an-api-key) to create one, or run:
> ```bash
> curl -s -X POST http://localhost:7979/v1/admin/api-keys \
>   -H "Content-Type: application/json" \
>   -H "X-Admin-API-Key: admin-bootstrap-key" \
>   -d '{"tenant_id":"acme-corp","name":"dev-key","permissions":["reservations:create","reservations:commit","reservations:release","reservations:extend","reservations:list","balances:read","decide","events:create"]}' | jq -r '.key_secret'
> ```
> The key (e.g. `cyc_live_abc123...`) is shown only once — save it immediately. For key rotation and lifecycle details, see [API Key Management](https://runcycles.io/how-to/api-key-management-in-cycles).

### Budget lifecycle

The `@cycles` decorator wraps your function in a reserve → execute → commit/release lifecycle:

| Scenario | Outcome | Detail |
|---|---|---|
| Reservation denied | **Neither** | `BudgetExceededError`, `OverdraftLimitExceededError`, or `DebtOutstandingError` raised; function never executes |
| `dry_run=True`, any decision | **Neither** | Returns `DryRunResult` or raises; no real reservation created |
| Function returns successfully | **Commit** | Actual amount charged; unused remainder auto-released |
| Function raises any exception | **Release** | Full reserved amount returned to budget; exception re-raised |
| Commit fails (5xx / network) | **Retry** | Exponential backoff with configurable attempts |
| Commit fails (non-retryable 4xx) | **Release** | Reservation released after non-retryable client error |
| Commit gets RESERVATION_EXPIRED | **Neither** | Server already reclaimed budget on TTL expiry |
| Commit gets RESERVATION_FINALIZED | **Neither** | Already committed or released (idempotent replay) |
| Commit gets IDEMPOTENCY_MISMATCH | **Neither** | Previous commit already processed; no release attempted |

All raised exceptions from the guarded function trigger release. See [How Reserve-Commit Works](https://runcycles.io/protocol/how-reserve-commit-works-in-cycles) for the full protocol-level explanation.

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

### Streaming

For streaming LLM responses, use the `stream_reservation()` context manager. It reserves budget on enter, auto-commits on successful exit, and auto-releases on exception:

```python
from runcycles import CyclesClient, CyclesConfig, Action, Amount, Unit

config = CyclesConfig(base_url="http://localhost:7878", api_key="your-api-key", tenant="acme")
client = CyclesClient(config)

with client.stream_reservation(
    action=Action(kind="llm.completion", name="gpt-4o"),
    estimate=Amount(unit=Unit.USD_MICROCENTS, amount=max_tokens * 1000),
    cost_fn=lambda u: u.tokens_input * 250 + u.tokens_output * 1000,
) as reservation:
    # Caps available immediately
    if reservation.caps and reservation.caps.max_tokens:
        max_tokens = min(max_tokens, reservation.caps.max_tokens)

    for chunk in openai_stream:
        if chunk.usage:
            reservation.usage.tokens_input = chunk.usage.prompt_tokens
            reservation.usage.tokens_output = chunk.usage.completion_tokens
# Committed automatically with actual cost from cost_fn
```

Also available as `async with client.stream_reservation(...)` for async clients. See [streaming_usage.py](examples/streaming_usage.py) for a complete example.

## Configuration

### From environment variables

```python
from runcycles import CyclesConfig

config = CyclesConfig.from_env()
# Reads: CYCLES_BASE_URL, CYCLES_API_KEY, CYCLES_TENANT, etc.
```

> **Need an API key?** See the [deployment guide](https://runcycles.io/quickstart/deploying-the-full-cycles-stack#step-3-create-an-api-key) or [API Key Management](https://runcycles.io/how-to/api-key-management-in-cycles).

### All options

```python
CyclesConfig(
    base_url="http://localhost:7878",
    api_key="your-api-key",
    tenant="acme",
    workspace="prod",
    app="chat",
    workflow="refund-flow",
    agent="planner",
    toolset="search-tools",
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
)

config = CyclesConfig(base_url="http://localhost:7878", api_key="your-key")

with CyclesClient(config) as client:
    response = client.create_reservation(ReservationCreateRequest(
        idempotency_key="req-002",
        subject=Subject(tenant="acme"),
        action=Action(kind="llm.completion", name="gpt-4"),
        estimate=Amount(unit=Unit.USD_MICROCENTS, amount=500_000),
    ))

    if response.is_transport_error:
        print(f"Transport error: {response.error_message}")
    elif not response.is_success:
        print(f"Error {response.status}: {response.error_message}")
        print(f"Request ID: {response.request_id}")
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

At least one subject filter (``tenant``, ``workspace``, ``app``, ``workflow``, ``agent``, or ``toolset``) is required:

```python
response = client.get_balances(tenant="acme")
if response.is_success:
    print(response.body)
```

## Response metadata

Every response exposes protocol headers for debugging and rate-limit awareness:

```python
response = client.create_reservation(request)
print(response.request_id)            # X-Request-Id
print(response.rate_limit_remaining)   # X-RateLimit-Remaining (int or None)
print(response.rate_limit_reset)       # X-RateLimit-Reset (int or None)
print(response.cycles_tenant)          # X-Cycles-Tenant
```

## Dry run (shadow mode)

Evaluate a reservation without persisting it. The `@cycles` decorator supports `dry_run=True`:

```python
@cycles(estimate=1000, dry_run=True, client=client)
def shadow_func() -> str:
    return "result"
```

In dry-run mode, the server evaluates the reservation and returns a decision, but no budget is held or consumed. The decorated function does not execute — a `DryRunResult` is returned instead.

## Overage policies

Control what happens when actual usage exceeds the estimate at commit time:

```python
from runcycles import CommitOveragePolicy

# REJECT — commit fails if budget is insufficient for the overage
# ALLOW_IF_AVAILABLE (default) — commit succeeds if remaining budget covers the overage
# ALLOW_WITH_OVERDRAFT — commit always succeeds, may create debt

@cycles(estimate=1000, overage_policy="ALLOW_WITH_OVERDRAFT", client=client)
def overdraft_func() -> str:
    return "result"
```

## Nested `@cycles` Calls

Calling a `@cycles`-decorated function from inside another `@cycles`-decorated function is allowed — it will not raise an error. However, each decorator creates an **independent reservation** that deducts budget separately:

```python
@cycles(estimate=100, action_name="inner")
def inner_call():
    return "done"

@cycles(estimate=500, action_name="outer")
def outer_call():
    return inner_call()  # creates a SECOND reservation — 600 total deducted, not 500
```

This means nested decorators **double-count budget**. The outer reservation already covers the full estimated cost of the operation, so an inner reservation deducts additional budget from the same pool.

**Recommended pattern:** Place `@cycles` at the outermost entry point only. Inner functions should be plain functions without their own guard:

```python
def inner_call():  # no @cycles — called within a guarded operation
    return "done"

@cycles(estimate=500, action_name="outer")
def outer_call():
    return inner_call()  # single reservation — 500 total
```

## Features

- **Decorator-based**: `@cycles` wraps functions with automatic reserve/execute/commit lifecycle
- **Programmatic client**: Full control via `CyclesClient` / `AsyncCyclesClient`
- **Sync + async**: Both synchronous and asyncio-based APIs
- **Automatic heartbeat**: TTL extension at half-interval keeps reservations alive
- **Commit retry**: Failed commits are retried with exponential backoff
- **Context access**: `get_cycles_context()` provides reservation details inside guarded functions
- **Typed exceptions**: `BudgetExceededError`, `OverdraftLimitExceededError`, etc. for precise error handling
- **Pydantic models**: Typed request/response models with spec-enforced validation constraints
- **Response metadata**: Access `request_id`, `rate_limit_remaining`, and `rate_limit_reset` on every response
- **Environment config**: `CyclesConfig.from_env()` for 12-factor apps

## Examples

The [`examples/`](examples/) directory contains runnable integration examples:

| Example | Description |
|---------|-------------|
| [basic_usage.py](examples/basic_usage.py) | Programmatic reserve → commit lifecycle |
| [decorator_usage.py](examples/decorator_usage.py) | `@cycles` decorator with estimates, caps, and metrics |
| [async_usage.py](examples/async_usage.py) | Async client and async decorator |
| [openai_integration.py](examples/openai_integration.py) | Guard OpenAI chat completions with budget checks |
| [anthropic_integration.py](examples/anthropic_integration.py) | Guard Anthropic messages with per-tool budget tracking |
| [streaming_usage.py](examples/streaming_usage.py) | Budget-managed streaming with token accumulation |
| [fastapi_integration.py](examples/fastapi_integration.py) | FastAPI middleware, dependency injection, per-tenant budgets |
| [langchain_integration.py](examples/langchain_integration.py) | LangChain callback handler for budget-aware agents |

See [examples/README.md](examples/README.md) for setup instructions.

## Development

```bash
pip install -e ".[dev]"

# Lint
ruff check .

# Type check (strict mode)
mypy runcycles

# Run tests with coverage (85% threshold enforced in CI)
pytest --cov runcycles --cov-fail-under=85
```

CI runs all three checks on Python 3.10 and 3.12 for every push and pull request.

## Documentation

- [Cycles Documentation](https://runcycles.io) — full docs site
- [Python Quickstart](https://runcycles.io/quickstart/getting-started-with-the-python-client) — getting started guide
- [Python Client Configuration Reference](https://runcycles.io/configuration/python-client-configuration-reference) — all configuration options
- [Error Handling Patterns in Python](https://runcycles.io/how-to/error-handling-patterns-in-python) — handling budget errors

## Requirements

- Python 3.10+
- httpx
- pydantic >= 2.0
