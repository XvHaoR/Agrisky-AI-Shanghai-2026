"""
Agrisky AI — 状态机回归测试
每次状态机或规则变更后必须跑通。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "api_gateway"))

from state_machine import (
    S,
    can_transition,
    can_call_tool,
    check_missing_fields,
    FORBIDDEN_SKIPS,
    STATE_TOOLS,
)


def test_forward_chain():
    """主链路正向流转：INIT → ... → ARCHIVED"""
    chain = [
        S.INIT,
        S.MATERIAL_CHECK,
        S.PREPROCESS_READY,
        S.SCREENING_DONE,
        S.COMPLIANCE_DONE,
        S.RULE_DONE,
        S.REPORT_DRAFTED,
        S.HUMAN_REVIEW,
        S.ARCHIVED,
    ]
    for i in range(len(chain) - 1):
        result = can_transition(chain[i], chain[i + 1])
        assert result.allowed, f"正向流转失败: {chain[i].value} → {chain[i+1].value}"



def test_forbidden_skips():
    """禁止的跳步行为"""
    for from_s, to_s in FORBIDDEN_SKIPS:
        result = can_transition(from_s, to_s)
        assert not result.allowed, f"禁止的跳步未被拦截: {from_s.value} → {to_s.value}"


def test_human_review_gate():
    """任何非 HUMAN_REVIEW 状态不能直接到 ARCHIVED"""
    for state in [S.RULE_DONE, S.REPORT_DRAFTED, S.COMPLIANCE_DONE]:
        result = can_transition(state, S.ARCHIVED)
        assert not result.allowed, f"{state.value} → ARCHIVED 应该被禁止"


def test_tool_state_permissions():
    """工具必须在正确的状态下才能调用"""
    # create_claim 只能在 INIT 调
    assert can_call_tool(S.INIT, "create_claim") is True
    assert can_call_tool(S.MATERIAL_CHECK, "create_claim") is False

    # run_rule_engine 只能在 COMPLIANCE_DONE 调
    assert can_call_tool(S.COMPLIANCE_DONE, "run_rule_engine") is True
    assert can_call_tool(S.INIT, "run_rule_engine") is False

    # 长势评估端点应与实际 API 名称保持一致
    assert can_call_tool(S.SCREENING_DONE, "run_growth_analysis_boundary_upload") is True
    assert can_call_tool(S.SCREENING_DONE, "run_growth_analysis_upload") is True
    assert can_call_tool(S.SCREENING_DONE, "run_ndvi_analysis") is False

    # generate_report 只能在 RULE_DONE 调
    assert can_call_tool(S.RULE_DONE, "generate_report") is True
    assert can_call_tool(S.COMPLIANCE_DONE, "generate_report") is False


def test_check_missing_fields():
    """关键字段缺失检测"""
    # 缺失 policy_id
    assert "policy_id" in check_missing_fields({})
    # 缺失 plot_id 和 insured_geom
    d = {"policy_id": "P1", "disaster_type": "flood", "loss_date": "2026-01-01", "crop_type": "rice"}
    missing = check_missing_fields(d)
    assert "plot_id 或 insured_geom" in str(missing)
    # 全部齐全
    d["plot_id"] = "P-001"
    assert len(check_missing_fields(d)) == 0


def test_rollback():
    """人工审核驳回回退"""
    result = can_transition(S.HUMAN_REVIEW, S.MATERIAL_CHECK)
    assert result.allowed, "应允许从 HUMAN_REVIEW 回退到 MATERIAL_CHECK"

    result = can_transition(S.HUMAN_REVIEW, S.INIT)
    assert not result.allowed, "不允许回退到 INIT"


def test_every_state_has_tools():
    """每个状态都有定义的工具列表"""
    for state in S:
        assert state in STATE_TOOLS, f"状态 {state.value} 未在 STATE_TOOLS 中定义"


if __name__ == "__main__":
    test_forward_chain()
    print("✅ 正向链路测试通过")
    test_forbidden_skips()
    print("✅ 禁止跳步测试通过")
    test_human_review_gate()
    print("✅ 人工审核门禁测试通过")
    test_tool_state_permissions()
    print("✅ 工具权限测试通过")
    test_check_missing_fields()
    print("✅ 字段缺失检测测试通过")
    test_rollback()
    print("✅ 回退流测试通过")
    test_every_state_has_tools()
    print("✅ 状态工具映射完整性测试通过")
    print("\n🎉 全部 7 项状态机回归测试通过")
