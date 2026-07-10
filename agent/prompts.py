# Each entry has a prompt and optional simulated user feedback.
# Feedback fields:
#   score    — 1 (positive), -1 (negative), or None (no numeric rating)
#   comment  — freeform text, or None

PROMPTS_WITH_FEEDBACK = [
    {
        "prompt": "What's the weather like in Paris today?",
        "feedback": {"score": 1, "comment": None},
    },
    {
        "prompt": "Search the web for information about OpenTelemetry.",
        "feedback": {"score": 1, "comment": None},
    },
    {
        "prompt": "What is the weather in Tokyo and Berlin right now?",
        "feedback": {
            "score": None,
            "comment": "Covered both cities well, but I would have liked a comparison summary.",
        },
    },
    {
        "prompt": "Search the web for Langfuse and tell me what it does.",
        "feedback": {"score": -1, "comment": None},
    },
    {
        "prompt": (
            "What's the weather in London and New York? "
            "Also search for information about large language models."
        ),
        "feedback": {
            "score": None,
            "comment": "Good multi-topic response. The LLM explanation was clear and concise.",
        },
    },
]
