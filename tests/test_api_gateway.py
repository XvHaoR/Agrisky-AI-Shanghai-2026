"""
Agrisky AI — API 网关基础测试
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "api_gateway"))
TEST_DB_PATH = Path(tempfile.gettempdir()) / "agrisky_test.db"
if TEST_DB_PATH.exists():
    TEST_DB_PATH.unlink()
os.environ["AGRISKY_DB_PATH"] = str(TEST_DB_PATH)

from fastapi.testclient import TestClient
import main
from main import app

client = TestClient(app)


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["version"] == "1.0.0"


def test_full_pipeline():
    """完整案件链路：建案 → 材料校验 → 卫星初筛 → ... → 归档"""

    # 1. 建案
    resp = client.post("/api/v1/tools/create_claim", json={
        "policy_id": "POL-2026-001",
        "disaster_type": "flood",
        "loss_date": "2026-07-18",
        "crop_type": "rice",
        "plot_id": "PLOT-008",
    })
    assert resp.status_code == 200
    claim_id = resp.json()["claim_id"]
    assert resp.json()["state"] == "MATERIAL_CHECK"

    # 2. 材料校验
    resp = client.post("/api/v1/tools/validate_materials", json={
        "claim_id": claim_id,
    })
    assert resp.status_code == 200
    assert resp.json()["passed"] is True

    # 3. 卫星初筛
    sample_roi = {
        "type": "Polygon",
        "coordinates": [[[113.5, 34.5], [113.6, 34.5], [113.6, 34.6], [113.5, 34.6], [113.5, 34.5]]],
    }
    resp = client.post("/api/v1/tools/run_satellite_screening", json={
        "claim_id": claim_id,
        "roi_geojson": sample_roi,
        "start_date": "2026-07-01",
        "end_date": "2026-07-20",
    })
    assert resp.status_code == 200
    # GEE 可能不可用，接受 Mock 结果

    # 4. 合规核验：旧接口不得接受客户端自报受灾几何
    sample_damage = {
        "type": "Polygon",
        "coordinates": [[[113.51, 34.51], [113.59, 34.51], [113.59, 34.59], [113.51, 34.59], [113.51, 34.51]]],
    }
    sample_insured = {
        "type": "Polygon",
        "coordinates": [[[113.50, 34.50], [113.55, 34.50], [113.55, 34.55], [113.50, 34.55], [113.50, 34.50]]],
    }
    legacy = client.post("/api/v1/tools/run_compliance_calc", json={
        "claim_id": claim_id,
        "damage_geojson": sample_damage,
        "insured_geom": sample_insured,
    })
    assert legacy.status_code == 409

    # 全流程改用服务端已持久化的遥感受损比例和在册边界
    resp = client.post("/api/v1/tools/run_compliance_estimate", json={
        "claim_id": claim_id,
    })
    assert resp.status_code == 200
    comp = resp.json()
    assert "valid_damage_area_mu" in comp
    assert "clip_log" in comp

    # 6. 规则引擎
    resp = client.post("/api/v1/tools/run_rule_engine", json={
        "claim_id": claim_id,
        "damage_ratio": comp["damage_ratio"],
        "crop_type": "rice",
    })
    assert resp.status_code == 200
    rule = resp.json()
    assert "risk_level" in rule
    assert "review_required" in rule

    # 7. 生成报告
    resp = client.post("/api/v1/tools/generate_report", json={
        "claim_id": claim_id,
    })
    assert resp.status_code == 200
    report = resp.json()
    assert report["status"] == "success"

    # 8. 查询状态
    resp = client.get(f"/api/v1/cases/{claim_id}")
    assert resp.status_code == 200
    assert resp.json()["state"] == "REPORT_DRAFTED"

    # 9. 人工审核通过
    resp = client.post(
        f"/api/v1/cases/{claim_id}/human_review",
        json={
            "decision": "approved",
            "generation_id": report["generation_id"],
            "comment": "测试审核通过",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["state"] == "ARCHIVED"

    # 10. 审计日志
    resp = client.get(f"/api/v1/audit/{claim_id}")
    assert resp.status_code == 200
    assert len(resp.json()) >= 7  # 至少 7 条记录

    print(f"✅ 完整链路测试通过: {claim_id}")


def test_state_machine_enforced():
    """状态机强制执行 —— 非法跳步应被拒绝"""

    # 建案
    resp = client.post("/api/v1/tools/create_claim", json={
        "policy_id": "POL-2026-001",
        "disaster_type": "flood",
        "loss_date": "2026-07-18",
        "crop_type": "rice",
        "plot_id": "PLOT-002",
    })
    claim_id = resp.json()["claim_id"]

    # 尝试在 MATERIAL_CHECK 状态直接调用 run_rule_engine → 应失败
    resp = client.post("/api/v1/tools/run_rule_engine", json={
        "claim_id": claim_id,
        "damage_ratio": 0.5,
        "crop_type": "rice",
    })
    assert resp.status_code == 400, "非法工具调用应返回 400"

    # 尝试在 INIT 状态调用 generate_report → 应失败（案件已建）
    resp2 = client.post("/api/v1/tools/create_claim", json={
        "policy_id": "POL-2026-001",
        "disaster_type": "drought",
        "loss_date": "2026-06-01",
        "crop_type": "rice",
        "plot_id": "PLOT-003",
    })
    claim_id2 = resp2.json()["claim_id"]
    resp = client.post("/api/v1/tools/run_rule_engine", json={
        "claim_id": claim_id2,
        "damage_ratio": 0.3,
        "crop_type": "wheat",
    })
    assert resp.status_code == 400, "MATERIAL_CHECK 状态不能调 run_rule_engine"

    print("✅ 状态机强制执行测试通过")


def test_api_key_auth_when_enabled(monkeypatch):
    monkeypatch.setattr(main, "AUTH_ENABLED", True)
    monkeypatch.setattr(
        main,
        "API_KEYS",
        {
            "viewer-token": "viewer",
            "analyst-token": "analyst",
            "auditor-token": "auditor",
        },
    )

    assert client.get("/health").status_code == 200
    assert client.get("/api/v1/cases").status_code == 401

    resp = client.get("/api/v1/cases", headers={"X-Agrisky-API-Key": "viewer-token"})
    assert resp.status_code == 200

    resp = client.post(
        "/api/v1/tools/create_claim",
        json={
            "policy_id": "POL-AUTH-001",
            "disaster_type": "flood",
            "loss_date": "2026-07-18",
            "crop_type": "rice",
            "plot_id": "PLOT-AUTH",
        },
        headers={"X-Agrisky-API-Key": "viewer-token"},
    )
    assert resp.status_code == 403

    resp = client.post(
        "/api/v1/tools/create_claim",
        json={
            "policy_id": "POL-2026-001",
            "disaster_type": "flood",
            "loss_date": "2026-07-18",
            "crop_type": "rice",
            "plot_id": "PLOT-AUTH",
        },
        headers={"X-Agrisky-API-Key": "analyst-token"},
    )
    assert resp.status_code == 200

    resp = client.post(
        "/api/v1/cases/CLAIM-MISSING/human_review",
        json={"decision": "approved", "generation_id": "v1-test", "comment": "test"},
        headers={"X-Agrisky-API-Key": "analyst-token"},
    )
    assert resp.status_code == 403

    resp = client.post(
        "/api/v1/cases/CLAIM-MISSING/human_review",
        json={"decision": "approved", "generation_id": "v1-test", "comment": "test"},
        headers={"X-Agrisky-API-Key": "auditor-token"},
    )
    assert resp.status_code == 403
    assert "管理员会话" in resp.json()["detail"]


if __name__ == "__main__":
    test_health()
    print("✅ 健康检查通过")
    test_full_pipeline()
    test_state_machine_enforced()
    print("\n🎉 全部 API 网关测试通过")
