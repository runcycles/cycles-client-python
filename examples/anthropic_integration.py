"""Integrating Cycles with the Anthropic Python SDK.

Guards Anthropic Messages API calls with budget reservations and
demonstrates per-tool-call budget tracking for agentic workflows.

Requirements:
    pip install runcycles anthropic

Environment variables:
    CYCLES_BASE_URL    - Cycles server URL (default: http://localhost:7878)
    CYCLES_API_KEY     - Cycles API key
    CYCLES_TENANT      - Tenant identifier
    ANTHROPIC_API_KEY  - Anthropic API key
"""

import os
import uuid

from anthropic import Anthropic

from runcycles import (
    Action,
    Amount,
    BudgetExceededError,
    CommitRequest,
    CyclesClient,
    CyclesConfig,
    CyclesMetrics,
    ReservationCreateRequest,
    Subject,
    Unit,
    cycles,
    get_cycles_context,
    set_default_client,
)

# ---------------------------------------------------------------------------
# 1. Configure clients
# ---------------------------------------------------------------------------
config = CyclesConfig(
    base_url=os.environ.get("CYCLES_BASE_URL", "http://localhost:7878"),
    api_key=os.environ.get("CYCLES_API_KEY", "your-api-key"),
    tenant=os.environ.get("CYCLES_TENANT", "acme"),
    app="anthropic-example",
)
cycles_client = CyclesClient(config)
set_default_client(cycles_client)

anthropic_client = Anthropic()  # reads ANTHROPIC_API_KEY from env

# Pricing in USD microcents (1 USD = 100_000_000 microcents).
PRICE_PER_INPUT_TOKEN = 300       # $3.00 / 1M tokens
PRICE_PER_OUTPUT_TOKEN = 1_500    # $15.00 / 1M tokens


# ---------------------------------------------------------------------------
# 2. Simple decorator-based integration
# ---------------------------------------------------------------------------
@cycles(
    estimate=lambda prompt, **kw: (
        len(prompt.split()) * 2 * PRICE_PER_INPUT_TOKEN
        + kw.get("max_tokens", 1024) * PRICE_PER_OUTPUT_TOKEN
    ),
    actual=lambda result: (
        result["usage"]["input_tokens"] * PRICE_PER_INPUT_TOKEN
        + result["usage"]["output_tokens"] * PRICE_PER_OUTPUT_TOKEN
    ),
    action_kind="llm.completion",
    action_name="claude-sonnet-4-20250514",
    unit="USD_MICROCENTS",
    ttl_ms=60_000,
)
def send_message(prompt: str, max_tokens: int = 1024) -> dict:
    """Send a message to Claude with budget protection."""
    ctx = get_cycles_context()

    # Respect caps
    if ctx and ctx.has_caps() and ctx.caps.max_tokens:
        max_tokens = min(max_tokens, ctx.caps.max_tokens)

    response = anthropic_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )

    if ctx:
        ctx.metrics = CyclesMetrics(
            tokens_input=response.usage.input_tokens,
            tokens_output=response.usage.output_tokens,
            model_version=response.model,
        )

    return {
        "content": response.content[0].text,
        "usage": {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        },
    }


# ---------------------------------------------------------------------------
# 3. Per-tool-call budget tracking (programmatic)
# ---------------------------------------------------------------------------
TOOLS = [
    {
        "name": "get_weather",
        "description": "Get current weather for a location.",
        "input_schema": {
            "type": "object",
            "properties": {"location": {"type": "string"}},
            "required": ["location"],
        },
    }
]


def handle_tool_call(tool_name: str, tool_input: dict) -> str:
    """Simulate executing a tool."""
    if tool_name == "get_weather":
        return f"72°F and sunny in {tool_input.get('location', 'unknown')}"
    return "Unknown tool"


def chat_with_tools(prompt: str) -> str:
    """Multi-turn conversation with tool use, each LLM call budget-guarded."""
    messages = [{"role": "user", "content": prompt}]
    turn = 0

    while True:
        turn += 1
        idempotency_key = f"tool-chat-{uuid.uuid4()}"

        # Reserve budget for this turn
        reserve_response = cycles_client.create_reservation(
            ReservationCreateRequest(
                idempotency_key=idempotency_key,
                subject=Subject(
                    tenant=config.tenant,
                    agent="tool-agent",
                    toolset="weather-tools",
                ),
                action=Action(
                    kind="llm.completion",
                    name="claude-sonnet-4-20250514",
                    tags=[f"turn-{turn}"],
                ),
                estimate=Amount(unit=Unit.USD_MICROCENTS, amount=2_000_000),
                ttl_ms=30_000,
            )
        )

        if not reserve_response.is_success:
            error_resp = reserve_response.get_error_response()
            if error_resp and error_resp.error == "BUDGET_EXCEEDED":
                return "Budget exhausted — cannot continue conversation."
            return f"Reservation failed: {reserve_response.error_message}"

        reservation_id = reserve_response.get_body_attribute("reservation_id")

        # Call Claude
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            tools=TOOLS,
            messages=messages,
        )

        # Commit actual cost
        actual_cost = (
            response.usage.input_tokens * PRICE_PER_INPUT_TOKEN
            + response.usage.output_tokens * PRICE_PER_OUTPUT_TOKEN
        )
        cycles_client.commit_reservation(
            reservation_id,
            CommitRequest(
                idempotency_key=f"commit-{idempotency_key}",
                actual=Amount(unit=Unit.USD_MICROCENTS, amount=actual_cost),
                metrics=CyclesMetrics(
                    tokens_input=response.usage.input_tokens,
                    tokens_output=response.usage.output_tokens,
                    model_version=response.model,
                    custom={"turn": turn, "tool_use": response.stop_reason == "tool_use"},
                ),
            ),
        )

        # Process response
        if response.stop_reason == "end_turn":
            # Final text response
            for block in response.content:
                if block.type == "text":
                    return block.text
            return ""

        if response.stop_reason == "tool_use":
            # Extract tool calls and execute them
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = handle_tool_call(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            messages.append({"role": "user", "content": tool_results})
        else:
            # Unexpected stop reason
            return f"Stopped with reason: {response.stop_reason}"

        if turn >= 5:
            return "Maximum turns reached."


# ---------------------------------------------------------------------------
# 4. Run it
# ---------------------------------------------------------------------------
def main() -> None:
    print("=== Simple message ===")
    try:
        result = send_message("What is budget authority in one sentence?")
        print(f"Response: {result['content']}")
        print(f"Tokens: {result['usage']}")
    except BudgetExceededError:
        print("Budget exhausted.")

    print("\n=== Tool-use conversation ===")
    answer = chat_with_tools("What's the weather like in San Francisco?")
    print(f"Answer: {answer}")


if __name__ == "__main__":
    main()
