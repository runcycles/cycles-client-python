"""Budget-managed streaming with Cycles.

Demonstrates the programmatic reserve → stream → commit pattern where the
actual cost is only known after the stream completes.

Requirements:
    pip install runcycles openai

Environment variables:
    CYCLES_BASE_URL  - Cycles server URL (default: http://localhost:7878)
    CYCLES_API_KEY   - Cycles API key
    CYCLES_TENANT    - Tenant identifier
    OPENAI_API_KEY   - OpenAI API key
"""

import os
import time
import uuid

from openai import OpenAI

from runcycles import (
    Action,
    Amount,
    BudgetExceededError,
    CommitRequest,
    CyclesClient,
    CyclesConfig,
    CyclesMetrics,
    CyclesProtocolError,
    ReleaseRequest,
    ReservationCreateRequest,
    Subject,
    Unit,
)

# ---------------------------------------------------------------------------
# 1. Configure clients
# ---------------------------------------------------------------------------
config = CyclesConfig(
    base_url=os.environ.get("CYCLES_BASE_URL", "http://localhost:7878"),
    api_key=os.environ.get("CYCLES_API_KEY", "your-api-key"),
    tenant=os.environ.get("CYCLES_TENANT", "acme"),
)
cycles_client = CyclesClient(config)
openai_client = OpenAI()

PRICE_PER_INPUT_TOKEN = 250
PRICE_PER_OUTPUT_TOKEN = 1_000


# ---------------------------------------------------------------------------
# 2. Streaming with budget management
# ---------------------------------------------------------------------------
def stream_with_budget(
    prompt: str,
    max_tokens: int = 1024,
    model: str = "gpt-4o",
) -> str:
    """Stream an OpenAI response with Cycles budget protection.

    The pattern:
    1. Reserve budget based on max_tokens (worst case)
    2. Stream the response, accumulating output
    3. Commit the actual cost after the stream completes
    4. Release the reservation if streaming fails
    """
    estimated_input_tokens = len(prompt.split()) * 2
    estimated_cost = (
        estimated_input_tokens * PRICE_PER_INPUT_TOKEN
        + max_tokens * PRICE_PER_OUTPUT_TOKEN
    )

    idempotency_key = str(uuid.uuid4())

    # Step 1: Reserve budget
    reserve_response = cycles_client.create_reservation(
        ReservationCreateRequest(
            idempotency_key=idempotency_key,
            subject=Subject(tenant=config.tenant, agent="streaming-agent"),
            action=Action(kind="llm.completion", name=model),
            estimate=Amount(unit=Unit.USD_MICROCENTS, amount=estimated_cost),
            ttl_ms=120_000,  # longer TTL for streaming
        )
    )

    if not reserve_response.is_success:
        error = reserve_response.get_error_response()
        if error and error.error == "BUDGET_EXCEEDED":
            raise BudgetExceededError(
                error.message,
                status=reserve_response.status,
                error_code=error.error,
                request_id=error.request_id,
                details=error.details,
            )
        msg = error.message if error else (reserve_response.error_message or "Reservation failed")
        raise CyclesProtocolError(
            msg,
            status=reserve_response.status,
            error_code=error.error if error else None,
            request_id=error.request_id if error else None,
            details=error.details if error else None,
        )

    reservation_id = reserve_response.get_body_attribute("reservation_id")
    decision = reserve_response.get_body_attribute("decision")

    # Check for caps
    caps = reserve_response.get_body_attribute("caps")
    if caps and caps.get("max_tokens"):
        max_tokens = min(max_tokens, caps["max_tokens"])
        print(f"  Budget authority capped max_tokens to {max_tokens}")

    # Step 2: Stream the response
    start_time = time.time()
    chunks: list[str] = []
    completion_tokens = 0

    try:
        stream = openai_client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            stream=True,
            stream_options={"include_usage": True},
        )

        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                text = chunk.choices[0].delta.content
                chunks.append(text)
                print(text, end="", flush=True)

            # The final chunk includes usage stats
            if chunk.usage:
                input_tokens = chunk.usage.prompt_tokens
                completion_tokens = chunk.usage.completion_tokens

        print()  # newline after streaming

    except Exception:
        # If streaming fails, release the reservation to free budget
        cycles_client.release_reservation(
            reservation_id,
            ReleaseRequest(idempotency_key=f"release-{idempotency_key}"),
        )
        raise

    # Step 3: Commit actual cost
    elapsed_ms = int((time.time() - start_time) * 1000)
    actual_cost = (
        input_tokens * PRICE_PER_INPUT_TOKEN
        + completion_tokens * PRICE_PER_OUTPUT_TOKEN
    )

    commit_response = cycles_client.commit_reservation(
        reservation_id,
        CommitRequest(
            idempotency_key=f"commit-{idempotency_key}",
            actual=Amount(unit=Unit.USD_MICROCENTS, amount=actual_cost),
            metrics=CyclesMetrics(
                tokens_input=input_tokens,
                tokens_output=completion_tokens,
                latency_ms=elapsed_ms,
                model_version=model,
                custom={"streamed": True, "decision": decision},
            ),
        ),
    )

    if not commit_response.is_success:
        print(f"  Warning: commit failed: {commit_response.error_message}")

    savings = estimated_cost - actual_cost
    print(f"  Estimated: {estimated_cost} microcents, Actual: {actual_cost} microcents")
    print(f"  Budget saved by accurate commit: {savings} microcents")

    return "".join(chunks)


# ---------------------------------------------------------------------------
# 3. Run it
# ---------------------------------------------------------------------------
def main() -> None:
    print("Streaming with budget management:\n")

    try:
        result = stream_with_budget(
            prompt="Write a haiku about budgets.",
            max_tokens=100,
        )
        print(f"\nFull response: {result}")
    except BudgetExceededError:
        print("Budget exhausted — cannot stream.")


if __name__ == "__main__":
    main()
