"""Versioned simulated agricultural insurance contract and payout rules.

The contract is deliberately labelled as a simulation.  It provides a stable,
auditable rule source for the competition workflow and must not be represented
as a real insurance policy or an underwriting commitment.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Cm, Pt

from compliance_library import crop_scheme
from document_intelligence import detect_media_type, extract_evidence, safe_filename, sha256_bytes


CONTRACT_TEMPLATE_VERSION = "sim-agri-multicrop-v2.0"
SIMULATION_DISCLOSURE = (
    "本合同为产品规则验证用模拟合同，不构成真实承保凭证、保险要约或赔付承诺。"
)


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def contract_sha256(contract: dict[str, Any]) -> str:
    payload = {key: value for key, value in contract.items() if key != "contract_sha256"}
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _policy_year() -> int:
    return datetime.now().year


def _crop_label(crop_type: str) -> str:
    labels = {
        "corn": "玉米",
        "maize": "玉米",
        "rice": "水稻",
        "wheat": "小麦",
    }
    return labels.get((crop_type or "").lower(), crop_type or "玉米")


def build_simulated_contract(
    policy: dict[str, Any], *, insurance_year: int | None = None
) -> dict[str, Any]:
    """Build deterministic terms for one policy version.

    Exact commercial rates are product- and region-specific in practice.  This
    rule pack is therefore frozen per policy version and cited in every payout
    result instead of pretending to be a national statutory formula.
    """
    policy_id = str(policy["policy_id"])
    policy_version_id = str(policy.get("policy_version_id") or f"{policy_id}:v1")
    crop_type = str(policy.get("crop_type") or "corn")
    crop_key = "corn" if crop_type.lower() == "maize" else crop_type.lower()
    scheme = crop_scheme(crop_key)
    holder_name = str(policy.get("holder_name") or "示范投保人")
    area_mu = round(float(policy.get("area_mu") or 0.0), 2)
    # The claim service binds this to the loss-event year before freezing the
    # contract.  A policy-only preview still uses the current underwriting year.
    year = int(insurance_year or policy.get("insurance_year") or _policy_year())
    seed = hashlib.sha256(f"{policy_id}|{policy_version_id}|{CONTRACT_TEMPLATE_VERSION}".encode("utf-8")).hexdigest()
    contract_id = f"HJIC-SIM-{seed[:12].upper()}"

    payout_terms = {
        "sum_insured_per_mu": float(scheme.get("sum_insured_per_mu") or 800.0),
        "deductible_loss_ratio": float(scheme.get("deductible_loss_ratio") or 0.10),
        "total_loss_ratio": float(scheme.get("total_loss_ratio") or 0.80),
        "growth_stage_factors": [
            {
                "name": stage.get("name"),
                "start": stage.get("start"),
                "end": stage.get("end"),
                "factor": stage.get("factor"),
            }
            for stage in scheme.get("stages") or []
        ],
        "formula": "核定受灾面积 × 每亩保险金额 × 综合减产率 × 生育期赔付系数",
        "cap": "累计赔款不超过总保险金额",
    }
    insurance_period = (
        {"start": f"{year - 1}-10-01", "end": f"{year}-06-30"}
        if crop_key == "wheat"
        else {"start": f"{year}-04-01", "end": f"{year}-11-30"}
    )
    clauses = [
        {
            "id": "C1",
            "title": "合同性质与当事人",
            "text": "甲方为华稷农业保险股份有限公司（模拟主体），乙方为投保人；被保险人为登记种植主体。Agrisky AI 仅提供遥感证据、规则计算和审计留痕能力，不作为保险人。",
        },
        {
            "id": "C2",
            "title": "保险标的与保险期间",
            "text": f"保险标的为乙方在保单登记边界内种植的{_crop_label(crop_type)}，登记面积 {area_mu:.2f} 亩。保险期间为 {insurance_period['start']} 至 {insurance_period['end']}，以保单版本和在册边界为准。",
        },
        {
            "id": "C3",
            "title": "保险责任",
            "text": "在保险期间内，暴雨、洪涝、内涝、干旱、冰雹、台风、霜冻和病虫害等直接造成保险标的减产的，按照本合同的面积核验、损失率和赔款计算规则处理。",
        },
        {
            "id": "C4",
            "title": "责任免除",
            "text": "不在登记边界内的面积、非保险期间发生的损失、故意行为、虚假材料、重复申领以及与本次灾害无因果关系的损失不纳入本次赔付测算。",
        },
        {
            "id": "C5",
            "title": "报案与证据授权",
            "text": "乙方应如实申报灾害日期和灾害类型，并授权甲方或其受托技术服务方在最小必要范围内处理承保边界、遥感影像、气象和查勘材料，用于核验、定损和审计。",
        },
        {
            "id": "C6",
            "title": "面积与损失认定",
            "text": "受灾面积以在册承保边界与经核验的遥感受灾范围相交结果为准；减产率由多源遥感、时序长势、气象与查勘证据综合评估。证据冲突、置信度不足或高风险情形进入人工复核。",
        },
        {
            "id": "C7",
            "title": "赔款计算",
            "text": "赔款以核定受灾面积、综合减产率、作物生育期赔付系数和每亩保险金额计算；减产率低于起赔点的不予赔付，累计赔款不超过总保险金额。",
        },
        {
            "id": "C8",
            "title": "人工审核与争议处理",
            "text": "系统输出为理赔辅助结论。赔款支付、拒赔或归档前须由具备权限的人工审核人员确认；审核意见、合同版本、证据哈希和规则轨迹均写入审计链路。",
        },
    ]
    contract = {
        "contract_id": contract_id,
        "contract_number": f"SIM-AGRI-{year}-{seed[:8].upper()}",
        "contract_version": CONTRACT_TEMPLATE_VERSION,
        "policy_id": policy_id,
        "policy_version_id": policy_version_id,
        "simulation": True,
        "simulation_disclosure": SIMULATION_DISCLOSURE,
        "insurer": "华稷农业保险股份有限公司（模拟主体）",
        "policyholder": holder_name,
        "insured": holder_name,
        "beneficiary": holder_name,
        "crop_type": crop_key,
        "crop_name": _crop_label(crop_type),
        "address": str(policy.get("address") or "保单登记地块"),
        "insured_area_mu": area_mu,
        "insurance_period": insurance_period,
        "coverage": ["flood", "drought", "hail", "typhoon", "pest", "frost"],
        "payout_terms": payout_terms,
        "crop_assessment_scheme": {
            "name": scheme.get("name"),
            "indices": scheme.get("indices") or [],
            "assessment_focus": scheme.get("assessment_focus"),
            "technical_refs": sorted({ref for stage in scheme.get("stages") or [] for ref in stage.get("refs") or []}),
        },
        "clauses": clauses,
        "created_at": datetime.now().astimezone().isoformat(),
    }
    contract["contract_sha256"] = contract_sha256(contract)
    return contract


def parse_uploaded_contract(
    policy: dict[str, Any], *, filename: str, data: bytes
) -> dict[str, Any]:
    """Extract and freeze demo contract terms without relying on an external LLM."""
    media_type = detect_media_type(filename, data)
    if media_type not in {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }:
        raise ValueError("保险合同仅支持 PDF 或 DOCX 原件")
    suffix = ".pdf" if media_type == "application/pdf" else ".docx"
    with tempfile.TemporaryDirectory(prefix="agrisky-contract-") as directory:
        path = Path(directory) / f"contract{suffix}"
        path.write_bytes(data)
        lines, extraction_method = extract_evidence(path, media_type)
    text = "\n".join(line.text for line in lines)
    compact = re.sub(r"\s+", "", text)
    if len(compact) < 120:
        raise ValueError("保险合同正文过短或无法识别，请上传文字清晰的 PDF/DOCX")

    expected_crop = _crop_label(str(policy.get("crop_type") or "corn"))
    if expected_crop not in compact:
        raise ValueError(f"合同正文未识别到登记作物“{expected_crop}”")

    number_match = re.search(r"合同编号[：:|]*([A-Za-z0-9._-]{6,80})", compact)
    period_match = re.search(
        r"保险期间[^0-9]{0,20}(20\d{2})[-年/.](\d{1,2})[-月/.](\d{1,2}).{0,12}?"
        r"(20\d{2})[-年/.](\d{1,2})[-月/.](\d{1,2})",
        compact,
    )
    amount_match = re.search(r"每亩保险金额[：:|]*([0-9]+(?:\.[0-9]+)?)元", compact)
    deductible_match = re.search(r"起赔点[：:|]*([0-9]+(?:\.[0-9]+)?)%", compact)
    total_loss_match = re.search(r"全损阈值[：:|]*([0-9]+(?:\.[0-9]+)?)%", compact)
    missing = [
        label
        for label, match in (
            ("合同编号", number_match),
            ("保险期间", period_match),
            ("每亩保险金额", amount_match),
            ("起赔点", deductible_match),
            ("全损阈值", total_loss_match),
        )
        if match is None
    ]
    if missing:
        raise ValueError(f"合同缺少或无法识别关键字段：{'、'.join(missing)}")

    assert number_match and period_match and amount_match and deductible_match and total_loss_match
    start = f"{int(period_match.group(1)):04d}-{int(period_match.group(2)):02d}-{int(period_match.group(3)):02d}"
    end = f"{int(period_match.group(4)):04d}-{int(period_match.group(5)):02d}-{int(period_match.group(6)):02d}"
    if date.fromisoformat(start) > date.fromisoformat(end):
        raise ValueError("合同保险期间起止日期无效")

    base = build_simulated_contract(policy, insurance_year=int(end[:4]))
    base["contract_number"] = number_match.group(1)
    base["insurance_period"] = {"start": start, "end": end}
    base["payout_terms"]["sum_insured_per_mu"] = float(amount_match.group(1))
    base["payout_terms"]["deductible_loss_ratio"] = float(deductible_match.group(1)) / 100
    base["payout_terms"]["total_loss_ratio"] = float(total_loss_match.group(1)) / 100
    base["source_type"] = "uploaded_contract"
    base["source_filename"] = safe_filename(filename)
    base["source_media_type"] = media_type
    base["source_sha256"] = sha256_bytes(data)
    base["extraction_method"] = extraction_method
    base["extracted_text_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    base["human_confirmed"] = True
    base["contract_sha256"] = contract_sha256(base)
    return base


def evaluate_contract_eligibility(
    contract: dict[str, Any], *, crop_type: str, disaster_type: str, loss_date: str
) -> dict[str, Any]:
    period = contract.get("insurance_period") or {}
    checks: list[dict[str, Any]] = []
    expected_crop = (contract.get("crop_type") or "").lower()
    actual_crop = (crop_type or "").lower()
    checks.append({
        "clause": "C2",
        "name": "保险标的一致性",
        "passed": expected_crop == actual_crop,
        "detail": f"合同作物={contract.get('crop_name') or expected_crop}，案件作物={crop_type}",
    })
    disaster = (disaster_type or "").lower()
    checks.append({
        "clause": "C3",
        "name": "保险责任匹配",
        "passed": disaster in set(contract.get("coverage") or []),
        "detail": f"灾害类型={disaster_type}",
    })
    try:
        event_date = date.fromisoformat(str(loss_date))
        start = date.fromisoformat(str(period.get("start")))
        end = date.fromisoformat(str(period.get("end")))
        in_period = start <= event_date <= end
    except (TypeError, ValueError):
        in_period = False
    checks.append({
        "clause": "C2",
        "name": "保险期间校验",
        "passed": in_period,
        "detail": f"出险日期={loss_date}，保险期间={period.get('start')} 至 {period.get('end')}",
    })
    return {"eligible": all(item["passed"] for item in checks), "checks": checks}


def _stage_factor(contract: dict[str, Any], loss_date: str) -> tuple[str, float]:
    try:
        month_day = date.fromisoformat(str(loss_date)).strftime("%m-%d")
    except ValueError:
        return "未识别生育期", 0.0
    for stage in (contract.get("payout_terms") or {}).get("growth_stage_factors") or []:
        stage_start = str(stage.get("start"))
        stage_end = str(stage.get("end"))
        in_stage = (
            stage_start <= month_day <= stage_end
            if stage_start <= stage_end
            else month_day >= stage_start or month_day <= stage_end
        )
        if in_stage:
            return str(stage.get("name")), float(stage.get("factor") or 0.0)
    return "未覆盖生育期", 0.0


def estimate_contract_payout(
    contract: dict[str, Any], *, loss_date: str, loss_ratio: float, affected_area_mu: float
) -> dict[str, Any]:
    terms = contract.get("payout_terms") or {}
    insured_area = round(float(contract.get("insured_area_mu") or 0.0), 2)
    affected_area = min(max(float(affected_area_mu or 0.0), 0.0), insured_area)
    loss = min(max(float(loss_ratio or 0.0), 0.0), 1.0)
    sum_insured = float(terms.get("sum_insured_per_mu") or 0.0)
    deductible = float(terms.get("deductible_loss_ratio") or 0.0)
    total_loss = float(terms.get("total_loss_ratio") or 1.0)
    stage_name, stage_factor = _stage_factor(contract, loss_date)
    eligible = loss >= deductible and stage_factor > 0 and affected_area > 0
    applied_loss = 1.0 if loss >= total_loss else loss
    gross = affected_area * sum_insured * applied_loss * stage_factor if eligible else 0.0
    total_sum_insured = insured_area * sum_insured
    amount = round(min(gross, total_sum_insured), 2)
    tier = "未达起赔点"
    if eligible and loss >= total_loss:
        tier = "全损赔付档"
    elif eligible:
        tier = "比例赔付档"
    trace = [
        f"合同 {contract.get('contract_number')} / {contract.get('contract_version')}，哈希 {contract.get('contract_sha256')}",
        f"C6 核定受灾面积：{affected_area:.2f} 亩（不超过承保面积 {insured_area:.2f} 亩）",
        f"C7 减产率：{loss:.2%}，起赔点：{deductible:.2%}，生育期：{stage_name}，系数：{stage_factor:.2f}",
        f"C7 计算：{affected_area:.2f} × {sum_insured:.2f} × {applied_loss:.4f} × {stage_factor:.2f} = {amount:.2f} 元",
        f"C7 限额校验：{amount:.2f} 元 <= 总保险金额 {total_sum_insured:.2f} 元",
        "C8：该测算须经人工审核确认后方可进入归档。",
    ]
    return {
        "status": "success",
        "crop_type": contract.get("crop_type") or "",
        "sum_insured_per_mu": sum_insured,
        "insured_area_mu": insured_area,
        "affected_area_mu": round(affected_area, 2),
        "total_sum_insured_yuan": round(total_sum_insured, 2),
        "yield_loss_ratio": round(loss, 4),
        "claimable_loss_ratio": round(applied_loss if eligible else 0.0, 4),
        "deductible_threshold": deductible,
        "payout_factor": round((applied_loss * stage_factor) if eligible else 0.0, 4),
        "growth_stage": stage_name,
        "growth_stage_factor": stage_factor,
        "tier_label": tier,
        "payout_amount_yuan": amount,
        "calculation_formula": terms.get("formula"),
        "contract_id": contract.get("contract_id"),
        "contract_number": contract.get("contract_number"),
        "contract_version": contract.get("contract_version"),
        "contract_sha256": contract.get("contract_sha256"),
        "contract_clause_refs": ["C2", "C3", "C4", "C6", "C7", "C8"],
        "rule_version": contract.get("contract_version"),
        "trace": trace,
    }


def contract_summary(contract: dict[str, Any], artifact_url: str | None = None) -> dict[str, Any]:
    return {
        "contract_id": contract.get("contract_id"),
        "contract_number": contract.get("contract_number"),
        "contract_version": contract.get("contract_version"),
        "contract_sha256": contract.get("contract_sha256"),
        "simulation": bool(contract.get("simulation", True)),
        "simulation_disclosure": contract.get("simulation_disclosure"),
        "insurer": contract.get("insurer"),
        "policyholder": contract.get("policyholder"),
        "insured_area_mu": contract.get("insured_area_mu"),
        "insurance_period": contract.get("insurance_period"),
        "payout_terms": contract.get("payout_terms"),
        "source_type": contract.get("source_type") or "legacy_generated_contract",
        "source_filename": contract.get("source_filename"),
        "source_sha256": contract.get("source_sha256"),
        "human_confirmed": contract.get("human_confirmed", False),
        "crop_assessment_scheme": contract.get("crop_assessment_scheme"),
        "artifact_url": artifact_url,
    }


def render_contract_docx(contract: dict[str, Any], output_path: str | Path) -> Path:
    """Render a formal downloadable attachment from the frozen contract JSON."""
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    doc = Document()
    section = doc.sections[0]
    section.top_margin = Cm(1.8)
    section.bottom_margin = Cm(1.8)
    section.left_margin = Cm(2.2)
    section.right_margin = Cm(2.2)
    normal = doc.styles["Normal"]
    normal.font.name = "Arial"
    normal._element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), "宋体")
    normal.font.size = Pt(10.5)

    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = title.add_run(f"华稷农业保险股份有限公司{contract.get('crop_name')}种植保险合同（模拟）")
    run.bold = True
    run.font.size = Pt(16)
    run._element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), "黑体")
    disclosure = doc.add_paragraph(SIMULATION_DISCLOSURE)
    disclosure.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for run in disclosure.runs:
        run.font.size = Pt(9)
        run.font.color.rgb = None

    doc.add_paragraph(f"合同编号：{contract.get('contract_number')}")
    doc.add_paragraph(f"保单号：{contract.get('policy_id')}")
    doc.add_paragraph(f"合同版本：{contract.get('contract_version')}")
    doc.add_paragraph(f"合同哈希（SHA-256）：{contract.get('contract_sha256')}")
    table = doc.add_table(rows=0, cols=2, style="Table Grid")
    fields = [
        ("甲方（保险人）", contract.get("insurer")),
        ("乙方（投保人）", contract.get("policyholder")),
        ("被保险人", contract.get("insured")),
        ("保险标的", f"{contract.get('crop_name')}，{contract.get('insured_area_mu')} 亩"),
        ("承保地点", contract.get("address")),
        ("保险期间", "至".join((contract.get("insurance_period") or {}).values())),
    ]
    for label, value in fields:
        cells = table.add_row().cells
        cells[0].text = str(label)
        cells[1].text = str(value or "")
    doc.add_paragraph()
    for clause in contract.get("clauses") or []:
        heading = doc.add_paragraph()
        heading_run = heading.add_run(f"{clause.get('id')} {clause.get('title')}")
        heading_run.bold = True
        heading_run._element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), "黑体")
        doc.add_paragraph(str(clause.get("text") or ""))

    terms = contract.get("payout_terms") or {}
    doc.add_paragraph("赔款计算参数", style="Heading 2")
    doc.add_paragraph(
        f"每亩保险金额：{terms.get('sum_insured_per_mu')} 元；起赔点：{terms.get('deductible_loss_ratio', 0) * 100:.0f}%；"
        f"全损阈值：{terms.get('total_loss_ratio', 0) * 100:.0f}%。"
    )
    doc.add_paragraph(f"计算公式：{terms.get('formula')}；{terms.get('cap')}。")
    stages = doc.add_table(rows=1, cols=3, style="Table Grid")
    for cell, label in zip(stages.rows[0].cells, ("生育期", "期间", "赔付系数")):
        cell.text = label
    for stage in terms.get("growth_stage_factors") or []:
        cells = stages.add_row().cells
        cells[0].text = str(stage.get("name"))
        cells[1].text = f"{stage.get('start')} 至 {stage.get('end')}"
        cells[2].text = str(stage.get("factor"))
    doc.add_paragraph()
    doc.add_paragraph("签署与留痕", style="Heading 2")
    doc.add_paragraph("甲方（模拟主体）：________________    乙方：________________")
    doc.add_paragraph("本附件由系统依据固定合同版本生成；版本、哈希、在册边界和理赔报告中的引用应保持一致。")
    doc.save(output)
    return output
