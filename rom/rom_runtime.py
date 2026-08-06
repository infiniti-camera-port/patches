"""One monitored build attempt from gate verdict through terminal event."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Final, Sequence

import rom_build as builds
import rom_gate as gate
import rom_monitor as monitor
from rom_notify import EventId, NotifyEvent, Notifier, load_notifier

GIB: Final = 1 << 30


@dataclass(frozen=True, slots=True)
class BuildOptions:
    goal: str
    override: str | None
    out_dir: Path | None


@dataclass(slots=True)  # noqa: MUTABLE_OK - observer accumulates child state
class BuildObserver:  # noqa: MUTABLE_OK
    """Mutable adapter from stream callbacks to durable run snapshots."""

    writer: monitor.StatusWriter
    fields: dict[str, monitor.Scalar]
    heartbeat_seconds: float
    phase_started: float
    phase_seconds: dict[str, int]
    child_pid: int = 0
    last_write: float = 0.0
    last_tail: tuple[str, ...] = ()

    @classmethod
    def create(
        cls, writer: monitor.StatusWriter, fields: dict[str, monitor.Scalar], heartbeat_seconds: float
    ) -> BuildObserver:
        now = time.monotonic()
        return cls(writer, fields, heartbeat_seconds, now, {}, last_write=now)

    def started(self, pid: int) -> None:
        self.child_pid = pid
        boot_id, ticks = monitor.process_identity(pid)
        self.fields.update(child_pid=pid, boot_id=boot_id, child_start_ticks=ticks)
        self.writer.write(self.fields, transition=True)

    def update(self, progress: monitor.Progress, tail: Sequence[str], transition: bool) -> None:
        now = time.monotonic()
        self.last_tail = tuple(tail)
        if transition:
            previous = str(self.fields.get("phase", "STARTING"))
            self.phase_seconds[previous] = int(now - self.phase_started)
            self.phase_started = now
        self.fields.update(
            phase=progress.phase,
            progress=progress.percent if progress.percent is not None else "-",
            completed=progress.completed if progress.completed is not None else "-",
            total=progress.total if progress.total is not None else "-",
            eta_s=progress.eta_seconds if progress.eta_seconds is not None else "-",
            native_eta=(progress.native_eta or "-").replace(" ", "_"),
        )
        if transition or now - self.last_write >= self.heartbeat_seconds:
            self.writer.write(self.fields, tail, transition=transition)
            self.last_write = now

    def heartbeat(self, tail: Sequence[str]) -> None:
        self.last_tail = tuple(tail)
        self.writer.write(self.fields, tail)
        self.last_write = time.monotonic()

    def finish_phase(self) -> str:
        phase = str(self.fields.get("phase", "STARTING"))
        self.phase_seconds[phase] = int(time.monotonic() - self.phase_started)
        return ",".join(f"{name}:{seconds}" for name, seconds in self.phase_seconds.items()) or "-"


def event_file(lane) -> Path:
    return lane.status_file.parent / f"{lane.name}-events.jsonl"


def write_simple_status(lane, state: str, detail: str, **extra: monitor.Scalar) -> None:
    fields: dict[str, monitor.Scalar] = {
        "state": state, "lane": lane.name, "target": lane.lunch, **extra, "detail": detail
    }
    monitor.StatusWriter(lane.status_file, event_file(lane)).write(fields, transition=True)


def disk_allows(path: Path, config: dict) -> tuple[bool, int, int]:
    usage = shutil.disk_usage(path)
    min_gib = int(config.get("min_free_gib", 0))
    min_percent = float(config.get("min_free_percent", 0))
    percent = int(usage.free * 100 / usage.total)
    return usage.free >= min_gib * GIB and percent >= min_percent, usage.free, percent


def _notify(
    notifier: Notifier | None,
    fields: dict[str, monitor.Scalar],
    state: str,
    lines: Sequence[str],
    artifact_source: str,
    artifact_dest: str,
    returncode: int,
) -> None:
    if notifier is None:
        return
    run_id = str(fields["run"])
    notifier.emit(NotifyEvent(
        event_id=EventId(f"{run_id}-{state.lower()}"), state=state,
        lane=str(fields["lane"]), run_id=run_id, phase=str(fields.get("phase", "-")),
        log=str(fields["log"]), excerpt=tuple(monitor.error_excerpt(lines) if state != "SUCCESS" else monitor.bounded_tail(lines)),
        build_id=str(fields["id"]), build_class=str(fields["cls"]),
        artifact_source=artifact_source, artifact_dest=artifact_dest,
        returncode=returncode, occurred=monitor.utc_now(),
    ))


def execute_build(options: BuildOptions, lane, toolchain: dict, verdict, repo_root: Path) -> int:
    build_class = gate.EXPERIMENTAL if verdict.state != gate.PINNED else gate.PINNED
    monitoring = toolchain.get("monitoring", {})
    allowed, free_bytes, free_percent = disk_allows(lane.tree, monitoring)
    if not allowed:
        write_simple_status(
            lane, gate.REFUSED, "disk-low", id=verdict.actual_id,
            free_bytes=free_bytes, free_percent=free_percent,
        )
        return 6

    started_utc = monitor.utc_now()
    started_monotonic = time.monotonic()
    started_epoch_ns = time.time_ns()
    stamp = started_utc.replace("-", "").replace(":", "")
    run_id = f"{lane.name}-{options.goal}-{stamp}-{uuid.uuid4().hex[:8]}"
    observed_out = options.out_dir or lane.tree / "out"
    if options.out_dir:
        options.out_dir.mkdir(parents=True, exist_ok=True)
    log = lane.status_file.parent / f"{lane.name}-build-{stamp}.log"
    writer = monitor.StatusWriter(lane.status_file, event_file(lane))
    fields: dict[str, monitor.Scalar] = {
        "state": "RUNNING", "lane": lane.name, "target": lane.lunch,
        "run": run_id, "phase": "STARTING", "progress": "-", "id": verdict.actual_id,
        "cls": build_class, "goal": options.goal, "log": str(log), "pid": os.getpid(),
        "child_pid": 0, "free_bytes": free_bytes, "free_percent": free_percent,
        "session": os.environ.get("ROM_SESSION_NAME", "-"), "detail": "started",
    }
    heartbeat = float(monitoring.get("heartbeat_seconds", 15))
    observer = BuildObserver.create(writer, fields, heartbeat)
    writer.write(fields, transition=True)
    notifier = load_notifier(Path(__file__).with_name("notify.local.json"))
    lines: tuple[str, ...] = ()
    terminalized = False

    def interrupted(signum: int, _frame: FrameType | None) -> None:
        if observer.child_pid:
            try:
                os.kill(observer.child_pid, signum)
            except ProcessLookupError:
                observer.child_pid = 0
        fields.update(state="INTERRUPTED", detail=f"signal-{signum}")
        writer.write(fields, observer.last_tail, transition=True)
        _notify(notifier, fields, "INTERRUPTED", observer.last_tail, "-", "-", 130)
        builds.record(
            builds.ledger_paths(toolchain, repo_root), lane, verdict, options.goal,
            "INTERRUPTED", cls=build_class, first_error=f"signal {signum}",
            run_id=run_id, started_utc=started_utc, finished_utc=monitor.utc_now(),
            seconds=int(time.monotonic() - started_monotonic), phase_seconds=observer.finish_phase(),
        )
        os._exit(130)

    for caught_signal in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(caught_signal, interrupted)

    try:
        request = monitor.StreamRequest(
            argv=builds.podman_argv(lane, toolchain, options.out_dir, options.goal),
            log=log, observer=observer, heartbeat_seconds=heartbeat,
        )
        result = monitor.stream_process(request)
        lines = result.lines
        root_owned = subprocess.run(
            ["find", str(observed_out), "-user", "root"], capture_output=True, text=True
        )
        strays = len(root_owned.stdout.splitlines())
        artifact_source = "-"
        artifact_dest = "-"
        digest = "-"
        returncode = result.returncode
        state = "FAILED"
        if returncode == 0:
            fields.update(phase="PUBLISHING")
            writer.write(fields, lines, transition=True)
            artifact_source, digest = builds.artifact(observed_out, options.goal, started_epoch_ns)
            if artifact_source == "-":
                returncode = 5
                fields["detail"] = "artifact-missing"
            else:
                artifact_dest = builds.publish(lane, verdict.actual_id, artifact_source)
                state = "SUCCESS"
                fields["detail"] = "rc-0"
        else:
            fields["detail"] = f"rc-{returncode}"
        excerpt = monitor.error_excerpt(lines)
        triggers = ("FAILED:", "ninja: error", "error:", "Traceback (most recent call last)")
        first_error = next(
            (line[:200] for line in reversed(lines) if any(token in line for token in triggers)), "-"
        )
        elapsed = int(time.monotonic() - started_monotonic)
        builds.record(
            builds.ledger_paths(toolchain, repo_root), lane, verdict, options.goal, state,
            cls=build_class, first_error=first_error, artifact_sha256=digest,
            artifact_dest=artifact_dest, run_id=run_id, started_utc=started_utc,
            finished_utc=monitor.utc_now(), seconds=elapsed,
            phase_seconds=observer.finish_phase(),
        )
        fields.update(
            state=state, root_owned=strays, artifact_source=artifact_source,
            artifact_dest=artifact_dest, artifact_sha256=digest,
        )
        writer.write(fields, monitor.bounded_tail(lines), transition=True)
        _notify(notifier, fields, state, lines, artifact_source, artifact_dest, returncode)
        terminalized = True
        print(f"build finished rc={returncode} log={log}")
        return returncode
    except Exception as exc:  # noqa: BROAD_EXCEPT_OK - attempt boundary terminalizes unknown failures
        fields.update(state="FAILED", detail=f"monitor-error-{type(exc).__name__}")
        writer.write(fields, lines, transition=True)
        _notify(notifier, fields, "FAILED", lines or (str(exc),), "-", "-", 4)
        builds.record(
            builds.ledger_paths(toolchain, repo_root), lane, verdict, options.goal, "FAILED",
            cls=build_class, first_error=str(exc)[:200], run_id=run_id,
            started_utc=started_utc, finished_utc=monitor.utc_now(),
            seconds=int(time.monotonic() - started_monotonic),
            phase_seconds=observer.finish_phase(),
        )
        terminalized = True
        return 4
    finally:
        if not terminalized:
            fields.update(state="FAILED", detail="unfinalized-attempt")
            writer.write(fields, lines, transition=True)
