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


class SnapshotWriteError(RuntimeError):
    pass


def fixture(tmp_path: Path) -> tuple[FakeLane, SimpleNamespace, Path]:
    tree = tmp_path / "tree"
    tree.mkdir()
    lane = FakeLane("test", tree, tmp_path / "logs" / "STATUS", tmp_path / "artifacts")
    project = ResolvedProject("project/real", "1" * 40, "2" * 40, "3" * 40, "4" * 40, True)
    verdict = SimpleNamespace(
        state=rom_runtime.gate.PINNED,
        actual_id="a" * 64,
        states=[project],
        base_oid="b" * 40,
        toolchain_digest="c" * 64,
    )
    return lane, verdict, tmp_path / "ledger"


def rows(ledger_root: Path) -> list[dict[str, str]]:
    return rom_runtime.builds.ledger.read_rows(rom_runtime.builds.ledger.Paths(ledger_root))


def install_child(monkeypatch, lane: FakeLane, case: str) -> None:
    if case == "setup":
        return
    output = lane.tree / "out" / "target" / "product" / "device" / "test.zip"
    script = (
        "raise SystemExit(23)"
        if case == "failed-child"
        else (
            "from pathlib import Path;"
            f"p=Path({str(output)!r});p.parent.mkdir(parents=True);p.write_bytes(b'artifact')"
        )
    )
    monkeypatch.setattr(rom_runtime.builds, "podman_argv", lambda *_args: [sys.executable, "-c", script])


def fail_primary_paths(monkeypatch) -> None:
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


@pytest.mark.parametrize(
    ("case", "expected_rc", "expected_state"),
    [("setup", 4, "FAILED"), ("failed-child", 23, "FAILED"), ("success", 0, "SUCCESS")],
)
def test_emergency_bundle_terminalizes_snapshot_event_and_ledger(
    tmp_path: Path, monkeypatch, case: str, expected_rc: int, expected_state: str
) -> None:
    # Given StatusWriter and the ordinary snapshot path both fail terminal writes.
    lane, verdict, ledger_root = fixture(tmp_path)
    fail_primary_paths(monkeypatch)
    install_child(monkeypatch, lane, case)

    # When the independent atomic fallback persists the terminal bundle.
    returncode = rom_runtime.execute_build(
        rom_runtime.BuildOptions("bacon", None, None), lane,
        {"ledger": str(ledger_root), "monitoring": {}, "image": "unused"}, verdict, tmp_path,
    )

    # Then all three durable representations agree on the terminal state.
    final = json.loads(lane.status_file.with_name("STATUS.json").read_text())
    events = lane.status_file.parent / f"{lane.name}-events.jsonl"
    assert returncode == expected_rc
    assert final["state"] == expected_state
    assert json.loads(events.read_text().splitlines()[-1])["state"] == expected_state
    assert [row["outcome"] for row in rows(ledger_root)] == [expected_state]


@pytest.mark.parametrize(
    ("case", "expected_rc", "expected_outcome"),
    [("setup", 4, "FAILED"), ("failed-child", 23, "FAILED"),
     ("success", rom_runtime.STATUS_WRITE_FAILED, "STATUS_WRITE_FAILED")],
)
def test_total_terminal_storage_failure_uses_authoritative_ledger(
    tmp_path: Path, monkeypatch, case: str, expected_rc: int, expected_outcome: str
) -> None:
    # Given every terminal snapshot route fails after RUNNING was persisted.
    lane, verdict, ledger_root = fixture(tmp_path)
    fail_primary_paths(monkeypatch)

    def fail_fallback(*_args, **_kwargs):
        raise OSError("fallback down")

    monkeypatch.setattr(
        rom_runtime.monitor, "write_terminal_fallback", fail_fallback,
    )
    install_child(monkeypatch, lane, case)

    # When the already-decided result cannot be persisted as terminal status.
    returncode = rom_runtime.execute_build(
        rom_runtime.BuildOptions("bacon", None, None), lane,
        {"ledger": str(ledger_root), "monitoring": {}, "image": "unused"}, verdict, tmp_path,
    )

    # Then stale snapshots are removed and one truthful ledger row is authoritative.
    recorded = rows(ledger_root)
    assert returncode == expected_rc
    assert not lane.status_file.exists()
    assert not lane.status_file.with_name("STATUS.json").exists()
    assert [row["outcome"] for row in recorded] == [expected_outcome]
    assert "status persistence failed" in recorded[0]["first_error"]
    assert f"primary {'SUCCESS' if case == 'success' else 'FAILED'} rc=" in recorded[0]["first_error"]
