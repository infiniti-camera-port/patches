from __future__ import annotations

import json
import os
import shutil
import subprocess
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


def test_watchdog_script_disables_local_bytecode_cache(tmp_path: Path) -> None:
    # Given ordinary /usr/bin/python3 is armed to create bytecode for local imports.
    control = tmp_path / "control"
    control.mkdir()
    (control / "dependency.py").write_text("VALUE = 1\n")
    (control / "probe.py").write_text("import dependency\n")
    subprocess.run(["/usr/bin/python3", str(control / "probe.py")], check=True)
    assert list(control.rglob("*.pyc"))

    source_root = ROOT.parent
    copied_root = tmp_path / "tooling"
    copied_rom = copied_root / "rom"
    copied_patches = copied_root / "build-patches"
    copied_rom.mkdir(parents=True)
    copied_patches.mkdir()
    for source in (source_root / "rom").glob("*.py"):
        shutil.copy2(source, copied_rom / source.name)
    for source in (source_root / "build-patches").glob("*.py"):
        shutil.copy2(source, copied_patches / source.name)
    environment = dict(os.environ)
    environment.pop("PYTHONDONTWRITEBYTECODE", None)
    environment["PYTHONPATH"] = f"{copied_rom}:{copied_patches}"

    # When the copied watchdog starts through the production interpreter route.
    subprocess.run(
        ["/usr/bin/python3", str(copied_rom / "rom_watchdog.py"), "--help"],
        cwd=copied_root,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )

    # Then its local tooling imports leave no bytecode cache behind.
    assert not list(copied_root.rglob("__pycache__"))
    assert not list(copied_root.rglob("*.pyc"))
