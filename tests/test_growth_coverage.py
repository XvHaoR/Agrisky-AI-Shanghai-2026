"""NDVI quality metadata must distinguish parcel pixels from valid cloud-free pixels."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


np = pytest.importorskip("numpy")
rasterio = pytest.importorskip("rasterio")
from rasterio.transform import from_origin


sys.path.insert(0, str(Path(__file__).parent.parent / "api_gateway"))

from growth_analysis import clip_raster_by_boundary


def test_clip_reports_valid_pixel_coverage(tmp_path: Path) -> None:
    source = tmp_path / "ndvi.tif"
    clipped = tmp_path / "ndvi-clipped.tif"
    boundary = tmp_path / "boundary.geojson"
    nodata = -9999.0
    values = np.full((4, 4), 0.6, dtype="float32")
    values[0, 0] = nodata
    with rasterio.open(
        source,
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
    boundary.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"plot_id": "PLOT-1"},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [[[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]]],
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    metadata = clip_raster_by_boundary(source, boundary, clipped, tmp_path)
    assert metadata["roi_pixel_count"] == 16
    assert metadata["valid_pixel_count"] == 15
    assert metadata["valid_pixel_coverage"] == 0.9375
