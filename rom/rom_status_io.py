from __future__ import annotations

import base64
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path

Scalar = str | int | float | bool | None


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _snapshot_payload(
    fields: Mapping[str, Scalar], tail: Sequence[str], updated: str
) -> tuple[dict[str, Scalar], list[str], bytes, bytes]:
    clean_tail = list(tail)
    merged = dict(fields)
    merged["heartbeat"] = str(merged.get("heartbeat") or updated)
    merged["updated"] = str(merged.get("updated") or updated)
    tail_bytes = "\n".join(clean_tail).encode()
    merged["tail64"] = base64.urlsafe_b64encode(tail_bytes).decode().rstrip("=") or "-"
    flat = " ".join(f"{key}={str(value).replace(' ', '_')}" for key, value in merged.items())
    document: dict[str, Scalar | list[str]] = dict(merged)
    document["tail"] = clean_tail
    return merged, clean_tail, f"{flat}\n".encode(), (json.dumps(document, sort_keys=True) + "\n").encode()


def append_transition(
    event_file: Path,
    merged: Mapping[str, Scalar],
    clean_tail: Sequence[str],
    previous_state: Scalar,
) -> None:
    event: dict[str, Scalar | list[str]] = dict(merged)
    event["tail"] = list(clean_tail)
    event["previous_state"] = previous_state
    event_file.parent.mkdir(parents=True, exist_ok=True)
    with event_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def write_status_snapshot(
    status_file: Path,
    fields: Mapping[str, Scalar],
    tail: Sequence[str],
    updated: str,
) -> dict[str, Scalar]:
    merged, _clean_tail, flat_payload, json_payload = _snapshot_payload(fields, tail, updated)
    _atomic_write(status_file, flat_payload)
    _atomic_write(status_file.with_name(f"{status_file.name}.json"), json_payload)
    return merged


def write_terminal_fallback(
    status_file: Path,
    event_file: Path,
    fields: Mapping[str, Scalar],
    previous_state: Scalar,
    tail: Sequence[str],
    updated: str,
) -> dict[str, Scalar]:
    merged, clean_tail, flat_payload, json_payload = _snapshot_payload(fields, tail, updated)
    _atomic_write(status_file, flat_payload)
    _atomic_write(status_file.with_name(f"{status_file.name}.json"), json_payload)
    append_transition(event_file, merged, clean_tail, previous_state)
    return merged
