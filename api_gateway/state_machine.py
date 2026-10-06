"""
Agrisky AI — 案件状态机
控制案件生命周期的合法流转。这是系统最核心的流程约束。
"""

from dataclasses import dataclass
from enum import Enum


class S(str, Enum):
    INIT = "INIT"
    MATERIAL_CHECK = "MATERIAL_CHECK"
    PREPROCESS_READY = "PREPROCESS_READY"
    SCREENING_DONE = "SCREENING_DONE"
    NDVI_DONE = "NDVI_DONE"

    COMPLIANCE_DONE = "COMPLIANCE_DONE"
    RULE_DONE = "RULE_DONE"
    REPORT_DRAFTED = "REPORT_DRAFTED"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    ARCHIVED = "ARCHIVED"


FORWARD_TRANSITIONS: dict[S, list[S]] = {
    S.INIT:              [S.MATERIAL_CHECK],
    S.MATERIAL_CHECK:    [S.PREPROCESS_READY],
    S.PREPROCESS_READY:  [S.SCREENING_DONE],
    S.SCREENING_DONE:    [S.NDVI_DONE, S.COMPLIANCE_DONE],
    S.NDVI_DONE:         [S.COMPLIANCE_DONE],
    S.COMPLIANCE_DONE:   [S.RULE_DONE],
    S.RULE_DONE:         [S.REPORT_DRAFTED],
    S.REPORT_DRAFTED:    [S.HUMAN_REVIEW],
    S.HUMAN_REVIEW:      [S.ARCHIVED],
    S.ARCHIVED:          [],
}

FORBIDDEN_SKIPS: tuple[tuple[S, S], ...] = (
    (S.INIT, S.SCREENING_DONE),
    (S.INIT, S.COMPLIANCE_DONE),
    (S.INIT, S.ARCHIVED),
    (S.MATERIAL_CHECK, S.SCREENING_DONE),
    (S.PREPROCESS_READY, S.COMPLIANCE_DONE),
    (S.RULE_DONE, S.ARCHIVED),
    (S.REPORT_DRAFTED, S.ARCHIVED),
)

ROLLBACK_TRANSITIONS: dict[S, list[S]] = {
    S.HUMAN_REVIEW: [
        S.MATERIAL_CHECK, S.PREPROCESS_READY, S.SCREENING_DONE,
        S.NDVI_DONE, S.COMPLIANCE_DONE,
        S.RULE_DONE, S.REPORT_DRAFTED,
    ],
}

STATE_TOOLS: dict[S, list[str]] = {
    S.INIT:             ["create_claim"],
    S.MATERIAL_CHECK:   ["validate_materials"],
    S.PREPROCESS_READY: ["run_satellite_screening"],
    S.SCREENING_DONE:   [
        "run_growth_analysis_upload",
        "run_growth_analysis_boundary_upload",
        "run_growth_analysis_by_claim",
        "run_loss_assessment",
        "run_compliance_estimate",
    ],
    S.NDVI_DONE:        ["run_loss_assessment", "run_compliance_estimate"],
    S.COMPLIANCE_DONE:  ["run_rule_engine"],
    S.RULE_DONE:        ["generate_report"],
    S.REPORT_DRAFTED:   [],
    S.HUMAN_REVIEW:     [],
    S.ARCHIVED:         [],
}


@dataclass
class TransitionResult:
    allowed: bool
    from_state: S
    to_state: S
    reason: str = ""


def can_transition(from_state: S, to_state: S) -> TransitionResult:
    if to_state in FORWARD_TRANSITIONS.get(from_state, []):
        return TransitionResult(True, from_state, to_state, "正向流转")
    if from_state in ROLLBACK_TRANSITIONS and to_state in ROLLBACK_TRANSITIONS[from_state]:
        return TransitionResult(True, from_state, to_state, "审核回退")
    return TransitionResult(False, from_state, to_state,
                            f"不允许从 {from_state.value} 直接到 {to_state.value}")


def can_call_tool(state: S, tool_name: str) -> bool:
    return tool_name in STATE_TOOLS.get(state, [])


def get_next_states(state: S) -> list[S]:
    return FORWARD_TRANSITIONS.get(state, []) + ROLLBACK_TRANSITIONS.get(state, [])


def get_allowed_tools(state: S) -> list[str]:
    return STATE_TOOLS.get(state, [])


def check_missing_fields(case_data: dict) -> list[str]:
    missing = []
    for f in ["policy_id", "disaster_type", "loss_date", "crop_type"]:
        if not case_data.get(f):
            missing.append(f)
    if not case_data.get("plot_id") and not case_data.get("insured_geom"):
        missing.append("plot_id 或 insured_geom")
    return missing


if __name__ == "__main__":
    print("=== 正向流转测试 ===")
    for t in [(S.INIT, S.MATERIAL_CHECK), (S.SCREENING_DONE, S.NDVI_DONE),
              (S.NDVI_DONE, S.COMPLIANCE_DONE), (S.INIT, S.SCREENING_DONE)]:
        r = can_transition(*t)
        print(f"  {t[0].value} -> {t[1].value}: {'OK' if r.allowed else 'BLOCKED'}")

    print("\n=== 工具权限测试 ===")
    for t in [(S.SCREENING_DONE, "run_ndvi_analysis"), (S.INIT, "run_ndvi_analysis")]:
        ok = can_call_tool(*t)
        print(f"  {t[0].value} / {t[1]}: {'OK' if ok else 'BLOCKED'}")

    print("\nAll tests passed.")
