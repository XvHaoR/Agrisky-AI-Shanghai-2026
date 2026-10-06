"""
Agrisky AI — Sentinel-1 SAR 洪涝提取模块
利用微波雷达数据穿透云层，提取洪涝淹没范围。
"""

import ee
import logging
from typing import Any
from space_engine.gee_auth import init_gee

logger = logging.getLogger(__name__)


def _ee_geometry(roi_geojson: dict) -> ee.Geometry:
    """Build an Earth Engine geometry from common GeoJSON geometry shapes."""
    geometry = roi_geojson.get("geometry") if roi_geojson.get("type") == "Feature" else roi_geojson
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if geometry_type == "Polygon":
        return ee.Geometry.Polygon(coordinates)
    if geometry_type == "MultiPolygon":
        return ee.Geometry.MultiPolygon(coordinates)
    if geometry_type == "FeatureCollection":
        return ee.FeatureCollection(geometry).geometry()
    return ee.Geometry(geometry)


def calculate_flood_ratio(
    roi_geojson: dict,
    start_date: str,
    end_date: str,
    water_threshold_db: float = -16.0,
    focal_radius_m: float = 50.0,
    project_id: str | None = None,
) -> dict[str, Any]:
    """
    基于 Sentinel-1 SAR 数据计算指定区域内的洪涝淹没比例。

    Args:
        roi_geojson: GeoJSON Polygon，兴趣区域（WGS84, EPSG:4326）
        start_date: 起始日期（YYYY-MM-DD）
        end_date: 结束日期（YYYY-MM-DD）
        water_threshold_db: VH 后向散射阈值 (dB)，默认 -16.0
        focal_radius_m: 中值滤波半径（米），默认 50
        project_id: GEE 项目 ID

    Returns:
        {
            "status": "success" | "error",
            "total_area_mu": float,        # 区域总面积（亩）
            "flooded_area_mu": float,      # 淹没面积（亩）
            "damage_ratio": float,         # 受灾比例
            "confidence": "high" | "medium" | "low",
            "reference_assets": list[str],  # 使用的影像 ID 列表
        }
    """
    try:
        if not init_gee(project_id=project_id):
            return {"status": "error", "error_code": "GEE_INIT_FAILED", "error_message": "GEE 初始化失败，请检查网络和代理设置"}

        roi = _ee_geometry(roi_geojson)
        roi_area_sqm = roi.area().getInfo()

        # 获取 Sentinel-1 GRD 数据
        collection = (
            ee.ImageCollection("COPERNICUS/S1_GRD")
            .filterBounds(roi)
            .filterDate(start_date, end_date)
            .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VH"))
            .filter(ee.Filter.eq("instrumentMode", "IW"))
        )

        image_count = collection.size().getInfo()
        if image_count == 0:
            return {
                "status": "error",
                "error_code": "NO_DATA",
                "error_message": f"时间段 {start_date}~{end_date} 内该区域无 Sentinel-1 数据",
                "retryable": False,
                "recommended_human_action": "请调整时间范围或使用其他数据源",
            }

        # 镶嵌 + 降噪 + 裁剪
        image = collection.select("VH").mosaic().clip(roi)
        smoothed = image.focal_median(focal_radius_m, "circle", "meters")

        # 阈值法提取水体
        water_mask = smoothed.lt(water_threshold_db)

        # 计算水体面积
        water_area_img = water_mask.multiply(ee.Image.pixelArea())
        water_stats = water_area_img.reduceRegion(
            reducer=ee.Reducer.sum(),
            geometry=roi,
            scale=10,
            maxPixels=1e9,
        )

        flooded_area_sqm = water_stats.get("VH").getInfo()
        damage_ratio = flooded_area_sqm / roi_area_sqm if roi_area_sqm > 0 else 0.0

        # 置信度评定
        if damage_ratio > 0.3:
            confidence = "high"
        elif damage_ratio > 0.1:
            confidence = "medium"
        else:
            confidence = "low"

        # 收集参考影像 ID
        asset_ids = collection.aggregate_array("system:index").getInfo()

        # 生成卫星影像缩略图
        thumb_url = None
        s2_thumb_url = None
        try:
            # SAR VH 影像 —— 灰度底图
            sar_gray = image.select("VH").visualize(min=-25, max=-5)
            # 水体掩膜 —— 青色
            water_viz = water_mask.selfMask().visualize(palette=["00FFFF"])
            # 叠加：非水体区域显示 SAR 灰度，水体区域显示青色
            composite = water_viz.unmask(sar_gray).clip(roi)
            thumb_url = composite.getThumbURL({
                "region": roi,
                "dimensions": 800,
                "format": "png",
            })
            logger.info(f"SAR 缩略图已生成: {thumb_url[:80]}...")
        except Exception as e:
            logger.warning(f"SAR 缩略图生成失败: {e}")

        # Sentinel-2 光学影像
        try:
            s2_col = (
                ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
                .filterBounds(roi)
                .filterDate(start_date, end_date)
                .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 30))
                .limit(1)
            )
            if s2_col.size().getInfo() > 0:
                s2_img = s2_col.first().clip(roi)
                s2_rgb = s2_img.visualize(
                    bands=["B4", "B3", "B2"],
                    min=[0, 0, 0],
                    max=[3000, 3000, 3000],
                )
                s2_thumb_url = s2_rgb.getThumbURL({
                    "region": roi,
                    "dimensions": 800,
                    "format": "png",
                })
                logger.info(f"S2 缩略图已生成: {s2_thumb_url[:80]}...")
        except Exception as e:
            logger.warning(f"Sentinel-2 缩略图生成失败: {e}")

        return {
            "status": "success",
            "total_area_mu": round(roi_area_sqm * 0.0015, 2),
            "flooded_area_mu": round(flooded_area_sqm * 0.0015, 2),
            "damage_ratio": round(damage_ratio, 4),
            "confidence": confidence,
            "water_threshold_db": water_threshold_db,
            "reference_assets": asset_ids[:10] if asset_ids else [],
            "image_count": image_count,
            "thumbnail_url": thumb_url,
            "s2_thumbnail_url": s2_thumb_url,
        }

    except Exception as e:
        logger.error(f"SAR 洪涝提取失败: {e}")
        return {
            "status": "error",
            "error_code": "ENGINE_ERROR",
            "error_message": str(e),
            "retryable": True,
            "recommended_human_action": "请检查 GEE 凭证和网络连接后重试",
        }


def calculate_flood_change(
    roi_geojson: dict,
    pre_start: str,
    pre_end: str,
    post_start: str,
    post_end: str,
    project_id: str | None = None,
) -> dict[str, Any]:
    """对比灾前灾后水体变化，检测新增淹没区域。"""
    try:
        init_gee(project_id=project_id)
        roi = _ee_geometry(roi_geojson)

        def _get_water_mask(start: str, end: str) -> ee.Image:
            col = (
                ee.ImageCollection("COPERNICUS/S1_GRD")
                .filterBounds(roi)
                .filterDate(start, end)
                .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VH"))
                .filter(ee.Filter.eq("instrumentMode", "IW"))
            )
            img = col.select("VH").mosaic().clip(roi)
            return img.focal_median(50, "circle", "meters").lt(-16.0)

        pre_water = _get_water_mask(pre_start, pre_end)
        post_water = _get_water_mask(post_start, post_end)

        # 新增水体 = 灾后有水 AND 灾前无水
        new_water = post_water.And(pre_water.Not())

        roi_area_sqm = roi.area().getInfo()
        new_water_area_img = new_water.multiply(ee.Image.pixelArea())
        stats = new_water_area_img.reduceRegion(
            reducer=ee.Reducer.sum(),
            geometry=roi,
            scale=10,
            maxPixels=1e9,
        )
        new_water_sqm = stats.get("VH").getInfo()

        return {
            "status": "success",
            "total_area_mu": round(roi_area_sqm * 0.0015, 2),
            "new_flooded_area_mu": round(new_water_sqm * 0.0015, 2),
            "damage_ratio": round(new_water_sqm / roi_area_sqm, 4) if roi_area_sqm > 0 else 0,
        }

    except Exception as e:
        logger.error(f"洪涝变化检测失败: {e}")
        return {
            "status": "error",
            "error_code": "ENGINE_ERROR",
            "error_message": str(e),
        }


if __name__ == "__main__":
    sample_roi = {
        "type": "Polygon",
        "coordinates": [[
            [113.5, 34.5],
            [113.6, 34.5],
            [113.6, 34.6],
            [113.5, 34.6],
            [113.5, 34.5],
        ]],
    }
    result = calculate_flood_ratio(sample_roi, "2026-07-01", "2026-07-20")
    print(result)
