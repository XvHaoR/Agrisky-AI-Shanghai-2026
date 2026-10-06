from __future__ import annotations

from PIL import Image

import growth_analysis


def test_draw_geojson_polygons_keeps_fill_transparent_and_draws_every_polygon(monkeypatch) -> None:
    monkeypatch.setattr(
        growth_analysis,
        "_report_local_xy",
        lambda lon, lat, _view: (lon, lat),
    )
    image = Image.new("RGBA", (100, 100), (24, 36, 48, 255))
    boundary = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {},
                "geometry": {
                    "type": "MultiPolygon",
                    "coordinates": [
                        [[[10, 10], [30, 10], [30, 30], [10, 30], [10, 10]]],
                        [[[60, 60], [80, 60], [80, 80], [60, 80], [60, 60]]],
                    ],
                },
            }
        ],
    }

    growth_analysis._draw_geojson_polygons(
        image,
        boundary,
        {},
        fill=(0, 0, 0, 0),
        outline=(13, 221, 195, 255),
        width=2,
    )

    assert image.getpixel((10, 10)) == (13, 221, 195, 255)
    assert image.getpixel((60, 60)) == (13, 221, 195, 255)
    assert image.getpixel((20, 20)) == (24, 36, 48, 255)
    assert image.getpixel((70, 70)) == (24, 36, 48, 255)
