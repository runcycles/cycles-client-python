"""Integrating Cycles with LangChain via the BaseCallbackHandler API.

This example uses LangChain's traditional callback-handler pattern to wrap each
LLM call with a Cycles reservation. It is the right fit for **non-agent**
LangChain workflows — bare ``ChatOpenAI``/``ChatAnthropic`` runnables, chains,
RAG pipelines, etc.

For LangChain **agents** built with ``langchain.agents.create_agent`` (the
``wrap_tool_call`` / ``before_model`` middleware API introduced in LangChain
1.x), use the dedicated middleware package instead:

    pip install langchain-runcycles
    # https://github.com/runcycles/langchain-runcycles

That package exposes ``CyclesModelGate``, ``CyclesToolGate``, and
``CyclesFanOutGate``. They work with sync and async agents and can deny model
or tool execution before it starts or halt an agent loop on remote policy.

Requirements:
    pip install runcycles langchain langchain-openai

Environment variables:
    CYCLES_BASE_URL  - Cycles server URL (default: http://localhost:7878)
    CYCLES_API_KEY   - Cycles API key
    CYCLES_TENANT    - Tenant identifier
    OPENAI_API_KEY   - OpenAI API key
"""

from __future__ import annotations

import os
import threading
import uuid
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import HumanMessage
from langchain_core.outputs import LLMResult
from langchain_openai import ChatOpenAI

from runcycles import (
    Action,
    Amount,
    BudgetExceededError,
    CyclesClient,
    CyclesConfig,
    StreamReservation,
    Subject,
    Unit,
)

# Pricing in USD microcents
PRICE_PER_INPUT_TOKEN = 250
PRICE_PER_CACHED_INPUT_TOKEN = 125
PRICE_PER_OUTPUT_TOKEN = 1_000


def _normalized_usage(response: LLMResult) -> tuple[int, int, int, str | None]:
    """Aggregate LangChain's provider-neutral ``AIMessage.usage_metadata``."""
    input_tokens = 0
    output_tokens = 0
    cached_input_tokens = 0
    model_version = None

    for generation_list in response.generations:
        for generation in generation_list:
            message = getattr(generation, "message", None)
            usage = getattr(message, "usage_metadata", None) or {}
            input_tokens += int(usage.get("input_tokens", 0))
            output_tokens += int(usage.get("output_tokens", 0))
            details = usage.get("input_token_details") or {}
            cached_input_tokens += int(details.get("cache_read", 0))
            response_metadata = getattr(message, "response_metadata", None) or {}
            model_version = model_version or response_metadata.get("model_name")

    return input_tokens, output_tokens, cached_input_tokens, model_version


# ---------------------------------------------------------------------------
# 1. Custom Callback Handler
# ---------------------------------------------------------------------------
class CyclesBudgetHandler(BaseCallbackHandler):
    """LangChain callback handler that wraps each LLM call with a Cycles reservation.

    Usage:
        handler = CyclesBudgetHandler(client, subject=Subject(tenant="acme"))
        llm = ChatOpenAI(callbacks=[handler])
    """

    # LangChain's default is False, which logs callback exceptions and lets the
    # model run. Budget denial and strict post-journal settlement errors must
    # reach the caller instead.
    raise_error = True

    def __init__(
        self,
        client: CyclesClient,
        subject: Subject,
        estimate_amount: int = 2_000_000,
        action_kind: str = "llm.completion",
        action_name: str = "gpt-4o",
    ) -> None:
        super().__init__()
        self.client = client
        self.subject = subject
        self.estimate_amount = estimate_amount
        self.action_kind = action_kind
        self.action_name = action_name
        # A handler instance may receive concurrent callback runs.
        self._reservations: dict[str, StreamReservation] = {}
        self._lock = threading.Lock()

    def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        *,
        run_id: uuid.UUID,
        **kwargs: Any,
    ) -> None:
        """Create a budget reservation before each LLM call."""
        run_key = str(run_id)
        reservation = self.client.stream_reservation(
            subject=self.subject,
            action=Action(kind=self.action_kind, name=self.action_name),
            estimate=Amount(unit=Unit.USD_MICROCENTS, amount=self.estimate_amount),
            ttl_ms=120_000,
            idempotency_key=f"langchain-llm-{run_key}",
            raise_on_commit_failure=True,
        )
        reservation.__enter__()
        with self._lock:
            self._reservations[run_key] = reservation

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: uuid.UUID,
        **kwargs: Any,
    ) -> None:
        """Commit actual cost after the LLM call completes."""
        run_key = str(run_id)
        with self._lock:
            reservation = self._reservations.pop(run_key, None)
        if reservation is None:
            return

        input_tokens, output_tokens, cached_input_tokens, model_version = _normalized_usage(response)
        billable_input_tokens = max(0, input_tokens - cached_input_tokens)
        reservation.usage.tokens_input = input_tokens
        reservation.usage.tokens_output = output_tokens
        reservation.usage.model_version = model_version or self.action_name
        reservation.usage.custom["cached_input_tokens"] = cached_input_tokens
        reservation.usage.actual_cost = (
            billable_input_tokens * PRICE_PER_INPUT_TOKEN
            + cached_input_tokens * PRICE_PER_CACHED_INPUT_TOKEN
            + output_tokens * PRICE_PER_OUTPUT_TOKEN
        )
        # Stops the heartbeat, journals known spend before the first commit,
        # and queues durable/event recovery before surfacing a commit failure.
        reservation.__exit__(None, None, None)

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: uuid.UUID,
        **kwargs: Any,
    ) -> None:
        """Release the reservation if the LLM call fails."""
        run_key = str(run_id)
        with self._lock:
            reservation = self._reservations.pop(run_key, None)
        if reservation is not None:
            reservation.__exit__(type(error), error, error.__traceback__)


# ---------------------------------------------------------------------------
# 2. Using the handler with a chat model
# ---------------------------------------------------------------------------
def simple_chain_example() -> None:
    """Run a simple LangChain invocation with budget protection."""
    config = CyclesConfig(
        base_url=os.environ.get("CYCLES_BASE_URL", "http://localhost:7878"),
        api_key=os.environ.get("CYCLES_API_KEY", "your-api-key"),
        tenant=os.environ.get("CYCLES_TENANT", "acme"),
    )
    client = CyclesClient(config)

    handler = CyclesBudgetHandler(
        client=client,
        subject=Subject(tenant=config.tenant, agent="langchain-agent"),
    )

    llm = ChatOpenAI(
        model="gpt-4o",
        callbacks=[handler],
    )

    print("=== Simple invocation ===")
    try:
        result = llm.invoke([HumanMessage(content="What is budget authority in one sentence?")])
        print(f"Response: {result.content}")
    except BudgetExceededError:
        print("Budget exhausted — cannot invoke LLM.")

    client.close()


# ---------------------------------------------------------------------------
# 3. Using with an agent that has tools
# ---------------------------------------------------------------------------
def agent_with_tools_example() -> None:
    """Run a LangChain agent with tools, each LLM call budget-guarded."""
    from langchain_core.tools import tool

    config = CyclesConfig(
        base_url=os.environ.get("CYCLES_BASE_URL", "http://localhost:7878"),
        api_key=os.environ.get("CYCLES_API_KEY", "your-api-key"),
        tenant=os.environ.get("CYCLES_TENANT", "acme"),
    )
    client = CyclesClient(config)

    handler = CyclesBudgetHandler(
        client=client,
        subject=Subject(tenant=config.tenant, agent="tool-agent", toolset="weather"),
    )

    @tool
    def get_weather(location: str) -> str:
        """Get the current weather for a location."""
        return f"72°F and sunny in {location}"

    llm = ChatOpenAI(model="gpt-4o", callbacks=[handler])
    llm_with_tools = llm.bind_tools([get_weather])

    print("\n=== Agent with tools ===")
    try:
        result = llm_with_tools.invoke([HumanMessage(content="What's the weather in San Francisco?")])
        print(f"Response: {result.content}")

        # If the model requested a tool call, show it
        if result.tool_calls:
            for tc in result.tool_calls:
                print(f"  Tool call: {tc['name']}({tc['args']})")
                tool_result = get_weather.invoke(tc["args"])
                print(f"  Tool result: {tool_result}")

    except BudgetExceededError:
        print("Budget exhausted — agent stopped.")

    client.close()


# ---------------------------------------------------------------------------
# 4. Run examples
# ---------------------------------------------------------------------------
def main() -> None:
    simple_chain_example()
    agent_with_tools_example()


if __name__ == "__main__":
    main()
