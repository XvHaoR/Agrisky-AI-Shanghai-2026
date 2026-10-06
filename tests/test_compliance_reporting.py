from __future__ import annotations

import re

from compliance_library import legal_citations, legal_documents, report_basis_rows


def test_report_citations_are_unique_and_bound_to_official_sources() -> None:
    documents = {item["id"]: item for item in legal_documents()}
    citations = legal_citations()
    ids = [item["id"] for item in citations]

    assert len(ids) == len(set(ids))
    assert {"L-01", "L-02", "L-03", "L-04", "L-05", "L-06"} <= set(documents)
    for item in citations:
        assert item["document_id"] in documents
        assert re.fullmatch(r"[0-9a-f]{64}", item["source_sha256"])
        assert item["source_url"].startswith(("http://", "https://"))
        assert item["summary"]
        assert item["applicability"]


def test_report_basis_separates_single_case_rules_from_institutional_context() -> None:
    rows = report_basis_rows()
    payout = next(item for item in rows if item["stage"] == "赔款测算")
    context = next(item for item in rows if item["stage"] == "制度与大灾风险背景")

    assert "L-01§15" in payout["basis_ids"]
    assert "L-03§55" in payout["basis_ids"]
    assert "L-06§15" not in payout["basis_ids"]
    assert "L-06§15" in context["basis_ids"]
    assert "不参与本案" in context["boundary"]
