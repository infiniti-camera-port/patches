#!/usr/bin/env python3
"""Reconcile stale RUNNING states and retry queued notifications."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import rom_lanes
from rom_monitor import StatusWriter, process_matches, utc_now
from rom_notify import EventId, NotifyEvent, load_notifier


def _age_seconds(timestamp: str, now: datetime) -> float:
    stamp = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (now - stamp).total_seconds()


def reconcile(status_file: Path, stale_seconds: int, notifier_path: Path) -> str:
    json_file = status_file.with_name(f"{status_file.name}.json")
    if not json_file.is_file():
        return "ABSENT"
    state = json.loads(json_file.read_text())
    if state.get("state") != "RUNNING":
        return "IDLE"
    if _age_seconds(str(state["heartbeat"]), datetime.now(timezone.utc)) < stale_seconds:
        return "FRESH"
    pid = int(state.get("child_pid", 0))
    alive = pid > 0 and process_matches(pid, str(state.get("boot_id", "")), str(state.get("child_start_ticks", "")))
    notifier = load_notifier(notifier_path)
    run_id = str(state.get("run", "unknown"))
    outcome = "STALLED" if alive else "LOST"
    event = NotifyEvent(
        event_id=EventId(f"{run_id}-{outcome.lower()}"), state=outcome,
        lane=str(state.get("lane", "unknown")), run_id=run_id,
        phase=str(state.get("phase", "UNKNOWN")), log=str(state.get("log", "-")),
        excerpt=tuple(state.get("tail", [])), build_id=str(state.get("id", "-")),
        build_class=str(state.get("cls", "-")), occurred=utc_now(),
    )
    marker = status_file.with_name(f"{status_file.name}.watchdog.json")
    already_sent = False
    if marker.is_file():
        previous = json.loads(marker.read_text())
        already_sent = previous.get("run_id") == run_id and previous.get("outcome") == outcome
    if notifier and not already_sent:
        notifier.emit(event)
    if not already_sent:
        marker.write_text(json.dumps({"run_id": run_id, "outcome": outcome}) + "\n")
    if alive:
        return "STALLED"
    state["state"] = "LOST"
    state["detail"] = "wrapper-and-child-absent"
    StatusWriter(status_file).write(state, state.get("tail", []), transition=True)
    return "LOST"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("lanes.json"))
    parser.add_argument("--notify", type=Path, default=Path(__file__).with_name("notify.local.json"))
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    stale = int(config.get("monitoring", {}).get("stale_seconds", 120))
    for name in config["lanes"]:
        lane, _ = rom_lanes.load_lane(name)
        reconcile(lane.status_file, stale, args.notify)
    notifier = load_notifier(args.notify)
    if notifier:
        notifier.retry_spool()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
