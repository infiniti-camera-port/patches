from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

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


class FailingNotifyError(RuntimeError):
    pass


class FailingNotifier:
    def emit(self, _event) -> None:
        raise FailingNotifyError("notify down")


def verdict() -> SimpleNamespace:
    project = ResolvedProject(
        path="project/real",
        revision="1" * 40,
        head="2" * 40,
        expected_tree="3" * 40,
        effective_tree="4" * 40,
        dirty=True,
    )
    return SimpleNamespace(
        state=rom_runtime.gate.PINNED,
        actual_id="a" * 64,
        states=[project],
        base_oid="b" * 40,
        toolchain_digest="c" * 64,
    )


def paths(tmp_path: Path) -> tuple[FakeLane, Path]:
    tree = tmp_path / "tree"
    tree.mkdir()
    return (
        FakeLane("test", tree, tmp_path / "logs" / "STATUS", tmp_path / "artifacts"),
        tmp_path / "ledger",
    )


def rows(ledger_root: Path) -> list[dict[str, str]]:
    return rom_runtime.builds.ledger.read_rows(
        rom_runtime.builds.ledger.Paths(ledger_root)
    )


def status(lane: FakeLane) -> dict[str, rom_runtime.monitor.Scalar]:
    return json.loads(lane.status_file.with_name("STATUS.json").read_text())


def test_notifier_failure_cannot_mask_missing_toolchain_terminalization(
    tmp_path: Path, monkeypatch
) -> None:
    # Given a missing required key and a notifier that raises at terminalization.
    lane, ledger_root = paths(tmp_path)
    monkeypatch.setattr(rom_runtime, "load_notifier", lambda *_args: FailingNotifier())
    toolchain: rom_runtime.ToolchainConfig = {
        "ledger": str(ledger_root),
        "monitoring": {},
        "image": "unused",
    }

    # When the caught setup failure is recorded and notified.
    returncode = rom_runtime.execute_build(
        rom_runtime.BuildOptions("bacon", None, None),
        lane,
        toolchain,
        verdict(),
        tmp_path,
    )

    # Then primary truth persists with one row and notifier detail stays secondary.
    final = status(lane)
    assert returncode == 4
    assert final["state"] == "FAILED"
    assert final["detail"] == "monitor-error-KeyError"
    assert "notify down" in str(final["secondary_error"])
    assert [row["outcome"] for row in rows(ledger_root)] == ["FAILED"]


def test_notifier_failure_cannot_mask_failed_child(
    tmp_path: Path, monkeypatch
) -> None:
    # Given a child returning 23 and a notifier that raises at terminalization.
    lane, ledger_root = paths(tmp_path)
    script = "print('error: child truth');raise SystemExit(23)"
    monkeypatch.setattr(
        rom_runtime.builds,
        "podman_argv",
        lambda *_args: [sys.executable, "-c", script],
    )
    monkeypatch.setattr(rom_runtime, "load_notifier", lambda *_args: FailingNotifier())

    # When the normal failed-child path records and notifies.
    returncode = rom_runtime.execute_build(
        rom_runtime.BuildOptions("bacon", None, None),
        lane,
        {"ledger": str(ledger_root), "monitoring": {}},
        verdict(),
        tmp_path,
    )

    # Then child rc/status/error and exactly one row survive notifier failure.
    final = status(lane)
    assert returncode == 23
    assert final["state"] == "FAILED"
    assert final["detail"] == "rc-23"
    assert "notify down" in str(final["secondary_error"])
    assert [row["outcome"] for row in rows(ledger_root)] == ["FAILED"]


def test_notifier_failure_cannot_mask_successful_child(
    tmp_path: Path, monkeypatch
) -> None:
    # Given a successful artifact and a notifier that raises at terminalization.
    lane, ledger_root = paths(tmp_path)
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
    monkeypatch.setattr(rom_runtime, "load_notifier", lambda *_args: FailingNotifier())

    # When the successful path records and notifies.
    returncode = rom_runtime.execute_build(
        rom_runtime.BuildOptions("bacon", None, None),
        lane,
        {"ledger": str(ledger_root), "monitoring": {}},
        verdict(),
        tmp_path,
    )

    # Then success remains primary and exactly one successful row is durable.
    final = status(lane)
    assert returncode == 0
    assert final["state"] == "SUCCESS"
    assert final["detail"] == "rc-0"
    assert "notify down" in str(final["secondary_error"])
    assert [row["outcome"] for row in rows(ledger_root)] == ["SUCCESS"]


def test_setup_failure_persists_secondary_ledger_error_once(
    tmp_path: Path, monkeypatch
) -> None:
    # Given missing ccache and a ledger boundary that fails on its only call.
    lane, ledger_root = paths(tmp_path)
    calls = 0

    def fail_store(*_args, **_kwargs) -> None:
        nonlocal calls
        calls += 1
        raise OSError("ledger down")

    monkeypatch.setattr(rom_runtime.builds.ledger, "store_preimage", fail_store)
    toolchain: rom_runtime.ToolchainConfig = {
        "ledger": str(ledger_root),
        "monitoring": {},
        "image": "unused",
    }

    # When setup failure terminalization reaches the ledger boundary.
    returncode = rom_runtime.execute_build(
        rom_runtime.BuildOptions("bacon", None, None),
        lane,
        toolchain,
        verdict(),
        tmp_path,
    )

    # Then the primary failure returns and the one ledger failure is persisted.
    final = status(lane)
    assert returncode == 4
    assert calls == 1
    assert final["state"] == "FAILED"
    assert final["detail"] == "monitor-error-KeyError"
    assert "ledger down" in str(final["secondary_error"])
