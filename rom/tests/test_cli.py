from __future__ import annotations

import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import rom_monitor


def test_session_name_is_stable_and_sanitized() -> None:
    namespace = runpy.run_path(str(ROOT / "rom"))

    name = namespace["session_name"]("Feature Gaps", "build", "boot image", "20260806T010203Z")

    assert name == "rom-feature-gaps-boot-image-20260806t010203z"


def test_launch_uses_canonical_rom_entry_without_redirection(monkeypatch, capsys) -> None:
    namespace = runpy.run_path(str(ROOT / "rom"))
    captured: list[str] = []

    def fake_run(argv):
        captured.extend(argv)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(namespace["subprocess"], "run", fake_run)
    args = SimpleNamespace(launch_action="build", goal="bacon")
    lane = SimpleNamespace(name="crdroid")

    assert namespace["cmd_launch"](args, lane, {}) == 0
    assert captured[:4] == ["tmux", "new-session", "-d", "-s"]
    assert str(ROOT / "rom") in captured
    assert ">" not in captured
    assert "2>&1" not in captured
    assert capsys.readouterr().out.startswith("rom-crdroid-bacon-")


def test_observe_renders_structured_status_and_transition_tail(tmp_path: Path, capsys) -> None:
    namespace = runpy.run_path(str(ROOT / "rom"))
    lane = SimpleNamespace(
        name="test", status_file=tmp_path / "STATUS", lunch="test-userdebug"
    )
    rom_monitor.StatusWriter(lane.status_file).write(
        {"state": "FAILED", "lane": "test", "phase": "NINJA", "log": "/tmp/build.log"},
        ["error: failed"], transition=True,
    )

    result = namespace["cmd_observe"](SimpleNamespace(json=False, follow=False), lane, {})

    output = capsys.readouterr().out
    assert result == 0
    assert "FAILED" in output
    assert "/tmp/build.log" in output
    assert "error: failed" in output
