from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger("agrisky.policy_config")

BASE_DIR = Path(__file__).resolve().parent.parent

DEFAULT_RISK_RULES: dict[str, Any] = {
    "version": "default_v1",
    "thresholds": {
        "flood": {"high": 0.40, "medium": 0.15},
        "drought": {"high": 0.50, "medium": 0.20},
        "hail": {"high": 0.30, "medium": 0.10},
        "typhoon": {"high": 0.40, "medium": 0.15},
        "pest": {"high": 0.35, "medium": 0.10},
        "frost": {"high": 0.40, "medium": 0.15},
        "other": {"high": 0.40, "medium": 0.20},
    },
    "high_payout_threshold_yuan": 500000,
}

DEFAULT_PAYOUT_RULES: dict[str, Any] = {
    "version": "payout_v1",
    # 赔付曲线模式：stepped(分档压平) / piecewise(按档位线性插值,连续) / linear(赔款∝减产率)
    "payout_mode": "piecewise",
    "deductible_threshold": 0.20,
    "sum_insured_per_mu": {
        "rice": 1000.0,
        "wheat": 800.0,
        "corn": 700.0,
        "maize": 700.0,
        "soybean": 700.0,
        "cotton": 1200.0,
        "peanut": 900.0,
        "rapeseed": 600.0,
        "default": 800.0,
    },
    "payout_tiers": [
        {"upper": 0.20, "factor": 0.00, "label": "below deductible"},
        {"upper": 0.40, "factor": 0.30, "label": "light loss, 30 percent payout"},
        {"upper": 0.60, "factor": 0.50, "label": "moderate loss, 50 percent payout"},
        {"upper": 0.80, "factor": 0.80, "label": "severe loss, 80 percent payout"},
        {"upper": 1.01, "factor": 1.00, "label": "extreme loss, 100 percent payout"},
    ],
}


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        import yaml
    except ImportError:
        logger.warning("PyYAML is not installed; using built-in policy defaults")
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Policy config must be a mapping: {path}")
    return data


def _merge(defaults: dict[str, Any], loaded: dict[str, Any]) -> dict[str, Any]:
    merged = dict(defaults)
    for key, value in loaded.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            nested = dict(merged[key])
            nested.update(value)
            merged[key] = nested
        else:
            merged[key] = value
    return merged


@lru_cache(maxsize=1)
def load_risk_rules() -> dict[str, Any]:
    path = Path(os.getenv("AGRISKY_RISK_RULES_PATH", BASE_DIR / "config" / "risk_rules.yaml"))
    if not path.is_absolute():
        path = BASE_DIR / path
    return _merge(DEFAULT_RISK_RULES, _load_yaml(path))


@lru_cache(maxsize=1)
def load_payout_rules() -> dict[str, Any]:
    path = Path(os.getenv("AGRISKY_PAYOUT_RULES_PATH", BASE_DIR / "config" / "payout_rules.yaml"))
    if not path.is_absolute():
        path = BASE_DIR / path
    return _merge(DEFAULT_PAYOUT_RULES, _load_yaml(path))
