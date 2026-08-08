from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import rom_identity
import rom_ledger
from build_composition import ResolvedProject


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


def test_nonempty_resolved_project_preimage_uses_direct_fields_and_recomputes_id(
    tmp_path: Path,
) -> None:
    # Given a real full-composition-shaped receipt with direct ResolvedProject fields.
    states = [
        ResolvedProject(
            path=f"project/{index:04d}",
            revision=f"{index:040x}",
            head=f"{index:040x}",
            expected_tree=f"{index + 1:040x}",
            effective_tree=f"{index + 2:040x}",
            dirty=index % 2 == 0,
        )
        for index in range(1193)
    ]
    lane = type("Lane", (), {"tree": tmp_path, "name": "features-v3", "lunch": "lineage_infiniti-userdebug"})()
    base_oid = "b" * 40
    toolchain_digest = "c" * 64
    verdict_id = rom_identity.fold(
        [(state.path, state.effective_tree) for state in states],
        base_oid,
        toolchain_digest,
        lane.lunch,
    )

    # When the normal ledger boundary stores the receipt.
    preimage = rom_ledger.store_preimage(
        rom_ledger.Paths(tmp_path / "ledger"),
        lane,
        verdict_id,
        states,
        base_oid,
        toolchain_digest,
    )

    # Then all direct records survive and independently fold to the verdict ID.
    assert len(__import__("json").loads(preimage.read_text())["projects"]) == 1193
    assert rom_ledger.recompute(preimage) == verdict_id
