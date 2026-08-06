from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import rom_monitor


def test_progress_parser_tracks_monotonic_phases_and_eta() -> None:
    parser = rom_monitor.ProgressParser()

    assert parser.feed("@@ROM_STAGE=SOONG", 0).phase == "SOONG"
    assert parser.feed("@@ROM_STAGE=NINJA", 1).phase == "NINJA"
    parser.feed("[ 10% 10/100 90s remaining] compile", 2)
    parser.feed("[ 20% 20/100 80s remaining] compile", 3)
    progress = parser.feed("[ 30% 30/100 70s remaining] compile", 4)

    assert progress.percent == 30
    assert progress.completed == 30
    assert progress.total == 100
    assert progress.eta_seconds == 7
    assert parser.feed("payload_signer", 5).phase == "SIGNING"
    assert parser.feed("ota_from_target_files", 6).phase == "SIGNING"


def test_progress_parser_resets_eta_when_total_changes_materially() -> None:
    parser = rom_monitor.ProgressParser(phase="NINJA")
    parser.feed("[ 10% 10/100] compile", 1)
    parser.feed("[ 20% 20/100] compile", 2)
    parser.feed("[ 30% 30/100] compile", 3)

    progress = parser.feed("[ 5% 10/200] compile", 4)

    assert progress.eta_seconds is None


def test_status_writer_preserves_flat_contract_and_plain_tail(tmp_path: Path) -> None:
    status = tmp_path / "STATUS"
    writer = rom_monitor.StatusWriter(status, tmp_path / "events.jsonl")

    writer.write({"state": "FAILED", "lane": "test", "detail": "rc 1"}, ["\x1b[31merror: bad\x1b[0m"], transition=True)

    assert "state=FAILED" in status.read_text()
    assert "detail=rc_1" in status.read_text()
    document = json.loads(writer.json_file.read_text())
    assert document["tail"] == ["error: bad"]
    assert json.loads((tmp_path / "events.jsonl").read_text())["state"] == "FAILED"


def test_error_excerpt_uses_last_error_cluster() -> None:
    lines = ["FAILED: stale warning", "work", "error: final failure", "detail", "after"]

    excerpt = rom_monitor.error_excerpt(lines)

    assert "error: final failure" in excerpt
    assert excerpt[-1] == "after"


@dataclass(slots=True)
class RecordingObserver:
    pid: int = 0
    phases: list[str] = field(default_factory=list)
    heartbeats: int = 0

    def started(self, pid: int) -> None:
        self.pid = pid

    def update(self, progress: rom_monitor.Progress, _tail, _transition: bool) -> None:
        self.phases.append(progress.phase)

    def heartbeat(self, _tail) -> None:
        self.heartbeats += 1


def test_stream_process_tees_fake_child_and_reports_progress(tmp_path: Path) -> None:
    observer = RecordingObserver()
    script = (
        "print('@@ROM_STAGE=SOONG', flush=True);"
        "print('@@ROM_STAGE=NINJA', flush=True);"
        "print('[ 50% 5/10 1s remaining] compile', flush=True);"
        "print('payload_signer', flush=True)"
    )

    result = rom_monitor.stream_process(rom_monitor.StreamRequest(
        argv=[sys.executable, "-c", script], log=tmp_path / "build.log",
        observer=observer, heartbeat_seconds=0.01,
    ))

    assert result.returncode == 0
    assert observer.pid > 0
    assert "NINJA" in observer.phases
    assert observer.phases[-1] == "SIGNING"
    assert "[ 50% 5/10" in (tmp_path / "build.log").read_text()
