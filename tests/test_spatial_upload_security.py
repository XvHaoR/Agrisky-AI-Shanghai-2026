"""Boundary archive intake must remain a preflight step, never an implicit bulk import."""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent / "api_gateway"))

from spatial_utils import load_boundary_from_upload


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def test_bulk_kml_zip_is_not_treated_as_one_insured_parcel() -> None:
    payload = _zip_bytes(
        {
            "地块/地块一.kml": b"<kml/>",
            "地块/地块二.kml": b"<kml/>",
        }
    )
    result = load_boundary_from_upload(payload, "地块.zip")
    assert result["status"] == "error"
    assert result["error_code"] == "NO_SHP_IN_ZIP"
    assert "不能直接入库" in result["error_message"]


def test_spatial_zip_rejects_path_traversal_before_extraction() -> None:
    result = load_boundary_from_upload(_zip_bytes({"../escape.shp": b"not-a-shapefile"}), "parcel.zip")
    assert result["status"] == "error"
    assert result["error_code"] == "UNSAFE_OR_INVALID_ZIP"
    assert "不安全路径" in result["error_message"]


def test_spatial_zip_rejects_ambiguous_multiple_shapefiles() -> None:
    result = load_boundary_from_upload(
        _zip_bytes({"one/a.shp": b"x", "two/b.shp": b"y"}),
        "parcels.zip",
    )
    assert result["status"] == "error"
    assert result["error_code"] == "MULTIPLE_SHP_IN_ZIP"
