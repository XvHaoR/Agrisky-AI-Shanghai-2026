"""Raster integration for per-feature, parcel, and union NDVI results."""

from __future__ import annotations

import re
from pathlib import Path

import pytest


np = pytest.importorskip("numpy")
rasterio = pytest.importorskip("rasterio")
from rasterio.transform import from_origin

from parcel_growth import analyze_parcel_growth


def _feature(parcel_id: str, feature_id: str, coordinates: list, area_mu: float) -> dict:
    return {
        "type": "Feature",
        "properties": {
            "parcel_id": parcel_id,
            "feature_id": feature_id,
            "area_mu": area_mu,
        },
        "geometry": {"type": "Polygon", "coordinates": [coordinates]},
    }


def test_analyzes_each_feature_parcel_and_overall_union(tmp_path: Path) -> None:
    raster_path = tmp_path / "ndvi.tif"
    nodata = -9999.0
    values = np.asarray(
        [
            [0.2, 0.2, 0.8, 0.8],
            [0.2, nodata, 0.8, 0.8],
            [0.4, 0.4, 0.6, 0.6],
            [0.4, 0.4, 0.6, 0.6],
        ],
        dtype="float32",
    )
    with rasterio.open(
        raster_path,
        "w",
        driver="GTiff",
        height=4,
        width=4,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=from_origin(0, 4, 1, 1),
        nodata=nodata,
    ) as dataset:
        dataset.write(values, 1)

    manifest = {
        "type": "FeatureCollection",
        "total_area_mu": 20,
        "features": [
            _feature("P-LEFT", "F-LEFT", [[0, 0], [2, 0], [2, 4], [0, 4], [0, 0]], 10),
            _feature("P-RIGHT", "F-RIGHT", [[2, 0], [4, 0], [4, 4], [2, 4], [2, 0]], 10),
        ],
    }

    result = analyze_parcel_growth(raster_path=raster_path, manifest=manifest)

    assert result["schema_version"] == "parcel_growth_v1"
    assert result["algorithm_version"] == "zonal_ndvi_union_v1"
    assert result["classification_scheme_version"] == "ndvi_upper_bound_digitize_v1"
    assert result["n_classes"] == 5
    assert re.fullmatch(r"[0-9a-f]{64}", result["boundary_sha256"])
    assert result["raster"]["filename"] == "ndvi.tif"
    assert re.fullmatch(r"[0-9a-f]{64}", result["raster"]["sha256"])
    assert "path" not in result["raster"]
    assert len(result["features"]) == 2
    assert len(result["parcels"]) == 2

    left = result["features"][0]
    right = result["features"][1]
    assert (left["parcel_id"], left["feature_id"]) == ("P-LEFT", "F-LEFT")
    assert left["roi_pixel_count"] == 8
    assert left["valid_pixel_count"] == 7
    assert left["valid_pixel_coverage"] == 0.875
    assert [row["count"] for row in left["summary"]] == [3, 4, 0, 0, 0]
    assert "poor_growth_dominant" in left["anomaly_flags"]
    assert right["valid_pixel_count"] == 8
    assert [row["count"] for row in right["summary"]] == [0, 0, 0, 4, 4]

    overall = result["overall"]
    assert overall["parcel_count"] == 2
    assert overall["feature_count"] == 2
    assert overall["roi_pixel_count"] == 16
    assert overall["valid_pixel_count"] == 15
    assert overall["valid_pixel_coverage"] == 0.9375
    assert overall["mean_ndvi"] == pytest.approx(0.52)
    assert [row["count"] for row in overall["summary"]] == [3, 4, 0, 4, 4]
    assert sum(row["area_mu"] for row in overall["summary"]) == 20
