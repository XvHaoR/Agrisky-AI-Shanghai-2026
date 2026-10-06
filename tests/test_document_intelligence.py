"""Document intelligence extraction, grounding, and deterministic checks."""

from __future__ import annotations

from api_gateway.document_intelligence import (
    EvidenceLine,
    compare_material_fields,
    detect_media_type,
    deterministic_fields,
    merge_fields,
    redact_sensitive_text,
)


def test_document_type_detection_checks_magic_and_extension() -> None:
    assert detect_media_type("claim.pdf", b"%PDF-1.7\ncontent") == "application/pdf"
    try:
        detect_media_type("claim.jpg", b"%PDF-1.7\ncontent")
    except ValueError as exc:
        assert "扩展名" in str(exc)
    else:
        raise AssertionError("mismatched extension must be rejected")


def test_deterministic_fields_keep_evidence_references() -> None:
    lines = [
        EvidenceLine("p1-l1", 1, "保单号：POL-2026-001"),
        EvidenceLine("p1-l2", 1, "作物：水稻"),
        EvidenceLine("p1-l3", 1, "出险日期：2026年07月28日"),
        EvidenceLine("p1-l4", 1, "灾害类型：洪涝"),
        EvidenceLine("p1-l5", 1, "申报受灾面积：120.5 亩"),
    ]
    fields = {item["field_name"]: item for item in deterministic_fields(lines)}
    assert fields["policy_id"]["normalized_value"] == "POL-2026-001"
    assert fields["crop_type"]["normalized_value"] == "rice"
    assert fields["loss_date"]["normalized_value"] == "2026-07-28"
    assert fields["disaster_type"]["normalized_value"] == "flood"
    assert fields["reported_damage_area_mu"]["source_ref"] == "p1-l5"


def test_merge_prefers_higher_confidence_and_compare_detects_conflict() -> None:
    regex = [{
        "field_name": "policy_id",
        "normalized_value": "POL-EXPECTED",
        "raw_text": "保单号 POL-EXPECTED",
        "confidence": 0.98,
        "page_number": 1,
        "bbox": None,
        "source_ref": "p1-l1",
        "extractor": "regex",
    }]
    llm = [{**regex[0], "normalized_value": "POL-WRONG", "confidence": 0.6, "extractor": "llm_json"}]
    merged = merge_fields(regex, llm)
    assert merged[0]["normalized_value"] == "POL-EXPECTED"

    findings = compare_material_fields(
        {
            "policy_id": "POL-AUTHORITY",
            "crop_type": "rice",
            "loss_date": "2026-07-28",
            "disaster_type": "flood",
            "plot_id": "PLOT-1",
        },
        {"area_mu": 100.0},
        merged,
    )
    assert findings[0]["code"] == "POLICY_ID_MISMATCH"
    assert findings[0]["source_ref"] == "p1-l1"


def test_sensitive_text_is_redacted_before_llm_context() -> None:
    text = "联系人 13800138000 身份证 11010519491231002X 账户 6222020202020202020"
    redacted = redact_sensitive_text(text)
    assert "13800138000" not in redacted
    assert "11010519491231002X" not in redacted
    assert "6222020202020202020" not in redacted
