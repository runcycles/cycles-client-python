"""Async usage of the Cycles client."""

import asyncio

from runcycles import (
    AsyncCyclesClient,
    CyclesConfig,
    CyclesMetrics,
    cycles,
    get_cycles_context,
)


config = CyclesConfig(
    base_url="http://localhost:7878",
    api_key="your-api-key",
    tenant="acme",
)
client = AsyncCyclesClient(config)


@cycles(
    estimate=lambda prompt: len(prompt) * 10,
    actual=lambda result: len(result) * 5,
    action_kind="llm.completion",
    action_name="gpt-4",
    client=client,
)
async def call_llm(prompt: str) -> str:
    ctx = get_cycles_context()
    if ctx:
        print(f"  Reservation: {ctx.reservation_id}")
        ctx.metrics = CyclesMetrics(tokens_input=100, tokens_output=50)

    # Simulate async LLM call
    await asyncio.sleep(0.1)
    return f"Response to: {prompt}"


async def main() -> None:
    async with client:
        result = await call_llm("Hello, world!")
        print(f"Result: {result}")


if __name__ == "__main__":
    asyncio.run(main())
