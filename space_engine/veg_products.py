"""
Agrisky AI — 多源遥感植被/生产力产品库

为"超越单期 NDVI"的灾损评估提供统一的产品接口。每个产品（指数或高阶产品）
都给出：灾前/灾后取值、归一化到 [0,1] 的"功能损失分量"（decline_score）、
数据来源（gee/synthetic）、以及分辨率/适用性等注意事项。

设计要点：
    - 每个产品都有 GEE 真实计算路径 + 本地确定性合成回退；
      本地无 Earth Engine 凭证时自动走合成，认证后自动切换真实数据。
    - 高阶产品（MODIS LAI/FAPAR、GPP、NPP）是 500m 量级，单个地块可能不足
      一个像元——这些只作"区域生产力背景/交叉校验"，注意事项里会标注。
    - decline_score 统一表示"该产品指示的植被功能相对损失比例"，便于上层融合
      成减产率（见 api_gateway/loss_engine.py）。
"""

from __future__ import annotations

import logging
import math
from typing import Any

logger = logging.getLogger("agrisky.veg_products")


# ── 产品注册表（NDVI 之外的"算法菜单"）──────────────────────────────
# scale: "field" = 适合地块级(10–20m)，"regional" = 区域级(≥250m)背景层
PRODUCTS: dict[str, dict[str, Any]] = {
    "ndvi": {
        "id": "ndvi", "name_cn": "NDVI 归一化植被指数",
        "sensor": "Sentinel-2 SR", "native_res_m": 10, "scale": "field",
        "measures": "绿度/叶绿素与覆盖度", "signal": "decline",
        "disasters": ["flood", "drought", "hail", "typhoon", "pest", "frost", "other"],
        "caveat": "高郁闭度易饱和，单期绝对值难区分受灾与本底差异",
    },
    "evi2": {
        "id": "evi2", "name_cn": "EVI2 增强植被指数",
        "sensor": "Sentinel-2 SR", "native_res_m": 10, "scale": "field",
        "measures": "抗饱和/抗土壤背景的绿度", "signal": "decline",
        "disasters": ["flood", "drought", "hail", "typhoon", "pest", "frost", "other"],
        "caveat": "对高生物量比 NDVI 稳健",
    },
    "ndwi": {
        "id": "ndwi", "name_cn": "NDWI/NDMI 水分指数",
        "sensor": "Sentinel-2 SR", "native_res_m": 20, "scale": "field",
        "measures": "冠层/地表含水量", "signal": "water",
        "disasters": ["flood", "typhoon", "drought"],
        "caveat": "洪涝/台风下水体上升、干旱下水分下降，方向随灾种解读",
    },
    "ndre": {
        "id": "ndre", "name_cn": "NDRE 红边指数",
        "sensor": "Sentinel-2 红边", "native_res_m": 20, "scale": "field",
        "measures": "叶绿素/氮素胁迫（病虫害敏感）", "signal": "decline",
        "disasters": ["pest", "frost", "hail", "drought"],
        "caveat": "对病虫害与营养胁迫早于 NDVI 响应",
    },
    "lai_fapar": {
        "id": "lai_fapar", "name_cn": "LAI/FAPAR 叶面积与光合有效辐射吸收比",
        "sensor": "MODIS MCD15A3H", "native_res_m": 500, "scale": "regional",
        "measures": "冠层结构与光能截获", "signal": "decline",
        "disasters": ["drought", "flood", "pest", "other"],
        "caveat": "500m 像元，单地块小于像元时仅作区域参考",
    },
    "gpp": {
        "id": "gpp", "name_cn": "GPP 总初级生产力",
        "sensor": "MODIS MOD17A2H", "native_res_m": 500, "scale": "regional",
        "measures": "8 天总初级生产力（接近产量）", "signal": "decline",
        "disasters": ["drought", "flood", "hail", "typhoon", "pest", "frost", "other"],
        "caveat": "500m/8天，比绿度更接近产量，但分辨率粗、仅作区域背景",
    },
    "npp": {
        "id": "npp", "name_cn": "NPP 净初级生产力",
        "sensor": "MODIS MOD17A3HGF", "native_res_m": 500, "scale": "regional",
        "measures": "年净初级生产力（产量代理）", "signal": "decline",
        "disasters": ["drought", "other"],
        "caveat": "年尺度 500m，适合趋势/年景对比，不用于单次灾损面积",
    },
    # ── 基于多年历史基线的算法（非单期 pre/post 差分）──────────────
    "gpp_anom": {
        "id": "gpp_anom", "name_cn": "GPP 生产力异常（对多年基线）",
        "sensor": "MODIS MOD17A2H", "native_res_m": 500, "scale": "regional",
        "measures": "当期总初级生产力相对历史同期基线的下降幅度", "signal": "anomaly",
        "disasters": ["flood", "drought", "hail", "typhoon", "pest", "frost", "other"],
        "caveat": "生产力气候态异常，比单期绿度更接近减产；500m 区域尺度",
    },
    "npp_anom": {
        "id": "npp_anom", "name_cn": "NPP 年净生产力异常（对历年均值）",
        "sensor": "MODIS MOD17A3HGF", "native_res_m": 500, "scale": "regional",
        "measures": "当年净初级生产力相对历年均值的下降（年景对比）", "signal": "anomaly",
        "disasters": ["flood", "drought", "hail", "typhoon", "pest", "frost", "other"],
        "caveat": "年尺度产量代理；与历年比可剔除常年低产本底，500m 区域尺度",
    },
    "vci": {
        "id": "vci", "name_cn": "VCI 植被状态指数（多年 NDVI 极值）",
        "sensor": "Sentinel-2 SR（多年）", "native_res_m": 20, "scale": "field",
        "measures": "当期 NDVI 在历史同期 min–max 中的相对位置，1−VCI 记为损失", "signal": "anomaly",
        "disasters": ["drought", "flood", "hail", "typhoon", "pest", "frost", "other"],
        "caveat": "标准农情/旱情条件指数，区分受灾与常年长势差异，需多年存档",
    },
}

# 多年基线回看年数（异常/VCI 算法用）。年数越多越稳但 GEE 调用越多、越慢。
_BASELINE_YEARS = 2


def available_products() -> list[dict[str, Any]]:
    """返回产品注册表（供前端/接口展示"可选算法菜单"）。"""
    return [dict(p) for p in PRODUCTS.values()]


def _roi_centroid(roi_geojson: dict) -> tuple[float, float]:
    """粗略求 ROI 几何质心（仅用于合成种子与定位，非精确）。"""
    coords: list[list[float]] = []

    def _walk(node: Any) -> None:
        if isinstance(node, (list, tuple)):
            if node and isinstance(node[0], (int, float)) and len(node) >= 2:
                coords.append([float(node[0]), float(node[1])])
            else:
                for child in node:
                    _walk(child)

    _walk(roi_geojson.get("coordinates", []))
    if not coords:
        return 0.0, 0.0
    lon = sum(c[0] for c in coords) / len(coords)
    lat = sum(c[1] for c in coords) / len(coords)
    return lon, lat


# ── 灾种 → 各产品"典型相对损失"基线（合成回退使用）──────────────────
# 仅用于本地无 GEE 时给出可复现、按灾种合理的演示值；真实数据来自 GEE。
_DISASTER_SYNTH_BASE: dict[str, dict[str, float]] = {
    "flood":   {"ndvi": 0.42, "evi2": 0.38, "ndwi": 0.55, "ndre": 0.30, "lai_fapar": 0.40, "gpp": 0.45, "npp": 0.30, "gpp_anom": 0.40, "npp_anom": 0.32, "vci": 0.45},
    "drought": {"ndvi": 0.35, "evi2": 0.33, "ndwi": 0.40, "ndre": 0.30, "lai_fapar": 0.38, "gpp": 0.42, "npp": 0.40, "gpp_anom": 0.48, "npp_anom": 0.45, "vci": 0.50},
    "hail":    {"ndvi": 0.50, "evi2": 0.46, "ndwi": 0.20, "ndre": 0.45, "lai_fapar": 0.35, "gpp": 0.40, "npp": 0.25, "gpp_anom": 0.35, "npp_anom": 0.26, "vci": 0.42},
    "typhoon": {"ndvi": 0.45, "evi2": 0.42, "ndwi": 0.48, "ndre": 0.35, "lai_fapar": 0.38, "gpp": 0.44, "npp": 0.28, "gpp_anom": 0.40, "npp_anom": 0.30, "vci": 0.44},
    "pest":    {"ndvi": 0.33, "evi2": 0.31, "ndwi": 0.18, "ndre": 0.48, "lai_fapar": 0.30, "gpp": 0.34, "npp": 0.22, "gpp_anom": 0.33, "npp_anom": 0.25, "vci": 0.40},
    "frost":   {"ndvi": 0.40, "evi2": 0.37, "ndwi": 0.22, "ndre": 0.42, "lai_fapar": 0.33, "gpp": 0.38, "npp": 0.24, "gpp_anom": 0.38, "npp_anom": 0.27, "vci": 0.43},
    "other":   {"ndvi": 0.30, "evi2": 0.28, "ndwi": 0.25, "ndre": 0.28, "lai_fapar": 0.28, "gpp": 0.30, "npp": 0.22, "gpp_anom": 0.30, "npp_anom": 0.24, "vci": 0.32},
}


def _synthetic_product(product_id: str, roi_geojson: dict, disaster_type: str) -> dict[str, Any]:
    """确定性合成单产品异常（本地无 GEE 时回退）。

    用 ROI 质心 + 灾种 + 产品 做种子，保证同输入可复现、且按灾种分布合理。
    """
    import numpy as np

    lon, lat = _roi_centroid(roi_geojson)
    base = _DISASTER_SYNTH_BASE.get(disaster_type, _DISASTER_SYNTH_BASE["other"]).get(product_id, 0.3)
    seed = int(abs(lon * 1000 + lat * 1000 + hash(product_id) % 9973 + hash(disaster_type) % 7919)) % (2**32 - 1)
    rng = np.random.default_rng(seed)
    decline = float(np.clip(base + rng.normal(0, 0.06), 0.0, 0.95))

    # 给出可读的"基线/当期"代理值（仅展示用）
    if product_id in ("gpp", "npp", "gpp_anom", "npp_anom"):
        pre = round(float(rng.uniform(60, 120)), 1)      # gC/m² 量级代理（基线）
        post = round(pre * (1 - decline), 1)
    elif product_id == "ndwi":
        pre = round(float(rng.uniform(0.05, 0.25)), 3)
        post = round(pre + decline * 0.4, 3)             # 水分指数：灾后上升代表淹没/积水
    else:
        pre = round(float(rng.uniform(0.55, 0.82)), 3)   # 指数灾前较高
        post = round(pre * (1 - decline), 3)

    return {
        "raw_pre": pre,
        "raw_post": post,
        "decline_score": round(decline, 4),
        "data_source": "synthetic",
    }


def _gee_product(
    product_id: str,
    roi_geojson: dict,
    pre_start: str,
    pre_end: str,
    post_start: str,
    post_end: str,
    project_id: str | None,
) -> dict[str, Any]:
    """从 Google Earth Engine 计算单产品灾前/灾后均值并转为损失分量。

    本地无凭证时不会走到这里（上层先用 _has_gee_credentials 判断）。
    任一步异常都抛出，由上层回退到合成。
    """
    import ee
    from space_engine.gee_auth import init_gee

    if not init_gee(project_id=project_id):
        raise RuntimeError("GEE 未初始化")

    roi = ee.Geometry(roi_geojson)

    def _s2_index(start: str, end: str, expr: str) -> float:
        col = (
            ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
            .filterBounds(roi)
            .filterDate(start, end)
            .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 40))
        )
        img = col.median()
        if expr == "ndvi":
            band = img.normalizedDifference(["B8", "B4"])
        elif expr == "evi2":
            band = img.expression(
                "2.5*(N-R)/(N+2.4*R+1)",
                {"N": img.select("B8").divide(10000), "R": img.select("B4").divide(10000)},
            )
        elif expr == "ndwi":
            band = img.normalizedDifference(["B8", "B11"])  # Gao NDWI/NDMI
        elif expr == "ndre":
            band = img.normalizedDifference(["B8", "B5"])
        else:
            raise ValueError(f"未知 S2 指数: {expr}")
        val = band.reduceRegion(ee.Reducer.mean(), roi, scale=20, maxPixels=1e9).values().get(0)
        return float(ee.Number(val).getInfo())

    def _modis_mean(collection: str, band: str, start: str, end: str, scale_factor: float) -> float:
        col = ee.ImageCollection(collection).filterBounds(roi).filterDate(start, end).select(band)
        img = col.mean().multiply(scale_factor)
        val = img.reduceRegion(ee.Reducer.mean(), roi, scale=500, maxPixels=1e9).values().get(0)
        return float(ee.Number(val).getInfo())

    def _shift_year(d_iso: str, k: int) -> str:
        from datetime import date
        d = date.fromisoformat(d_iso)
        try:
            return d.replace(year=d.year - k).isoformat()
        except ValueError:  # 2/29
            return d.replace(year=d.year - k, day=28).isoformat()

    def _baseline_mean(coll: str, band: str, start: str, end: str, sf: float) -> float:
        """历史同期（过去 _BASELINE_YEARS 年）均值作为气候态基线。"""
        vals = []
        for k in range(1, _BASELINE_YEARS + 1):
            try:
                vals.append(_modis_mean(coll, band, _shift_year(start, k), _shift_year(end, k), sf))
            except Exception:  # noqa: BLE001 某些基线年缺数据则跳过
                continue
        if not vals:
            raise RuntimeError("基线年无可用数据")
        return sum(vals) / len(vals)

    if product_id in ("ndvi", "evi2", "ndwi", "ndre"):
        pre = _s2_index(pre_start, pre_end, product_id)
        post = _s2_index(post_start, post_end, product_id)
    elif product_id == "lai_fapar":
        pre = _modis_mean("MODIS/061/MCD15A3H", "Lai", pre_start, pre_end, 0.1)
        post = _modis_mean("MODIS/061/MCD15A3H", "Lai", post_start, post_end, 0.1)
    elif product_id == "gpp":
        pre = _modis_mean("MODIS/061/MOD17A2H", "Gpp", pre_start, pre_end, 0.0001)
        post = _modis_mean("MODIS/061/MOD17A2H", "Gpp", post_start, post_end, 0.0001)
    elif product_id == "npp":
        pre = _modis_mean("MODIS/061/MOD17A3HGF", "Npp", pre_start, pre_end, 0.0001)
        post = _modis_mean("MODIS/061/MOD17A3HGF", "Npp", post_start, post_end, 0.0001)
    elif product_id == "gpp_anom":
        # 算法：当期 GPP vs 历史同期多年基线 → 生产力异常（≠ 单期 pre/post 差分）
        post = _modis_mean("MODIS/061/MOD17A2H", "Gpp", post_start, post_end, 0.0001)
        pre = _baseline_mean("MODIS/061/MOD17A2H", "Gpp", post_start, post_end, 0.0001)
    elif product_id == "npp_anom":
        # 算法：当年 NPP vs 历年均值 → 年景对比，剔除常年低产本底
        yr = int(post_start[:4])
        post = _modis_mean("MODIS/061/MOD17A3HGF", "Npp", f"{yr}-01-01", f"{yr}-12-31", 0.0001)
        base_vals = []
        for k in range(1, _BASELINE_YEARS + 1):
            try:
                base_vals.append(
                    _modis_mean("MODIS/061/MOD17A3HGF", "Npp", f"{yr - k}-01-01", f"{yr - k}-12-31", 0.0001)
                )
            except Exception:  # noqa: BLE001
                continue
        if not base_vals:
            raise RuntimeError("NPP 基线年无可用数据")
        pre = sum(base_vals) / len(base_vals)
    elif product_id == "vci":
        # 算法：VCI = (NDVI_cur - NDVI_min) / (NDVI_max - NDVI_min)，多年同期极值；1-VCI 记损
        cur = _s2_index(post_start, post_end, "ndvi")
        yearly = [cur]
        for k in range(1, _BASELINE_YEARS + 1):
            try:
                yearly.append(_s2_index(_shift_year(post_start, k), _shift_year(post_end, k), "ndvi"))
            except Exception:  # noqa: BLE001
                continue
        nmin, nmax = min(yearly), max(yearly)
        vci = (cur - nmin) / (nmax - nmin) if nmax > nmin else 1.0
        loss = float(min(max(1.0 - vci, 0.0), 0.98))
        return {
            "raw_pre": round(nmax, 4),   # 历史同期最佳 NDVI
            "raw_post": round(cur, 4),   # 当期 NDVI
            "decline_score": round(loss, 4),
            "data_source": "gee",
        }
    else:
        raise ValueError(f"未知产品: {product_id}")

    signal = PRODUCTS[product_id]["signal"]
    if signal == "water":
        # 水分指数：灾后相对上升（积水/淹没）记为损失分量
        decline = max(0.0, (post - pre)) / (abs(pre) + 0.2)
    else:
        decline = max(0.0, (pre - post)) / (abs(pre) + 1e-6)
    decline = float(min(decline, 0.98))

    return {
        "raw_pre": round(pre, 4),
        "raw_post": round(post, 4),
        "decline_score": round(decline, 4),
        "data_source": "gee",
    }


def compute_product(
    product_id: str,
    roi_geojson: dict,
    *,
    disaster_type: str = "other",
    pre_start: str = "",
    pre_end: str = "",
    post_start: str = "",
    post_end: str = "",
    project_id: str | None = None,
) -> dict[str, Any]:
    """计算单个产品的灾损分量。优先 GEE，无凭证或失败时回退合成。"""
    if product_id not in PRODUCTS:
        raise ValueError(f"未注册的产品: {product_id}")

    meta = PRODUCTS[product_id]
    result: dict[str, Any]
    try:
        from space_engine.gee_auth import _has_gee_credentials

        if _has_gee_credentials() and pre_start and post_start:
            result = _gee_product(
                product_id, roi_geojson, pre_start, pre_end, post_start, post_end, project_id
            )
        else:
            result = _synthetic_product(product_id, roi_geojson, disaster_type)
            result["fallback_reason"] = "无 GEE 凭证或缺少灾前/灾后日期"
    except Exception as exc:  # noqa: BLE001 — 任意 GEE 失败都回退合成
        logger.warning(f"产品 {product_id} GEE 计算失败，回退合成: {exc}")
        result = _synthetic_product(product_id, roi_geojson, disaster_type)
        result["fallback_reason"] = str(exc)

    result.update(
        {
            "id": product_id,
            "name_cn": meta["name_cn"],
            "sensor": meta["sensor"],
            "native_res_m": meta["native_res_m"],
            "scale": meta["scale"],
            "measures": meta["measures"],
            "caveat": meta["caveat"],
        }
    )
    return result
