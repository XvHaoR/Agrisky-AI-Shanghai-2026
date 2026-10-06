from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest
from PIL import Image

import map_runtime


SAMPLE_MAP = """<!doctype html>
<html><head>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
 integrity="sha256-css" crossorigin="anonymous" />
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
 integrity="sha256-js" crossorigin="anonymous"></script>
</head><body><div id="map"></div><script>
const evidence = {"boundary": [[132.229, 46.868]], "classes": [1, 2, 3]};
window.__evidenceDigest = "unchanged";
</script></body></html>"""

SAMPLE_MAP_WITH_RASTER = SAMPLE_MAP.replace(
    "const evidence =",
    "const map = L.map('map').setView([46.87, 132.24], 13);\n    const rasterBounds = [[46.86,132.22],[46.88,132.26]];\nconst evidence =",
)


def test_runtime_document_localizes_leaflet_without_changing_evidence(tmp_path: Path) -> None:
    source = tmp_path / "map.html"
    source.write_text(SAMPLE_MAP, encoding="utf-8")

    document = map_runtime.build_runtime_document(source)

    assert "unpkg.com" not in document
    assert f'{map_runtime.VENDOR_URL}/leaflet.css' in document
    assert f'{map_runtime.VENDOR_URL}/leaflet.js' in document
    assert 'const evidence = {"boundary": [[132.229, 46.868]], "classes": [1, 2, 3]};' in document
    assert 'window.__evidenceDigest = "unchanged";' in document


def test_runtime_security_headers_hash_inline_script_and_allow_tiles(tmp_path: Path) -> None:
    source = tmp_path / "map.html"
    source.write_text(SAMPLE_MAP, encoding="utf-8")
    document = map_runtime.build_runtime_document(source)

    headers = map_runtime.runtime_security_headers(
        document,
        ["http://127.0.0.1:3012", "https://agrisky.example.com"],
    )

    csp = headers["Content-Security-Policy"]
    script = map_runtime._INLINE_SCRIPT.findall(document)[0]
    expected_digest = base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode("ascii")
    assert f"'sha256-{expected_digest}'" in csp
    assert "https://server.arcgisonline.com" in csp
    assert "frame-ancestors 'self' http://127.0.0.1:3012 https://agrisky.example.com" in csp


def test_runtime_document_rejects_unrecognized_leaflet_structure(tmp_path: Path) -> None:
    source = tmp_path / "map.html"
    source.write_text("<html><body>no runtime dependency</body></html>", encoding="utf-8")

    with pytest.raises(map_runtime.MapRuntimeError):
        map_runtime.build_runtime_document(source)


def test_runtime_document_embeds_georeferenced_local_basemap(tmp_path: Path) -> None:
    source = tmp_path / "map.html"
    source.write_text(SAMPLE_MAP_WITH_RASTER, encoding="utf-8")
    image_path = tmp_path / "context.png"
    Image.new("RGB", (760, 540), (32, 64, 48)).save(image_path)
    boundary = {
        "type": "Polygon",
        "coordinates": [[[132.22, 46.86], [132.26, 46.86], [132.26, 46.88], [132.22, 46.88], [132.22, 46.86]]],
    }

    local_basemap = map_runtime.embedded_local_basemap(image_path, boundary)
    document = map_runtime.build_runtime_document(source, local_basemap)

    assert "data:image/png;base64," in document
    assert "agriskyLocalBasemap" in document
    assert document.index("agriskyLocalBasemap") < document.index("const rasterBounds")
    bounds = local_basemap["bounds"]
    assert bounds[0][0] < 46.86
    assert bounds[1][0] > 46.88


def test_vendor_asset_path_rejects_traversal() -> None:
    with pytest.raises(map_runtime.MapRuntimeError):
        map_runtime.vendor_asset_path("../main.py")

    assert map_runtime.vendor_asset_path("leaflet.js").name == "leaflet.js"
