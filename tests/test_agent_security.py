"""智能体服务密钥与投保人案件范围回归测试。"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path


ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "api_gateway"))

import main
from agent import agent_runtime


def test_agent_api_base_accepts_gateway_origin_or_versioned_base() -> None:
    normalize = agent_runtime._normalize_api_base_url
    assert normalize("http://127.0.0.1:8000") == "http://127.0.0.1:8000/api/v1"
    assert normalize("http://127.0.0.1:8000/") == "http://127.0.0.1:8000/api/v1"
    assert normalize("http://api:8000/api/v1/") == "http://api:8000/api/v1"


def test_embedded_agent_key_is_registered_as_analyst() -> None:
    assert agent_runtime.GATEWAY_API_KEY
    assert agent_runtime.GATEWAY_API_KEY == main.INTERNAL_AGENT_API_KEY
    assert main.API_KEYS[agent_runtime.GATEWAY_API_KEY] == "analyst"


def test_gateway_curl_adds_internal_api_key(monkeypatch) -> None:
    captured: dict = {}

    def fake_run(command, **_kwargs):
        captured["command"] = command
        return types.SimpleNamespace(returncode=0, stdout=b"{}", stderr=b"")

    monkeypatch.setattr(agent_runtime.shutil, "which", lambda _name: "curl")
    monkeypatch.setattr(agent_runtime.subprocess, "run", fake_run)
    agent_runtime._curl("GET", f"{agent_runtime.API_BASE}/cases/CLAIM-TEST")
    command = captured["command"]
    header_index = command.index("-H")
    assert command[header_index + 1] == f"x-agrisky-api-key: {agent_runtime.GATEWAY_API_KEY}"


def test_policyholder_scope_rejects_other_claim_before_tool_call(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    def fake_curl(method, url, **_kwargs):
        calls.append((method, url))
        return {"claim_id": "CLAIM-OTHER", "policy_id": "POL-OTHER"}

    monkeypatch.setattr(agent_runtime, "_curl", fake_curl)
    result = agent_runtime.execute_tool(
        "validate_materials",
        {"claim_id": "CLAIM-OTHER"},
        scope={"username": "holder", "policy_ids": ["POL-OWN"]},
    )
    assert result["status"] == "error"
    assert "不属于当前投保人" in result["error_message"]
    assert calls == [("GET", f"{agent_runtime.API_BASE}/cases/CLAIM-OTHER")]


def test_agent_registry_and_runtime_include_new_growth_tools(monkeypatch) -> None:
    registry = json.loads((ROOT / "agent" / "tools_schema.json").read_text(encoding="utf-8"))
    registered_names = {item["function"]["name"] for item in registry["tools"]}
    runtime_names = {item["function"]["name"] for item in agent_runtime.TOOLS}
    assert registered_names == runtime_names
    assert {"run_growth_analysis", "run_historical_ndvi", "run_parcel_growth"} <= runtime_names

    calls: list[tuple[str, str, dict, int]] = []

    def fake_curl(method, url, *, body=None, timeout=180, **_kwargs):
        calls.append((method, url, body or {}, timeout))
        return {"status": "success"}

    monkeypatch.setattr(agent_runtime, "_curl", fake_curl)
    history = agent_runtime.execute_tool(
        "run_historical_ndvi",
        {"claim_id": "CLAIM-TEST", "start_year": 2022, "end_year": 2026},
    )
    parcels = agent_runtime.execute_tool("run_parcel_growth", {"claim_id": "CLAIM-TEST"})
    assert history["status"] == "success"
    assert parcels["status"] == "success"
    assert calls[0][1].endswith("/tools/run_historical_ndvi_by_claim")
    assert calls[0][2]["scale_m"] == 10
    assert calls[0][3] == 900
    assert calls[1][1].endswith("/tools/run_parcel_growth_by_claim")
    assert calls[1][3] == 600


def test_tool_arguments_are_rejected_before_execution() -> None:
    validated, error = agent_runtime._validate_tool_args(
        "validate_materials",
        {"claim_id": "CLAIM-1", "force_skip": True},
    )
    assert validated is None
    assert "未声明参数" in str(error)

    validated, error = agent_runtime._validate_tool_args("create_claim", {"policy_id": "POL-1"})
    assert validated is None
    assert "disaster_type" in str(error)
    assert "loss_date" in str(error)


def test_material_tool_context_does_not_include_raw_text(monkeypatch) -> None:
    def fake_curl(method, url, **_kwargs):
        assert method == "GET"
        assert url.endswith("/cases/CLAIM-1/material-review")
        return {
            "claim_id": "CLAIM-1",
            "passed": False,
            "blocking_count": 1,
            "documents": [{
                "document_id": "DOC-1",
                "document_type": "claim_notice",
                "original_filename": "notice.pdf",
                "parse_status": "completed",
                "sha256": "a" * 64,
            }],
            "fields": [{
                "field_name": "policy_id",
                "normalized_value": "POL-1",
                "confidence": 0.98,
                "source_ref": "p1-l1",
                "document_id": "DOC-1",
                "extractor": "regex",
                "raw_text": "包含不应进入模型的完整原文",
                "bbox": [1, 2, 3, 4],
            }],
            "findings": [],
        }

    monkeypatch.setattr(agent_runtime, "_curl", fake_curl)
    result = agent_runtime.execute_tool(
        "compare_case_materials",
        {"claim_id": "CLAIM-1"},
    )
    serialized = json.dumps(result, ensure_ascii=False)
    assert "包含不应进入模型的完整原文" not in serialized
    assert "bbox" not in serialized
    assert result["fields"][0]["source_ref"] == "p1-l1"


def test_claim_analysis_filters_policyholder_and_recomputes_queue(monkeypatch) -> None:
    def fake_curl(method, url, **_kwargs):
        assert method == "GET"
        assert "/cases?" in url
        return {
            "total": 2,
            "items": [
                {
                    "claim_id": "CLAIM-OWN",
                    "policy_id": "POL-OWN",
                    "state": "SCREENING_DONE",
                    "risk_level": "high",
                    "yield_loss_ratio": 0.4,
                    "payout_amount_yuan": None,
                    "boundary_registered": True,
                    "boundary_sha256": "a" * 64,
                },
                {
                    "claim_id": "CLAIM-OTHER",
                    "policy_id": "POL-OTHER",
                    "state": "REPORT_DRAFTED",
                    "risk_level": None,
                    "payout_amount_yuan": 999999,
                    "boundary_registered": False,
                },
            ],
        }

    monkeypatch.setattr(agent_runtime, "_curl", fake_curl)
    result = agent_runtime.execute_tool(
        "analyze_claims",
        {"limit": 200},
        scope={"username": "holder", "policy_ids": ["POL-OWN"]},
    )
    assert result["status"] == "success"
    assert result["scope"] == "policyholder"
    assert result["total"] == 1
    assert result["by_state"] == {"SCREENING_DONE": 1}
    assert result["high_risk_count"] == 1
    assert result["total_payout_yuan"] == 0
    assert [item["claim_id"] for item in result["action_queue"]] == ["CLAIM-OWN"]


def test_boundary_inspection_uses_local_registry_without_exposing_coordinates(monkeypatch) -> None:
    raw_boundary = {
        "type": "Feature",
        "properties": {"name": "敏感地块"},
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[120.0, 31.0], [120.1, 31.0], [120.0, 31.0]]],
        },
    }

    def fake_curl(method, url, **_kwargs):
        assert method == "GET"
        assert url.endswith("/policies/POL-LOCAL")
        return {
            "policy_id": "POL-LOCAL",
            "policy_version_id": "POL-LOCAL:v3",
            "crop_type": "rice",
            "address": "测试地址",
            "area_mu": 42.5,
            "boundary_registered": True,
            "boundary_sha256": "b" * 64,
            "boundary_hash_scheme": "canonical_geometry_v1",
            "boundary_geojson": raw_boundary,
            "roi_geojson": raw_boundary,
        }

    monkeypatch.setattr(agent_runtime, "_curl", fake_curl)
    result = agent_runtime.execute_tool("inspect_policy_boundary", {"policy_id": "POL-LOCAL"})
    serialized = json.dumps(result, ensure_ascii=False)
    assert result["status"] == "success"
    assert result["geometry_type"] == "Polygon"
    assert result["feature_count"] == 1
    assert result["boundary_sha256"] == "b" * 64
    assert result["raw_geometry_exposed_to_model"] is False
    assert "coordinates" not in serialized
    assert "120.0" not in serialized


def test_contract_inspection_reads_frozen_contract_without_claiming_insurer(monkeypatch) -> None:
    def fake_curl(method: str, url: str, **_kwargs):
        assert method == "GET"
        assert url.endswith("/policies/POL-1/contract")
        return {
            "contract": {
                "simulation": True,
                "simulation_disclosure": "模拟合同",
                "contract_id": "CON-1",
                "contract_number": "SIM-1",
                "contract_version": "sim-v1",
                "contract_sha256": "a" * 64,
                "insurer": "模拟保险人",
                "policyholder": "模拟投保人",
                "insurance_period": {"start": "2026-04-01", "end": "2026-11-30"},
                "payout_terms": {"sum_insured_per_mu": 800},
                "artifact_url": "/download",
            },
            "clauses": [{"id": "C7", "title": "赔款计算", "text": "合同规则"}],
        }

    monkeypatch.setattr(agent_runtime, "_curl", fake_curl)
    result = agent_runtime.execute_tool("inspect_policy_contract", {"policy_id": "POL-1"})
    assert result["status"] == "success"
    assert result["simulation"] is True
    assert result["contract_sha256"] == "a" * 64
    assert result["clauses"][0]["id"] == "C7"


def test_case_details_preserves_zero_compliance_ratio(monkeypatch) -> None:
    claim_id = "CLAIM-ZERO-RATIO"

    def fake_curl(method, url, **_kwargs):
        if method == "GET" and url.endswith(f"/cases/{claim_id}"):
            return {"claim_id": claim_id, "policy_id": "POL-ZERO"}
        if method == "GET" and url.endswith(f"/cases/{claim_id}/full"):
            return {
                "claim_id": claim_id,
                "state": "COMPLIANCE_DONE",
                "case": {"policy_id": "POL-ZERO"},
                "results": {"compliance": {"damage_ratio": 0.0, "loss_ratio": 0.75}},
            }
        raise AssertionError((method, url))

    monkeypatch.setattr(agent_runtime, "_curl", fake_curl)
    result = agent_runtime.execute_tool("get_case_details", {"claim_id": claim_id})
    assert result["status"] == "success"
    assert result["metrics"]["compliance_ratio"] == 0.0


def test_full_workflow_runs_growth_and_stops_at_report_draft(monkeypatch) -> None:
    claim_id = "CLAIM-FLOW"
    state = {"value": "MATERIAL_CHECK"}
    results: dict[str, dict] = {}
    posts: list[str] = []
    boundary = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [0, 0]]]}

    def fake_curl(method, url, *, body=None, **_kwargs):
        if method == "GET" and url.endswith(f"/cases/{claim_id}/full"):
            return {
                "claim_id": claim_id,
                "state": state["value"],
                "case": {"policy_id": "POL-FLOW"},
                "results": dict(results),
            }
        if method == "GET" and url.endswith(f"/cases/{claim_id}"):
            return {
                "claim_id": claim_id,
                "policy_id": "POL-FLOW",
                "loss_date": "2026-07-01",
                "state": state["value"],
            }
        if method == "GET" and url.endswith("/policies/POL-FLOW"):
            return {"policy_id": "POL-FLOW", "roi_geojson": boundary}
        if method != "POST":
            raise AssertionError((method, url))
        posts.append(url.rsplit("/", 1)[-1])
        if url.endswith("/tools/validate_materials"):
            state["value"] = "PREPROCESS_READY"
        elif url.endswith("/tools/run_satellite_screening"):
            state["value"] = "SCREENING_DONE"
            results["satellite"] = {"damage_ratio": 0.3}
        elif url.endswith("/tools/run_growth_analysis_by_claim"):
            results["growth"] = {"task_id": "growth-1", "status": "success"}
        elif url.endswith("/tools/run_loss_assessment"):
            results["loss_assessment"] = {"yield_loss_ratio": 0.25}
        elif url.endswith("/tools/run_compliance_estimate"):
            state["value"] = "COMPLIANCE_DONE"
            results["compliance"] = {"damage_ratio": 0.25}
        elif url.endswith("/tools/run_payout_estimate"):
            results["payout"] = {"payout_amount_yuan": 12000}
        elif url.endswith("/tools/run_rule_engine"):
            state["value"] = "RULE_DONE"
            results["rule"] = {"risk_level": "medium"}
        elif url.endswith("/tools/generate_report"):
            state["value"] = "REPORT_DRAFTED"
            results["report"] = {"generation_id": "generation-1"}
        else:
            raise AssertionError(url)
        return {"status": "success", "claim_id": claim_id, "state": state["value"]}

    monkeypatch.setattr(agent_runtime, "_curl", fake_curl)
    result = agent_runtime.execute_tool("run_claim_workflow", {"claim_id": claim_id})
    assert result["status"] == "success"
    assert result["final_state"] == "REPORT_DRAFTED"
    assert result["stopped_before_human_review"] is True
    assert result.get("synthetic_fallback_used") is None
    assert posts == [
        "validate_materials",
        "run_satellite_screening",
        "run_growth_analysis_by_claim",
        "run_loss_assessment",
        "run_compliance_estimate",
        "run_payout_estimate",
        "run_rule_engine",
        "generate_report",
    ]


def test_full_workflow_stops_when_material_state_does_not_advance(monkeypatch) -> None:
    claim_id = "CLAIM-MATERIAL-BLOCKED"
    posts: list[str] = []

    def fake_curl(method, url, **_kwargs):
        if method == "GET" and url.endswith(f"/cases/{claim_id}"):
            return {
                "claim_id": claim_id,
                "policy_id": "POL-MATERIAL-BLOCKED",
                "state": "MATERIAL_CHECK",
            }
        if method == "POST" and url.endswith("/tools/validate_materials"):
            posts.append("validate_materials")
            return {"passed": False, "missing_fields": ["insured_geom"]}
        raise AssertionError((method, url))

    monkeypatch.setattr(agent_runtime, "_curl", fake_curl)
    result = agent_runtime.execute_tool("run_claim_workflow", {"claim_id": claim_id})
    assert result["status"] == "error"
    assert result["final_state"] == "MATERIAL_CHECK"
    assert result["failed_tool"] == "validate_materials"
    assert "避免重复执行" in result["error_message"]
    assert posts == ["validate_materials"]


def test_full_workflow_fails_closed_when_growth_fails(monkeypatch) -> None:
    claim_id = "CLAIM-GROWTH-FAIL"

    def fake_curl(method, url, **_kwargs):
        if method == "GET" and url.endswith(f"/cases/{claim_id}/full"):
            return {"claim_id": claim_id, "state": "SCREENING_DONE", "results": {}}
        if method == "GET" and url.endswith(f"/cases/{claim_id}"):
            return {"claim_id": claim_id, "policy_id": "POL-FAIL", "state": "SCREENING_DONE"}
        if method == "POST" and url.endswith("/tools/run_growth_analysis_by_claim"):
            return {"status": "error", "error_message": "gee_unavailable"}
        raise AssertionError((method, url))

    monkeypatch.setattr(agent_runtime, "_curl", fake_curl)
    result = agent_runtime.execute_tool("run_claim_workflow", {"claim_id": claim_id})
    assert result["status"] == "error"
    assert result["failed_tool"] == "run_growth_analysis"
    assert result["synthetic_fallback_used"] is False
