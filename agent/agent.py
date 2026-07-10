import os
from pydantic_ai import Agent

agent = Agent(
    "openai-chat:gpt-4o-mini",
    system_prompt=(
        "You are a helpful assistant. You can check the weather for cities "
        "and search the web for information. Always use the available tools "
        "when the user asks about weather or wants to search for something."
    ),
)


@agent.tool_plain
def get_weather(city: str) -> str:
    """Get the current weather for a given city."""
    data = {
        "Paris": "Partly cloudy, 18°C (64°F), humidity 65%, wind 12 km/h NW",
        "New York": "Sunny, 24°C (75°F), humidity 45%, wind 8 km/h SW",
        "Tokyo": "Rainy, 15°C (59°F), humidity 80%, wind 20 km/h E",
        "London": "Overcast, 12°C (54°F), humidity 75%, wind 15 km/h W",
        "Sydney": "Clear, 22°C (72°F), humidity 55%, wind 10 km/h NE",
        "Berlin": "Foggy, 9°C (48°F), humidity 90%, wind 5 km/h S",
    }
    return data.get(city, f"Weather data not available for {city}. Try: {', '.join(data.keys())}")


@agent.tool_plain
def search_web(query: str) -> str:
    """Search the web for information about a topic."""
    results = {
        "opentelemetry": (
            "OpenTelemetry is a vendor-neutral observability framework for instrumenting, "
            "generating, collecting, and exporting telemetry data (metrics, logs, traces). "
            "It is a CNCF incubating project with wide industry adoption."
        ),
        "langfuse": (
            "Langfuse is an open-source LLM engineering platform providing tracing, "
            "evaluation, and prompt management. It supports OpenTelemetry ingestion and "
            "can be self-hosted via Docker Compose."
        ),
        "mlflow": (
            "MLflow is an open-source platform for managing the ML lifecycle including "
            "experiment tracking, model registry, and LLM tracing. It exposes an OTLP "
            "endpoint for receiving OpenTelemetry spans."
        ),
        "pydantic": (
            "Pydantic is a Python data validation library. Pydantic AI is a Python agent "
            "framework built on top of Pydantic that provides OpenTelemetry instrumentation "
            "following the GenAI semantic conventions."
        ),
        "large language model": (
            "Large Language Models (LLMs) are neural networks trained on vast amounts of text. "
            "They power conversational AI systems and can follow complex instructions, generate "
            "text, write code, and reason about problems."
        ),
    }
    query_lower = query.lower()
    for key, result in results.items():
        if key in query_lower:
            return result
    return (
        f"Search results for '{query}': No cached results found. "
        "In a real deployment this would call a search API."
    )
