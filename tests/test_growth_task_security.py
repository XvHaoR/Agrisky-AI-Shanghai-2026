"""Security contracts for standalone growth artifacts and interactive maps."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from docx import Document
from fastapi.testclient import TestClient
from PIL import Image

import main
from growth_analysis import (
    GROWTH_ANALYSIS_ALGORITHM_VERSION,
    GROWTH_ANALYSIS_SCHEMA_VERSION,
    create_interactive_map_html,
)


def _boundary() -> dict:
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": "</script><script>window.pwned=1</script>"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [[114.0, 34.0], [114.01, 34.0], [114.01, 34.01], [114.0, 34.01], [114.0, 34.0]]
                    ],
                },
            }
        ],
    }


def test_interactive_map_uses_script_safe_json_and_dom_text(tmp_path: Path) -> None:
    boundary_path = tmp_path / "boundary.geojson"
    boundary_path.write_text(json.dumps(_boundary(), ensure_ascii=False), encoding="utf-8")
    class_overlay = tmp_path / "class.png"
    ndvi_overlay = tmp_path / "ndvi.png"
    Image.new("RGBA", (16, 16), (0, 200, 80, 180)).save(class_overlay)
    Image.new("RGBA", (16, 16), (20, 100, 220, 180)).save(ndvi_overlay)
    output = tmp_path / "map.html"

    create_interactive_map_html(
        out_html=output,
        boundary_geojson=boundary_path,
        class_overlay_png=class_overlay,
        ndvi_overlay_png=ndvi_overlay,
        summary=[
            {
                "value": 5,
                "label": "优</script><script>alert(1)</script>",
                "ratio": 1.0,
                "color": "#2f9e44",
            }
        ],
        bounds=[114.0, 34.0, 114.01, 34.01],
        crop_label="玉米</script><script>alert(2)</script>",
    )

    document = output.read_text(encoding="utf-8")
    assert "</script><script>alert" not in document
    assert "innerHTML" not in document
    assert "textContent" in document
    assert "\\u003c/script\\u003e" in document
    assert "data:image/png;base64," in document
    headers = main._growth_html_security_headers(output)
    assert "'sha256-" in headers["Content-Security-Policy"]
    assert "'unsafe-inline'" not in headers["Content-Security-Policy"].split("style-src", 1)[0]


def test_standalone_growth_outputs_are_private_registered_and_hash_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main, "OUTPUT_ROOT", tmp_path)
    monkeypatch.setattr(main, "AUTH_ENABLED", False)

    def fake_growth_from_boundary(**kwargs):
        output_dir = Path(kwargs["output_dir"])
        task_id = kwargs["task_id"]
        ndvi = output_dir / "ndvi_clip.tif"
        preview = output_dir / "class_preview.png"
        report = output_dir / "growth_report.docx"
        map_path = output_dir / "map.html"
        ndvi.write_bytes(b"standalone-private-raster")
        Image.new("RGB", (20, 20), "green").save(preview)
        document = Document()
        document.add_paragraph("standalone private report")
        document.save(report)
        map_path.write_text(
            "<!doctype html><html><body><script>document.body.dataset.ready='1';</script></body></html>",
            encoding="utf-8",
        )
        digest = main._sha256_file(ndvi)
        prefix = kwargs["url_prefix"]
        return {
            "status": "success",
            "schema_version": GROWTH_ANALYSIS_SCHEMA_VERSION,
            "algorithm_version": GROWTH_ANALYSIS_ALGORITHM_VERSION,
            "task_id": task_id,
            "method": "fixed",
            "n_classes": 5,
            "total_area_mu": 1.0,
            "valid_pixel_count": 1,
            "class_breaks": [0.3, 0.45, 0.6, 0.75, 1.0],
            "raster": {
                "ndvi_clip_sha256": digest,
                "ndvi_clip_size_bytes": ndvi.stat().st_size,
                "ndvi_source": "gee",
            },
            "summary": [
                {"value": 5, "label": "优", "count": 1, "ratio": 1.0, "area_mu": 1.0, "color": "#2f9e44"}
            ],
            "outputs": {
                "ndvi_clip_tif": f"{prefix}/{ndvi.name}",
                "class_preview_png": f"{prefix}/{preview.name}",
                "report_docx": f"{prefix}/{report.name}",
                "map_html": f"{prefix}/{map_path.name}",
            },
            "message": "ok",
        }

    monkeypatch.setattr(
        "growth_analysis.run_growth_analysis_from_boundary", fake_growth_from_boundary
    )
    boundary = json.dumps(_boundary(), ensure_ascii=False).encode("utf-8")
    with TestClient(main.app) as api:
        response = api.post(
            "/api/v1/tools/run_growth_analysis_boundary_upload",
            files={"boundary_file": ("boundary.geojson", boundary, "application/geo+json")},
            data={"ndvi_source": "gee", "method": "fixed"},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["task_id"].startswith("growth-")
        assert all(
            value is None or value.startswith("/api/v1/tools/growth_analysis/")
            for value in payload["outputs"].values()
        )
        assert "/outputs/" not in json.dumps(payload)

        map_url = payload["outputs"]["map_html"]
        map_response = api.get(map_url)
        assert map_response.status_code == 200
        assert "Content-Security-Policy" in map_response.headers
        assert "'sha256-" in map_response.headers["Content-Security-Policy"]

        task_id = payload["task_id"]
        replay = api.get(f"/api/v1/tools/growth_analysis/{task_id}")
        assert replay.status_code == 200
        assert replay.json()["outputs"]["map_html"] == map_url

        preview_url = payload["outputs"]["class_preview_png"]
        preview_path = tmp_path / "growth" / task_id / "class_preview.png"
        preview_path.write_bytes(b"tampered")
        assert api.get(preview_url).status_code == 409

    con = main._db()
    with pytest.raises(Exception):
        con.execute(
            "UPDATE growth_tasks SET creator_principal = ? WHERE task_id = ?",
            ("attacker", task_id),
        )
    con.rollback()
    con.close()
