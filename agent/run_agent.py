#!/usr/bin/env python3
"""
Run the tracing-poc agent and emit OTEL traces to the configured collector.

Usage — single prompt:
    python run_agent.py --prompt "What's the weather in Paris?"

Usage — single prompt with feedback:
    python run_agent.py --prompt "What's the weather in Paris?" --feedback-score 1
    python run_agent.py --prompt "..." --feedback-score -1 --feedback-comment "Too brief."

Usage — feedback only (no new prompt):
    python run_agent.py --conversation-id <hex> --feedback-score 1
    python run_agent.py --conversation-id <hex> --feedback-score -1 --feedback-comment "Too brief."
    python run_agent.py --conversation-id <hex> --turn-trace-id <hex> --feedback-score 1

Usage — run canned batch of prompts (with pre-canned feedback):
    python run_agent.py

Usage — continue an existing conversation:
    python run_agent.py --conversation-id <32-char-hex> --prompt "Follow-up question"

The conversation ID is printed at startup. Pass it back via --conversation-id on
subsequent runs to group all turns under the same OTEL trace ID.

Environment variables:
    OTEL_COLLECTOR_ENDPOINT    OTLP HTTP endpoint (default: http://localhost:4318)
    OTEL_SERVICE_NAME          Service name in traces (default: telemetry-poc)
    OPENAI_API_KEY             OpenAI API key (required)
    TELEMETRY_CONVERSATION_ID  32-char hex conversation ID (optional)
    TELEMETRY_PROMPT           Single prompt to run (optional, overrides batch)
    TELEMETRY_USER_ID          User ID attached to every span (default: anonymous)
    TELEMETRY_FEEDBACK_SCORE   Feedback score: 1 (positive) or -1 (negative) (optional)
    TELEMETRY_FEEDBACK_COMMENT Feedback comment text (optional)
    TELEMETRY_TURN_TRACE_ID    Specific turn trace ID to link feedback to (optional)
"""

import argparse
import asyncio
import json
import os
import random
import uuid

from opentelemetry import trace as otel_trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

from pydantic_ai import Agent

from agent import agent
from prompts import PROMPTS_WITH_FEEDBACK


def setup_otel() -> None:
    endpoint = os.environ.get("OTEL_COLLECTOR_ENDPOINT", "http://localhost:4318")
    service_name = os.environ.get("OTEL_SERVICE_NAME", "telemetry-poc")
    provider = TracerProvider(
        resource=Resource({"service.name": service_name})
    )
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces"))
    )
    otel_trace.set_tracer_provider(provider)


def new_conversation_id() -> str:
    """Generate a random 128-bit hex string for use as an OTEL trace_id.

    OTEL trace IDs are exactly 128 bits (32 hex chars). uuid4().hex is exactly
    32 chars. Adding extra chars produces a >128-bit integer which the OTEL SDK
    marks as is_valid=False, causing the context to be silently discarded.
    """
    return uuid.uuid4().hex


def conversation_context(conversation_id: str) -> otel_trace.Context:
    """Return an OTEL context whose trace_id equals conversation_id.

    Any span started with this context inherits the same trace_id, so multiple
    turns of the same conversation appear as one trace in the backend.
    The NonRecordingSpan acts as a virtual remote parent — never exported, it
    only carries the trace context forward.
    """
    span_context = SpanContext(
        trace_id=int(conversation_id, 16),
        span_id=random.getrandbits(64),
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )
    return otel_trace.set_span_in_context(NonRecordingSpan(span_context))


def record_feedback(
    tracer: otel_trace.Tracer,
    ctx: otel_trace.Context,
    score: int | None,
    comment: str | None,
    turn_trace_id: str | None = None,
) -> None:
    """Emit a user_feedback span under the conversation context.

    Both score and comment are optional; if neither is provided the span is
    skipped (no feedback to record). turn_trace_id, if given, links this
    feedback to a specific prior turn.
    """
    if score is None and not comment:
        return
    with tracer.start_as_current_span("user_feedback", context=ctx) as span:
        if turn_trace_id:
            span.set_attribute("feedback.turn_trace_id", turn_trace_id)
        if score is not None:
            span.set_attribute("feedback.score", score)
        if comment:
            span.set_attribute("feedback.comment", comment)

    parts = []
    if score is not None:
        parts.append(f"score={score:+d}")
    if comment:
        parts.append(f"comment='{comment}'")
    print(f"Feedback recorded: {', '.join(parts)}")


async def run_prompts(
    items: list[dict],
    conversation_id: str,
    user_id: str,
) -> None:
    """Run a list of prompt items, each a dict with 'prompt' and optional 'feedback'."""
    tracer = otel_trace.get_tracer("run_agent")
    ctx = conversation_context(conversation_id)

    for i, item in enumerate(items, 1):
        prompt = item["prompt"]
        feedback = item.get("feedback", {})

        print(f"\n{'='*60}")
        print(f"Turn {i}/{len(items)}: {prompt}")

        with tracer.start_as_current_span("agent_turn", context=ctx) as span:
            turn_trace_id = format(span.get_span_context().trace_id, "032x")
            span.set_attribute(
                "gen_ai.input.messages",
                json.dumps([{"role": "user", "content": prompt}]),
            )
            span.set_attribute("user.id", user_id)
            span.set_attribute("conversation.id", conversation_id)

            result = await agent.run(prompt)

            span.set_attribute(
                "gen_ai.output.messages",
                json.dumps([{"role": "assistant", "content": result.output}]),
            )

        print(f"Response:  {result.output}")
        print(f"Trace ID:  {turn_trace_id}")

        record_feedback(
            tracer, ctx,
            score=feedback.get("score"),
            comment=feedback.get("comment"),
            turn_trace_id=turn_trace_id,
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the tracing-poc agent and emit OTEL traces to the configured collector.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Environment variables:\n"
            "  OTEL_COLLECTOR_ENDPOINT, OTEL_SERVICE_NAME\n"
            "  OPENAI_API_KEY, TELEMETRY_CONVERSATION_ID\n"
            "  TELEMETRY_PROMPT, TELEMETRY_USER_ID\n"
            "  TELEMETRY_FEEDBACK_SCORE, TELEMETRY_FEEDBACK_COMMENT"
        ),
    )
    parser.add_argument(
        "--prompt",
        metavar="TEXT",
        default=os.environ.get("TELEMETRY_PROMPT"),
        help=(
            "Run a single prompt instead of the canned batch. "
            "Env: TELEMETRY_PROMPT."
        ),
    )
    parser.add_argument(
        "--conversation-id",
        metavar="HEX",
        default=os.environ.get("TELEMETRY_CONVERSATION_ID"),
        help=(
            "32-char hex conversation ID to continue an existing trace. "
            "Omit to start a new conversation. Env: TELEMETRY_CONVERSATION_ID."
        ),
    )
    parser.add_argument(
        "--user-id",
        metavar="ID",
        default=os.environ.get("TELEMETRY_USER_ID", "anonymous"),
        help="User identity attached to every span. Env: TELEMETRY_USER_ID.",
    )
    parser.add_argument(
        "--feedback-score",
        metavar="SCORE",
        type=int,
        choices=[1, -1],
        default=(
            int(os.environ["TELEMETRY_FEEDBACK_SCORE"])
            if os.environ.get("TELEMETRY_FEEDBACK_SCORE")
            else None
        ),
        help=(
            "Feedback score for a single-prompt run: 1 (positive) or -1 (negative). "
            "Ignored in batch mode (feedback is pre-canned per prompt). "
            "Env: TELEMETRY_FEEDBACK_SCORE."
        ),
    )
    parser.add_argument(
        "--feedback-comment",
        metavar="TEXT",
        default=os.environ.get("TELEMETRY_FEEDBACK_COMMENT"),
        help=(
            "Freeform feedback comment. In batch mode, ignored (feedback is pre-canned). "
            "Env: TELEMETRY_FEEDBACK_COMMENT."
        ),
    )
    parser.add_argument(
        "--turn-trace-id",
        metavar="HEX",
        default=os.environ.get("TELEMETRY_TURN_TRACE_ID"),
        help=(
            "32-char hex trace ID of a specific prior turn to link feedback to. "
            "Only relevant in feedback-only mode. Env: TELEMETRY_TURN_TRACE_ID."
        ),
    )
    args = parser.parse_args()

    has_feedback = args.feedback_score is not None or bool(args.feedback_comment)
    feedback_only = has_feedback and not args.prompt

    if feedback_only and not args.conversation_id:
        parser.error("--conversation-id is required when submitting feedback without a prompt")

    conversation_id = args.conversation_id or new_conversation_id()

    print(f"\n{'='*60}")
    print(f"Collector:       {os.environ.get('OTEL_COLLECTOR_ENDPOINT', 'http://localhost:4318')}")
    print(f"Conversation ID: {conversation_id}")
    if not feedback_only:
        print(f"User ID:         {args.user_id}")
    print(f"{'='*60}")

    setup_otel()
    Agent.instrument_all()

    if feedback_only:
        tracer = otel_trace.get_tracer("run_agent")
        ctx = conversation_context(conversation_id)
        record_feedback(
            tracer, ctx,
            score=args.feedback_score,
            comment=args.feedback_comment,
            turn_trace_id=args.turn_trace_id,
        )
    else:
        if args.prompt:
            items = [{
                "prompt": args.prompt,
                "feedback": {"score": args.feedback_score, "comment": args.feedback_comment},
            }]
        else:
            items = PROMPTS_WITH_FEEDBACK

        asyncio.run(run_prompts(items, conversation_id, args.user_id))

        print(f"\n{'='*60}")
        print("Session complete.")
        print(f"Conversation ID: {conversation_id}")
        print(f"  (pass --conversation-id {conversation_id} to continue this conversation)")


if __name__ == "__main__":
    main()
