"""Versioned legal and crop-assessment references used by reports and UI."""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent.parent
LEGAL_ROOT = BASE_DIR / "legal_corpus"
CROP_SCHEMES_PATH = BASE_DIR / "config" / "crop_claim_schemes.json"
REPORT_CITATIONS_PATH = LEGAL_ROOT / "report_citations.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@lru_cache(maxsize=1)
def load_crop_scheme_pack() -> dict[str, Any]:
    return json.loads(CROP_SCHEMES_PATH.read_text(encoding="utf-8"))


def crop_scheme(crop_type: str) -> dict[str, Any]:
    key = (crop_type or "corn").lower()
    if key == "maize":
        key = "corn"
    schemes = load_crop_scheme_pack().get("schemes") or {}
    return dict(schemes.get(key) or schemes.get("corn") or {})


@lru_cache(maxsize=1)
def legal_documents() -> list[dict[str, Any]]:
    manifest = json.loads((LEGAL_ROOT / "manifest.json").read_text(encoding="utf-8"))
    documents: list[dict[str, Any]] = []
    for item in manifest.get("documents") or []:
        entry = dict(item)
        normalized_path = LEGAL_ROOT / str(entry["normalized_path"])
        official_path = LEGAL_ROOT / str(entry["official_path"])
        entry["sha256"] = _sha256(official_path)
        entry["normalized_sha256"] = _sha256(normalized_path)
        entry["download_url"] = f"/api/v1/compliance/documents/{entry['id']}/download"
        documents.append(entry)
    return documents


@lru_cache(maxsize=1)
def legal_citations() -> list[dict[str, Any]]:
    """Return verified report citations joined to their immutable source files."""
    payload = json.loads(REPORT_CITATIONS_PATH.read_text(encoding="utf-8"))
    documents = {item["id"]: item for item in legal_documents()}
    citations: list[dict[str, Any]] = []
    for raw in payload.get("citations") or []:
        item = dict(raw)
        document = documents.get(str(item.get("document_id") or ""))
        if not document:
            raise ValueError(f"法规引用 {item.get('id')} 指向未登记文件")
        item.update(
            {
                "document_title": document["title"],
                "issuer": document["issuer"],
                "source_url": document["source_url"],
                "source_sha256": document["sha256"],
                "download_url": document["download_url"],
            }
        )
        citations.append(item)
    return citations


def citation_ids_for(*sections: str) -> list[str]:
    requested = {section for section in sections if section}
    return [
        str(item["id"])
        for item in legal_citations()
        if requested.intersection(item.get("report_sections") or [])
    ]


def citation_marker(*sections: str) -> str:
    return "".join(f"[{item}]" for item in citation_ids_for(*sections))


def report_basis_rows(
    contract: dict[str, Any] | None = None, crop_type: str = ""
) -> list[dict[str, Any]]:
    """Map report conclusions to deterministic legal, contract and technical bases."""
    scheme = crop_scheme(crop_type) if crop_type else {}
    technical = sorted(
        {ref for stage in scheme.get("stages") or [] for ref in stage.get("refs") or []}
    )
    contract_clause_ids = {
        str(clause.get("id") or ""): clause
        for clause in (contract or {}).get("clauses") or []
    }

    def contract_refs(*ids: str) -> list[str]:
        return [f"C-{item.lstrip('C')}" for item in ids if item in contract_clause_ids]

    return [
        {
            "stage": "合同登记与版本冻结",
            "conclusion": "保险责任、期间、标的、保额和赔偿办法均以登记时上传并冻结的合同版本为准。",
            "basis_ids": ["L-03§18", "L-05§8", "L-05§23", *contract_refs("C2", "C3", "C4")],
            "boundary": "不在报告生成阶段改写合同条件。",
        },
        {
            "stage": "遥感查勘与灾损评估",
            "conclusion": "SAR、光学和长势时序用于辅助查勘、定位受灾范围并评估损失程度。",
            "basis_ids": ["L-01§12", "L-02§20", "L-02§21", "L-02§23", "L-03§64", *technical],
            "boundary": "遥感结果不单独构成最终赔付决定。",
        },
        {
            "stage": "面积与损失核定",
            "conclusion": "受灾范围与在册边界求交，综合减产率按固定技术规则计算并进入人工复核。",
            "basis_ids": ["L-01§12", "L-02§26", "L-02§31", *contract_refs("C6")],
            "boundary": "证据冲突、置信度不足或高风险案件必须人工复核。",
        },
        {
            "stage": "赔款测算",
            "conclusion": "赔款金额只使用冻结合同约定的保额、起赔点、损失率、面积和生育期系数。",
            "basis_ids": ["L-01§15", "L-03§55", "L-05§7", "L-05§9", "L-05§23", *contract_refs("C7")],
            "boundary": "L-06 属保险机构大灾准备金管理依据，不改变单案合同限额。",
        },
        {
            "stage": "归档与人工审核",
            "conclusion": "报告、影像、合同版本、计算轨迹和审核回执形成可追溯档案。",
            "basis_ids": ["L-01§22", "L-02§31", "L-02§46", "L-03§23", *contract_refs("C8")],
            "boundary": "审核通过前报告仅为理赔辅助草稿。",
        },
        {
            "stage": "制度与大灾风险背景",
            "conclusion": "农业保险服务于农业防灾减灾和恢复生产；大灾准备金属于保险机构层面的风险分散安排。",
            "basis_ids": ["L-04§46", "L-04§47", "L-06§13", "L-06§15"],
            "boundary": "本组依据用于制度说明，不参与本案损失率或赔款金额计算。",
        },
    ]


def legal_document_path(document_id: str) -> tuple[dict[str, Any], Path] | None:
    item = next((doc for doc in legal_documents() if doc.get("id") == document_id), None)
    if not item:
        return None
    path = (LEGAL_ROOT / str(item["official_path"])).resolve()
    if not path.is_relative_to(LEGAL_ROOT.resolve()) or not path.is_file():
        return None
    return item, path


def reference_index(contract: dict[str, Any] | None = None, crop_type: str = "") -> list[dict[str, Any]]:
    refs = [
        {
            "id": item["id"],
            "category": "法律法规",
            "title": f"{item['document_title']} {item['article']} - {item['topic']}",
            "issuer": item["issuer"],
            "source": item["source_url"],
            "sha256": item["source_sha256"],
            "summary": item["summary"],
            "applicability": item["applicability"],
        }
        for item in legal_citations()
    ]
    if contract:
        refs.append({
            "id": "C-01",
            "category": "保险合同",
            "title": f"保险合同 {contract.get('contract_number') or '—'}",
            "issuer": str(contract.get("insurer") or "—"),
            "source": "登记时上传并冻结的保险合同原件",
            "sha256": str(contract.get("artifact_sha256") or contract.get("contract_sha256") or ""),
        })
        for clause in contract.get("clauses") or []:
            refs.append({
                "id": f"C-{str(clause.get('id') or '').lstrip('C')}",
                "category": "合同条款",
                "title": str(clause.get("title") or clause.get("id") or "合同条款"),
                "issuer": str(contract.get("contract_number") or "—"),
                "source": str(clause.get("text") or ""),
                "sha256": str(contract.get("contract_sha256") or ""),
            })
    pack = load_crop_scheme_pack()
    scheme = crop_scheme(crop_type) if crop_type else {}
    referenced = {ref for stage in scheme.get("stages") or [] for ref in stage.get("refs") or []}
    for item in pack.get("technical_references") or []:
        if referenced and item.get("id") not in referenced:
            continue
        refs.append({
            "id": item["id"],
            "category": "技术依据",
            "title": item["title"],
            "issuer": item["publisher"],
            "source": item["url"],
            "sha256": "",
        })
    return refs


def public_library_payload() -> dict[str, Any]:
    pack = load_crop_scheme_pack()
    citation_pack = json.loads(REPORT_CITATIONS_PATH.read_text(encoding="utf-8"))
    return {
        "schema_version": "agrisky-compliance-library-v1.0",
        "legal_documents": legal_documents(),
        "report_citations": legal_citations(),
        "report_basis": report_basis_rows(),
        "crop_schemes": pack.get("schemes") or {},
        "technical_references": pack.get("technical_references") or [],
        "disclosure": pack.get("disclosure"),
        "citation_disclosure": citation_pack.get("disclosure"),
    }
