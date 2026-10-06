"""
Agrisky AI — 多源遥感灾损融合引擎（减产率产出层）

把多个遥感产品（NDVI/EVI2/NDWI/NDRE + MODIS LAI/GPP/NPP）的灾损分量按灾种
加权融合成一个**减产率 yield_loss_ratio (0–1)**，并给出：
    - 置信度（产品来源 + 一致性）
    - 各产品贡献明细
    - 主导驱动因子
    - 注意事项（如 500m 高阶产品仅作区域参考）

这是对"单期 NDVI 分级"的替代：从"场景内相对排名"升级为"基于异常、可落到金额"
的绝对减产率。减产率随后供合规面积与（后续的）赔付引擎使用。
"""

from __future__ import annotations

import sys
from pathlib import Path
from statistics import pstdev
from typing import Any

# space_engine 在项目根下；确保无论从哪个 cwd 启动都能导入
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from space_engine.veg_products import PRODUCTS, compute_product  # noqa: E402


# 灾种 → {贡献项: 权重}；在所选项上归一化。
# 三类算法并用：①急性指数变化(ndvi/evi2/ndwi/ndre 灾前后差分) ②生产力气候态异常
# (gpp_anom/npp_anom 对多年基线) ③植被状态条件指数(vci)。productivity+VCI 占主导，
# NDVI 仅作其中一项（≤0.18），避免"只用 NDVI"。
DISASTER_WEIGHTS: dict[str, dict[str, float]] = {
    "flood":   {"ndvi": 0.15, "ndwi": 0.20, "evi2": 0.10, "vci": 0.15, "gpp_anom": 0.22, "npp_anom": 0.18},
    "drought": {"ndvi": 0.12, "evi2": 0.08, "ndwi": 0.13, "vci": 0.20, "gpp_anom": 0.25, "npp_anom": 0.22},
    "hail":    {"ndvi": 0.18, "ndre": 0.15, "evi2": 0.10, "vci": 0.15, "gpp_anom": 0.22, "npp_anom": 0.20},
    "typhoon": {"ndvi": 0.15, "ndwi": 0.17, "evi2": 0.08, "vci": 0.15, "gpp_anom": 0.25, "npp_anom": 0.20},
    "pest":    {"ndre": 0.22, "ndvi": 0.12, "evi2": 0.08, "vci": 0.16, "gpp_anom": 0.22, "npp_anom": 0.20},
    "frost":   {"ndvi": 0.15, "ndre": 0.15, "evi2": 0.08, "vci": 0.15, "gpp_anom": 0.25, "npp_anom": 0.22},
    "other":   {"ndvi": 0.18, "evi2": 0.12, "vci": 0.20, "gpp_anom": 0.25, "npp_anom": 0.25},
}

# 减产率 → 严重度分级（展示用，5 级与现有长势配色一致）
SEVERITY_BANDS = [
    (0.10, 1, "轻微", "#2f9e44"),
    (0.25, 2, "偏轻", "#8cc152"),
    (0.45, 3, "中度", "#e4c441"),
    (0.70, 4, "较重", "#ef8f35"),
    (1.01, 5, "严重", "#d64545"),
]


def _severity(loss_ratio: float) -> dict[str, Any]:
    for upper, value, label, color in SEVERITY_BANDS:
        if loss_ratio < upper:
            return {"value": value, "label": label, "color": color}
    return {"value": 5, "label": "严重", "color": "#d64545"}


def assess_yield_loss(
    roi_geojson: dict,
    disaster_type: str,
    *,
    pre_start: str = "",
    pre_end: str = "",
    post_start: str = "",
    post_end: str = "",
    project_id: str | None = None,
    products: list[str] | None = None,
) -> dict[str, Any]:
    """多源融合估算减产率。

    Returns 包含 yield_loss_ratio、confidence、各产品 breakdown、主导因子、
    注意事项与数据来源的结构化评估。
    """
    disaster_type = (disaster_type or "other").lower()
    weights = dict(DISASTER_WEIGHTS.get(disaster_type, DISASTER_WEIGHTS["other"]))
    if products:
        weights = {pid: weights.get(pid, 0.10) for pid in products if pid in PRODUCTS}
        if not weights:
            weights = dict(DISASTER_WEIGHTS["other"])

    total_w = sum(weights.values()) or 1.0
    breakdown: list[dict[str, Any]] = []
    weighted_sum = 0.0
    sources: set[str] = set()

    for pid, raw_w in weights.items():
        prod = compute_product(
            pid,
            roi_geojson,
            disaster_type=disaster_type,
            pre_start=pre_start,
            pre_end=pre_end,
            post_start=post_start,
            post_end=post_end,
            project_id=project_id,
        )
        w = raw_w / total_w
        contribution = prod["decline_score"] * w
        weighted_sum += contribution
        sources.add(prod["data_source"])
        breakdown.append(
            {
                "id": prod["id"],
                "name_cn": prod["name_cn"],
                "sensor": prod["sensor"],
                "native_res_m": prod["native_res_m"],
                "scale": prod["scale"],
                "decline_score": prod["decline_score"],
                "weight": round(w, 4),
                "contribution": round(contribution, 4),
                "raw_pre": prod.get("raw_pre"),
                "raw_post": prod.get("raw_post"),
                "data_source": prod["data_source"],
                "caveat": prod.get("caveat", ""),
            }
        )

    yield_loss_ratio = round(min(max(weighted_sum, 0.0), 0.98), 4)

    # 主导因子：贡献最大的前两个产品
    drivers = sorted(breakdown, key=lambda r: r["contribution"], reverse=True)[:2]
    dominant = [d["name_cn"] for d in drivers]

    # 置信度：真实数据 > 合成；产品一致性高 → 更可信
    scores = [r["decline_score"] for r in breakdown]
    spread = pstdev(scores) if len(scores) > 1 else 0.0
    if sources == {"gee"}:
        base_conf = 0.85
    elif "gee" in sources:
        base_conf = 0.70
    else:
        base_conf = 0.45  # 全合成（本地演示）
    confidence_score = round(max(0.2, base_conf - spread), 3)
    confidence = "high" if confidence_score >= 0.75 else "medium" if confidence_score >= 0.5 else "low"

    # 注意事项
    caveats: list[str] = []
    if sources == {"synthetic"}:
        caveats.append("本地无 GEE 凭证，全部产品使用合成数据，仅供流程演示")
    regional = [r["name_cn"] for r in breakdown if r["scale"] == "regional"]
    if regional:
        caveats.append(f"{'、'.join(regional)} 为 500m 量级，地块小于像元时仅作区域生产力背景")
    if spread > 0.18:
        caveats.append("各产品指示的损失差异较大，建议人工复核")

    return {
        "status": "success",
        "disaster_type": disaster_type,
        "yield_loss_ratio": yield_loss_ratio,
        "severity": _severity(yield_loss_ratio),
        "confidence": confidence,
        "confidence_score": confidence_score,
        "dominant_drivers": dominant,
        "product_breakdown": breakdown,
        "data_sources": sorted(sources),
        "caveats": caveats,
        "method": "multi-algorithm fusion v2: 急性指数变化 + GPP/NPP 多年基线异常 + VCI 条件指数",
        "window": {
            "pre": [pre_start, pre_end],
            "post": [post_start, post_end],
        },
    }
