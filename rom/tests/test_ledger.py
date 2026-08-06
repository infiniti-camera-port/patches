from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import rom_ledger


def test_old_ledger_rows_are_read_with_new_columns(tmp_path: Path) -> None:
    paths = rom_ledger.Paths(tmp_path)
    paths.attempts.write_text(
        "id\tutc\tlane\tgoal\tclass\toutcome\tfirst_error\tartifact_sha256\tartifact_dest\n"
        "abc\tnow\ttest\tbacon\tPINNED\tSUCCESS\t-\tsha\t/file\n"
    )

    rows = rom_ledger.read_rows(paths)

    assert rows[0]["run_id"] == "-"
    assert rows[0]["outcome"] == "SUCCESS"


def test_append_migrates_old_header_without_losing_rows(tmp_path: Path) -> None:
    paths = rom_ledger.Paths(tmp_path)
    paths.attempts.write_text("id\toutcome\nold\tFAILED\n")

    rom_ledger.append_row(paths, id="new", outcome="SUCCESS", run_id="run-new")

    rows = rom_ledger.read_rows(paths)
    assert [row["id"] for row in rows] == ["old", "new"]
    assert rows[1]["run_id"] == "run-new"
