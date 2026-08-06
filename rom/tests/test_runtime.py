from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import rom_runtime


@dataclass(frozen=True, slots=True)
class FakeLane:
    name: str
    tree: Path
    status_file: Path
    artifacts: Path
    lunch: str = "test-userdebug"


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
        state=rom_runtime.gate.PINNED, actual_id="a" * 64, states=[],
        base_oid="b" * 40, toolchain_digest="c" * 64,
    )
    toolchain = {"ledger": str(tmp_path / "ledger"), "monitoring": {}}

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
        state=rom_runtime.gate.PINNED, actual_id="a" * 64, states=[],
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
