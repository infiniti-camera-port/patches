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


class UnexpectedNotifier:
    def emit(self, _event) -> None:
        raise AssertionError("unexpected notifier defect")


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


@pytest.mark.parametrize(
    ("case", "expected_state", "expected_detail"),
    [
        ("setup", "FAILED", "monitor-error-KeyError"),
        ("failed-child", "FAILED", "rc-23"),
        ("success", "SUCCESS", "rc-0"),
    ],
)
def test_unexpected_notifier_exception_preserves_decided_terminal_truth(
    tmp_path: Path,
    monkeypatch,
    case: str,
    expected_state: str,
    expected_detail: str,
) -> None:
    # Given a decided setup/child outcome and an unexpected notifier defect.
    lane, verdict, ledger_root = fixture(tmp_path)
    monkeypatch.setattr(rom_runtime, "load_notifier", lambda *_args: UnexpectedNotifier())
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

    # When the programmer exception propagates after the sole ledger attempt.
    with pytest.raises(AssertionError, match="unexpected notifier defect"):
        rom_runtime.execute_build(
            rom_runtime.BuildOptions("bacon", None, None),
            lane,
            toolchain,
            verdict,
            tmp_path,
        )

    # Then finally persists the decided state without a duplicate or RUNNING lie.
    final = status(lane)
    assert final["state"] == expected_state
    assert final["detail"] == expected_detail
    assert outcomes(ledger_root) == [expected_state]


def test_unexpected_signal_notifier_exception_preserves_interrupted_truth(
    tmp_path: Path, monkeypatch
) -> None:
    # Given SIGTERM terminalization reaches an unexpected notifier defect.
    lane, verdict, ledger_root = fixture(tmp_path)
    handlers: dict[int, SignalHandler] = {}

    def capture_handler(number: int, handler: SignalHandler) -> signal.Handlers:
        handlers[number] = handler
        return signal.SIG_DFL

    def interrupt_stream(_request) -> Never:
        handler = handlers[signal.SIGTERM]
        handler(signal.SIGTERM, None)
        raise AssertionError("signal handler returned")

    monkeypatch.setattr(rom_runtime.signal, "signal", capture_handler)
    monkeypatch.setattr(rom_runtime.monitor, "stream_process", interrupt_stream)
    monkeypatch.setattr(rom_runtime, "load_notifier", lambda *_args: UnexpectedNotifier())
    toolchain: rom_runtime.ToolchainConfig = {
        "ledger": str(ledger_root),
        "monitoring": {},
        "ccache": "/tmp/cache",
        "image": "unused",
    }

    # When the unexpected notifier exception propagates from the signal handler.
    with pytest.raises(AssertionError, match="unexpected notifier defect"):
        rom_runtime.execute_build(
            rom_runtime.BuildOptions("bacon", None, None),
            lane,
            toolchain,
            verdict,
            tmp_path,
        )

    # Then finally persists INTERRUPTED and the one matching ledger row.
    final = status(lane)
    assert final["state"] == "INTERRUPTED"
    assert final["detail"] == f"signal-{signal.SIGTERM}"
    assert outcomes(ledger_root) == ["INTERRUPTED"]
