"""
Agrisky AI — Pydantic 数据契约（防幻觉"防弹玻璃"）

所有 API 的输入输出都通过这里的 Schema 强制校验。
大模型传什么参数、引擎返回什么数据，都必须在这些框里。
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# ═══════════════════════════════════════════════════════════
# 枚举
# ═══════════════════════════════════════════════════════════

class CaseState(str, Enum):
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


class NextAction(str, Enum):
    ASK_USER = "ASK_USER"
    CALL_TOOL = "CALL_TOOL"
    WAIT_REVIEW = "WAIT_REVIEW"
    GENERATE_REPORT = "GENERATE_REPORT"
    STOP = "STOP"


class DisasterType(str, Enum):
    FLOOD = "flood"
    DROUGHT = "drought"
    HAIL = "hail"
    TYPHOON = "typhoon"
    PEST = "pest"
    FROST = "frost"
    OTHER = "other"


class RiskLevel(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ReviewStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


# ═══════════════════════════════════════════════════════════
# 1. 建案
# ═══════════════════════════════════════════════════════════

class CreateClaimRequest(BaseModel):
    """建案请求 —— 5 个关键字段缺一不可"""
    policy_id: str = Field(..., description="承保保单唯一 ID")
    disaster_type: DisasterType = Field(..., description="灾害类型")
    loss_date: date = Field(..., description="灾害发生日期")
    crop_type: str = Field(..., description="作物类型，如 rice, wheat, corn")
    plot_id: Optional[str] = Field(None, description="地块编号")
    insured_geom: Optional[dict] = Field(None, description="承保地块 GeoJSON")


class CreateClaimResponse(BaseModel):
    claim_id: str
    state: CaseState = CaseState.MATERIAL_CHECK
    created_at: datetime


# ═══════════════════════════════════════════════════════════
# 2. 材料校验
# ═══════════════════════════════════════════════════════════

class ValidateMaterialsRequest(BaseModel):
    claim_id: str = Field(..., description="案件编号")
    insured_geom_path: Optional[str] = Field(None, description="承保边界文件路径")


    photo_paths: list[str] = Field(default_factory=list, description="现场照片路径列表")


class FileError(BaseModel):
    file_path: str
    error_type: str  # MISSING / UNREADABLE / WRONG_FORMAT / DATE_OUT_OF_RANGE
    detail: str


class MaterialCheckResult(BaseModel):
    passed: bool
    missing_fields: list[str] = Field(default_factory=list)
    file_errors: list[FileError] = Field(default_factory=list)
    quality_flags: dict[str, str] = Field(default_factory=dict)


# ═══════════════════════════════════════════════════════════
# 3. 卫星初筛
# ═══════════════════════════════════════════════════════════

class SatelliteScreeningRequest(BaseModel):
    claim_id: str = Field(..., description="案件编号")
    roi_geojson: dict = Field(..., description="兴趣区域 GeoJSON (EPSG:4326)")
    start_date: str = Field(..., description="起始日期 YYYY-MM-DD")
    end_date: str = Field(..., description="结束日期 YYYY-MM-DD")


class SatelliteScreeningResult(BaseModel):
    status: str
    suspected_damage_area_mu: float = 0.0
    damage_ratio: float = 0.0
    confidence: str = "low"
    reference_assets: list[str] = Field(default_factory=list)
    image_count: int = 0
    thumbnail_url: str | None = None
    s2_thumbnail_url: str | None = None
    error_message: str | None = None
    source: str | None = None
    source_label: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    is_mock: bool = False
    boundary_sha256: str | None = None
    boundary_hash_scheme: str | None = None
    thumbnail_integrity: dict[str, dict[str, Any]] = Field(default_factory=dict)


# ═══════════════════════════════════════════════════════════
# 4. 合规面积核验
# ═══════════════════════════════════════════════════════════

class ComplianceCalcRequest(BaseModel):
    claim_id: str = Field(..., description="案件编号")
    damage_geojson: dict = Field(..., description="受灾多边形 GeoJSON")
    insured_geom: dict = Field(..., description="承保红线 GeoJSON（来自保单数据库）")


class ComplianceCalcResult(BaseModel):
    status: str
    valid_damage_area_mu: float = 0.0       # 合规受灾面积（亩）
    damage_ratio: float = 0.0                # 合规受灾比例
    excluded_area_mu: float = 0.0            # 被剔除的越界面积（亩）
    insured_area_mu: float = 0.0             # 承保面积（亩）
    clip_log: list[str] = Field(default_factory=list)  # 剔除日志
    boundary_sha256: str | None = None
    boundary_hash_scheme: str | None = None
    contract_id: str | None = None
    contract_version: str | None = None
    contract_checks: list[dict[str, Any]] = Field(default_factory=list)


# ═══════════════════════════════════════════════════════════
# 6. 规则引擎
# ═══════════════════════════════════════════════════════════

class RuleEngineRequest(BaseModel):
    claim_id: str
    damage_ratio: float | None = Field(None, ge=0.0, le=1.0, description="兼容字段，服务端忽略")
    crop_type: str | None = Field(None, description="兼容字段，服务端读取案件作物")
    threshold_set: str | None = Field(None, description="兼容字段，服务端使用当前规则版本")
    estimated_payout_yuan: float | None = Field(None, description="兼容字段，服务端读取赔付测算")


class RuleEngineResult(BaseModel):
    risk_level: RiskLevel = RiskLevel.LOW
    review_required: bool = False
    rule_trace: list[str] = Field(default_factory=list)
    rule_version: str = "default_v1"


# ═══════════════════════════════════════════════════════════
# 7. 报告生成
# ═══════════════════════════════════════════════════════════

class ReportGenerateRequest(BaseModel):
    claim_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        description="案件编号；文件导出只接受安全的服务端案件号",
    )
    template_version: str | None = Field(
        None,
        description="兼容字段；报告模板版本由服务端注册表决定",
        deprecated=True,
    )
    data: dict = Field(
        default_factory=dict,
        description="兼容字段；服务端忽略，报告只读取已持久化案件快照",
        deprecated=True,
    )


class ReportGenerateResult(BaseModel):
    status: Literal["success"] = "success"
    claim_id: Optional[str] = None
    report_docx_url: Optional[str] = None
    growth_report_docx_url: Optional[str] = None
    excel_report_url: Optional[str] = None
    bundle_zip_url: Optional[str] = None
    report_pdf_url: Optional[str] = None
    template_version: str = "v1.1"
    generation_id: Optional[str] = None
    generated_at: Optional[str] = None
    snapshot_sha256: Optional[str] = None
    bundle_sha256: Optional[str] = None
    growth_status: Literal["included", "summary_rebuilt", "not_available"] = "not_available"
    historical_ndvi_status: Literal[
        "included", "included_no_usable_data", "not_available"
    ] = "not_available"
    historical_ndvi_years: list[int] = Field(default_factory=list)
    parcel_growth_status: Literal["included", "not_available"] = "not_available"
    legal_basis: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    sections: list[str] = Field(default_factory=list)


# ═══════════════════════════════════════════════════════════
# 8. 作物长势 NDVI 分析
# ═══════════════════════════════════════════════════════════

class GrowthLevelSummary(BaseModel):
    value: int
    label: str
    count: int
    ratio: float
    area_mu: float
    color: str


class GrowthAnalysisResult(BaseModel):
    status: str
    schema_version: str = "agrisky.growth-analysis/v1"
    algorithm_version: str = "ndvi-five-level-v1"
    task_id: str
    method: str = "jenks"
    n_classes: int = 5
    total_area_mu: float
    valid_pixel_count: int
    area_estimation_method: str = "各等级有效像元占比 × 地块总面积（对无效像元按有效像元分布作比例外推）"
    class_breaks: list[float] = Field(default_factory=list)
    raster: dict[str, Any] = Field(default_factory=dict)
    summary: list[GrowthLevelSummary] = Field(default_factory=list)
    outputs: dict[str, Optional[str]] = Field(default_factory=dict)
    message: str = ""


# ═══════════════════════════════════════════════════════════
# 8. NDVI 长势 / 损害分析
# ═══════════════════════════════════════════════════════════

class NDVIAnalysisRequest(BaseModel):
    claim_id: str = Field(..., description="案件编号")
    roi_geojson: dict = Field(..., description="兴趣区域 GeoJSON")
    pre_start_date: str = Field(..., description="灾前起始 YYYY-MM-DD")
    pre_end_date: str = Field(..., description="灾前结束 YYYY-MM-DD")
    post_start_date: str = Field(..., description="灾后起始 YYYY-MM-DD")
    post_end_date: str = Field(..., description="灾后结束 YYYY-MM-DD")
    crop_label: str = Field("玉米", description="作物类型")


class NDVIAnalysisResult(BaseModel):
    status: str
    pre_mean_ndvi: float = 0.0
    post_mean_ndvi: float = 0.0
    ndvi_decline_pct: float = 0.0             # NDVI 下降百分比
    damaged_area_mu: float = 0.0               # 受损面积（NDVI下降超15%的区域）
    damaged_ratio: float = 0.0                 # 受损比例
    health_level: str = "unknown"              # 整体长势：差/一般/中/良/优
    image_count: int = 0
    thumbnail_url: str | None = None
    result_json_url: str | None = None


# ═══════════════════════════════════════════════════════════
# 10. 多源遥感灾损评估（减产率）—— 替代单期 NDVI
# ═══════════════════════════════════════════════════════════

class LossAssessmentRequest(BaseModel):
    claim_id: str = Field(..., description="案件编号")
    roi_geojson: dict = Field(..., description="评估区域 GeoJSON (EPSG:4326)")
    pre_start: str = Field("", description="灾前起始 YYYY-MM-DD（留空走合成）")
    pre_end: str = Field("", description="灾前结束 YYYY-MM-DD")
    post_start: str = Field("", description="灾后起始 YYYY-MM-DD")
    post_end: str = Field("", description="灾后结束 YYYY-MM-DD")
    products: Optional[list[str]] = Field(None, description="可选；覆盖该灾种默认产品集")


class LossProductContribution(BaseModel):
    id: str
    name_cn: str
    sensor: str
    native_res_m: int
    scale: str                       # field / regional
    decline_score: float             # 该产品指示的相对损失 [0,1]
    weight: float
    contribution: float
    raw_pre: Optional[float] = None
    raw_post: Optional[float] = None
    data_source: str                 # gee / synthetic
    caveat: str = ""


class LossAssessmentResult(BaseModel):
    status: str
    disaster_type: str = "other"
    yield_loss_ratio: float = 0.0    # 融合减产率 [0,1]
    severity: dict[str, Any] = Field(default_factory=dict)
    confidence: str = "low"
    confidence_score: float = 0.0
    dominant_drivers: list[str] = Field(default_factory=list)
    product_breakdown: list[LossProductContribution] = Field(default_factory=list)
    data_sources: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    method: str = ""
    window: dict[str, Any] = Field(default_factory=dict)
    boundary_sha256: str | None = None
    boundary_hash_scheme: str | None = None


# ═══════════════════════════════════════════════════════════
# 10a. 保单-地块库（承保边界在册，凭保单号自动取用）
# ═══════════════════════════════════════════════════════════

class PolicyRegisterRequest(BaseModel):
    policy_id: str = Field(..., description="承保保单唯一 ID")
    policy_version_id: str | None = Field(
        None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
        description="保单版本标签；留空时首次登记使用 <policy_id>:v1。新版本必须使用新的 policy_id，禁止覆盖已引用版本。",
    )
    holder_name: str = Field("", description="投保人/合作社名称")
    crop_type: str = Field("", description="承保作物")
    address: str = Field("", description="地块地址/区域")
    boundary_geojson: dict = Field(..., description="承保地块边界 GeoJSON（Polygon/Feature/FeatureCollection）")


class HistoricalNDVIByClaimRequest(BaseModel):
    claim_id: str = Field(..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    start_year: int = Field(2022, ge=2017, le=2100)
    end_year: int = Field(..., ge=2017, le=2100)
    as_of_date: date | None = Field(None, description="统计截止日；留空使用服务器当日")
    max_cloud_pct: float = Field(30.0, ge=0.0, le=100.0)
    scale_m: int = Field(10, ge=10, le=100)


class ParcelMappingConfirmation(BaseModel):
    proposed_parcel_id: str = Field(..., min_length=1, max_length=128)
    policy_id: str = Field(..., min_length=1, max_length=128)
    policy_version_id: str = Field(..., min_length=1, max_length=128)
    parcel_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
        description="人工确认后的正式地块编号",
    )
    resolution_note: str = Field("", max_length=2000)


class ParcelPreflightConfirmRequest(BaseModel):
    manifest_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    mappings: list[ParcelMappingConfirmation] = Field(..., min_length=1, max_length=256)
    confirmation_statement: Literal["I_CONFIRM_REVIEWED_PARCEL_MAPPINGS"]
    comment: str = Field("", max_length=2000)
    idempotency_key: str | None = Field(None, min_length=8, max_length=128)


class ParcelGrowthByClaimRequest(BaseModel):
    claim_id: str = Field(..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


# ═══════════════════════════════════════════════════════════
# 10b. 边界框版合规核验（对话智能体用，免文件上传）
# ═══════════════════════════════════════════════════════════

class ComplianceEstimateRequest(BaseModel):
    claim_id: str = Field(..., description="案件编号")
    roi_geojson: dict | None = Field(None, description="兼容字段，服务端使用保单在册边界")


# ═══════════════════════════════════════════════════════════
# 12. 对话式智能体
# ═══════════════════════════════════════════════════════════

class AgentChatRequest(BaseModel):
    messages: list[dict] = Field(default_factory=list, description="对话历史（user/assistant/tool，不含 system）")


# ═══════════════════════════════════════════════════════════
# 13. 投保人登录门户
# ═══════════════════════════════════════════════════════════

class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=128, description="投保人账号")
    password: str = Field(..., min_length=1, max_length=1024, description="密码")


class HumanReviewRequest(BaseModel):
    decision: Literal["approved", "rejected"] = Field(..., description="人工审核结论")
    generation_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        description="审核员实际查看的报告生成号",
    )
    comment: str = Field("", max_length=2000, description="审核意见；驳回时必填")
    return_to: Literal[
        "MATERIAL_CHECK",
        "PREPROCESS_READY",
        "SCREENING_DONE",
        "NDVI_DONE",
        "COMPLIANCE_DONE",
        "RULE_DONE",
    ] | None = Field(None, description="驳回后的回退状态；默认 RULE_DONE")
    idempotency_key: str | None = Field(
        None,
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
        description="可选幂等键",
    )


# ═══════════════════════════════════════════════════════════
# 11. 赔付测算
# ═══════════════════════════════════════════════════════════

class PayoutEstimateRequest(BaseModel):
    claim_id: str = Field(..., description="案件编号")


class PayoutResult(BaseModel):
    status: str
    crop_type: str = ""
    sum_insured_per_mu: float = 0.0          # 保额（元/亩）
    insured_area_mu: float = 0.0             # 承保面积（亩）
    total_sum_insured_yuan: float = 0.0      # 总保额（元）
    yield_loss_ratio: float = 0.0            # 减产率
    deductible_threshold: float = 0.0        # 起赔点
    payout_factor: float = 0.0               # 赔付比例
    tier_label: str = ""                     # 赔付档说明
    payout_amount_yuan: float = 0.0          # 预估赔款（元）
    rule_version: str = "payout_v1"
    trace: list[str] = Field(default_factory=list)
    affected_area_mu: float = 0.0
    claimable_loss_ratio: float = 0.0
    growth_stage: str = ""
    growth_stage_factor: float = 0.0
    calculation_formula: str = ""
    contract_id: str | None = None
    contract_number: str | None = None
    contract_version: str | None = None
    contract_sha256: str | None = None
    contract_clause_refs: list[str] = Field(default_factory=list)


# ═══════════════════════════════════════════════════════════
# Agent 统一输出壳
# ═══════════════════════════════════════════════════════════

class AgentOutput(BaseModel):
    """Agent 每次响应都必须套在这个结构里"""
    case_id: str = ""
    state: CaseState = CaseState.INIT
    missing_fields: list[str] = Field(default_factory=list)
    next_action: NextAction = NextAction.ASK_USER
    need_human_review: bool = False
    tool_name: Optional[str] = None
    tool_args: Optional[dict[str, Any]] = None
    result_summary: str = ""
    result_source: list[str] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)
    notes: str = ""


# ═══════════════════════════════════════════════════════════
# 错误返回
# ═══════════════════════════════════════════════════════════

class ToolError(BaseModel):
    error_code: str
    error_message: str
    retryable: bool = False
    recommended_human_action: str = ""


# ═══════════════════════════════════════════════════════════
# 数据模型（最小集合）
# ═══════════════════════════════════════════════════════════

class ClaimCase(BaseModel):
    """案件主表"""
    claim_id: str
    policy_id: str
    state: CaseState = CaseState.INIT
    disaster_type: DisasterType
    loss_date: date
    crop_type: str
    plot_id: Optional[str] = None
    reported_at: datetime = Field(default_factory=datetime.now)


class AuditLog(BaseModel):
    """审计日志"""
    log_id: str
    claim_id: str
    action: str
    tool_name: Optional[str] = None
    tool_args: Optional[dict] = None
    tool_result_summary: Optional[str] = None
    prompt_version: Optional[str] = None
    actor: str  # "agent" | "human"
    created_at: datetime = Field(default_factory=datetime.now)
