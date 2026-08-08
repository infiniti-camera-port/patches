from __future__ import annotations

import json
import signal
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import FrameType, SimpleNamespace
from typing import Never

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import rom_runtime
from build_composition import ResolvedProject

SignalHandler = Callable[[int, FrameType | None], None]


@dataclass(frozen=True, slots=True)
class FakeLane:
    name: str
    tree: Path
    status_file: Path
    artifacts: Path
    lunch: str = "test-userdebug"


class ExitCalled(BaseException):
    pass


class SignalNotifyError(RuntimeError):
    pass


class FailingNotifier:
    def emit(self, _event) -> None:
        raise SignalNotifyError("notify down")


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


def status(lane: FakeLane) -> dict[str, rom_runtime.monitor.Scalar]:
    return json.loads(lane.status_file.with_name("STATUS.json").read_text())


def outcomes(ledger_root: Path) -> list[str]:
    paths = rom_runtime.builds.ledger.Paths(ledger_root)
    return [row["outcome"] for row in rom_runtime.builds.ledger.read_rows(paths)]


@pytest.mark.parametrize("persistent", [False, True], ids=["one-shot", "persistent"])
def test_initial_status_failure_refuses_before_child_with_one_failed_row(
    tmp_path: Path, monkeypatch, persistent: bool
) -> None:
    # Given the first RUNNING status write fails once or persistently.
    lane, verdict, ledger_root = fixture(tmp_path)
    original_write = rom_runtime.monitor.StatusWriter.write
    writes = 0

    def fail_initial(self, *args, **kwargs) -> None:
        nonlocal writes
        writes += 1
        if writes == 1 or persistent:
            raise OSError("status unavailable")
        original_write(self, *args, **kwargs)

    monkeypatch.setattr(rom_runtime.monitor.StatusWriter, "write", fail_initial)
    toolchain: rom_runtime.ToolchainConfig = {
        "ledger": str(ledger_root),
        "monitoring": {},
        "ccache": "/tmp/cache",
        "image": "unused",
    }

    # When build execution attempts its initial durable state.
    returncode = rom_runtime.execute_build(
        rom_runtime.BuildOptions("bacon", None, None),
        lane,
        toolchain,
        verdict,
        tmp_path,
    )

    # Then it refuses before launch and fallback aligns status with one failed row.
    final = status(lane)
    assert returncode == 4
    assert writes == (3 if persistent else 2)
    assert final["state"] == "FAILED"
    assert final["detail"] == "status-start-error-OSError"
    assert outcomes(ledger_root) == ["FAILED"]


@pytest.mark.parametrize(
    ("case", "expected_rc", "expected_state", "expected_detail"),
    [
        ("setup", 4, "FAILED", "monitor-error-KeyError"),
        ("failed-child", 23, "FAILED", "rc-23"),
        ("success", 0, "SUCCESS", "rc-0"),
    ],
)
def test_persistent_terminal_writer_failure_preserves_primary_and_one_row(
    tmp_path: Path,
    monkeypatch,
    case: str,
    expected_rc: int,
    expected_state: str,
    expected_detail: str,
) -> None:
    # Given terminal StatusWriter.write calls fail persistently after RUNNING.
    lane, verdict, ledger_root = fixture(tmp_path)
    original_write = rom_runtime.monitor.StatusWriter.write
    terminal_writes = 0

    def fail_terminal(self, *args, **kwargs) -> None:
        nonlocal terminal_writes
        fields = args[0]
        if fields["state"] != "RUNNING":
            terminal_writes += 1
            raise OSError("terminal status unavailable")
        original_write(self, *args, **kwargs)

    monkeypatch.setattr(rom_runtime.monitor.StatusWriter, "write", fail_terminal)
    toolchain: rom_runtime.ToolchainConfig = {
        "ledger": str(ledger_root),
        "monitoring": {},
        "image": "unused",
    }
    if case != "setup":
        output = lane.tree / "out" / "target" / "product" / "device" / "test.zip"
        script = (
            "raise SystemExit(23)"
            if case == "failed-child"
            else (
                "from pathlib import Path;"
                f"p=Path({str(output)!r});p.parent.mkdir(parents=True);p.write_bytes(b'artifact')"
            )
        )
        monkeypatch.setattr(
            rom_runtime.builds,
            "podman_argv",
            lambda *_args: [sys.executable, "-c", script],
        )

    # When the already-determined terminal result is persisted.
    returncode = rom_runtime.execute_build(
        rom_runtime.BuildOptions("bacon", None, None),
        lane,
        toolchain,
        verdict,
        tmp_path,
    )

    # Then fallback status and the sole ledger row preserve the primary truth.
    final = status(lane)
    assert returncode == expected_rc
    assert terminal_writes == 2
    assert final["state"] == expected_state
    assert final["detail"] == expected_detail
    assert "terminal status unavailable" in str(final["secondary_error"])
    assert outcomes(ledger_root) == [expected_state]


def test_signal_terminalization_records_once_before_failing_notification(
    tmp_path: Path, monkeypatch
) -> None:
    # Given SIGTERM arrives and notification fails after the ledger attempt.
    lane, verdict, ledger_root = fixture(tmp_path)
    handlers: dict[int, SignalHandler] = {}

    def capture_handler(number: int, handler: SignalHandler) -> signal.Handlers:
        handlers[number] = handler
        return signal.SIG_DFL

    def interrupt_stream(_request) -> Never:
        handler = handlers[signal.SIGTERM]
        assert callable(handler)
        handler(signal.SIGTERM, None)
        raise AssertionError("signal handler returned")

    def stop_process(code: int) -> Never:
        raise ExitCalled(code)

    monkeypatch.setattr(rom_runtime.signal, "signal", capture_handler)
    monkeypatch.setattr(rom_runtime.monitor, "stream_process", interrupt_stream)
    monkeypatch.setattr(rom_runtime.os, "_exit", stop_process)
    monkeypatch.setattr(rom_runtime, "load_notifier", lambda *_args: FailingNotifier())
    toolchain: rom_runtime.ToolchainConfig = {
        "ledger": str(ledger_root),
        "monitoring": {},
        "ccache": "/tmp/cache",
        "image": "unused",
    }

    # When the installed handler performs terminalization.
    with pytest.raises(ExitCalled, match="130"):
        rom_runtime.execute_build(
            rom_runtime.BuildOptions("bacon", None, None),
            lane,
            toolchain,
            verdict,
            tmp_path,
        )

    # Then signal truth, secondary notification detail, and one row are durable.
    final = status(lane)
    assert final["state"] == "INTERRUPTED"
    assert final["detail"] == f"signal-{signal.SIGTERM}"
    assert "notify down" in str(final["secondary_error"])
    assert outcomes(ledger_root) == ["INTERRUPTED"]


def test_signal_total_terminal_storage_failure_preserves_interrupt_row(
    tmp_path: Path, monkeypatch
) -> None:
    # Given SIGTERM arrives while every terminal persistence route is unavailable.
    lane, verdict, ledger_root = fixture(tmp_path)
    handlers: dict[int, SignalHandler] = {}
    original_write = rom_runtime.monitor.StatusWriter.write
    original_snapshot = rom_runtime.monitor.write_status_snapshot

    def capture_handler(number: int, handler: SignalHandler) -> signal.Handlers:
        handlers[number] = handler
        return signal.SIG_DFL

    def interrupt_stream(_request) -> Never:
        handler = handlers[signal.SIGTERM]
        handler(signal.SIGTERM, None)
        raise AssertionError("signal handler returned")

    def stop_process(code: int) -> Never:
        raise ExitCalled(code)

    def fail_terminal(self, fields, *args, **kwargs) -> None:
        if fields["state"] != "RUNNING":
            raise OSError("terminal writer down")
        original_write(self, fields, *args, **kwargs)

    def fail_snapshot(status_file, fields, *args, **kwargs):
        if fields["state"] != "RUNNING":
            raise OSError("snapshot down")
        return original_snapshot(status_file, fields, *args, **kwargs)

    def fail_fallback(*_args, **_kwargs):
        raise OSError("fallback down")

    monkeypatch.setattr(rom_runtime.signal, "signal", capture_handler)
    monkeypatch.setattr(rom_runtime.monitor, "stream_process", interrupt_stream)
    monkeypatch.setattr(rom_runtime.os, "_exit", stop_process)
    monkeypatch.setattr(rom_runtime.monitor.StatusWriter, "write", fail_terminal)
    monkeypatch.setattr(rom_runtime.monitor, "write_status_snapshot", fail_snapshot)
    monkeypatch.setattr(rom_runtime.monitor, "write_terminal_fallback", fail_fallback)

    # When signal terminalization fails closed through the ledger.
    with pytest.raises(ExitCalled, match="130"):
        rom_runtime.execute_build(
            rom_runtime.BuildOptions("bacon", None, None),
            lane,
            {
                "ledger": str(ledger_root),
                "monitoring": {},
                "ccache": "/tmp/cache",
                "image": "unused",
            },
            verdict,
            tmp_path,
        )

    # Then the stale snapshots are gone and the sole row preserves interruption.
    assert not lane.status_file.exists()
    assert not lane.status_file.with_name("STATUS.json").exists()
    assert outcomes(ledger_root) == ["INTERRUPTED"]
