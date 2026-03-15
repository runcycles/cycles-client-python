"""Integrating Cycles with LangChain.

Demonstrates a custom callback handler that creates budget reservations
for every LLM call in a LangChain chain or agent.

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

# Pricing in USD microcents
PRICE_PER_INPUT_TOKEN = 250
PRICE_PER_OUTPUT_TOKEN = 1_000


# ---------------------------------------------------------------------------
# 1. Custom Callback Handler
# ---------------------------------------------------------------------------
class CyclesBudgetHandler(BaseCallbackHandler):
    """LangChain callback handler that wraps each LLM call with a Cycles reservation.

    Usage:
        handler = CyclesBudgetHandler(client, subject=Subject(tenant="acme"))
        llm = ChatOpenAI(callbacks=[handler])
    """

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
        # Track active reservations by run_id
        self._reservations: dict[str, str] = {}
        self._idempotency_keys: dict[str, str] = {}

    def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        *,
        run_id: uuid.UUID,
        **kwargs: Any,
    ) -> None:
        """Create a budget reservation before each LLM call."""
        key = str(uuid.uuid4())
        self._idempotency_keys[str(run_id)] = key

        response = self.client.create_reservation(
            ReservationCreateRequest(
                idempotency_key=key,
                subject=self.subject,
                action=Action(kind=self.action_kind, name=self.action_name),
                estimate=Amount(unit=Unit.USD_MICROCENTS, amount=self.estimate_amount),
                ttl_ms=60_000,
            )
        )

        if not response.is_success:
            error = response.get_error_response()
            if error and error.error_code == "BUDGET_EXCEEDED":
                raise BudgetExceededError(
                    error.message,
                    status=response.status,
                    error_code=error.error,
                    request_id=error.request_id,
                    details=error.details,
                )
            msg = error.message if error else (response.error_message or "Reservation failed")
            raise CyclesProtocolError(
                msg,
                status=response.status,
                error_code=error.error if error else None,
                request_id=error.request_id if error else None,
                details=error.details if error else None,
            )

        reservation_id = response.get_body_attribute("reservation_id")
        self._reservations[str(run_id)] = reservation_id

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: uuid.UUID,
        **kwargs: Any,
    ) -> None:
        """Commit actual cost after the LLM call completes."""
        run_key = str(run_id)
        reservation_id = self._reservations.pop(run_key, None)
        idempotency_key = self._idempotency_keys.pop(run_key, None)

        if not reservation_id or not idempotency_key:
            return

        # Extract token usage from LangChain's response
        token_usage = (response.llm_output or {}).get("token_usage", {})
        input_tokens = token_usage.get("prompt_tokens", 0)
        output_tokens = token_usage.get("completion_tokens", 0)

        actual_cost = (
            input_tokens * PRICE_PER_INPUT_TOKEN
            + output_tokens * PRICE_PER_OUTPUT_TOKEN
        )

        self.client.commit_reservation(
            reservation_id,
            CommitRequest(
                idempotency_key=f"commit-{idempotency_key}",
                actual=Amount(unit=Unit.USD_MICROCENTS, amount=actual_cost),
                metrics=CyclesMetrics(
                    tokens_input=input_tokens,
                    tokens_output=output_tokens,
                    model_version=token_usage.get("model_name", self.action_name),
                ),
            ),
        )

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: uuid.UUID,
        **kwargs: Any,
    ) -> None:
        """Release the reservation if the LLM call fails."""
        run_key = str(run_id)
        reservation_id = self._reservations.pop(run_key, None)
        idempotency_key = self._idempotency_keys.pop(run_key, None)

        if reservation_id and idempotency_key:
            self.client.release_reservation(
                reservation_id,
                ReleaseRequest(idempotency_key=f"release-{idempotency_key}"),
            )


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
        result = llm_with_tools.invoke(
            [HumanMessage(content="What's the weather in San Francisco?")]
        )
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
