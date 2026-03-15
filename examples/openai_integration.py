"""Integrating Cycles with the OpenAI Python SDK.

Guards OpenAI chat completion calls with budget reservations,
caps-aware token limiting, and accurate cost tracking.

Requirements:
    pip install runcycles openai

Environment variables:
    CYCLES_BASE_URL   - Cycles server URL (default: http://localhost:7878)
    CYCLES_API_KEY    - Cycles API key
    CYCLES_TENANT     - Tenant identifier
    OPENAI_API_KEY    - OpenAI API key
"""

import os

from openai import OpenAI

from runcycles import (
    CyclesClient,
    CyclesConfig,
    CyclesMetrics,
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
    app="openai-example",
)
cycles_client = CyclesClient(config)
set_default_client(cycles_client)

# ---------------------------------------------------------------------------
# 2. Configure OpenAI
# ---------------------------------------------------------------------------
openai_client = OpenAI()  # reads OPENAI_API_KEY from env

# Approximate per-token pricing in USD microcents (1 USD = 100_000_000 microcents).
# Adjust these for the model you use.
PRICE_PER_INPUT_TOKEN = 250       # $2.50 / 1M tokens → 250 microcents / token
PRICE_PER_OUTPUT_TOKEN = 1_000    # $10.00 / 1M tokens → 1000 microcents / token


def estimate_cost(prompt: str, max_tokens: int = 1024) -> int:
    """Estimate the worst-case cost before calling the API."""
    estimated_input_tokens = len(prompt.split()) * 2  # rough tokenizer proxy
    input_cost = estimated_input_tokens * PRICE_PER_INPUT_TOKEN
    output_cost = max_tokens * PRICE_PER_OUTPUT_TOKEN
    return input_cost + output_cost


def actual_cost(result: dict) -> int:
    """Compute the real cost from the API response usage."""
    usage = result["usage"]
    return (
        usage["prompt_tokens"] * PRICE_PER_INPUT_TOKEN
        + usage["completion_tokens"] * PRICE_PER_OUTPUT_TOKEN
    )


# ---------------------------------------------------------------------------
# 3. Budget-guarded OpenAI call
# ---------------------------------------------------------------------------
@cycles(
    estimate=lambda prompt, **kw: estimate_cost(prompt, kw.get("max_tokens", 1024)),
    actual=actual_cost,
    action_kind="llm.completion",
    action_name="gpt-4o",
    unit="USD_MICROCENTS",
    ttl_ms=60_000,
)
def chat_completion(prompt: str, max_tokens: int = 1024) -> dict:
    """Call OpenAI with budget protection."""
    ctx = get_cycles_context()

    # Respect caps: if the budget authority limits max_tokens, obey it
    if ctx and ctx.has_caps() and ctx.caps.max_tokens:
        max_tokens = min(max_tokens, ctx.caps.max_tokens)

    response = openai_client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
    )

    usage = response.usage

    # Report detailed metrics for observability
    if ctx:
        ctx.metrics = CyclesMetrics(
            tokens_input=usage.prompt_tokens,
            tokens_output=usage.completion_tokens,
            latency_ms=None,  # set if you measure wall-clock time
            model_version=response.model,
        )

    return {
        "content": response.choices[0].message.content,
        "usage": {
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
        },
    }


# ---------------------------------------------------------------------------
# 4. Run it
# ---------------------------------------------------------------------------
def main() -> None:
    from runcycles import BudgetExceededError

    try:
        result = chat_completion("Explain what budget authority means in three sentences.")
        print(f"Response: {result['content']}")
        print(f"Tokens used: {result['usage']}")
    except BudgetExceededError:
        print("Budget exhausted — falling back to cached response.")


if __name__ == "__main__":
    main()
