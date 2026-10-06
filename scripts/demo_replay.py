"""Replay real HTTP endpoints against the explicitly synthetic localhost demo.

Run demo_server.py first. Not a live LLM, real satellite or production validation.
The output deliberately excludes cookies, credentials and uploaded documents.
"""
import json
import os
from pathlib import Path

import requests

BASE = "http://127.0.0.1:8000"
session = requests.Session()
session.headers["Origin"] = "http://127.0.0.1:3000"
records = []


def call(method, path, payload=None, expected=200):
    result = session.request(method, BASE + path, json=payload, timeout=180)
    value = result.json()
    records.append({"method": method, "path": path, "http_status": result.status_code,
                    "response": value})
    assert result.status_code == expected, f"{path}: {result.status_code} {value}"
    print(f"{result.status_code} {path}", flush=True)
    return value


def main():
    password = os.environ["AGRISKY_DEMO_PASSWORD"]
    call("POST", "/api/v1/auth/login", {"username": "reviewer", "password": password})
    claim = call("POST", "/api/v1/tools/create_claim", {
        "policy_id": "POL-2026-001", "disaster_type": "flood",
        "loss_date": "2026-07-18", "crop_type": "rice", "plot_id": "PLOT-DEMO-SHANGHAI"})
    claim_id = claim["claim_id"]
    call("POST", "/api/v1/tools/run_rule_engine", {
        "claim_id": claim_id, "damage_ratio": 0.2, "crop_type": "rice"}, expected=400)
    call("POST", "/api/v1/tools/validate_materials", {"claim_id": claim_id})
    screening = call("POST", "/api/v1/tools/run_satellite_screening", {
        "claim_id": claim_id, "start_date": "2026-07-01", "end_date": "2026-07-20",
        "roi_geojson": {"type": "Polygon", "coordinates": [
            [[113.5, 34.5], [113.6, 34.5], [113.6, 34.6], [113.5, 34.6], [113.5, 34.5]]]}})
    assert screening["is_mock"] is True and screening["source"] == "synthetic"
    comp = call("POST", "/api/v1/tools/run_compliance_estimate", {"claim_id": claim_id})
    call("POST", "/api/v1/tools/run_rule_engine", {
        "claim_id": claim_id, "damage_ratio": comp["damage_ratio"], "crop_type": "rice"})
    report = call("POST", "/api/v1/tools/generate_report", {"claim_id": claim_id})
    assert report["status"] == "success"
    case = call("GET", f"/api/v1/cases/{claim_id}")
    assert case["state"] == "REPORT_DRAFTED"
    # This is a synthetic demo reviewer, never approval of a real claim.
    reviewed = call("POST", f"/api/v1/cases/{claim_id}/human_review", {
        "decision": "approved", "generation_id": report["generation_id"],
        "comment": "合成案例流程验收，不构成真实保险赔付决定"})
    assert reviewed["state"] == "ARCHIVED"
    audit = call("GET", f"/api/v1/audit/{claim_id}")
    assert len(audit) >= 7
    output = Path(os.environ.get("AGRISKY_REPLAY_OUTPUT", ".runtime/demo-replay.json"))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"mode": "synthetic_http_replay", "live_llm": False,
        "live_gee": False, "claim_id": claim_id, "records": records}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"PASS: {claim_id}; {len(records)} HTTP checks; {len(audit)} audit events")


if __name__ == "__main__":
    main()
