"""Budget-managed streaming with Cycles.

Demonstrates the StreamReservation context manager: reserve on enter,
auto-commit on success, auto-release on exception.

Requirements:
    pip install runcycles openai

Environment variables:
    CYCLES_BASE_URL  - Cycles server URL (default: http://localhost:7878)
    CYCLES_API_KEY   - Cycles API key
    CYCLES_TENANT    - Tenant identifier
    OPENAI_API_KEY   - OpenAI API key
"""

import os

from openai import OpenAI

from runcycles import (
    Action,
    Amount,
    BudgetExceededError,
    CyclesClient,
    CyclesConfig,
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
# 2. Streaming with budget management (context manager API)
# ---------------------------------------------------------------------------
def stream_with_budget(
    prompt: str,
    max_tokens: int = 1024,
    model: str = "gpt-4o",
) -> str:
    """Stream an OpenAI response with Cycles budget protection.

    The StreamReservation context manager handles:
    - Creating a reservation on enter
    - Auto-committing actual cost on successful exit
    - Auto-releasing the reservation on exception
    - Heartbeat-based TTL extension for long streams
    """
    estimated_input_tokens = len(prompt.split()) * 2
    estimated_cost = estimated_input_tokens * PRICE_PER_INPUT_TOKEN + max_tokens * PRICE_PER_OUTPUT_TOKEN

    with cycles_client.stream_reservation(
        action=Action(kind="llm.completion", name=model),
        estimate=Amount(unit=Unit.USD_MICROCENTS, amount=estimated_cost),
        cost_fn=lambda u: u.tokens_input * PRICE_PER_INPUT_TOKEN + u.tokens_output * PRICE_PER_OUTPUT_TOKEN,
    ) as reservation:
        # Caps are available immediately after entering the context
        if reservation.caps and reservation.caps.max_tokens:
            max_tokens = min(max_tokens, reservation.caps.max_tokens)
            print(f"  Budget authority capped max_tokens to {max_tokens}")

        stream = openai_client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            stream=True,
            stream_options={"include_usage": True},
        )

        chunks: list[str] = []
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                text = chunk.choices[0].delta.content
                chunks.append(text)
                print(text, end="", flush=True)

            # The final chunk includes usage stats
            if chunk.usage:
                reservation.usage.tokens_input = chunk.usage.prompt_tokens
                reservation.usage.tokens_output = chunk.usage.completion_tokens

        print()  # newline after streaming

    # Auto-committed on exit with actual cost computed by cost_fn
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
