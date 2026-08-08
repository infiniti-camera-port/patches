from __future__ import annotations

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


def real_project() -> ResolvedProject:
    return ResolvedProject(
        path="project/real",
        revision="1" * 40,
        head="2" * 40,
        expected_tree="3" * 40,
        effective_tree="4" * 40,
        dirty=True,
    )


def test_disk_gate_refuses_impossible_threshold(tmp_path: Path) -> None:
    allowed, free_bytes, free_percent = rom_runtime.disk_allows(
        tmp_path, {"min_free_gib": 10**9, "min_free_percent": 0}
    )

    assert not allowed
    assert free_bytes > 0
    assert free_percent >= 0


def test_execute_build_publishes_and_records_before_success(tmp_path: Path, monkeypatch) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    lane = FakeLane("test", tree, tmp_path / "logs" / "STATUS", tmp_path / "artifacts")
    output = tree / "out" / "target" / "product" / "device" / "test.zip"
    script = (
        "from pathlib import Path;"
        f"p=Path({str(output)!r});p.parent.mkdir(parents=True);"
        "print('@@ROM_STAGE=SOONG',flush=True);"
        "print('@@ROM_STAGE=NINJA',flush=True);"
        "print('[ 50% 1/2 1s remaining] compile',flush=True);"
        "print('payload_signer',flush=True);"
        "p.write_bytes(b'artifact')"
    )
    monkeypatch.setattr(rom_runtime.builds, "podman_argv", lambda *_args: [sys.executable, "-c", script])
    verdict = SimpleNamespace(
        state=rom_runtime.gate.PINNED, actual_id="a" * 64, states=[real_project()],
        base_oid="b" * 40, toolchain_digest="c" * 64,
    )
    toolchain: rom_runtime.ToolchainConfig = {
        "ledger": str(tmp_path / "ledger"),
        "monitoring": {},
    }

    returncode = rom_runtime.execute_build(
        rom_runtime.BuildOptions("bacon", None, None), lane, toolchain, verdict, tmp_path
    )

    assert returncode == 0
    status = lane.status_file.with_name("STATUS.json")
    assert __import__("json").loads(status.read_text())["state"] == "SUCCESS"
    attempts = tmp_path / "ledger" / "attempts.tsv"
    assert "\tSUCCESS\t" in attempts.read_text()
    assert attempts.stat().st_mtime_ns <= status.stat().st_mtime_ns
    assert next((lane.artifacts / verdict.actual_id).glob("*.zip")).read_bytes() == b"artifact"


def test_execute_build_failure_carries_final_error_tail(tmp_path: Path, monkeypatch) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    lane = FakeLane("test", tree, tmp_path / "logs" / "STATUS", tmp_path / "artifacts")
    script = "print('@@ROM_STAGE=NINJA');print('error: final failure');raise SystemExit(1)"
    monkeypatch.setattr(rom_runtime.builds, "podman_argv", lambda *_args: [sys.executable, "-c", script])
    verdict = SimpleNamespace(
        state=rom_runtime.gate.PINNED, actual_id="a" * 64, states=[real_project()],
        base_oid="b" * 40, toolchain_digest="c" * 64,
    )

    returncode = rom_runtime.execute_build(
        rom_runtime.BuildOptions("bacon", None, None), lane,
        {"ledger": str(tmp_path / "ledger"), "monitoring": {}}, verdict, tmp_path,
    )

    state = __import__("json").loads(lane.status_file.with_name("STATUS.json").read_text())
    assert returncode == 1
    assert state["state"] == "FAILED"
    assert "error: final failure" in state["tail"]


def test_child_failure_survives_secondary_ledger_failure_without_recursive_record(
    tmp_path: Path, monkeypatch
) -> None:
    # Given a child that fails and a ledger serializer that also fails.
    tree = tmp_path / "tree"
    tree.mkdir()
    lane = FakeLane("test", tree, tmp_path / "logs" / "STATUS", tmp_path / "artifacts")
    script = "print('@@ROM_STAGE=NINJA');print('error: child truth');raise SystemExit(23)"
    monkeypatch.setattr(rom_runtime.builds, "podman_argv", lambda *_args: [sys.executable, "-c", script])
    calls = 0

    def fail_store(*_args, **_kwargs) -> None:
        nonlocal calls
        calls += 1
        raise OSError("injected ledger failure")

    monkeypatch.setattr(rom_runtime.builds.ledger, "store_preimage", fail_store)
    verdict = SimpleNamespace(
        state=rom_runtime.gate.PINNED, actual_id="a" * 64, states=[real_project()],
        base_oid="b" * 40, toolchain_digest="c" * 64,
    )

    # When the attempt reaches terminal recording.
    returncode = rom_runtime.execute_build(
        rom_runtime.BuildOptions("bacon", None, None), lane,
        {"ledger": str(tmp_path / "ledger"), "monitoring": {}}, verdict, tmp_path,
    )

    # Then child truth wins and the failing record path is not called recursively.
    status = __import__("json").loads(lane.status_file.with_name("STATUS.json").read_text())
    assert returncode == 23
    assert calls == 1
    assert status["state"] == "FAILED"
    assert status["detail"] == "rc-23"
    assert "error: child truth" in status["tail"]
    assert "ledger" in status["secondary_error"]


def test_failed_child_records_nonempty_resolved_project_without_masking_result(
    tmp_path: Path, monkeypatch
) -> None:
    # Given a failing child and the same direct-field project shape as a real receipt.
    tree = tmp_path / "tree"
    tree.mkdir()
    lane = FakeLane("test", tree, tmp_path / "logs" / "STATUS", tmp_path / "artifacts")
    script = "print('error: child truth');raise SystemExit(23)"
    monkeypatch.setattr(
        rom_runtime.builds,
        "podman_argv",
        lambda *_args: [sys.executable, "-c", script],
    )
    project = real_project()
    verdict = SimpleNamespace(
        state=rom_runtime.gate.PINNED,
        actual_id="a" * 64,
        states=[project],
        base_oid="b" * 40,
        toolchain_digest="c" * 64,
    )
    ledger_root = tmp_path / "ledger"

    # When terminal recording serializes the nonempty receipt after child failure.
    returncode = rom_runtime.execute_build(
        rom_runtime.BuildOptions("bacon", None, None),
        lane,
        {"ledger": str(ledger_root), "monitoring": {}},
        verdict,
        tmp_path,
    )

    # Then the child result survives and the direct project fields are persisted.
    status = __import__("json").loads(lane.status_file.with_name("STATUS.json").read_text())
    preimage = __import__("json").loads((ledger_root / "preimages" / f"{verdict.actual_id}.json").read_text())
    assert returncode == 23
    assert status["state"] == "FAILED"
    assert status["detail"] == "rc-23"
    assert preimage["projects"] == [
        {
            "dirty": True,
            "effective_tree": project.effective_tree,
            "head": project.head,
            "path": project.path,
        }
    ]


def test_successful_child_with_ledger_failure_cannot_claim_success(
    tmp_path: Path, monkeypatch
) -> None:
    # Given a successful child/artifact and a ledger I/O failure.
    tree = tmp_path / "tree"
    tree.mkdir()
    lane = FakeLane("test", tree, tmp_path / "logs" / "STATUS", tmp_path / "artifacts")
    output = tree / "out" / "target" / "product" / "device" / "test.zip"
    script = (
        "from pathlib import Path;"
        f"p=Path({str(output)!r});p.parent.mkdir(parents=True);p.write_bytes(b'artifact')"
    )
    monkeypatch.setattr(rom_runtime.builds, "podman_argv", lambda *_args: [sys.executable, "-c", script])
    calls = 0

    def fail_store(*_args, **_kwargs) -> None:
        nonlocal calls
        calls += 1
        raise OSError("injected ledger failure")

    monkeypatch.setattr(rom_runtime.builds.ledger, "store_preimage", fail_store)
    verdict = SimpleNamespace(
        state=rom_runtime.gate.PINNED, actual_id="a" * 64, states=[real_project()],
        base_oid="b" * 40, toolchain_digest="c" * 64,
    )

    # When the attempt would otherwise certify SUCCESS.
    returncode = rom_runtime.execute_build(
        rom_runtime.BuildOptions("bacon", None, None), lane,
        {"ledger": str(tmp_path / "ledger"), "monitoring": {}}, verdict, tmp_path,
    )

    # Then the dedicated ledger state is terminal and nonrecursive.
    status = __import__("json").loads(lane.status_file.with_name("STATUS.json").read_text())
    assert returncode == rom_runtime.LEDGER_WRITE_FAILED
    assert calls == 1
    assert status["state"] == "LEDGER_WRITE_FAILED"
    assert status["detail"] == "ledger-write-failed"


@pytest.mark.parametrize(
    ("partial_toolchain", "missing_key"),
    [
        ({"image": "unused"}, "ccache"),
        ({"ccache": "/tmp/cache"}, "image"),
    ],
)
def test_missing_required_toolchain_key_terminalizes_one_failed_ledger_row(
    tmp_path: Path,
    partial_toolchain: rom_runtime.ToolchainConfig,
    missing_key: str,
) -> None:
    # Given a nonempty receipt and one missing directly indexed toolchain key.
    tree = tmp_path / "tree"
    tree.mkdir()
    lane = FakeLane("test", tree, tmp_path / "logs" / "STATUS", tmp_path / "artifacts")
    verdict = SimpleNamespace(
        state=rom_runtime.gate.PINNED,
        actual_id="a" * 64,
        states=[real_project()],
        base_oid="b" * 40,
        toolchain_digest="c" * 64,
    )
    ledger_root = tmp_path / "ledger"
    toolchain: rom_runtime.ToolchainConfig = {
        "ledger": str(ledger_root),
        "monitoring": {},
        **partial_toolchain,
    }

    # When attempt setup reaches the required toolchain access.
    returncode = rom_runtime.execute_build(
        rom_runtime.BuildOptions("bacon", None, None),
        lane,
        toolchain,
        verdict,
        tmp_path,
    )

    # Then the config failure is contained, terminal, truthful, and recorded once.
    status = __import__("json").loads(lane.status_file.with_name("STATUS.json").read_text())
    rows = rom_runtime.builds.ledger.read_rows(rom_runtime.builds.ledger.Paths(ledger_root))
    assert returncode == 4
    assert status["state"] == "FAILED"
    assert status["detail"] == "monitor-error-KeyError"
    assert len(rows) == 1
    assert rows[0]["outcome"] == "FAILED"
    assert missing_key in rows[0]["first_error"]
