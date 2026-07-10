"""Shared OTLP JSON normalisation and post-processing utilities.

Used by both ``push_to_langfuse.py`` and ``push_to_mlflow.py`` to:

- Convert protobuf artefacts in OTLP JSON (base64 ID fields, string int64s).
- Strip dangling ``parentSpanId`` references (phantom parent spans).
- Merge ``running tool <name>`` span data into ``knowledge_search`` spans
  and remove the ``running tool`` spans before pushing to a backend.
- Discover span files on disk.
"""

import base64
import json
from pathlib import Path

# ---------------------------------------------------------------------------
# Bytes / int normalisation
# ---------------------------------------------------------------------------

_BYTES_AS_HEX: frozenset[str] = frozenset({"traceId", "spanId", "parentSpanId"})
_TRACE_ID_B64_LEN: int = 24
_SPAN_ID_B64_LEN: int = 12


def fix_bytes_fields(obj: object) -> object:
    """Convert base64-encoded OTLP bytes fields (traceId/spanId) to hex strings.

    ``google.protobuf.json_format.MessageToDict`` encodes ``bytes`` fields as
    base64.  The OTLP JSON spec requires ``traceId``, ``spanId``, and
    ``parentSpanId`` to be lowercase hex strings.  Strict consumers such as
    MLflow reject the base64 form with a 400 error.

    Safe to call on already-normalised data — hex strings don't match the
    expected base64 lengths and are passed through unchanged.

    Parameters:
        obj: A decoded JSON value (dict, list, or scalar).

    Returns:
        The same structure with base64 bytes fields converted to hex strings.
    """
    if isinstance(obj, dict):
        result: dict[str, object] = {}
        for k, v in obj.items():
            if k in _BYTES_AS_HEX and isinstance(v, str):
                expected = _TRACE_ID_B64_LEN if k == "traceId" else _SPAN_ID_B64_LEN
                if len(v) == expected:
                    try:
                        result[k] = base64.b64decode(v).hex()
                        continue
                    except Exception:  # noqa: BLE001
                        pass
            result[k] = fix_bytes_fields(v)
        return result
    if isinstance(obj, list):
        return [fix_bytes_fields(item) for item in obj]
    return obj


def fix_int_values(obj: object) -> object:
    """Convert ``intValue`` strings to JSON integers (protobuf int64 artefact).

    ``google.protobuf.json_format.MessageToDict`` serialises protobuf
    ``int64`` fields as JSON strings to preserve precision.  The OTLP JSON
    spec requires them to be JSON numbers.  Backends show ``null`` for token
    counts when they receive strings here.

    Parameters:
        obj: A decoded JSON value (dict, list, or scalar).

    Returns:
        The same structure with ``intValue`` strings converted to integers.
    """
    if isinstance(obj, dict):
        return {
            k: (int(v) if k == "intValue" and isinstance(v, str) else fix_int_values(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [fix_int_values(item) for item in obj]
    return obj


# ---------------------------------------------------------------------------
# Phantom-parent stripping
# ---------------------------------------------------------------------------


def strip_phantom_parents(otlp_dict: dict, known_span_ids: set[str]) -> dict:
    """Remove ``parentSpanId`` fields referencing spans absent from all files.

    Uses a two-pass approach: ``known_span_ids`` is built from every span ID
    across all span files so any ``parentSpanId`` not in that set is a
    dangling reference and is removed.  This handles ``NonRecordingSpan``
    virtual parents or other artefacts that were never exported.

    Parameters:
        otlp_dict: A decoded, normalised OTLP JSON batch.
        known_span_ids: All span IDs present in the span files being pushed.

    Returns:
        The same dict with dangling ``parentSpanId`` fields removed.
    """
    for rs in otlp_dict.get("resourceSpans", []):
        for ss in rs.get("scopeSpans", []):
            for span in ss.get("spans", []):
                psid = span.get("parentSpanId")
                if psid and psid not in known_span_ids:
                    del span["parentSpanId"]
    return otlp_dict


def collect_all_span_ids(files: list[Path]) -> set[str]:
    """Return every spanId that appears in the given span files (first pass).

    Parameters:
        files: Paths to span files (OTLP JSON, one batch per line).

    Returns:
        Set of hex span ID strings.
    """
    ids: set[str] = set()
    for path in files:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                data = fix_bytes_fields(json.loads(line))
                for rs in data.get("resourceSpans", []):  # type: ignore[union-attr]
                    for ss in rs.get("scopeSpans", []):  # type: ignore[union-attr]
                        for span in ss.get("spans", []):  # type: ignore[union-attr]
                            sid = span.get("spanId")
                            if sid:
                                ids.add(sid)
    return ids


def normalize_otlp_line(line: str, known_span_ids: set[str]) -> str:
    """Parse, normalise, and re-serialise one OTLP JSON line.

    Applies :func:`fix_int_values`, :func:`fix_bytes_fields`, and
    :func:`strip_phantom_parents` in sequence.

    Parameters:
        line: Raw JSON string (one line from a spans file).
        known_span_ids: All span IDs present in the files being pushed.

    Returns:
        A single compact JSON string ready for posting to an OTLP endpoint.
    """
    data = strip_phantom_parents(
        fix_bytes_fields(fix_int_values(json.loads(line))),  # type: ignore[arg-type]
        known_span_ids,
    )
    return json.dumps(data)


# ---------------------------------------------------------------------------
# Span extraction helpers
# ---------------------------------------------------------------------------


def get_attr_str(attrs: list, key: str) -> str | None:
    """Return the ``stringValue`` for *key* from an OTLP attributes list.

    Parameters:
        attrs: List of OTLP attribute dicts (``{"key": ..., "value": {...}}``).
        key: Attribute key to look up.

    Returns:
        The string value, or ``None`` if the key is not present.
    """
    for attr in attrs:
        if isinstance(attr, dict) and attr.get("key") == key:
            val = attr.get("value", {})
            if isinstance(val, dict) and "stringValue" in val:
                return str(val["stringValue"])
    return None


def extract_spans(otlp: dict) -> list[dict]:
    """Return every span dict contained in an OTLP export batch.

    Parameters:
        otlp: Decoded OTLP JSON batch dict.

    Returns:
        Flat list of span dicts.
    """
    spans: list[dict] = []
    for rs in otlp.get("resourceSpans", []):
        for ss in rs.get("scopeSpans", []):
            spans.extend(ss.get("spans", []))
    return spans


# ---------------------------------------------------------------------------
# Post-processing: merge running-tool spans into knowledge_search spans
# ---------------------------------------------------------------------------


def merge_tool_calls(batches: list[dict]) -> list[dict]:
    """Merge ``running tool X`` span data into ``knowledge_search`` spans.

    LlamaStack emits ``knowledge_search`` spans with no attributes — the
    query and retrieved results are not recorded on the span itself.
    ``record_tool_call_spans`` in ``tracing.tracer`` emits ``running tool
    <name>`` spans that carry the correct ``input``, ``output``, and
    ``gen_ai.tool.name``.

    This function:

    1. Finds all ``knowledge_search`` spans grouped by ``parentSpanId``.
    2. Finds all ``running tool X`` spans grouped by ``parentSpanId``.
    3. Positionally matches them within each parent group (1st ks ↔ 1st
       tool, etc.) and injects ``input``, ``output``, and
       ``gen_ai.tool.name`` onto the ``knowledge_search`` span.
    4. Removes all ``running tool X`` spans from the returned batch list.

    Parameters:
        batches: List of decoded, normalised OTLP batch dicts.

    Returns:
        Modified batch list with merges applied and ``running tool`` spans
        removed.
    """
    ks_by_parent: dict[str, list[tuple[int, dict]]] = {}
    tool_by_parent: dict[str, list[tuple[int, dict]]] = {}

    for batch_idx, batch in enumerate(batches):
        for span in extract_spans(batch):
            name: str = span.get("name", "")
            parent: str = span.get("parentSpanId", "")
            if name == "knowledge_search":
                ks_by_parent.setdefault(parent, []).append((batch_idx, span))
            elif name.startswith("running tool "):
                tool_by_parent.setdefault(parent, []).append((batch_idx, span))

    if not tool_by_parent:
        return batches

    # Collect tool span IDs for removal.
    tool_span_ids: set[str] = set()
    all_parents = set(ks_by_parent) | set(tool_by_parent)
    for parent_id in all_parents:
        ks_list = ks_by_parent.get(parent_id, [])
        tool_list = tool_by_parent.get(parent_id, [])
        for i, (_, tool_span) in enumerate(tool_list):
            tool_attrs = tool_span.get("attributes", [])
            tool_input = get_attr_str(tool_attrs, "input")
            tool_output = get_attr_str(tool_attrs, "output")
            tool_name = get_attr_str(tool_attrs, "gen_ai.tool.name")

            if i < len(ks_list):
                _, ks_span = ks_list[i]
                existing_keys = {
                    a.get("key")
                    for a in ks_span.get("attributes", [])
                    if isinstance(a, dict)
                }
                for key, val in (
                    ("input", tool_input),
                    ("output", tool_output),
                    ("gen_ai.tool.name", tool_name),
                ):
                    if val is not None and key not in existing_keys:
                        ks_span.setdefault("attributes", []).append(
                            {"key": key, "value": {"stringValue": val}}
                        )

            sid = tool_span.get("spanId")
            if sid:
                tool_span_ids.add(sid)

    result: list[dict] = []
    for batch in batches:
        new_batch = json.loads(json.dumps(batch))  # deep copy
        for rs in new_batch.get("resourceSpans", []):
            for ss in rs.get("scopeSpans", []):
                ss["spans"] = [
                    s for s in ss.get("spans", [])
                    if s.get("spanId") not in tool_span_ids
                ]
        has_spans = any(
            ss.get("spans")
            for rs in new_batch.get("resourceSpans", [])
            for ss in rs.get("scopeSpans", [])
        )
        if has_spans:
            result.append(new_batch)

    return result


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------


def span_files(traces_dir: Path) -> list[Path]:
    """Return all span files in *traces_dir*, sorted chronologically.

    Finds both ``spans.json`` (the current active append-only file) and
    rotated archives matching ``spans_*.json``.  Also accepts legacy
    ``turn_*.json`` files for backward compatibility.

    Parameters:
        traces_dir: Directory to search.

    Returns:
        Sorted list of non-empty span file paths.
    """
    files = sorted(f for f in traces_dir.glob("spans*.json") if f.stat().st_size > 0)
    legacy = sorted(f for f in traces_dir.glob("turn_*.json") if f.stat().st_size > 0)
    return files + legacy
