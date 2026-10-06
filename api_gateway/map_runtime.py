"""Presentation-only runtime adapter for immutable Leaflet map evidence."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
from pathlib import Path
from urllib.parse import urlsplit


VENDOR_ROOT = Path(__file__).resolve().parent / "vendor" / "leaflet"
VENDOR_URL = "/api/v1/map-runtime/vendor"

_LEAFLET_CSS = re.compile(
    r'<link\s+rel="stylesheet"\s+href="https://unpkg\.com/leaflet@1\.9\.4/dist/leaflet\.css"[^>]*>',
    re.I | re.S,
)
_LEAFLET_JS = re.compile(
    r'<script\s+src="https://unpkg\.com/leaflet@1\.9\.4/dist/leaflet\.js"[^>]*></script>',
    re.I | re.S,
)
_INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.I | re.S)


class MapRuntimeError(ValueError):
    """Raised when a stored map cannot be rendered without changing its evidence body."""


def _global_pixel(lon: float, lat: float, zoom: int) -> tuple[float, float]:
    lat = max(min(lat, 85.05112878), -85.05112878)
    sin_lat = math.sin(math.radians(lat))
    size = 256 * (2**zoom)
    return (
        (lon + 180.0) / 360.0 * size,
        (0.5 - math.log((1 + sin_lat) / (1 - sin_lat)) / (4 * math.pi)) * size,
    )


def _lonlat(global_x: float, global_y: float, zoom: int) -> tuple[float, float]:
    size = 256 * (2**zoom)
    lon = global_x / size * 360.0 - 180.0
    mercator_y = math.pi - 2.0 * math.pi * global_y / size
    lat = math.degrees(math.atan(math.sinh(mercator_y)))
    return lon, lat


def _coordinate_pairs(value: object):
    if isinstance(value, (list, tuple)):
        if len(value) >= 2 and all(isinstance(item, (int, float)) for item in value[:2]):
            yield float(value[0]), float(value[1])
            return
        for item in value:
            yield from _coordinate_pairs(item)


def embedded_local_basemap(image_path: Path, boundary: dict) -> dict[str, object]:
    """Encode a verified screening image with the same Web Mercator view used at capture."""
    from PIL import Image

    geometry = boundary.get("geometry") if boundary.get("type") == "Feature" else boundary
    if boundary.get("type") == "FeatureCollection":
        coordinates = [
            (feature.get("geometry") or {}).get("coordinates", [])
            for feature in boundary.get("features", [])
        ]
    else:
        coordinates = (geometry or {}).get("coordinates", [])
    pairs = list(_coordinate_pairs(coordinates))
    if not pairs:
        raise MapRuntimeError("本地底图缺少有效边界")
    min_lon = min(pair[0] for pair in pairs)
    max_lon = max(pair[0] for pair in pairs)
    min_lat = min(pair[1] for pair in pairs)
    max_lat = max(pair[1] for pair in pairs)
    if min_lon >= max_lon or min_lat >= max_lat:
        raise MapRuntimeError("本地底图边界范围无效")

    try:
        with Image.open(image_path) as image:
            width, height = image.size
    except OSError as exc:
        raise MapRuntimeError("本地底图无法读取") from exc
    zoom = 6
    for candidate in range(19, 5, -1):
        x1, y1 = _global_pixel(min_lon, max_lat, candidate)
        x2, y2 = _global_pixel(max_lon, min_lat, candidate)
        if abs(x2 - x1) <= width * 0.70 and abs(y2 - y1) <= height * 0.70:
            zoom = candidate
            break
    center_lon = (min_lon + max_lon) / 2.0
    center_lat = (min_lat + max_lat) / 2.0
    center_x, center_y = _global_pixel(center_lon, center_lat, zoom)
    west, north = _lonlat(center_x - width / 2.0, center_y - height / 2.0, zoom)
    east, south = _lonlat(center_x + width / 2.0, center_y + height / 2.0, zoom)
    data_url = "data:image/png;base64," + base64.b64encode(image_path.read_bytes()).decode("ascii")
    return {"data_url": data_url, "bounds": [[south, west], [north, east]]}


def build_runtime_document(path: Path, local_basemap: dict[str, object] | None = None) -> str:
    """Replace only Leaflet CDN tags while preserving the immutable map body and data."""
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise MapRuntimeError("长势地图 HTML 无法读取") from exc

    document, css_count = _LEAFLET_CSS.subn(
        f'<link rel="stylesheet" href="{VENDOR_URL}/leaflet.css" />', source
    )
    document, js_count = _LEAFLET_JS.subn(
        f'<script src="{VENDOR_URL}/leaflet.js"></script>', document
    )
    if css_count != 1 or js_count != 1:
        raise MapRuntimeError("长势地图 Leaflet 依赖结构无效")
    if local_basemap:
        marker = "    const rasterBounds ="
        if document.count(marker) != 1:
            raise MapRuntimeError("长势地图栅格结构无效")
        payload = {
            "data_url": str(local_basemap["data_url"]),
            "bounds": local_basemap["bounds"],
        }
        payload_json = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        injection = (
            "    map.createPane('agriskyLocalBasemap');\n"
            "    map.getPane('agriskyLocalBasemap').style.zIndex = '150';\n"
            "    map.getPane('agriskyLocalBasemap').style.pointerEvents = 'none';\n"
            f"    const agriskyLocalBasemap = {payload_json};\n"
            "    L.imageOverlay(agriskyLocalBasemap.data_url, agriskyLocalBasemap.bounds, "
            "{ pane: 'agriskyLocalBasemap', opacity: 1, alt: 'Local verified context basemap' }).addTo(map);\n"
        )
        document = document.replace(marker, injection + marker, 1)
    return document


def runtime_security_headers(document: str, cors_origins: list[str]) -> dict[str, str]:
    """Build a CSP for the transformed view using hashes of its unchanged inline scripts."""
    scripts = _INLINE_SCRIPT.findall(document)
    if not scripts:
        raise MapRuntimeError("长势地图缺少可验证的内联脚本")
    script_hashes = " ".join(
        f"'sha256-{base64.b64encode(hashlib.sha256(script.encode('utf-8')).digest()).decode('ascii')}'"
        for script in scripts
    )
    frame_ancestors = ["'self'"]
    for configured_origin in cors_origins:
        parsed = urlsplit(configured_origin)
        if (
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and parsed.netloc
            and parsed.path in {"", "/"}
            and not parsed.query
            and not parsed.fragment
            and "'" not in configured_origin
            and "*" not in configured_origin
        ):
            origin = f"{parsed.scheme}://{parsed.netloc}"
            if origin not in frame_ancestors:
                frame_ancestors.append(origin)
    return {
        "Content-Security-Policy": (
            "default-src 'none'; "
            f"script-src 'self' {script_hashes}; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: https://tile.openstreetmap.org https://server.arcgisonline.com; "
            "connect-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'; "
            f"frame-ancestors {' '.join(frame_ancestors)}"
        ),
        "Referrer-Policy": "no-referrer",
    }


def vendor_asset_path(filename: str) -> Path:
    """Resolve one checked Leaflet distribution asset without allowing path traversal."""
    if not filename or "\\" in filename:
        raise MapRuntimeError("地图运行依赖文件名无效")
    root = VENDOR_ROOT.resolve()
    candidate = (root / filename).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise MapRuntimeError("地图运行依赖不存在")
    return candidate
