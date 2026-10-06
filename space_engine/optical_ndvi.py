"""
Agrisky AI — Sentinel-2 光学植被指数模块
用于农作物健康评估、长势监测与干旱预警。
"""

import ee
import logging
from typing import Any
from space_engine.gee_auth import init_gee

logger = logging.getLogger(__name__)


def calculate_ndvi(
    roi_geojson: dict,
    start_date: str,
    end_date: str,
    max_cloud_pct: float = 20.0,
    project_id: str | None = None,
) -> dict[str, Any]:
    """计算指定区域在指定时间段内的 NDVI 均值。"""
    try:
        init_gee(project_id=project_id)

        roi = ee.Geometry.Polygon(roi_geojson["coordinates"])

        collection = (
            ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
            .filterBounds(roi)
            .filterDate(start_date, end_date)
            .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", max_cloud_pct))
        )

        image_count = collection.size().getInfo()
        if image_count == 0:
            return {
                "status": "error",
                "error_code": "NO_CLEAR_IMAGE",
                "error_message": f"时间段内无清晰影像（云量 < {max_cloud_pct}%）",
            }

        image = collection.median().clip(roi)
        ndvi = image.normalizedDifference(["B8", "B4"]).rename("NDVI")

        stats = ndvi.reduceRegion(
            reducer=ee.Reducer.mean(),
            geometry=roi, scale=10, maxPixels=1e9,
        )
        mean_ndvi = stats.get("NDVI").getInfo()

        if mean_ndvi is None:
            return {"status": "error", "error_code": "CALC_ERROR", "error_message": "NDVI 返回空值"}

        health = "GOOD" if mean_ndvi > 0.6 else "FAIR" if mean_ndvi > 0.3 else "POOR"
        asset_ids = collection.aggregate_array("system:index").getInfo()

        return {
            "status": "success",
            "mean_ndvi": round(mean_ndvi, 4),
            "health_status": health,
            "image_count": image_count,
            "reference_assets": asset_ids[:10] if asset_ids else [],
        }
    except Exception as e:
        logger.error(f"NDVI 计算失败: {e}")
        return {"status": "error", "error_code": "ENGINE_ERROR", "error_message": str(e)}


def calculate_ndvi_anomaly(
    roi_geojson: dict,
    target_start: str,
    target_end: str,
    baseline_years: int = 3,
    max_cloud_pct: float = 20.0,
    project_id: str | None = None,
) -> dict[str, Any]:
    """NDVI 异常检测：将当前时段与历史同期均值对比。"""
    try:
        init_gee(project_id=project_id)

        current = calculate_ndvi(roi_geojson, target_start, target_end, max_cloud_pct, project_id=project_id)
        if current["status"] != "success":
            return current

        current_ndvi = current["mean_ndvi"]

        import datetime
        ts = datetime.date.fromisoformat(target_start)
        te = datetime.date.fromisoformat(target_end)

        historical_ndvis = []
        for y in range(1, baseline_years + 1):
            hs = ts.replace(year=ts.year - y).isoformat()
            he = te.replace(year=te.year - y).isoformat()
            hr = calculate_ndvi(roi_geojson, hs, he, max_cloud_pct, project_id=project_id)
            if hr["status"] == "success":
                historical_ndvis.append(hr["mean_ndvi"])

        if not historical_ndvis:
            return {"status": "error", "error_code": "NO_HISTORICAL_DATA", "error_message": "无历史数据"}

        baseline = sum(historical_ndvis) / len(historical_ndvis)
        dev = (current_ndvi - baseline) / baseline * 100 if baseline else 0
        is_anomaly = dev < -15.0

        return {
            "status": "success",
            "current_ndvi": current_ndvi,
            "baseline_mean_ndvi": round(baseline, 4),
            "deviation_pct": round(dev, 2),
            "is_anomaly": is_anomaly,
            "historical_samples": len(historical_ndvis),
        }
    except Exception as e:
        logger.error(f"NDVI 异常检测失败: {e}")
        return {"status": "error", "error_code": "ENGINE_ERROR", "error_message": str(e)}
