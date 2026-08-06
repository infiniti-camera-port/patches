"""Live build progress and durable status snapshots."""

from __future__ import annotations

import base64
import json
import os
import re
import selectors
import subprocess
import time
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, NewType, Protocol

RunId = NewType("RunId", str)
Scalar = str | int | float | bool | None

ANSI_RE: Final = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
CONTROL_RE: Final = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
NINJA_RE: Final = re.compile(
    r"^\[\s*(?P<percent>\d+)%\s+(?P<done>\d+)/(?P<total>\d+)"
    r"(?:\s+(?P<eta>[^]]+))?\]"
)
PHASES: Final = ("STARTING", "SOONG", "NINJA", "PACKAGING", "SIGNING", "PUBLISHING")
PACKAGING_MARKERS: Final = ("ota_from_target_files", "full_update_generator", "Package Complete:")
SIGNING_MARKERS: Final = ("payload_signer", "Signing the output file")


def utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sanitize_line(line: str) -> str:
    return CONTROL_RE.sub("", ANSI_RE.sub("", line)).rstrip()


def bounded_tail(lines: Sequence[str], limit_lines: int = 40, limit_bytes: int = 8192) -> list[str]:
    selected: deque[str] = deque(maxlen=limit_lines)
    size = 0
    for raw in reversed(lines):
        clean = sanitize_line(raw)
        encoded = clean.encode("utf-8", errors="replace")
        if selected and size + len(encoded) + 1 > limit_bytes:
            break
        selected.appendleft(clean)
        size += len(encoded) + 1
    return list(selected)


def error_excerpt(lines: Sequence[str]) -> list[str]:
    triggers = ("FAILED:", "ninja: error", "error:", "Traceback (most recent call last)")
    for index in range(len(lines) - 1, -1, -1):
        if any(token in lines[index] for token in triggers):
            return bounded_tail(lines[max(0, index - 3) : index + 7])
    return bounded_tail(lines)


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


class StatusWriter:
    """Write compatible flat status and structured state atomically."""

    def __init__(self, status_file: Path, event_file: Path | None = None) -> None:
        self.status_file = status_file
        self.json_file = status_file.with_name(f"{status_file.name}.json")
        self.event_file = event_file or status_file.parent / "rom-events.jsonl"
        self.last_fields: dict[str, Scalar] = {}

    def write(
        self,
        fields: Mapping[str, Scalar],
        tail: Sequence[str] = (),
        *,
        transition: bool = False,
    ) -> None:
        clean_tail = bounded_tail(tail)
        merged = dict(fields)
        merged["heartbeat"] = str(merged.get("heartbeat") or utc_now())
        merged["updated"] = str(merged.get("updated") or utc_now())
        tail_bytes = "\n".join(clean_tail).encode()
        merged["tail64"] = base64.urlsafe_b64encode(tail_bytes).decode().rstrip("=") or "-"
        flat = " ".join(f"{key}={str(value).replace(' ', '_')}" for key, value in merged.items())
        _atomic_write(self.status_file, f"{flat}\n".encode())
        document = dict(merged)
        document["tail"] = clean_tail
        _atomic_write(self.json_file, (json.dumps(document, sort_keys=True) + "\n").encode())
        if transition:
            event = dict(document)
            event["previous_state"] = self.last_fields.get("state", "-")
            self.event_file.parent.mkdir(parents=True, exist_ok=True)
            with self.event_file.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, sort_keys=True) + "\n")
        self.last_fields = merged

    def read(self) -> dict[str, Scalar | list[str]]:
        return json.loads(self.json_file.read_text())


@dataclass(frozen=True, slots=True)
class Progress:
    phase: str
    percent: int | None = None
    completed: int | None = None
    total: int | None = None
    eta_seconds: int | None = None
    native_eta: str | None = None


@dataclass(slots=True)  # noqa: MUTABLE_OK - streaming parser is an accumulator
class ProgressParser:  # noqa: MUTABLE_OK
    """Mutable accumulator for one process's line-oriented progress."""

    phase: str = "STARTING"
    lines: deque[str] = field(default_factory=lambda: deque(maxlen=200))
    samples: deque[tuple[float, int, int]] = field(default_factory=lambda: deque(maxlen=8))

    def feed(self, raw: str, now: float | None = None) -> Progress:
        line = sanitize_line(raw)
        self.lines.append(line)
        previous = self.phase
        if line == "@@ROM_STAGE=SOONG":
            self.phase = "SOONG"
            self.samples.clear()
        elif line == "@@ROM_STAGE=NINJA":
            self.phase = "NINJA"
            self.samples.clear()
        elif any(marker in line for marker in SIGNING_MARKERS):
            self.phase = "SIGNING"
        elif any(marker in line for marker in PACKAGING_MARKERS) and PHASES.index(self.phase) < PHASES.index("PACKAGING"):
            self.phase = "PACKAGING"

        match = NINJA_RE.match(line)
        if match is None:
            return Progress(phase=self.phase if PHASES.index(self.phase) >= PHASES.index(previous) else previous)
        completed = int(match.group("done"))
        total = int(match.group("total"))
        percent = int(match.group("percent"))
        stamp = time.monotonic() if now is None else now
        if self.samples and abs(total - self.samples[-1][2]) > max(5, self.samples[-1][2] // 20):
            self.samples.clear()
        self.samples.append((stamp, completed, total))
        eta = self._eta(completed, total)
        return Progress(self.phase, percent, completed, total, eta, match.group("eta"))

    def _eta(self, completed: int, total: int) -> int | None:
        if len(self.samples) < 3 or completed >= total:
            return None
        rates: list[float] = []
        for left, right in zip(self.samples, list(self.samples)[1:]):
            seconds = right[0] - left[0]
            delta = right[1] - left[1]
            if seconds > 0 and delta > 0:
                rates.append(delta / seconds)
        if len(rates) < 2:
            return None
        weighted = sum(rate * (index + 1) for index, rate in enumerate(rates))
        divisor = sum(range(1, len(rates) + 1))
        return int((total - completed) / (weighted / divisor))


class StreamObserver(Protocol):
    def started(self, pid: int) -> None: ...

    def update(self, progress: Progress, tail: Sequence[str], transition: bool) -> None: ...

    def heartbeat(self, tail: Sequence[str]) -> None: ...


@dataclass(frozen=True, slots=True)
class StreamRequest:
    argv: Sequence[str]
    log: Path
    observer: StreamObserver
    heartbeat_seconds: float = 15.0


@dataclass(frozen=True, slots=True)
class StreamResult:
    returncode: int
    pid: int
    lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StreamPipeError(RuntimeError):
    pid: int

    def __str__(self) -> str:
        return f"child {self.pid} stdout pipe unavailable"


def stream_process(request: StreamRequest) -> StreamResult:
    request.log.parent.mkdir(parents=True, exist_ok=True)
    parser = ProgressParser()
    with request.log.open("wb") as log_handle:
        process = subprocess.Popen(
            list(request.argv), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0
        )
        request.observer.started(process.pid)
        if process.stdout is None:
            raise StreamPipeError(process.pid)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        pending = b""
        last_heartbeat = time.monotonic()
        previous_phase = parser.phase
        while process.poll() is None or selector.get_map():
            events = selector.select(timeout=min(1.0, request.heartbeat_seconds))
            if events:
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    selector.unregister(process.stdout)
                    continue
                log_handle.write(chunk)
                log_handle.flush()
                pending += chunk
                while b"\n" in pending:
                    raw, pending = pending.split(b"\n", 1)
                    progress = parser.feed(raw.decode(errors="replace"))
                    transition = progress.phase != previous_phase
                    request.observer.update(progress, tuple(parser.lines), transition)
                    previous_phase = progress.phase
            now = time.monotonic()
            if now - last_heartbeat >= request.heartbeat_seconds:
                request.observer.heartbeat(tuple(parser.lines))
                last_heartbeat = now
        if pending:
            log_handle.write(pending)
            progress = parser.feed(pending.decode(errors="replace"))
            request.observer.update(progress, tuple(parser.lines), progress.phase != previous_phase)
        returncode = process.wait()
    return StreamResult(returncode, process.pid, tuple(parser.lines))


def process_identity(pid: int) -> tuple[str, str]:
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    fields = Path(f"/proc/{pid}/stat").read_text().split()
    return boot_id, fields[21]


def process_matches(pid: int, boot_id: str, start_ticks: str) -> bool:
    try:
        return process_identity(pid) == (boot_id, start_ticks)
    except (FileNotFoundError, ProcessLookupError, PermissionError, IndexError):
        return False
