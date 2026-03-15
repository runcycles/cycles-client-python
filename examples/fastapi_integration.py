"""Integrating Cycles with FastAPI.

Demonstrates middleware-based preflight checks, dependency injection,
per-tenant budget isolation, and exception handling.

Requirements:
    pip install runcycles fastapi uvicorn

Environment variables:
    CYCLES_BASE_URL  - Cycles server URL (default: http://localhost:7878)
    CYCLES_API_KEY   - Cycles API key
    CYCLES_TENANT    - Default tenant (overridden per-request via header)

Run:
    python examples/fastapi_integration.py
    # Then: curl -H "X-Tenant-ID: acme" http://localhost:8000/chat?prompt=hello
"""

import os
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from runcycles import (
    Action,
    Amount,
    AsyncCyclesClient,
    BudgetExceededError,
    CyclesConfig,
    CyclesMetrics,
    CyclesProtocolError,
    DecisionRequest,
    Subject,
    Unit,
    cycles,
    get_cycles_context,
    set_default_client,
)

# ---------------------------------------------------------------------------
# 1. Configure Cycles
# ---------------------------------------------------------------------------
config = CyclesConfig(
    base_url=os.environ.get("CYCLES_BASE_URL", "http://localhost:7878"),
    api_key=os.environ.get("CYCLES_API_KEY", "your-api-key"),
    tenant=os.environ.get("CYCLES_TENANT", "acme"),
    app="fastapi-example",
)


# ---------------------------------------------------------------------------
# 2. Lifespan: manage client lifecycle
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    client = AsyncCyclesClient(config)
    set_default_client(client)
    app.state.cycles_client = client
    yield
    await client.aclose()


app = FastAPI(title="Cycles + FastAPI Example", lifespan=lifespan)


# ---------------------------------------------------------------------------
# 3. Exception handlers
# ---------------------------------------------------------------------------
@app.exception_handler(BudgetExceededError)
async def budget_exceeded_handler(request: Request, exc: BudgetExceededError):
    return JSONResponse(
        status_code=402,
        content={
            "error": "budget_exceeded",
            "message": "Insufficient budget for this request.",
            "retry_after_ms": exc.retry_after_ms,
        },
    )


@app.exception_handler(CyclesProtocolError)
async def protocol_error_handler(request: Request, exc: CyclesProtocolError):
    status = 429 if exc.is_retryable() else 503
    return JSONResponse(
        status_code=status,
        content={
            "error": str(exc.error_code),
            "message": str(exc),
            "retry_after_ms": exc.retry_after_ms,
        },
    )


# ---------------------------------------------------------------------------
# 4. Dependencies
# ---------------------------------------------------------------------------
def get_tenant(x_tenant_id: str = Header(default="acme")) -> str:
    """Extract tenant from request header."""
    return x_tenant_id


def get_client(request: Request) -> AsyncCyclesClient:
    """Provide the Cycles client as a FastAPI dependency."""
    return request.app.state.cycles_client


# ---------------------------------------------------------------------------
# 5. Middleware: preflight budget check
# ---------------------------------------------------------------------------
@app.middleware("http")
async def budget_preflight_middleware(request: Request, call_next):
    """Check budget before processing expensive endpoints."""
    # Only check specific paths
    if request.url.path not in ("/chat", "/summarize"):
        return await call_next(request)

    tenant = request.headers.get("X-Tenant-ID", "acme")
    client: AsyncCyclesClient = request.app.state.cycles_client

    response = await client.decide(
        DecisionRequest(
            idempotency_key=str(uuid.uuid4()),
            subject=Subject(tenant=tenant, app="fastapi-example"),
            action=Action(kind="api.request", name=request.url.path),
            estimate=Amount(unit=Unit.USD_MICROCENTS, amount=1_000_000),
        )
    )

    if response.is_success:
        decision = response.get_body_attribute("decision")
        if decision == "DENY":
            return JSONResponse(
                status_code=402,
                content={"error": "budget_exceeded", "message": "Preflight denied."},
            )

    return await call_next(request)


# ---------------------------------------------------------------------------
# 6. Budget-guarded endpoint
# ---------------------------------------------------------------------------
@cycles(
    estimate=lambda prompt, max_tokens=256, **kw: max_tokens * 1_000,
    actual=lambda result: result.get("cost", 0),
    action_kind="llm.completion",
    action_name="gpt-4o",
    unit="USD_MICROCENTS",
    ttl_ms=30_000,
)
async def guarded_llm_call(prompt: str, max_tokens: int = 256, tenant: str = "acme") -> dict:
    """Simulate an LLM call with budget protection."""
    ctx = get_cycles_context()

    if ctx and ctx.has_caps() and ctx.caps.max_tokens:
        max_tokens = min(max_tokens, ctx.caps.max_tokens)

    # Simulate LLM response (replace with actual OpenAI/Anthropic call)
    response_text = f"Simulated response to: {prompt}"
    input_tokens = len(prompt.split()) * 2
    output_tokens = len(response_text.split()) * 2

    if ctx:
        ctx.metrics = CyclesMetrics(
            tokens_input=input_tokens,
            tokens_output=output_tokens,
            model_version="gpt-4o",
            custom={"tenant": tenant},
        )

    cost = input_tokens * 250 + output_tokens * 1_000

    return {
        "content": response_text,
        "cost": cost,
        "tokens": {"input": input_tokens, "output": output_tokens},
    }


@app.get("/chat")
async def chat_endpoint(
    prompt: str,
    max_tokens: int = 256,
    tenant: str = Depends(get_tenant),
):
    """Chat endpoint with per-tenant budget isolation."""
    result = await guarded_llm_call(prompt, max_tokens=max_tokens, tenant=tenant)
    return {
        "response": result["content"],
        "tokens": result["tokens"],
    }


# ---------------------------------------------------------------------------
# 7. Balance endpoint
# ---------------------------------------------------------------------------
@app.get("/budget/{tenant_id}")
async def get_budget(
    tenant_id: str,
    client: AsyncCyclesClient = Depends(get_client),
):
    """Query remaining budget for a tenant."""
    response = await client.get_balances(tenant=tenant_id)
    if not response.is_success:
        raise HTTPException(status_code=500, detail=response.error_message)
    return response.body


# ---------------------------------------------------------------------------
# 8. Health check
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# 9. Run
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
