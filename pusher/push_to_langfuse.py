#!/usr/bin/env python3
"""
Push disk-collected spans to a Langfuse server.

This script is the "push phase" of the OTEL disk-tracing workflow.

The OpenTelemetry Collector file exporter appends newline-delimited OTLP JSON
to ``spans.json`` (one JSON object per line).  This script pushes **only new
lines since the last successful run**, using a checkpoint file in the traces
directory.  The active ``spans.json`` is never deleted or moved — the
collector keeps its open file descriptor valid.

When the collector rotates ``spans.json`` (built-in ``rotation:`` settings),
the renamed archive appears as ``spans-<timestamp>.json``.  The checkpoint
offset from the previous active file is inherited onto that rotated archive;
the new ``spans.json`` is tailed from offset zero.

Usage:
    python trace_pusher/push_to_langfuse.py [TRACES_DIR]

Arguments:
    TRACES_DIR  Directory containing spans*.json files.
                Defaults to /tmp/traces.

Options:
    --reset-checkpoint  Ignore saved offsets and push all lines from all files.

Required env vars:
    LANGFUSE_PUBLIC_KEY  (default: pk-lf-telemetry-poc)
    LANGFUSE_SECRET_KEY  (default: sk-lf-telemetry-poc)
    LANGFUSE_HOST        (default: http://localhost:3000)
"""

from __future__ import annotations

import argparse
import base64
import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

import requests
from otlp_utils import (
    collect_all_span_ids,
    merge_tool_calls,
    normalize_otlp_line,
    span_files,
)

DEFAULT_TRACES_DIR = Path("/tmp/traces")
CHECKPOINT_FILENAME = ".langfuse_push_state.json"
ACTIVE_SPAN_FILE = "spans.json"


def checkpoint_path(traces_dir: Path) -> Path:
    """Return the checkpoint file path for *traces_dir*."""
    return traces_dir / CHECKPOINT_FILENAME


def load_checkpoint_state(traces_dir: Path) -> dict[str, Any]:
    """Load per-file push offsets from disk."""
    path = checkpoint_path(traces_dir)
    if not path.exists():
        return {"files": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {"files": {}}
    files = data.get("files")
    if not isinstance(files, dict):
        return {"files": {}}
    return {"files": files}


def save_checkpoint_state(traces_dir: Path, state: dict[str, Any]) -> None:
    """Atomically persist checkpoint state."""
    path = checkpoint_path(traces_dir)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def prune_missing_files(
    files_state: dict[str, dict[str, Any]],
    traces_dir: Path,
) -> dict[str, dict[str, Any]]:
    """Drop checkpoint entries for span files that no longer exist."""
    pruned = {
        name: entry
        for name, entry in files_state.items()
        if (traces_dir / name).exists()
    }
    return pruned


def read_complete_lines_from_offset(
    path: Path,
    offset: int,
) -> tuple[list[str], int]:
    """Read complete JSON lines from *path* starting at *offset*.

    The returned offset always ends on a newline boundary so a partially
    written trailing line is retried on the next push.

    Parameters:
        path: Span file to read.
        offset: Byte offset to start from.

    Returns:
        Tuple of (complete line strings, new byte offset checkpoint).
    """
    if offset < 0:
        offset = 0

    with open(path, "rb") as handle:
        handle.seek(offset)
        chunk = handle.read()

    if not chunk:
        return [], offset

    if chunk.endswith(b"\n"):
        text = chunk.decode("utf-8")
        lines = [line for line in text.splitlines() if line.strip()]
        return lines, offset + len(chunk)

    last_newline = chunk.rfind(b"\n")
    if last_newline == -1:
        return [], offset

    complete = chunk[: last_newline + 1]
    lines = [line for line in complete.decode("utf-8").splitlines() if line.strip()]
    return lines, offset + len(complete)


def apply_rotation_handoff(
    files_state: dict[str, dict[str, Any]],
    traces_dir: Path,
    *,
    active_name: str = ACTIVE_SPAN_FILE,
) -> dict[str, dict[str, Any]]:
    """Inherit the active-file checkpoint onto the oldest new rotated archive.

    When ``spans.json`` is rotated by the collector, the previous contents
    move to ``spans-<timestamp>.json`` and a fresh ``spans.json`` is created.
    The byte offset stored for the old active file applies only to the **oldest**
    rotated file that has no checkpoint yet — that file is the former active
    file from the first rotation after the last push.

    If multiple rotations occurred between pushes, every other new rotated
    archive and the current ``spans.json`` are tailed from offset zero (full
    upload of their contents).

    Parameters:
        files_state: Mutable map of filename → ``{offset, inode}`` entries.
        traces_dir: Directory containing span files.
        active_name: Basename of the collector's active span file.

    Returns:
        Updated ``files_state`` map.
    """
    active_path = traces_dir / active_name
    if not active_path.exists():
        return files_state

    active_entry = files_state.get(active_name, {})
    stored_inode = active_entry.get("inode")
    stored_offset = int(active_entry.get("offset", 0))
    current_stat = active_path.stat()
    current_inode = current_stat.st_ino
    current_size = current_stat.st_size

    rotated_paths = sorted(
        (
            path
            for path in traces_dir.glob("spans*.json")
            if path.name != active_name and path.stat().st_size > 0
        ),
        key=lambda path: path.stat().st_mtime,
    )
    new_rotated = [path for path in rotated_paths if path.name not in files_state]

    rotation_detected = stored_inode is not None and stored_inode != current_inode
    rotation_detected = rotation_detected or (
        stored_offset > 0 and current_size < stored_offset and bool(new_rotated)
    )

    if rotation_detected and new_rotated:
        # Oldest uncheckpointed archive is the renamed active file from the
        # first rotation since the last successful push.
        oldest_rotated = new_rotated[0]
        files_state[oldest_rotated.name] = {
            "offset": stored_offset,
            "inode": oldest_rotated.stat().st_ino,
        }
        files_state[active_name] = {"offset": 0, "inode": current_inode}
        new_rotated = [
            path for path in new_rotated if path.name != oldest_rotated.name
        ]

    for path in new_rotated:
        files_state[path.name] = {
            "offset": 0,
            "inode": path.stat().st_ino,
        }

    if active_name not in files_state:
        files_state[active_name] = {"offset": 0, "inode": current_inode}
    else:
        files_state[active_name]["inode"] = current_inode

    return files_state


def file_start_offset(
    files_state: dict[str, dict[str, Any]],
    filename: str,
) -> int:
    """Return the saved byte offset for *filename*."""
    entry = files_state.get(filename, {})
    return int(entry.get("offset", 0))


def push_batches(
    batches: list[dict[str, Any]],
    *,
    otlp_url: str,
    otlp_headers: dict[str, str],
) -> tuple[int, bool]:
    """POST OTLP batches to Langfuse.

    Returns:
        Tuple of (batch count pushed, whether every POST succeeded).
    """
    pushed = 0
    all_ok = True
    for batch in batches:
        resp = requests.post(
            otlp_url,
            headers=otlp_headers,
            data=json.dumps(batch),
            timeout=30,
        )
        if resp.status_code not in (200, 204):
            all_ok = False
            print(
                f"\n  WARNING: OTLP push returned {resp.status_code}: "
                f"{resp.text[:200]}"
            )
        pushed += 1
    return pushed, all_ok


def main() -> None:
    """Push new span batches from disk to Langfuse."""
    parser = argparse.ArgumentParser(
        description="Push disk-collected spans to Langfuse."
    )
    parser.add_argument(
        "traces_dir",
        nargs="?",
        type=Path,
        default=DEFAULT_TRACES_DIR,
        help="Directory containing spans*.json files (default: %(default)s)",
    )
    parser.add_argument(
        "--reset-checkpoint",
        action="store_true",
        help="Push all lines from all span files and rebuild the checkpoint.",
    )
    args = parser.parse_args()
    traces_dir: Path = args.traces_dir.resolve()

    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "pk-lf-telemetry-poc")
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY", "sk-lf-telemetry-poc")
    host = os.environ.get("LANGFUSE_HOST", "http://localhost:3000")

    files = span_files(traces_dir)
    if not files:
        print(f"No span files found in {traces_dir}; nothing to push.")
        return

    checkpoint = (
        {"files": {}} if args.reset_checkpoint else load_checkpoint_state(traces_dir)
    )
    files_state = prune_missing_files(dict(checkpoint.get("files", {})), traces_dir)
    files_state = apply_rotation_handoff(files_state, traces_dir)

    # Full-file scan so phantom-parent stripping sees spans from earlier lines.
    known_span_ids = collect_all_span_ids(files)

    auth = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    otlp_url = f"{host}/api/public/otel/v1/traces"
    otlp_headers = {
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/json",
    }

    print(f"Found {len(files)} span file(s); pushing incrementally → {otlp_url}")
    total_batches = 0
    updated_state = deepcopy(files_state)

    for span_file in files:
        start_offset = (
            0
            if args.reset_checkpoint
            else file_start_offset(files_state, span_file.name)
        )
        raw_lines, new_offset = read_complete_lines_from_offset(span_file, start_offset)

        print(
            f"  {span_file.name} "
            f"(offset {start_offset} → {new_offset}, {len(raw_lines)} new batch(es)) ...",
            end=" ",
            flush=True,
        )

        if not raw_lines:
            updated_state[span_file.name] = {
                "offset": new_offset,
                "inode": span_file.stat().st_ino,
            }
            print("0 batch(es)")
            continue

        batches = [
            json.loads(normalize_otlp_line(line, known_span_ids)) for line in raw_lines
        ]
        batches = merge_tool_calls(batches)

        file_batches, all_ok = push_batches(
            batches,
            otlp_url=otlp_url,
            otlp_headers=otlp_headers,
        )
        print(f"{file_batches} batch(es)")

        if all_ok:
            updated_state[span_file.name] = {
                "offset": new_offset,
                "inode": span_file.stat().st_ino,
            }
            total_batches += file_batches
        else:
            print(
                f"  WARNING: checkpoint for {span_file.name} not advanced "
                f"(left at offset {start_offset})"
            )

    save_checkpoint_state(traces_dir, {"files": updated_state})
    print(f"  Total: {total_batches} new span batch(es) pushed.")
    print("\nDone. Open Langfuse UI to view traces:")
    print(f"  {host}")


if __name__ == "__main__":
    main()
