"""Decorator-based usage of the Cycles client."""

from runcycles import (
    CyclesClient,
    CyclesConfig,
    CyclesMetrics,
    cycles,
    get_cycles_context,
)

config = CyclesConfig(
    base_url="http://localhost:7878",
    api_key="your-api-key",
    tenant="acme",
    app="chat",
)
client = CyclesClient(config)


# Simplest form: constant estimate used as actual
@cycles(estimate=1000, client=client)
def simple_call() -> str:
    return "Hello"


# With callable estimate and actual
@cycles(
    estimate=lambda prompt, tokens: tokens * 10,
    actual=lambda result: len(result) * 5,
    action_kind="llm.completion",
    action_name="gpt-4",
    client=client,
)
def call_llm(prompt: str, tokens: int) -> str:
    ctx = get_cycles_context()
    if ctx:
        print(f"  Reservation: {ctx.reservation_id}, decision: {ctx.decision}")
        if ctx.has_caps():
            print(f"  Caps: max_tokens={ctx.caps.max_tokens}")

        # Report metrics
        ctx.metrics = CyclesMetrics(
            tokens_input=tokens,
            tokens_output=42,
            model_version="gpt-4-0613",
        )
        ctx.commit_metadata = {"source": "demo"}

    return "Generated response for: " + prompt


def main() -> None:
    print("Simple call:")
    result1 = simple_call()
    print(f"  Result: {result1}")

    print("\nLLM call with metrics:")
    result2 = call_llm("Tell me a joke", tokens=200)
    print(f"  Result: {result2}")


if __name__ == "__main__":
    main()
