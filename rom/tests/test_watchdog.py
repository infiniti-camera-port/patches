from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import rom_monitor
import rom_watchdog


def running_status(path: Path, pid: int, ticks: str) -> None:
    boot, _ = rom_monitor.process_identity(os.getpid())
    rom_monitor.StatusWriter(path).write({
        "state": "RUNNING", "lane": "test", "run": "test-run", "phase": "NINJA",
        "log": "/tmp/test.log", "heartbeat": "2000-01-01T00:00:00Z",
        "child_pid": pid, "boot_id": boot, "child_start_ticks": ticks,
    }, ["last line"])


def test_watchdog_marks_absent_process_lost(tmp_path: Path) -> None:
    status = tmp_path / "STATUS"
    running_status(status, 99999999, "1")

    result = rom_watchdog.reconcile(status, 1, tmp_path / "missing-notify.json")

    assert result == "LOST"
    assert json.loads(status.with_name("STATUS.json").read_text())["state"] == "LOST"


def test_watchdog_alerts_but_does_not_relabel_live_process(tmp_path: Path) -> None:
    status = tmp_path / "STATUS"
    _, ticks = rom_monitor.process_identity(os.getpid())
    running_status(status, os.getpid(), ticks)

    result = rom_watchdog.reconcile(status, 1, tmp_path / "missing-notify.json")

    assert result == "STALLED"
    assert json.loads(status.with_name("STATUS.json").read_text())["state"] == "RUNNING"


def test_watchdog_rejects_reused_pid_identity(tmp_path: Path) -> None:
    status = tmp_path / "STATUS"
    running_status(status, os.getpid(), "0")

    result = rom_watchdog.reconcile(status, 1, tmp_path / "missing-notify.json")

    assert result == "LOST"
