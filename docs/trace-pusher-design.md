# Trace Pusher Design: Checkpointing, File Rotation, and Incremental Push

## Overview

The trace pipeline uses an **indirect push model**: the OTel Collector writes
spans to disk as newline-delimited JSON, and a separate long-running process
(the trace pusher) periodically reads those files and POSTs new spans to
Langfuse. The two processes share a volume but are otherwise decoupled.

This document explains why a checkpointing scheme is necessary, how the
collector's file exporter and its log-rotation behaviour interact with the
pusher, and the design choices made to handle each edge case.

---

## The File Exporter's Behaviour

The OTel Collector's `file` exporter appends one complete OTLP JSON object per
line to a single file (`spans.json`). Key properties:

- **Append-only while open.** The collector holds a persistent open file
  descriptor. New span batches are appended to the end; existing content is
  never modified or truncated.
- **One batch per line.** Each line is an independent, self-contained OTLP
  export request serialised as JSON. There is no partial batch across lines.
- **Lines may be partially written.** If the pusher reads the file at the exact
  moment the collector is in the middle of flushing a line, the last line in
  the file may be incomplete (no trailing newline yet).
- **Rotation renames, not truncates.** When the rotation thresholds are hit
  (configurable by size, age, or backup count), the collector closes and renames
  the active file to `spans-<timestamp>.json`, then opens a fresh `spans.json`.
  The renamed file is complete; the new active file starts at byte zero.

## Why Rotation Alone Cannot Drive Push Timing

An appealing simpler design would be: configure the collector to rotate
`spans.json` frequently, and have the pusher only process *completed* rotated
archives (files that are no longer active). Each rotated file is pushed once and
then ignored. No byte-offset tracking required.

This does not work in practice because the file exporter's rotation settings
are coarse-grained file-management controls, not push-timing controls:

- **Size-based rotation** (`max_megabytes`) accepts whole megabytes; 1 MB is
  the smallest unit. For a low-traffic agent PoC, even 1 MB of spans may take
  hours or days to accumulate — far longer than a 15-minute push interval.
- **Age-based rotation** (`max_days`) accepts whole days; 1 day is the smallest
  unit. The underlying rotation library (lumberjack) has no sub-day trigger.
  There is no way to rotate every hour or every 15 minutes.
- Setting both thresholds as small as possible (1 MB / 1 day) still only
  guarantees at most one rotation per day, regardless of how frequently the
  pusher wants to run.

The fundamental issue is that rotation is designed to **bound file size and
age** for storage management. Push timing is a separate concern. Conflating the
two forces you to choose between pushing too infrequently (coarse rotation) or
generating excessive file churn (fine rotation).

The byte-offset checkpoint design decouples the two concerns entirely: the
collector rotates on its own schedule for storage reasons, while the pusher runs
on its own interval (15 minutes by default) and reads whatever new bytes have
accumulated in the active file since the last successful push — regardless of
whether any rotation has occurred.

## Why Naive Re-Reading Would Not Work

Without checkpointing, the pusher has two bad options:

1. **Re-read everything on every cycle.** Every span ever written would be
   re-posted to Langfuse, producing duplicate traces on each push interval.
   Langfuse deduplicates by `traceId+spanId` in some cases, but duplicate
   batches still cause noise, wasted network I/O, and incorrect span counts.

2. **Delete or truncate after reading.** The collector holds an open file
   descriptor to `spans.json`. Deleting or truncating the file from under the
   collector corrupts its write position — subsequent appends may go to a
   ghost inode or overwrite old content. This is a classic "don't delete a file
   that another process has open" problem.

Checkpointing solves both: the pusher tracks **where it last successfully
read** in each file using byte offsets, advances those offsets only after a
confirmed successful POST, and never touches the files themselves.

---

## The Checkpoint File

The pusher maintains a single JSON file in the traces directory:

```
/tmp/traces/.langfuse_push_state.json
```

Structure:

```json
{
  "files": {
    "spans.json": {
      "offset": 14728,
      "inode": 3145728
    },
    "spans-2026-07-09T14:30:00.json": {
      "offset": 204800,
      "inode": 3145727
    }
  }
}
```

- **`offset`** — byte position in the file up to which all lines have been
  successfully pushed. On the next cycle, the pusher `seek()`s to this offset
  and reads only new bytes.
- **`inode`** — the filesystem inode number of the file at the time of the last
  push. Used to detect rotation (see below).

The checkpoint is written **atomically** using a write-then-rename pattern
(`tmp → final`). A crash mid-write leaves the previous checkpoint intact; the
file is never in a partially-written state.

Offsets are advanced **only on success**. If a POST returns a non-2xx status,
the offset for that file is not updated, and the same lines are retried on the
next push cycle.

---

## Handling Partially-Written Trailing Lines

Because the collector may be in mid-write when the pusher reads, the pusher
does not blindly read to the end of the file. Instead,
`read_complete_lines_from_offset` backs the read up to the **last newline
boundary**:

```
[complete line]\n[complete line]\n[partial line...
                                  ^^^^ stop here
```

The offset saved to the checkpoint points to the byte immediately after the
last `\n`. The incomplete trailing line is left for the next cycle, when the
collector will have finished writing it.

This means a span batch can never be skipped due to a race — it is simply
deferred by one push interval.

---

## The Rotation Handoff Problem

Rotation introduces a critical naming ambiguity. Consider this sequence:

```
Before rotation:
  spans.json  (size: 100 KB, inode: 42, checkpoint offset: 80 KB)

Collector hits rotation threshold:
  spans.json         → renamed to spans-20260709T143000.json  (inode: 42)
  spans.json (new)   → created fresh                          (inode: 43, size: 0)

Next push cycle sees:
  spans.json                    — inode 43, size 0 (new active file)
  spans-20260709T143000.json    — inode 42, size 100 KB (the renamed former active)
```

The checkpoint still has an entry keyed by **name** (`spans.json`) with offset
80 KB. But that entry now describes a file that *no longer exists at that
path*. The data from bytes 80 KB to 100 KB (20 KB of spans written just before
rotation) is in `spans-20260709T143000.json` — and there is no checkpoint entry
for it yet.

Without a handoff, the pusher would:
- Read `spans-20260709T143000.json` from offset **0** → re-push the first 80 KB
  of already-pushed spans, causing duplicates.
- Read `spans.json` from offset **80 KB** → seek past end of a zero-byte file,
  reading nothing.

### The `apply_rotation_handoff` Solution

Before processing any files, the pusher calls `apply_rotation_handoff`, which:

1. **Detects rotation** by comparing the stored inode for `spans.json` against
   the current inode on disk. If they differ, rotation has occurred. As a
   fallback (for filesystems that reuse inodes), it also treats a situation
   where `stored_offset > current_size` and new rotated files exist as a
   rotation signal.

2. **Finds the oldest newly-seen rotated archive** — the rotated file that does
   not yet have a checkpoint entry, sorted by modification time ascending. This
   is the former active file from the first rotation since the last push.

3. **Inherits the saved offset onto that oldest new archive.** The rotated file
   *is* the old `spans.json`; the data from the saved offset to the end of the
   rotated file is the previously-unseen tail.

4. **Resets the active-file checkpoint to offset 0** with the new inode. The
   fresh `spans.json` has not been read at all.

After the handoff:

```
spans-20260709T143000.json:  offset = 80 KB, inode = 42  ← inherited
spans.json:                  offset = 0,      inode = 43  ← reset
```

The pusher then reads the tail of the rotated archive (bytes 80 KB → 100 KB)
and the new active file from the beginning — exactly the spans that were not
yet pushed.

### Multiple Rotations Between Pushes

If the push interval is long relative to the rotation interval (e.g. a slow
Langfuse or a 15-minute push cycle with a small rotation threshold), multiple
rotations may occur between pushes:

```
spans.json  (active, inode 45)
spans-T3.json  (second rotation, inode 44)
spans-T2.json  (first rotation since last push, inode 43)
spans-T1.json  (older, already checkpointed, inode 42)
```

Only `spans-T2.json` — the **oldest** new (uncheckpointed) rotated file — gets
the inherited offset. This is the file that was the active `spans.json` at the
time of the first rotation, and it is the only one that contains the
not-yet-pushed tail from before the push interval ended.

All other new rotated archives (`spans-T3.json`) and the current active
`spans.json` (inode 45) are read from offset 0. They contain only spans written
after the first rotation and were never partially pushed.

---

## The Phantom Parent Problem

The agent code creates a `NonRecordingSpan` as a virtual conversation root — a
synthetic parent span that carries a stable `traceId` (derived from the
conversation ID) so all turns of the same conversation share one trace in
Langfuse. The `NonRecordingSpan` is never exported: it carries context forward
but emits no spans of its own.

pydantic-ai's instrumentation records child spans that reference this
non-exported parent via `parentSpanId`. When those spans are posted to Langfuse
with a `parentSpanId` that Langfuse has never seen, Langfuse either orphans them
or rejects the reference, depending on its version.

### Two-Pass Phantom Strip

The pusher resolves this with a two-pass approach:

**Pass 1 (`collect_all_span_ids`)** — scans every byte of every span file
(including already-pushed sections) and builds the complete set of known
`spanId` values. This is intentionally a full scan rather than an incremental
one: spans from earlier lines may be the parents of spans in later lines, and
we cannot know at incremental-read time whether a `parentSpanId` is phantom or
merely not-yet-seen.

**Pass 2 (`strip_phantom_parents`)** — applied to each normalised batch before
pushing. Any `parentSpanId` not present in the known-span-ids set is removed
from the span. The span is still pushed; it just becomes a root span in Langfuse
rather than a child of a missing parent.

This approach correctly handles the `NonRecordingSpan` case (its ID is never
written to any file, so it is never in the known set) while preserving all real
parent–child relationships within the exported spans.

---

## The Atomic Checkpoint Write

The checkpoint save uses a write-to-temp-then-rename pattern:

```python
tmp = path.with_suffix(".json.tmp")
tmp.write_text(json.dumps(state), encoding="utf-8")
tmp.replace(path)  # atomic on POSIX
```

`os.replace` (underlying `Path.replace`) is atomic on POSIX-compliant
filesystems: any reader of `.langfuse_push_state.json` sees either the old
complete file or the new complete file, never a partially-written intermediate
state. If the pusher crashes between writing the `.tmp` file and replacing it,
the `.tmp` file is left behind but the checkpoint is unchanged — the next cycle
will overwrite `.tmp` and proceed normally.

---

## The `--reset-checkpoint` Escape Hatch

If the checkpoint becomes inconsistent (e.g. the traces volume was cleared and
recreated, or a bug caused the offset to advance past real data), the pusher
can be restarted with `PUSH_RESET_CHECKPOINT=true` (or
`python push_to_langfuse.py --reset-checkpoint`). This ignores the saved state,
reads every line in every file from offset 0, and rebuilds a fresh checkpoint.

Langfuse deduplicates spans by `traceId+spanId`, so a full re-push is safe
in most cases — it may create duplicate trace entries in the UI for the same
conversation, but individual spans within a trace are idempotent.

---

## Summary of Invariants

| Invariant | How it is upheld |
|-----------|-----------------|
| No span is pushed twice on normal operation | Byte-offset checkpoint advanced only after successful POST |
| No span is lost across a rotation | Rotation handoff inherits the active-file offset onto the renamed archive |
| Partial writes don't cause parse errors | Read only up to the last newline boundary; defer the rest |
| Checkpoint is always consistent on disk | Atomic write-then-rename |
| Phantom parent references don't orphan spans | Two-pass span-ID scan + strip before push |
| Push failure does not corrupt the checkpoint | Offset not advanced if any POST fails |
