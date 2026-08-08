from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import rom_runtime
from build_composition import ResolvedProject


@dataclass(frozen=True, slots=True)
class FakeLane:
    name: str
    tree: Path
    status_file: Path
    artifacts: Path
    lunch: str = "test-userdebug"


class NotifierLoadError(RuntimeError):
    pass


class SnapshotWriteError(RuntimeError):
    pass


def fixture(tmp_path: Path) -> tuple[FakeLane, SimpleNamespace, Path]:
    tree = tmp_path / "tree"
    tree.mkdir()
    lane = FakeLane("test", tree, tmp_path / "logs" / "STATUS", tmp_path / "artifacts")
    project = ResolvedProject(
        path="project/real",
        revision="1" * 40,
        head="2" * 40,
        expected_tree="3" * 40,
        effective_tree="4" * 40,
        dirty=True,
    )
    verdict = SimpleNamespace(
        state=rom_runtime.gate.PINNED,
        actual_id="a" * 64,
        states=[project],
        base_oid="b" * 40,
        toolchain_digest="c" * 64,
    )
    return lane, verdict, tmp_path / "ledger"


def final_status(lane: FakeLane) -> dict[str, rom_runtime.monitor.Scalar]:
    return json.loads(lane.status_file.with_name("STATUS.json").read_text())


def ledger_outcomes(ledger_root: Path) -> list[str]:
    paths = rom_runtime.builds.ledger.Paths(ledger_root)
    return [row["outcome"] for row in rom_runtime.builds.ledger.read_rows(paths)]


def event_states(lane: FakeLane) -> list[str]:
    path = lane.status_file.parent / f"{lane.name}-events.jsonl"
    return [json.loads(line)["state"] for line in path.read_text().splitlines()]


@pytest.mark.parametrize(
    "load_fault",
    [OSError("load down"), NotifierLoadError("load down")],
    ids=["oserror", "runtime-error"],
)
def test_notifier_load_failure_is_secondary_to_caught_setup_failure(
    tmp_path: Path, monkeypatch, load_fault: BaseException
) -> None:
    # Given notifier construction fails before missing ccache is discovered.
    lane, verdict, ledger_root = fixture(tmp_path)

    def fail_load(*_args):
        raise load_fault

    monkeypatch.setattr(rom_runtime, "load_notifier", fail_load)
    toolchain: rom_runtime.ToolchainConfig = {
        "ledger": str(ledger_root),
        "monitoring": {},
        "image": "unused",
    }

    # When attempt setup reaches both protected failures.
    returncode = rom_runtime.execute_build(
        rom_runtime.BuildOptions("bacon", None, None),
        lane,
        toolchain,
        verdict,
        tmp_path,
    )

    # Then setup truth and one ledger row survive with loader detail secondary.
    final = final_status(lane)
    assert returncode == 4
    assert final["state"] == "FAILED"
    assert final["detail"] == "monitor-error-KeyError"
    assert "notify-load" in str(final["secondary_error"])
    assert ledger_outcomes(ledger_root) == ["FAILED"]


def test_one_shot_terminal_status_failure_preserves_success_and_ledger(
    tmp_path: Path, monkeypatch
) -> None:
    # Given a successful child and a status writer failing on its terminal write.
    lane, verdict, ledger_root = fixture(tmp_path)
    output = lane.tree / "out" / "target" / "product" / "device" / "test.zip"
    script = (
        "from pathlib import Path;"
        f"p=Path({str(output)!r});p.parent.mkdir(parents=True);p.write_bytes(b'artifact')"
    )
    monkeypatch.setattr(
        rom_runtime.builds,
        "podman_argv",
        lambda *_args: [sys.executable, "-c", script],
    )
    monkeypatch.setattr(rom_runtime, "load_notifier", lambda *_args: None)
    original_write = rom_runtime.monitor.StatusWriter.write
    terminal_writes = 0

    def flaky_write(self, *args, **kwargs) -> None:
        nonlocal terminal_writes
        fields = args[0]
        if fields["state"] != "RUNNING":
            terminal_writes += 1
        if terminal_writes == 1:
            raise OSError("status down once")
        original_write(self, *args, **kwargs)

    monkeypatch.setattr(rom_runtime.monitor.StatusWriter, "write", flaky_write)

    # When the terminal status write fails once after ledger success.
    returncode = rom_runtime.execute_build(
        rom_runtime.BuildOptions("bacon", None, None),
        lane,
        {"ledger": str(ledger_root), "monitoring": {}},
        verdict,
        tmp_path,
    )

    # Then retry persists success and secondary detail without a second ledger row.
    final = final_status(lane)
    assert returncode == 0
    assert terminal_writes == 2
    assert final["state"] == "SUCCESS"
    assert final["detail"] == "rc-0"
    assert "status down once" in str(final["secondary_error"])
    assert ledger_outcomes(ledger_root) == ["SUCCESS"]


def test_emergency_terminal_snapshot_prevents_stale_running_success(
    tmp_path: Path, monkeypatch
) -> None:
    # Given both StatusWriter retries and the ordinary snapshot fallback fail.
    lane, verdict, ledger_root = fixture(tmp_path)
    output = lane.tree / "out" / "target" / "product" / "device" / "test.zip"
    script = (
        "from pathlib import Path;"
        f"p=Path({str(output)!r});p.parent.mkdir(parents=True);p.write_bytes(b'artifact')"
    )
    monkeypatch.setattr(
        rom_runtime.builds,
        "podman_argv",
        lambda *_args: [sys.executable, "-c", script],
    )
    original_write = rom_runtime.monitor.StatusWriter.write
    original_snapshot = rom_runtime.monitor.write_status_snapshot

    def fail_terminal(self, fields, *args, **kwargs) -> None:
        if fields["state"] != "RUNNING":
            raise OSError("terminal writer down")
        original_write(self, fields, *args, **kwargs)

    def fail_snapshot(status_file, fields, *args, **kwargs):
        if fields["state"] != "RUNNING":
            raise SnapshotWriteError("snapshot down")
        return original_snapshot(status_file, fields, *args, **kwargs)

    monkeypatch.setattr(rom_runtime.monitor.StatusWriter, "write", fail_terminal)
    monkeypatch.setattr(rom_runtime.monitor, "write_status_snapshot", fail_snapshot)

    # When the successful result reaches terminal persistence.
    returncode = rom_runtime.execute_build(
        rom_runtime.BuildOptions("bacon", None, None),
        lane,
        {"ledger": str(ledger_root), "monitoring": {}},
        verdict,
        tmp_path,
    )

    # Then emergency persistence matches the sole SUCCESS row instead of RUNNING.
    final = final_status(lane)
    assert returncode == 0
    assert final["state"] == "SUCCESS"
    assert final["detail"] == "rc-0"
    assert "snapshot down" in str(final["secondary_error"])
    assert ledger_outcomes(ledger_root) == ["SUCCESS"]
    assert event_states(lane)[-1] == "SUCCESS"
