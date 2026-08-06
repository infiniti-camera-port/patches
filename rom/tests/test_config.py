from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import rom_lanes


def test_monitoring_policy_is_propagated_into_toolchain(tmp_path: Path, monkeypatch) -> None:
    config = {
        "monitoring": {"heartbeat_seconds": 7, "min_free_gib": 1},
        "toolchain": {},
        "lanes": {
            "test": {
                "tree": str(tmp_path), "base_manifest": {}, "governed_manifest": {},
                "lunch": "test-userdebug", "status_file": str(tmp_path / "STATUS"),
                "artifacts": str(tmp_path / "artifacts"), "governed_org": "test",
            }
        },
    }
    path = tmp_path / "lanes.json"
    path.write_text(json.dumps(config))
    monkeypatch.setenv("ROM_LANES", str(path))

    _, toolchain = rom_lanes.load_lane("test")

    assert toolchain["monitoring"]["heartbeat_seconds"] == 7
    assert toolchain["monitoring"]["min_free_gib"] == 1
