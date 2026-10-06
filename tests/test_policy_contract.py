from __future__ import annotations

import json
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

import main
from contract_engine import build_simulated_contract, render_contract_docx


ROI = {
    "type": "Polygon",
    "coordinates": [[[113.1, 34.1], [113.11, 34.1], [113.11, 34.11], [113.1, 34.11], [113.1, 34.1]]],
}


def test_simulated_contract_is_downloadable_and_drives_payout(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(main, "OUTPUT_ROOT", tmp_path)
    monkeypatch.setattr(main, "AUTH_ENABLED", False)
    policy_id = f"POL-CONTRACT-{uuid.uuid4().hex[:10]}"
    claim_id = f"CLAIM-CONTRACT-{uuid.uuid4().hex[:10]}"
    saved = main._save_policy(
        policy_id=policy_id,
        holder_name="谢保农业种植专业合作社（模拟）",
        crop_type="corn",
        address="示范地块",
        boundary_geojson=ROI,
    )
    contract = saved["contract"]
    assert contract["simulation"] is True
    assert contract["contract_version"]

    con = main._db()
    try:
        con.execute(
            "INSERT INTO cases VALUES (?,?,?,?,?,?,?,?)",
            (claim_id, policy_id, "SCREENING_DONE", "flood", "2025-08-15", "corn", "PLOT-001", "2025-08-15T00:00:00"),
        )
        con.execute(
            "INSERT INTO case_results VALUES (?,?,?,?)",
            (claim_id, "satellite", json.dumps({"damage_ratio": 0.25}), "2025-08-15T00:00:01"),
        )
        con.execute(
            "INSERT INTO case_results VALUES (?,?,?,?)",
            (claim_id, "loss_assessment", json.dumps({"yield_loss_ratio": 0.50}), "2025-08-15T00:00:02"),
        )
        con.commit()
    finally:
        con.close()

    with TestClient(main.app) as api:
        contract_response = api.get(f"/api/v1/policies/{policy_id}/contract")
        assert contract_response.status_code == 200
        assert contract_response.json()["contract"]["insurance_period"] == {
            "start": "2025-04-01", "end": "2025-11-30"
        }
        download_response = api.get(f"/api/v1/policies/{policy_id}/contract/download")
        assert download_response.status_code == 200
        assert download_response.headers["x-content-sha256"]
        assert download_response.content[:2] == b"PK"

        compliance_response = api.post(
            "/api/v1/tools/run_compliance_estimate", json={"claim_id": claim_id}
        )
        assert compliance_response.status_code == 200
        compliance = compliance_response.json()
        assert all(item["passed"] for item in compliance["contract_checks"])
        case_contract = main._load_case_contract(claim_id)
        assert case_contract is not None
        assert case_contract["contract_id"] == compliance["contract_id"]
        assert case_contract["insurance_period"]["start"] == "2025-04-01"

        payout_response = api.post(
            "/api/v1/tools/run_payout_estimate", json={"claim_id": claim_id}
        )
        assert payout_response.status_code == 200
        payout = payout_response.json()
        assert payout["contract_number"] == case_contract["contract_number"]
        assert payout["affected_area_mu"] > 0
        assert payout["yield_loss_ratio"] == 0.5
        assert payout["payout_amount_yuan"] > 0
        assert "C7" in payout["contract_clause_refs"]


def test_policy_upload_freezes_uploaded_contract_and_exposes_compliance_library(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(main, "OUTPUT_ROOT", tmp_path / "outputs")
    monkeypatch.setattr(main, "AUTH_ENABLED", False)
    policy_id = f"POL-UPLOAD-{uuid.uuid4().hex[:10]}"
    policy = {
        "policy_id": policy_id,
        "policy_version_id": f"{policy_id}:v1",
        "holder_name": "示范水稻合作社",
        "crop_type": "rice",
        "address": "示范稻田",
        "area_mu": 100.0,
    }
    contract_path = tmp_path / "signed-contract.docx"
    render_contract_docx(build_simulated_contract(policy, insurance_year=2025), contract_path)

    with TestClient(main.app) as api:
        missing = api.post(
            "/api/v1/policies/upload",
            data={
                "policy_id": policy_id,
                "holder_name": policy["holder_name"],
                "crop_type": "rice",
                "address": policy["address"],
            },
            files={"boundary_file": ("boundary.geojson", json.dumps(ROI).encode(), "application/json")},
        )
        assert missing.status_code == 422

        response = api.post(
            "/api/v1/policies/upload",
            data={
                "policy_id": policy_id,
                "holder_name": policy["holder_name"],
                "crop_type": "rice",
                "address": policy["address"],
            },
            files={
                "boundary_file": ("boundary.geojson", json.dumps(ROI).encode(), "application/json"),
                "contract_file": (
                    contract_path.name,
                    contract_path.read_bytes(),
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ),
            },
        )
        assert response.status_code == 200, response.text
        frozen = response.json()["contract"]
        assert frozen["source_type"] == "uploaded_contract"
        assert frozen["human_confirmed"] is True
        assert frozen["source_sha256"]
        assert frozen["insurance_period"] == {"start": "2025-04-01", "end": "2025-11-30"}

        download = api.get(f"/api/v1/policies/{policy_id}/contract/download")
        assert download.status_code == 200
        assert download.content == contract_path.read_bytes()

        library = api.get("/api/v1/compliance/library")
        assert library.status_code == 200
        payload = library.json()
        assert {doc["id"] for doc in payload["legal_documents"]} >= {
            "L-01", "L-02", "L-03", "L-04", "L-05", "L-06"
        }
        citation_ids = {item["id"] for item in payload["report_citations"]}
        assert {"L-01§12", "L-02§21", "L-03§18", "L-06§15"} <= citation_ids
        assert any(row["stage"] == "赔款测算" for row in payload["report_basis"])
        assert set(payload["crop_schemes"]) >= {"rice", "corn", "wheat"}
