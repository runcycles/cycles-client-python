# Cycles Python Client Examples

Runnable examples demonstrating how to integrate the `runcycles` client into real-world Python applications.

## Prerequisites

1. A running Cycles server (see [Deploy the Full Stack](https://runcycles.io/quickstart/deploying-the-full-cycles-stack))
2. Set environment variables:

```bash
export CYCLES_BASE_URL="http://localhost:7878"
export CYCLES_API_KEY="your-api-key"   # create via Admin Server — see link above
export CYCLES_TENANT="acme"
```

3. Install the client:

```bash
pip install runcycles
```

## Examples

| File | Description | Extra Dependencies |
|------|-------------|-------------------|
| [basic_usage.py](basic_usage.py) | Programmatic reserve → commit lifecycle | — |
| [decorator_usage.py](decorator_usage.py) | `@cycles` decorator with estimates, caps, and metrics | — |
| [async_usage.py](async_usage.py) | Async client and async decorator | — |
| [openai_integration.py](openai_integration.py) | Guard OpenAI chat completions with budget checks | `openai` |
| [anthropic_integration.py](anthropic_integration.py) | Guard Anthropic messages with per-tool budget tracking | `anthropic` |
| [streaming_usage.py](streaming_usage.py) | `stream_reservation()` context manager with auto-commit | `openai` |
| [fastapi_integration.py](fastapi_integration.py) | FastAPI middleware, dependency injection, per-tenant budgets | `fastapi`, `uvicorn` |
| [langchain_integration.py](langchain_integration.py) | LangChain callback handler for budget-aware agents | `langchain`, `langchain-openai` |

## Running

```bash
# Basic examples (only need a Cycles server)
python examples/basic_usage.py
python examples/decorator_usage.py
python examples/async_usage.py

# Integration examples (need additional API keys)
export OPENAI_API_KEY="sk-..."
python examples/openai_integration.py
python examples/streaming_usage.py

export ANTHROPIC_API_KEY="sk-ant-..."
python examples/anthropic_integration.py

# FastAPI (starts a server on port 8000)
pip install fastapi uvicorn
python examples/fastapi_integration.py

# LangChain
pip install langchain langchain-openai
python examples/langchain_integration.py
```
