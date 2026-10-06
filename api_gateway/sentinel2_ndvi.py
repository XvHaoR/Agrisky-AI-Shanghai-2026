"""Shared Sentinel-2 SR helpers for single-window and historical NDVI jobs.

The functions in this module deliberately only build Earth Engine expressions;
they do not initialize Earth Engine or trigger remote evaluation.  Keeping the
quality mask here prevents the single-period and quarterly pipelines from
silently drifting to different cloud/shadow rules.
"""

from __future__ import annotations

from typing import Any


S2_SR_COLLECTION = "COPERNICUS/S2_SR_HARMONIZED"
S2_NDVI_FORMULA = "NDVI = (Sentinel-2 B8 - B4) / (B8 + B4)"
S2_SCL_INVALID_BASE = (0, 1, 2)
S2_SCL_CLOUD_SHADOW_SNOW = (3, 8, 9, 10, 11)
S2_MASK_DILATION_METERS = 20
S2_CLOUD_MASK_VERSION = "s2-scl-v2-20m"
S2_CLOUD_MASK_DESCRIPTION = (
    "SCL 排除 0/1/2/3/8/9/10/11；云、阴影和雪边缘外扩 20 m"
)


def _or_equals(band: Any, values: tuple[int, ...]) -> Any:
    """Build ``band == value`` joined by Earth Engine logical OR."""
    if not values:
        raise ValueError("SCL 掩膜类别不能为空")
    condition = band.eq(values[0])
    for value in values[1:]:
        condition = condition.Or(band.eq(value))
    return condition


def mask_s2_sr(image: Any) -> Any:
    """Mask invalid SCL classes and dilate cloud/shadow/snow by 20 metres."""
    scl = image.select("SCL")
    invalid_base = _or_equals(scl, S2_SCL_INVALID_BASE)
    cloud_shadow_snow = _or_equals(scl, S2_SCL_CLOUD_SHADOW_SNOW)
    invalid = invalid_base.Or(
        cloud_shadow_snow.focalMax(radius=S2_MASK_DILATION_METERS, units="meters")
    )
    return image.updateMask(invalid.Not())


def add_ndvi_band(image: Any) -> Any:
    """Add a floating-point ``ndvi`` band derived from Sentinel-2 B8/B4."""
    return image.addBands(image.normalizedDifference(["B8", "B4"]).rename("ndvi"))


def build_s2_sr_collection(
    ee_module: Any,
    *,
    roi: Any,
    start_date: str,
    end_exclusive: str,
    max_cloud_pct: float,
) -> Any:
    """Build the consistently filtered and masked Sentinel-2 collection."""
    return (
        ee_module.ImageCollection(S2_SR_COLLECTION)
        .filterDate(start_date, end_exclusive)
        .filterBounds(roi)
        .filter(ee_module.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", max_cloud_pct))
        .map(mask_s2_sr)
        .map(add_ndvi_band)
    )
