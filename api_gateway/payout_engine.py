"""
Agrisky AI payout engine.

Converts a server-side yield-loss ratio into an estimated payout. Policy
parameters are loaded from ``config/payout_rules.yaml`` by default and can be
overridden with ``AGRISKY_PAYOUT_RULES_PATH``.
"""

from __future__ import annotations

from typing import Any

from policy_config import load_payout_rules


def sum_insured_per_mu(crop_type: str) -> float:
    """Return insured amount per mu for the crop, falling back to default."""
    rules = load_payout_rules()
    insured = rules["sum_insured_per_mu"]
    if not crop_type:
        return float(insured["default"])
    key = crop_type.strip().lower()
    return float(insured.get(crop_type) or insured.get(key) or insured["default"])


def _factor(loss_ratio: float, rules: dict[str, Any]) -> tuple[float, str]:
    """减产率 → 赔付比例，支持三种曲线模式。

    stepped   : 分档定赔（同档同比例；不同减产率落同档会得到相同赔款）。
    piecewise : 以各档上界为控制点做线性插值，连续单调；保留档位形状但消除"压平"。
    linear    : 起赔点以上赔付比例 = 减产率（赔款与减产率成正比）。
    """
    mode = str(rules.get("payout_mode", "stepped")).lower()
    deductible = float(rules["deductible_threshold"])
    tiers = rules["payout_tiers"]

    if loss_ratio < deductible:
        return 0.0, f"below deductible ({deductible * 100:.0f}%)"

    if mode == "linear":
        f = min(max(loss_ratio, 0.0), 1.0)
        return round(f, 4), f"linear: payout proportional to loss ({loss_ratio * 100:.1f}%)"

    if mode == "piecewise":
        # 控制点：(起赔点, 0) 起，叠加各档 (upper, factor)；在相邻控制点间线性插值
        pts = [(deductible, 0.0)]
        pts += [(float(t["upper"]), float(t["factor"])) for t in tiers if float(t["upper"]) > deductible]
        pts = sorted(set(pts))
        for i in range(1, len(pts)):
            u0, f0 = pts[i - 1]
            u1, f1 = pts[i]
            if loss_ratio < u1:
                frac = (loss_ratio - u0) / (u1 - u0) if u1 > u0 else 0.0
                f = f0 + frac * (f1 - f0)
                return round(min(max(f, 0.0), 1.0), 4), f"piecewise: loss {loss_ratio * 100:.1f}% -> {f * 100:.1f}%"
        return round(float(pts[-1][1]), 4), f"piecewise: loss {loss_ratio * 100:.1f}% -> {pts[-1][1] * 100:.0f}%"

    # stepped（默认/兜底）
    for item in tiers:
        if loss_ratio < float(item["upper"]):
            return float(item["factor"]), str(item["label"])
    last = tiers[-1]
    return float(last["factor"]), str(last["label"])


def estimate_payout(
    crop_type: str,
    insured_area_mu: float,
    yield_loss_ratio: float,
) -> dict[str, Any]:
    """Estimate payout from crop type, insured area, and yield-loss ratio."""
    crop_type = crop_type or "default"
    insured_area_mu = max(float(insured_area_mu or 0.0), 0.0)
    yield_loss_ratio = min(max(float(yield_loss_ratio or 0.0), 0.0), 1.0)

    rules = load_payout_rules()
    deductible_threshold = float(rules["deductible_threshold"])
    sipm = sum_insured_per_mu(crop_type)
    total_si = round(insured_area_mu * sipm, 2)
    factor, tier_label = _factor(yield_loss_ratio, rules)
    payout = round(total_si * factor, 2)

    trace = [
        f"crop={crop_type}; sum_insured={sipm:.0f} yuan/mu",
        f"insured_area={insured_area_mu:.2f} mu; total_sum_insured={total_si:,.2f} yuan",
        f"yield_loss_ratio={yield_loss_ratio*100:.1f}%; deductible={deductible_threshold*100:.0f}%",
        f"payout_tier={tier_label}",
        f"estimated_payout={total_si:,.2f} * {factor*100:.0f}% = {payout:,.2f} yuan",
    ]

    return {
        "status": "success",
        "crop_type": crop_type,
        "sum_insured_per_mu": sipm,
        "insured_area_mu": round(insured_area_mu, 2),
        "total_sum_insured_yuan": total_si,
        "yield_loss_ratio": round(yield_loss_ratio, 4),
        "deductible_threshold": deductible_threshold,
        "payout_factor": factor,
        "tier_label": tier_label,
        "payout_amount_yuan": payout,
        "rule_version": str(rules["version"]),
        "trace": trace,
    }
