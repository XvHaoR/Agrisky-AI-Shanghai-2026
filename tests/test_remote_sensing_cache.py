from __future__ import annotations

import json
import sys
from pathlib import Path

from fastapi import HTTPException

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "api_gateway"))

import main


def test_satellite_cache_window_accepts_only_inclusive_date_normalization() -> None:
    assert main._compatible_satellite_window(
        "2025-08-19", "2025-09-13", "2025-08-20", "2025-09-14"
    )
    assert main._compatible_satellite_window(
        "2025-08-05", "2025-08-30", "2025-08-05", "2025-08-30"
    )
    assert not main._compatible_satellite_window(
        "2025-08-19", "2025-09-13", "2025-08-22", "2025-09-16"
    )
    assert not main._compatible_satellite_window(
        "not-a-date", "2025-09-13", "2025-08-20", "2025-09-14"
    )


def test_growth_cache_requires_verified_local_artifacts(monkeypatch) -> None:
    boundary = {
        "type": "Polygon",
        "coordinates": [[[113.1, 34.1], [113.11, 34.1], [113.11, 34.11], [113.1, 34.11], [113.1, 34.1]]],
    }
    target = {
        "disaster_type": "flood",
        "loss_date": "2025-08-15",
        "crop_type": "corn",
        "policy_id": "POL-TARGET",
        "boundary_geojson": json.dumps(boundary),
    }
    cached_payload = {
        "status": "success",
        "task_id": "growth-missing-artifacts",
        "raster": {
            "ndvi_source": "gee",
            "ndvi_meta": {"start_date": "2025-07-01", "end_date": "2025-09-30"},
        },
    }
    source = {
        **target,
        "claim_id": "CLAIM-CACHED",
        "result_json": json.dumps(cached_payload),
    }

    class Cursor:
        def __init__(self, value):
            self.value = value

        def fetchone(self):
            return self.value

        def fetchall(self):
            return self.value

    class Connection:
        def __init__(self):
            self.calls = 0

        def execute(self, *_args):
            self.calls += 1
            return Cursor(target if self.calls == 1 else [source])

        def close(self):
            pass

    monkeypatch.setattr(main, "REMOTE_SENSING_CACHE_MODE", True)
    monkeypatch.setattr(main, "_db", Connection)
    monkeypatch.setattr(main, "_external_remote_sensing_cache_rows", lambda *_args: [])
    monkeypatch.setattr(
        main,
        "_verified_growth_artifacts",
        lambda _payload: (_ for _ in ()).throw(HTTPException(409, "missing artifacts")),
    )

    assert main._matching_cached_result(
        "CLAIM-TARGET", "growth", start_date="2025-07-01", end_date="2025-09-30"
    ) is None
