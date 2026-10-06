"""
Agrisky AI — FastAPI 业务网关（L2 防弹玻璃层）

职责：
    1. 校验 Agent 的每一个请求（Pydantic 数据契约）
    2. 路由到正确的底层计算引擎
    3. 强制执行空间合规核验
    4. 运行规则引擎
    5. 记录全链路审计日志

所有面积、比例、等级必须从此层产出，Agent 只做 1:1 引用。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import math
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
import uuid
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from schemas import (
    AgentOutput,
    AuditLog,
    CaseState,
    ClaimCase,
    ComplianceCalcRequest,
    ComplianceCalcResult,
    CreateClaimRequest,
    CreateClaimResponse,
    GrowthAnalysisResult,
    HistoricalNDVIByClaimRequest,
    HumanReviewRequest,
    AgentChatRequest,
    ComplianceEstimateRequest,
    LoginRequest,
    LossAssessmentRequest,
    LossAssessmentResult,
    MaterialCheckResult,
    PayoutEstimateRequest,
    PayoutResult,
    ParcelGrowthByClaimRequest,
    ParcelPreflightConfirmRequest,
    PolicyRegisterRequest,
    NextAction,
    ReportGenerateRequest,
    ReportGenerateResult,
    RiskLevel,
    RuleEngineRequest,
    RuleEngineResult,
    SatelliteScreeningRequest,
    SatelliteScreeningResult,
    ToolError,
    ValidateMaterialsRequest,
)
from state_machine import (
    S,
    can_call_tool,
    can_transition,
    check_missing_fields,
    get_allowed_tools,
)
from policy_config import load_risk_rules
from contract_engine import (
    build_simulated_contract,
    contract_sha256,
    contract_summary,
    estimate_contract_payout,
    evaluate_contract_eligibility,
    parse_uploaded_contract,
    render_contract_docx,
)
from map_runtime import (
    MapRuntimeError,
    build_runtime_document,
    embedded_local_basemap,
    runtime_security_headers,
    vendor_asset_path,
)
from compliance_library import (
    legal_document_path,
    public_library_payload,
    reference_index,
    report_basis_rows,
)
from document_intelligence import (
    DOCUMENT_TYPES,
    compare_material_fields,
    deterministic_fields,
    detect_media_type,
    extract_evidence,
    llm_fields,
    merge_fields,
    normalize_fields_for_document_type,
    required_document_types,
    safe_filename,
    sha256_bytes,
)

# ── 日志 ──────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")
load_dotenv(Path(__file__).resolve().parent / ".env")


def _csv_env(name: str, default: list[str]) -> list[str]:
    value = os.getenv(name)
    if not value:
        return default
    return [item.strip() for item in value.split(",") if item.strip()]


def _path_env(name: str, default: str | Path) -> Path:
    path = Path(os.getenv(name, str(default)))
    if not path.is_absolute():
        path = BASE_DIR / path
    return path.resolve()


def _bounded_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return min(maximum, max(minimum, value))


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("agrisky.api")

# ── 应用实例 ──────────────────────────────────────────────
app = FastAPI(
    title="Agrisky AI — 核心业务网关",
    version="1.0.0",
    description="农业保险灾后查勘与定损辅助系统 API",
)

CORS_ORIGINS = _csv_env(
    "AGRISKY_CORS_ORIGINS",
    ["http://localhost:3000", "http://127.0.0.1:3000"],
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── 可选 API key 鉴权 ─────────────────────────────────────
AUTH_ENABLED = os.getenv("AGRISKY_AUTH_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
SESSION_COOKIE_NAME = "agrisky_session"
SESSION_MAX_AGE_SECONDS = _bounded_int_env(
    "AGRISKY_SESSION_MAX_AGE_SECONDS", 28800, 60, 31 * 24 * 60 * 60
)
COOKIE_SECURE = os.getenv("AGRISKY_COOKIE_SECURE", "false").lower() in {"1", "true", "yes", "on"}


def _parse_api_keys(raw: str | None) -> dict[str, str]:
    """Parse role=key entries into key->role lookup."""
    parsed: dict[str, str] = {}
    if not raw:
        return parsed
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" in item:
            role, key = item.split("=", 1)
        elif ":" in item:
            role, key = item.split(":", 1)
        else:
            logger.warning("Ignoring malformed AGRISKY_API_KEYS entry")
            continue
        role = role.strip().lower()
        key = key.strip()
        if role and key:
            parsed[key] = role
    return parsed


API_KEYS = _parse_api_keys(os.getenv("AGRISKY_API_KEYS"))
# Web 智能体通过 loopback HTTP 调用同一网关。默认生成进程内随机服务密钥；
# 多 worker 或外置 Agent 部署应显式配置同一个 AGRISKY_AGENT_API_KEY。
_configured_agent_key = os.getenv("AGRISKY_AGENT_API_KEY", "").strip()
INTERNAL_AGENT_API_KEY = _configured_agent_key or secrets.token_urlsafe(32)
API_KEYS[INTERNAL_AGENT_API_KEY] = "analyst"
os.environ["AGRISKY_AGENT_API_KEY"] = INTERNAL_AGENT_API_KEY
ROLE_PERMISSIONS: dict[str, set[str]] = {
    "viewer": {"read"},
    "analyst": {"read", "write"},
    "auditor": {"read", "review"},
    "admin": {"read", "write", "review", "admin"},
}


def _extract_api_key(request: Request) -> str:
    header_key = request.headers.get("x-agrisky-api-key") or request.headers.get("x-api-key")
    if header_key:
        return header_key.strip()
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def _required_permission(method: str, path: str) -> str | None:
    if method == "OPTIONS":
        return None
    if method == "GET" and path.startswith("/api/v1/map-runtime/vendor/"):
        return None
    if path in {
        "/health", "/openapi.json", "/api/v1/auth/login", "/api/v1/auth/logout", "/api/v1/me"
    } or path.startswith(("/docs", "/redoc")):
        return None
    if path.startswith("/api/v1/cases/") and path.endswith("/human_review"):
        return "review"
    if path.startswith("/api/v1/policies/parcels/preflights/") and path.endswith("/confirm"):
        return "review"
    if method == "GET":
        return "read"
    return "write"


def _role_for_api_key(api_key: str) -> str | None:
    for configured_key, role in API_KEYS.items():
        if secrets.compare_digest(api_key, configured_key):
            return role
    return None


def _role_has_permission(role: str, permission: str) -> bool:
    return permission in ROLE_PERMISSIONS.get(role, set())


@app.middleware("http")
async def api_key_auth(request: Request, call_next):
    if request.url.path.startswith("/outputs/reports/"):
        return JSONResponse(
            {"detail": "报告目录禁止静态直链，请使用案件受控下载接口"},
            status_code=403,
        )
    if not AUTH_ENABLED:
        return await call_next(request)
    permission = _required_permission(request.method, request.url.path)
    if permission is None:
        return await call_next(request)

    portal_token = (
        request.headers.get("x-agrisky-token", "").strip()
        or request.cookies.get(SESSION_COOKIE_NAME, "").strip()
    )
    portal_user = _auth_user(portal_token) if portal_token else None
    if portal_user and portal_user.get("role") == "admin":
        request.state.agrisky_role = "admin"
        request.state.agrisky_user = portal_user.get("username")
        return await call_next(request)
    # 普通投保人会话只可进入自身案件的受控下载或限权智能体端点；端点再次校验范围。
    if portal_user and (
        "/artifacts/" in request.url.path
        or "/analysis-artifacts/" in request.url.path
        or "/evidence/" in request.url.path
        or "/review-receipts/" in request.url.path
        or "/confirmation-receipts/" in request.url.path
        or "/documents" in request.url.path
        or "/material-review" in request.url.path
        or request.url.path.endswith("/preliminary-excel")
        or request.url.path == "/api/v1/agent/chat"
        or request.url.path.startswith("/api/v1/agent/runs")
        or request.url.path == "/api/v1/agent/health"
    ):
        request.state.agrisky_role = portal_user.get("role") or "policyholder"
        request.state.agrisky_user = portal_user.get("username")
        return await call_next(request)

    role = _role_for_api_key(_extract_api_key(request))
    if not role:
        return JSONResponse({"detail": "Missing or invalid API key"}, status_code=401)
    if not _role_has_permission(role, permission):
        return JSONResponse({"detail": f"Role '{role}' cannot perform '{permission}'"}, status_code=403)
    request.state.agrisky_role = role
    return await call_next(request)


# ── 私有输出根目录（所有下载均经案件级授权与摘要复核）────────
OUTPUT_ROOT = _path_env("AGRISKY_OUTPUT_ROOT", "outputs")
OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
for _private_namespace in ("growth", "screening", "contracts"):
    (OUTPUT_ROOT / _private_namespace).mkdir(parents=True, exist_ok=True)

REPORT_TEMPLATE_VERSION = "v1.3-legal-basis"
_SAFE_CLAIM_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))
}


class MaterialFindingResolutionRequest(BaseModel):
    decision: Literal["accepted", "corrected", "dismissed"]
    corrected_value: Any | None = None
    note: str = Field("", max_length=1000)


class AgentRunCreateRequest(BaseModel):
    messages: list[dict] = Field(default_factory=list)

# ── SQLite 持久化存储 ──────────────────────────────────────
DB_PATH = _path_env("AGRISKY_DB_PATH", BASE_DIR / "data" / "agrisky.db")

def _init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""CREATE TABLE IF NOT EXISTS cases (
        claim_id TEXT PRIMARY KEY, policy_id TEXT, state TEXT DEFAULT 'INIT',
        disaster_type TEXT, loss_date TEXT, crop_type TEXT, plot_id TEXT,
        reported_at TEXT
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS audit_log (
        log_id TEXT PRIMARY KEY, claim_id TEXT, action TEXT, tool_name TEXT,
        tool_args TEXT, tool_result_summary TEXT, prompt_version TEXT,
        actor TEXT DEFAULT 'agent', created_at TEXT
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS deleted_records (
        entity_type TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        deleted_at TEXT NOT NULL,
        deleted_by TEXT NOT NULL,
        PRIMARY KEY (entity_type, entity_id),
        CHECK (entity_type IN ('case', 'policy'))
    )""")
    con.execute("""CREATE TRIGGER IF NOT EXISTS trg_deleted_record_no_update
        BEFORE UPDATE ON deleted_records
        BEGIN SELECT RAISE(ABORT, 'deletion records are immutable'); END
    """)
    con.execute("""CREATE TRIGGER IF NOT EXISTS trg_deleted_record_no_delete
        BEFORE DELETE ON deleted_records
        BEGIN SELECT RAISE(ABORT, 'deletion records are immutable'); END
    """)
    # 每步引擎结果的服务端权威副本：报告与赔付据此组装，不依赖前端回传
    con.execute("""CREATE TABLE IF NOT EXISTS case_results (
        claim_id TEXT, step TEXT, result_json TEXT, created_at TEXT,
        PRIMARY KEY (claim_id, step)
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS case_documents (
        document_id TEXT PRIMARY KEY,
        claim_id TEXT NOT NULL,
        document_type TEXT NOT NULL,
        original_filename TEXT NOT NULL,
        stored_filename TEXT NOT NULL,
        media_type TEXT NOT NULL,
        size_bytes INTEGER NOT NULL,
        sha256 TEXT NOT NULL,
        storage_relpath TEXT NOT NULL,
        parse_status TEXT NOT NULL DEFAULT 'pending',
        uploaded_by TEXT NOT NULL,
        uploaded_at TEXT NOT NULL,
        UNIQUE (claim_id, sha256)
    )""")
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_case_documents_claim "
        "ON case_documents (claim_id, uploaded_at DESC)"
    )
    con.execute("""CREATE TABLE IF NOT EXISTS document_extraction_runs (
        extraction_id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL,
        status TEXT NOT NULL,
        engine TEXT,
        model TEXT,
        result_sha256 TEXT,
        error_message TEXT,
        started_at TEXT NOT NULL,
        completed_at TEXT
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS document_fields (
        field_id TEXT PRIMARY KEY,
        extraction_id TEXT NOT NULL,
        document_id TEXT NOT NULL,
        claim_id TEXT NOT NULL,
        field_name TEXT NOT NULL,
        normalized_value_json TEXT,
        raw_text TEXT NOT NULL,
        confidence REAL NOT NULL,
        page_number INTEGER NOT NULL,
        bbox_json TEXT,
        source_ref TEXT NOT NULL,
        extractor TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""")
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_document_fields_claim "
        "ON document_fields (claim_id, field_name, created_at DESC)"
    )
    con.execute("""CREATE TABLE IF NOT EXISTS document_findings (
        finding_id TEXT PRIMARY KEY,
        claim_id TEXT NOT NULL,
        extraction_id TEXT,
        code TEXT NOT NULL,
        severity TEXT NOT NULL,
        field_name TEXT,
        expected_value_json TEXT,
        actual_value_json TEXT,
        source_ref TEXT,
        message TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open',
        created_at TEXT NOT NULL,
        resolved_by TEXT,
        resolved_at TEXT,
        resolution_note TEXT
    )""")
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_document_findings_claim "
        "ON document_findings (claim_id, status, severity, created_at DESC)"
    )
    con.execute("""CREATE TABLE IF NOT EXISTS document_confirmations (
        confirmation_id TEXT PRIMARY KEY,
        claim_id TEXT NOT NULL,
        target_type TEXT NOT NULL,
        target_id TEXT NOT NULL,
        decision TEXT NOT NULL,
        confirmed_value_json TEXT,
        note TEXT,
        confirmed_by TEXT NOT NULL,
        confirmed_at TEXT NOT NULL,
        receipt_sha256 TEXT NOT NULL
    )""")
    con.execute("""CREATE TRIGGER IF NOT EXISTS trg_document_confirmation_no_update
        BEFORE UPDATE ON document_confirmations
        BEGIN SELECT RAISE(ABORT, 'document confirmations are immutable'); END
    """)
    con.execute("""CREATE TRIGGER IF NOT EXISTS trg_document_confirmation_no_delete
        BEFORE DELETE ON document_confirmations
        BEGIN SELECT RAISE(ABORT, 'document confirmations are immutable'); END
    """)
    con.execute("""CREATE TABLE IF NOT EXISTS agent_runs (
        run_id TEXT PRIMARY KEY,
        actor TEXT NOT NULL,
        scope_json TEXT,
        request_json TEXT NOT NULL,
        status TEXT NOT NULL,
        response_json TEXT,
        error_message TEXT,
        cancel_requested INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        started_at TEXT,
        completed_at TEXT
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS agent_run_events (
        event_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        sequence_no INTEGER NOT NULL,
        event_type TEXT NOT NULL,
        event_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (run_id, sequence_no)
    )""")
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_run_events_run "
        "ON agent_run_events (run_id, sequence_no)"
    )
    con.execute("""CREATE TABLE IF NOT EXISTS review_receipts (
        review_id TEXT PRIMARY KEY,
        claim_id TEXT NOT NULL,
        decision TEXT NOT NULL,
        generation_id TEXT NOT NULL,
        receipt_json TEXT NOT NULL,
        receipt_sha256 TEXT NOT NULL,
        idempotency_key TEXT,
        created_at TEXT NOT NULL,
        UNIQUE (claim_id, idempotency_key)
    )""")
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_review_receipts_claim "
        "ON review_receipts (claim_id, created_at DESC)"
    )
    con.execute("""CREATE TRIGGER IF NOT EXISTS trg_review_receipt_no_update
        BEFORE UPDATE ON review_receipts
        BEGIN SELECT RAISE(ABORT, 'review receipts are immutable'); END
    """)
    con.execute("""CREATE TRIGGER IF NOT EXISTS trg_review_receipt_no_delete
        BEFORE DELETE ON review_receipts
        BEGIN SELECT RAISE(ABORT, 'review receipts are immutable'); END
    """)
    # 保单-地块库：承保边界在册，理赔时凭保单号自动取用（免每次输坐标/传文件）
    con.execute("""CREATE TABLE IF NOT EXISTS policies (
        policy_id TEXT PRIMARY KEY, holder_name TEXT, crop_type TEXT,
        address TEXT, boundary_geojson TEXT, area_mu REAL, created_at TEXT
    )""")
    try:
        con.execute("ALTER TABLE policies ADD COLUMN policy_version_id TEXT")
    except sqlite3.OperationalError:
        pass
    con.execute(
        "UPDATE policies SET policy_version_id = policy_id || ':v1' "
        "WHERE policy_version_id IS NULL OR TRIM(policy_version_id) = ''"
    )
    con.execute("""CREATE TABLE IF NOT EXISTS policy_contracts (
        policy_id TEXT NOT NULL,
        policy_version_id TEXT NOT NULL,
        contract_id TEXT NOT NULL UNIQUE,
        contract_version TEXT NOT NULL,
        contract_json TEXT NOT NULL,
        contract_sha256 TEXT NOT NULL,
        artifact_relative_path TEXT NOT NULL,
        artifact_sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (policy_id, policy_version_id)
    )""")
    con.execute("""CREATE TRIGGER IF NOT EXISTS trg_policy_contract_no_update
        BEFORE UPDATE ON policy_contracts
        BEGIN SELECT RAISE(ABORT, 'policy contracts are immutable'); END
    """)
    con.execute("""CREATE TRIGGER IF NOT EXISTS trg_policy_contract_no_delete
        BEFORE DELETE ON policy_contracts
        BEGIN SELECT RAISE(ABORT, 'policy contracts are immutable'); END
    """)
    con.execute("""CREATE TABLE IF NOT EXISTS case_contract_bindings (
        claim_id TEXT PRIMARY KEY,
        policy_id TEXT NOT NULL,
        policy_version_id TEXT NOT NULL,
        contract_id TEXT NOT NULL,
        contract_sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""")
    con.execute("""CREATE TRIGGER IF NOT EXISTS trg_case_contract_binding_no_update
        BEFORE UPDATE ON case_contract_bindings
        BEGIN SELECT RAISE(ABORT, 'case contract bindings are immutable'); END
    """)
    con.execute("""CREATE TRIGGER IF NOT EXISTS trg_case_contract_binding_no_delete
        BEFORE DELETE ON case_contract_bindings
        BEGIN SELECT RAISE(ABORT, 'case contract bindings are immutable'); END
    """)
    con.execute("""CREATE TABLE IF NOT EXISTS parcel_preflight_manifests (
        preflight_id TEXT PRIMARY KEY,
        manifest_json TEXT NOT NULL,
        manifest_sha256 TEXT NOT NULL,
        archive_sha256 TEXT NOT NULL,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""")
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_parcel_preflight_manifest_sha "
        "ON parcel_preflight_manifests (manifest_sha256)"
    )
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_parcel_preflight_archive_sha "
        "ON parcel_preflight_manifests (archive_sha256)"
    )
    con.execute("""CREATE TRIGGER IF NOT EXISTS trg_parcel_preflight_no_update
        BEFORE UPDATE ON parcel_preflight_manifests
        BEGIN SELECT RAISE(ABORT, 'parcel preflight manifests are immutable'); END
    """)
    con.execute("""CREATE TRIGGER IF NOT EXISTS trg_parcel_preflight_no_delete
        BEFORE DELETE ON parcel_preflight_manifests
        BEGIN SELECT RAISE(ABORT, 'parcel preflight manifests are immutable'); END
    """)
    con.execute("""CREATE TABLE IF NOT EXISTS parcel_confirmations (
        confirmation_id TEXT PRIMARY KEY,
        preflight_id TEXT NOT NULL,
        manifest_sha256 TEXT NOT NULL,
        proposed_parcel_id TEXT NOT NULL,
        policy_id TEXT NOT NULL,
        policy_version_id TEXT NOT NULL,
        parcel_id TEXT NOT NULL,
        parcel_json TEXT NOT NULL,
        mapping_json TEXT NOT NULL,
        parcel_boundary_sha256 TEXT NOT NULL,
        confirmation_batch_id TEXT NOT NULL,
        receipt_sha256 TEXT NOT NULL,
        confirmed_by TEXT NOT NULL,
        confirmed_at TEXT NOT NULL,
        comment TEXT,
        UNIQUE (preflight_id, proposed_parcel_id),
        UNIQUE (policy_id, policy_version_id, parcel_id)
    )""")
    for column_name in ("confirmation_batch_id", "receipt_sha256"):
        try:
            con.execute(f"ALTER TABLE parcel_confirmations ADD COLUMN {column_name} TEXT")
        except sqlite3.OperationalError:
            pass
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_parcel_confirmations_policy "
        "ON parcel_confirmations (policy_id, policy_version_id, parcel_id)"
    )
    con.execute("""CREATE TRIGGER IF NOT EXISTS trg_parcel_confirmation_no_update
        BEFORE UPDATE ON parcel_confirmations
        BEGIN SELECT RAISE(ABORT, 'parcel confirmations are immutable'); END
    """)
    con.execute("""CREATE TRIGGER IF NOT EXISTS trg_parcel_confirmation_no_delete
        BEFORE DELETE ON parcel_confirmations
        BEGIN SELECT RAISE(ABORT, 'parcel confirmations are immutable'); END
    """)
    # Once any claim or parcel confirmation references a policy, its insured terms
    # are immutable at the database boundary as well as in the API transaction.
    con.execute("""CREATE TRIGGER IF NOT EXISTS trg_referenced_policy_terms_no_update
        BEFORE UPDATE OF holder_name, crop_type, address, boundary_geojson, area_mu, policy_version_id
        ON policies
        WHEN (
            EXISTS (SELECT 1 FROM cases WHERE policy_id = OLD.policy_id)
            OR EXISTS (SELECT 1 FROM parcel_confirmations WHERE policy_id = OLD.policy_id)
        ) AND (
            OLD.holder_name IS NOT NEW.holder_name
            OR OLD.crop_type IS NOT NEW.crop_type
            OR OLD.address IS NOT NEW.address
            OR OLD.boundary_geojson IS NOT NEW.boundary_geojson
            OR OLD.area_mu IS NOT NEW.area_mu
            OR OLD.policy_version_id IS NOT NEW.policy_version_id
        )
        BEGIN SELECT RAISE(ABORT, 'referenced policy terms are immutable'); END
    """)
    con.execute("""CREATE TABLE IF NOT EXISTS parcel_confirmation_batches (
        confirmation_batch_id TEXT PRIMARY KEY,
        preflight_id TEXT NOT NULL,
        manifest_sha256 TEXT NOT NULL,
        idempotency_key TEXT,
        request_sha256 TEXT NOT NULL,
        receipt_json TEXT NOT NULL,
        receipt_sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (preflight_id, idempotency_key)
    )""")
    con.execute("""CREATE TRIGGER IF NOT EXISTS trg_parcel_confirmation_batch_no_update
        BEFORE UPDATE ON parcel_confirmation_batches
        BEGIN SELECT RAISE(ABORT, 'parcel confirmation batches are immutable'); END
    """)
    con.execute("""CREATE TRIGGER IF NOT EXISTS trg_parcel_confirmation_batch_no_delete
        BEFORE DELETE ON parcel_confirmation_batches
        BEGIN SELECT RAISE(ABORT, 'parcel confirmation batches are immutable'); END
    """)
    con.execute("""CREATE TABLE IF NOT EXISTS analysis_artifacts (
        claim_id TEXT NOT NULL,
        step TEXT NOT NULL,
        task_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        relative_path TEXT NOT NULL,
        media_type TEXT NOT NULL,
        size_bytes INTEGER NOT NULL,
        sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (claim_id, step, task_id, kind),
        UNIQUE (claim_id, step, task_id, relative_path)
    )""")
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_analysis_artifacts_claim_step "
        "ON analysis_artifacts (claim_id, step, task_id)"
    )
    con.execute("""CREATE TRIGGER IF NOT EXISTS trg_analysis_artifact_no_update
        BEFORE UPDATE ON analysis_artifacts
        BEGIN SELECT RAISE(ABORT, 'analysis artifacts are immutable'); END
    """)
    con.execute("""CREATE TRIGGER IF NOT EXISTS trg_analysis_artifact_no_delete
        BEFORE DELETE ON analysis_artifacts
        BEGIN SELECT RAISE(ABORT, 'analysis artifacts are immutable'); END
    """)
    con.execute("""CREATE TABLE IF NOT EXISTS growth_tasks (
        task_id TEXT PRIMARY KEY,
        creator_principal TEXT NOT NULL,
        result_json TEXT NOT NULL,
        result_sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS growth_task_artifacts (
        task_id TEXT NOT NULL,
        output_key TEXT NOT NULL,
        relative_path TEXT NOT NULL,
        media_type TEXT NOT NULL,
        size_bytes INTEGER NOT NULL,
        sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (task_id, output_key),
        UNIQUE (task_id, relative_path),
        FOREIGN KEY (task_id) REFERENCES growth_tasks(task_id)
    )""")
    for table, label in (
        ("growth_tasks", "growth tasks"),
        ("growth_task_artifacts", "growth task artifacts"),
    ):
        con.execute(f"""CREATE TRIGGER IF NOT EXISTS trg_{table}_no_update
            BEFORE UPDATE ON {table}
            BEGIN SELECT RAISE(ABORT, '{label} are immutable'); END
        """)
        con.execute(f"""CREATE TRIGGER IF NOT EXISTS trg_{table}_no_delete
            BEFORE DELETE ON {table}
            BEGIN SELECT RAISE(ABORT, '{label} are immutable'); END
        """)
    con.commit()
    # 示例地块只允许在显式演示模式写入；生产新库不得静默出现“在册”权威边界。
    try:
        seed_demo_data = os.getenv("AGRISKY_SEED_DEMO_DATA", "false").lower() in {
            "1", "true", "yes", "on"
        }
        if seed_demo_data and con.execute("SELECT COUNT(*) FROM policies").fetchone()[0] == 0:
            sample = BASE_DIR / "data" / "sample_cases" / "insured_boundary_sample.geojson"
            if sample.exists():
                con.execute(
                    "INSERT INTO policies "
                    "(policy_id, holder_name, crop_type, address, boundary_geojson, area_mu, created_at, policy_version_id) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    ("POL-2026-001", "示范种植合作社", "rice", "示范研究区",
                     sample.read_text(encoding="utf-8"), None, datetime.now().isoformat(), "POL-2026-001:v1"),
                )
                con.commit()
    except Exception:  # noqa: BLE001 种子失败不应阻断启动
        pass
    # 投保人门户：账号 + 会话；保单加 holder_account 关联到投保人账号
    try:
        con.execute("ALTER TABLE policies ADD COLUMN holder_account TEXT")
        con.commit()
    except Exception:  # noqa: BLE001 列已存在
        pass
    con.execute("""CREATE TABLE IF NOT EXISTS accounts (
        username TEXT PRIMARY KEY, pwd_hash TEXT, salt TEXT,
        holder_name TEXT, role TEXT DEFAULT 'policyholder', created_at TEXT
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS sessions (
        token TEXT PRIMARY KEY, username TEXT, created_at TEXT
    )""")
    con.commit()
    # 生产环境不再自动创建公开默认密码。首次部署可通过一次性环境变量引导管理员，
    # 或在明确的本地演示模式下创建示范账号。
    try:
        if con.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0:
            bootstrap_username = os.getenv("AGRISKY_BOOTSTRAP_ADMIN_USERNAME", "").strip()
            bootstrap_password = os.getenv("AGRISKY_BOOTSTRAP_ADMIN_PASSWORD", "")
            bootstrap_holder = os.getenv("AGRISKY_BOOTSTRAP_ADMIN_NAME", "系统管理员").strip() or "系统管理员"
            seed_demo = os.getenv("AGRISKY_SEED_DEMO_ACCOUNTS", "false").lower() in {
                "1", "true", "yes", "on"
            }
            if bootstrap_username and len(bootstrap_password) >= 12:
                salt = secrets.token_hex(16)
                password_hash = hashlib.pbkdf2_hmac(
                    "sha256", bootstrap_password.encode("utf-8"), bytes.fromhex(salt), 100_000
                ).hex()
                con.execute(
                    "INSERT INTO accounts VALUES (?,?,?,?,?,?)",
                    (
                        bootstrap_username,
                        password_hash,
                        salt,
                        bootstrap_holder,
                        "admin",
                        datetime.now().isoformat(),
                    ),
                )
                con.commit()
                logger.warning("已写入示范保单/地块，仅可用于隔离的本地演示环境")
                logger.info("已创建引导管理员账号；请移除一次性引导密码环境变量")
            elif seed_demo:
                salt1 = secrets.token_hex(16)
                pwd1 = hashlib.pbkdf2_hmac(
                    "sha256", "demo123".encode(), bytes.fromhex(salt1), 100_000
                ).hex()
                salt2 = secrets.token_hex(16)
                pwd2 = hashlib.pbkdf2_hmac(
                    "sha256", "admin123".encode(), bytes.fromhex(salt2), 100_000
                ).hex()
                con.execute(
                    "INSERT INTO accounts VALUES (?,?,?,?,?,?)",
                    ("nonghu", pwd1, salt1, "示范种植合作社", "policyholder", datetime.now().isoformat()),
                )
                con.execute(
                    "INSERT INTO accounts VALUES (?,?,?,?,?,?)",
                    ("admin", pwd2, salt2, "系统管理员", "admin", datetime.now().isoformat()),
                )
                con.execute(
                    "UPDATE policies SET holder_account = ? WHERE policy_id = ?",
                    ("nonghu", "POL-2026-001"),
                )
                con.commit()
                logger.warning("已启用公开示范账号，仅可用于隔离的本地演示环境")
            else:
                logger.warning(
                    "账号库为空；请配置 AGRISKY_BOOTSTRAP_ADMIN_USERNAME/PASSWORD 后首次启动"
                )
    except Exception:  # noqa: BLE001
        pass
    con.close()

_init_db()

def _db():
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    return con


def _require_admin_actor(request: Request) -> str:
    """Require an administrator for destructive administrative operations."""
    if not AUTH_ENABLED:
        return "development"
    role = getattr(request.state, "agrisky_role", None)
    if role != "admin":
        raise HTTPException(403, "仅管理员可执行删除操作")
    username = getattr(request.state, "agrisky_user", None)
    return f"session:{username}" if username else "api-key:admin"


def _is_deleted(con: sqlite3.Connection, entity_type: str, entity_id: str) -> bool:
    return con.execute(
        "SELECT 1 FROM deleted_records WHERE entity_type = ? AND entity_id = ?",
        (entity_type, entity_id),
    ).fetchone() is not None


def _hash_pwd(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 100_000).hex()


_DUMMY_AUTH_SALT = "00" * 16
_DUMMY_AUTH_HASH = _hash_pwd("agrisky-invalid-account", _DUMMY_AUTH_SALT)
_LOGIN_ACCOUNT_MAX_FAILURES = _bounded_int_env("AGRISKY_LOGIN_ACCOUNT_MAX_FAILURES", 5, 1, 1000)
_LOGIN_IP_MAX_FAILURES = _bounded_int_env("AGRISKY_LOGIN_IP_MAX_FAILURES", 25, 1, 10000)
_LOGIN_WINDOW_SECONDS = _bounded_int_env("AGRISKY_LOGIN_WINDOW_SECONDS", 300, 1, 86400)
_LOGIN_BLOCK_SECONDS = _bounded_int_env("AGRISKY_LOGIN_BLOCK_SECONDS", 900, 1, 7 * 86400)
_LOGIN_RATE_MAX_KEYS = _bounded_int_env("AGRISKY_LOGIN_RATE_MAX_KEYS", 10000, 100, 1_000_000)
_LOGIN_RATE_LOCK = threading.Lock()
_LOGIN_FAILURES: dict[str, list[float]] = {}
_LOGIN_BLOCKED_UNTIL: dict[str, float] = {}


def _login_rate_keys(request: Request, username: str) -> list[tuple[str, int]]:
    client_host = request.client.host if request.client else "unknown"
    return [
        (f"account:{username}", max(1, _LOGIN_ACCOUNT_MAX_FAILURES)),
        (f"ip:{client_host}", max(1, _LOGIN_IP_MAX_FAILURES)),
    ]


def _prune_login_rate_state(now: float) -> None:
    """Prune expired counters and blocks. Caller must hold _LOGIN_RATE_LOCK."""
    window = max(1, _LOGIN_WINDOW_SECONDS)
    for key, attempts in list(_LOGIN_FAILURES.items()):
        current = [timestamp for timestamp in attempts if now - timestamp <= window]
        if current:
            _LOGIN_FAILURES[key] = current
        else:
            _LOGIN_FAILURES.pop(key, None)
    for key, blocked_until in list(_LOGIN_BLOCKED_UNTIL.items()):
        if blocked_until <= now:
            _LOGIN_BLOCKED_UNTIL.pop(key, None)


def _make_login_rate_capacity(key: str) -> None:
    """Bound attacker-controlled account buckets. Caller must hold _LOGIN_RATE_LOCK."""
    if key in _LOGIN_FAILURES or key in _LOGIN_BLOCKED_UNTIL:
        return
    while len(set(_LOGIN_FAILURES) | set(_LOGIN_BLOCKED_UNTIL)) >= _LOGIN_RATE_MAX_KEYS:
        candidate = next(
            (
                existing
                for existing in _LOGIN_FAILURES
                if existing.startswith("account:") and existing not in _LOGIN_BLOCKED_UNTIL
            ),
            None,
        )
        if candidate is None:
            candidate = next(iter(_LOGIN_FAILURES), None)
        if candidate is None:
            candidate = min(_LOGIN_BLOCKED_UNTIL, key=_LOGIN_BLOCKED_UNTIL.get, default=None)
        if candidate is None:
            break
        _LOGIN_FAILURES.pop(candidate, None)
        _LOGIN_BLOCKED_UNTIL.pop(candidate, None)


def _login_retry_after(keys: list[tuple[str, int]]) -> int:
    now = time.monotonic()
    with _LOGIN_RATE_LOCK:
        _prune_login_rate_state(now)
        remaining = [
            until - now
            for key, _limit in keys
            if (until := _LOGIN_BLOCKED_UNTIL.get(key, 0.0)) > now
        ]
    return max(1, math.ceil(max(remaining))) if remaining else 0


def _register_login_failure(keys: list[tuple[str, int]]) -> int:
    now = time.monotonic()
    retry_after = 0
    with _LOGIN_RATE_LOCK:
        _prune_login_rate_state(now)
        for key, limit in keys:
            _make_login_rate_capacity(key)
            attempts = [
                timestamp
                for timestamp in _LOGIN_FAILURES.get(key, [])
                if now - timestamp <= max(1, _LOGIN_WINDOW_SECONDS)
            ]
            attempts.append(now)
            if len(attempts) >= limit:
                until = now + max(1, _LOGIN_BLOCK_SECONDS)
                _LOGIN_BLOCKED_UNTIL[key] = until
                attempts = []
                retry_after = max(retry_after, math.ceil(until - now))
            _LOGIN_FAILURES[key] = attempts
    return retry_after


def _clear_login_account_failures(username: str) -> None:
    key = f"account:{username}"
    with _LOGIN_RATE_LOCK:
        _LOGIN_FAILURES.pop(key, None)
        _LOGIN_BLOCKED_UNTIL.pop(key, None)


def _auth_user(token: str) -> dict | None:
    """凭会话 token 取投保人账号；无效返回 None。"""
    if not token:
        return None
    con = _db()
    row = con.execute(
        "SELECT a.username, a.holder_name, a.role, s.created_at AS session_created_at FROM sessions s "
        "JOIN accounts a ON s.username = a.username WHERE s.token = ?", (token,)
    ).fetchone()
    if row:
        try:
            created_at = datetime.fromisoformat(row["session_created_at"])
            if (datetime.now() - created_at).total_seconds() > max(60, SESSION_MAX_AGE_SECONDS):
                con.execute("DELETE FROM sessions WHERE token = ?", (token,))
                con.commit()
                row = None
        except (TypeError, ValueError):
            con.execute("DELETE FROM sessions WHERE token = ?", (token,))
            con.commit()
            row = None
    con.close()
    if not row:
        return None
    user = dict(row)
    user.pop("session_created_at", None)
    return user


def report_file_ready(path: Path) -> bool:
    """Return True only for a structurally valid DOCX package."""
    return _office_package_ready(path, {"[Content_Types].xml", "word/document.xml"})


def _office_package_ready(path: Path, required_members: set[str]) -> bool:
    if not path.is_file() or path.stat().st_size <= 0 or not zipfile.is_zipfile(path):
        return False
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            return required_members.issubset(names) and archive.testzip() is None
    except (OSError, zipfile.BadZipFile):
        return False


def _xlsx_file_ready(path: Path) -> bool:
    return _office_package_ready(path, {"[Content_Types].xml", "xl/workbook.xml"})


def _zip_file_ready(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size <= 0 or not zipfile.is_zipfile(path):
        return False
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            return len(names) == len(set(names)) and archive.testzip() is None and "manifest.json" in names
    except (OSError, zipfile.BadZipFile):
        return False


def _safe_claim_file_id(claim_id: str) -> str:
    """Validate a business id before it is ever used in a filesystem name."""
    if not _SAFE_CLAIM_ID.fullmatch(claim_id or ""):
        raise HTTPException(400, "案件编号包含不安全字符，不能用于报告导出")
    if claim_id.rstrip(". ") != claim_id or claim_id.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
        raise HTTPException(400, "案件编号是操作系统保留名称，不能用于报告导出")
    return claim_id


def _audit(claim_id: str, action: str, tool_name: str | None = None,
           tool_args: dict | None = None, result_summary: str | None = None,
           actor: str = "agent") -> None:
    """记录审计日志到 SQLite。"""
    con = _db()
    con.execute("INSERT INTO audit_log VALUES (?,?,?,?,?,?,?,?,?)", (
        uuid.uuid4().hex, claim_id, action, tool_name,
        json.dumps(tool_args, ensure_ascii=False, default=str) if tool_args else None,
        result_summary, None, actor, datetime.now().isoformat(),
    ))
    con.commit()
    con.close()
    logger.info(f"[AUDIT] {claim_id} | {action} | {tool_name}")


def _save_result(claim_id: str, step: str, payload: dict) -> None:
    """持久化某一步的引擎结果（服务端权威副本，幂等覆盖）。"""
    con = _db()
    con.execute(
        "INSERT OR REPLACE INTO case_results (claim_id, step, result_json, created_at) VALUES (?,?,?,?)",
        (claim_id, step, json.dumps(payload, ensure_ascii=False, default=str), datetime.now().isoformat()),
    )
    con.commit()
    con.close()


def _get_result(claim_id: str, step: str) -> dict | None:
    """读取某一步已持久化的引擎结果。"""
    con = _db()
    row = con.execute(
        "SELECT result_json FROM case_results WHERE claim_id = ? AND step = ?", (claim_id, step)
    ).fetchone()
    con.close()
    if not row or not row["result_json"]:
        return None
    try:
        return json.loads(row["result_json"])
    except (json.JSONDecodeError, TypeError):
        return None


REMOTE_SENSING_CACHE_MODE = os.getenv(
    "AGRISKY_REMOTE_SENSING_CACHE_MODE", "false"
).lower() in {"1", "true", "yes", "on"}


def _external_remote_sensing_cache_rows(step: str, claim_id: str) -> list[sqlite3.Row]:
    """Read an optional, read-only cache of previously verified GEE results.

    The cache remains subject to the exact same geometry, crop, disaster and
    observation-window checks as results in the active database. It is useful
    when a local proxy temporarily cannot complete a GEE TLS handshake.
    """
    raw_path = os.getenv("AGRISKY_REMOTE_SENSING_CACHE_DB", "").strip()
    if not raw_path:
        return []
    cache_path = Path(raw_path).expanduser()
    if not cache_path.is_file():
        logger.warning("configured remote-sensing cache database is unavailable")
        return []
    try:
        con = sqlite3.connect(str(cache_path))
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT c.claim_id, c.disaster_type, c.loss_date, c.crop_type, c.policy_id, "
            "p.boundary_geojson, r.result_json FROM case_results r "
            "JOIN cases c ON c.claim_id = r.claim_id "
            "JOIN policies p ON p.policy_id = c.policy_id "
            "WHERE r.step = ? AND c.claim_id <> ? ORDER BY r.created_at DESC",
            (step, claim_id),
        ).fetchall()
        con.close()
        return rows
    except sqlite3.Error:
        logger.warning("unable to read remote-sensing cache database", exc_info=True)
        return []


def _compatible_satellite_window(
    cached_start: str | None,
    cached_end: str | None,
    requested_start: str | None,
    requested_end: str | None,
) -> bool:
    """Accept an exact window or a one-day inclusive/exclusive normalization."""
    if cached_start == requested_start and cached_end == requested_end:
        return True
    try:
        return (
            abs((date.fromisoformat(str(cached_start)) - date.fromisoformat(str(requested_start))).days) <= 1
            and abs((date.fromisoformat(str(cached_end)) - date.fromisoformat(str(requested_end))).days) <= 1
        )
    except (TypeError, ValueError):
        return False


def _matching_cached_result(
    claim_id: str,
    step: str,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    window: dict[str, list[str]] | None = None,
) -> tuple[str, dict] | None:
    """Return a real persisted result only when all material inputs match."""
    if not REMOTE_SENSING_CACHE_MODE:
        return None
    con = _db()
    target = con.execute(
        "SELECT c.disaster_type, c.loss_date, c.crop_type, c.policy_id, p.boundary_geojson "
        "FROM cases c JOIN policies p ON p.policy_id = c.policy_id WHERE c.claim_id = ?",
        (claim_id,),
    ).fetchone()
    rows = con.execute(
        "SELECT c.claim_id, c.disaster_type, c.loss_date, c.crop_type, c.policy_id, "
        "p.boundary_geojson, r.result_json FROM case_results r "
        "JOIN cases c ON c.claim_id = r.claim_id "
        "JOIN policies p ON p.policy_id = c.policy_id "
        "WHERE r.step = ? AND c.claim_id <> ? ORDER BY r.created_at DESC",
        (step, claim_id),
    ).fetchall()
    con.close()
    rows.extend(_external_remote_sensing_cache_rows(step, claim_id))
    if not target or not target["boundary_geojson"]:
        return None
    try:
        target_boundary = json.loads(target["boundary_geojson"])
        target_hash = _geometry_sha256(_normalize_geometry(target_boundary))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    for row in rows:
        if (
            row["disaster_type"] != target["disaster_type"]
            or row["crop_type"] != target["crop_type"]
        ):
            continue
        try:
            source_boundary = json.loads(row["boundary_geojson"])
            source_hash = _geometry_sha256(_normalize_geometry(source_boundary))
            payload = json.loads(row["result_json"])
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if source_hash != target_hash or payload.get("status") != "success":
            continue
        if step == "satellite":
            if payload.get("is_mock") or payload.get("source") != "gee":
                continue
            if not _compatible_satellite_window(
                payload.get("start_date"), payload.get("end_date"), start_date, end_date
            ):
                continue
        elif step == "growth":
            meta = (payload.get("raster") or {}).get("ndvi_meta") or {}
            if (payload.get("raster") or {}).get("ndvi_source") != "gee":
                continue
            if meta.get("start_date") != start_date or meta.get("end_date") != end_date:
                continue
            # A growth result references a task-scoped artifact set.  A result
            # from an external cache is reusable only after that set is present
            # in this output root and its registered digests still verify.
            try:
                _verified_growth_artifacts(payload)
            except HTTPException:
                continue
            meta["boundary_source"] = f"保单 {target['policy_id']} 在册地块边界"
            meta["boundary_sha256"] = target_hash
        elif step == "loss_assessment":
            if payload.get("disaster_type") != target["disaster_type"]:
                continue
            if window is not None and payload.get("window") != window:
                continue
        return row["claim_id"], payload
    return None


def _get_result_with_revision(claim_id: str, step: str) -> tuple[dict | None, str | None]:
    """Read one mutable result pointer together with its CAS revision."""
    con = _db()
    row = con.execute(
        "SELECT result_json, created_at FROM case_results WHERE claim_id = ? AND step = ?",
        (claim_id, step),
    ).fetchone()
    con.close()
    if not row or not row["result_json"]:
        return None, None
    try:
        payload = json.loads(row["result_json"])
    except (json.JSONDecodeError, TypeError):
        return None, row["created_at"]
    return (payload if isinstance(payload, dict) else None), row["created_at"]


def _authoritative_damage_ratio(claim_id: str) -> tuple[float | None, str]:
    """Return a server-persisted damage ratio and its provenance.

    Client-supplied ratios are presentation hints only and must never drive a
    compliance, payout, or risk decision.
    """
    assessment = _get_result(claim_id, "loss_assessment")
    if assessment:
        ratio = assessment.get("yield_loss_ratio")
        if _valid_ratio(ratio):
            sources = "/".join(assessment.get("data_sources", []) or ["n/a"])
            return float(ratio), f"多源灾损评估减产率（{sources}）"

    satellite = _get_result(claim_id, "satellite")
    if satellite:
        ratio = satellite.get("damage_ratio")
        if _valid_ratio(ratio):
            return float(ratio), "服务端卫星初筛受损比例"

    return None, ""


def _authoritative_damage_area_ratio(claim_id: str) -> tuple[float | None, str]:
    """Prefer screening coverage for affected area, then fall back to loss assessment."""
    satellite = _get_result(claim_id, "satellite")
    if satellite:
        ratio = satellite.get("damage_ratio")
        if _valid_ratio(ratio):
            return float(ratio), "服务端卫星初筛受灾面积比例"
    return _authoritative_damage_ratio(claim_id)


def _contract_artifact_path(relative_path: str) -> Path:
    path = (OUTPUT_ROOT / relative_path).resolve()
    contracts_root = (OUTPUT_ROOT / "contracts").resolve()
    if not path.is_relative_to(contracts_root):
        raise HTTPException(500, "保险合同附件路径无效")
    return path


def _load_policy_contract(
    policy_id: str, policy_version_id: str | None = None
) -> dict[str, Any] | None:
    con = _db()
    try:
        if policy_version_id:
            row = con.execute(
                "SELECT * FROM policy_contracts WHERE policy_id = ? AND policy_version_id = ?",
                (policy_id, policy_version_id),
            ).fetchone()
        else:
            row = con.execute(
                "SELECT * FROM policy_contracts WHERE policy_id = ? ORDER BY created_at DESC LIMIT 1",
                (policy_id,),
            ).fetchone()
    finally:
        con.close()
    if not row:
        return None
    try:
        contract = json.loads(row["contract_json"])
    except (json.JSONDecodeError, TypeError):
        raise HTTPException(500, "保险合同记录损坏")
    if not isinstance(contract, dict):
        raise HTTPException(500, "保险合同记录格式无效")
    contract["artifact_relative_path"] = row["artifact_relative_path"]
    contract["artifact_sha256"] = row["artifact_sha256"]
    contract["created_at"] = row["created_at"]
    return contract


def _contract_year_from_loss_date(loss_date: str) -> int:
    try:
        return date.fromisoformat(str(loss_date)).year
    except (TypeError, ValueError) as exc:
        raise HTTPException(409, "出险日期无效，无法冻结合同保险期间") from exc


def _contract_version_for_year(policy_version_id: str, insurance_year: int | None) -> str:
    if insurance_year is None:
        return policy_version_id
    return f"{policy_version_id}:period-{insurance_year}"


def _ensure_policy_contract(
    policy: dict[str, Any], *, insurance_year: int | None = None
) -> dict[str, Any]:
    """Create exactly one immutable simulated contract for a policy version and year."""
    policy_id = str(policy["policy_id"])
    base_version_id = str(policy.get("policy_version_id") or f"{policy_id}:v1")
    version_id = _contract_version_for_year(base_version_id, insurance_year)
    existing = _load_policy_contract(policy_id, version_id)
    if existing:
        return existing

    contract_policy = {
        **policy,
        "policy_version_id": version_id,
        "insurance_year": insurance_year,
    }
    contract = build_simulated_contract(contract_policy, insurance_year=insurance_year)
    contract_key = hashlib.sha256(
        f"{policy_id}|{version_id}".encode("utf-8")
    ).hexdigest()[:20]
    filename = f"{contract['contract_number']}.docx"
    relative_path = (Path("contracts") / contract_key / filename).as_posix()
    artifact_path = _contract_artifact_path(relative_path)
    render_contract_docx(contract, artifact_path)
    artifact_sha256 = _sha256_file(artifact_path)
    created_at = datetime.now(timezone.utc).isoformat()

    con = _db()
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT * FROM policy_contracts WHERE policy_id = ? AND policy_version_id = ?",
            (policy_id, version_id),
        ).fetchone()
        if row:
            con.commit()
            return _load_policy_contract(policy_id, version_id) or contract
        con.execute(
            "INSERT INTO policy_contracts "
            "(policy_id, policy_version_id, contract_id, contract_version, contract_json, "
            "contract_sha256, artifact_relative_path, artifact_sha256, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                policy_id,
                version_id,
                contract["contract_id"],
                contract["contract_version"],
                json.dumps(contract, ensure_ascii=False, sort_keys=True),
                contract["contract_sha256"],
                relative_path,
                artifact_sha256,
                created_at,
            ),
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    contract["artifact_relative_path"] = relative_path
    contract["artifact_sha256"] = artifact_sha256
    contract["created_at"] = created_at
    return contract


def _load_case_contract(claim_id: str) -> dict[str, Any] | None:
    con = _db()
    try:
        row = con.execute(
            "SELECT pc.contract_json, pc.contract_sha256, pc.artifact_relative_path, "
            "pc.artifact_sha256, pc.created_at "
            "FROM case_contract_bindings cb "
            "JOIN policy_contracts pc ON pc.policy_id = cb.policy_id "
            "AND pc.policy_version_id = cb.policy_version_id "
            "WHERE cb.claim_id = ?",
            (claim_id,),
        ).fetchone()
    finally:
        con.close()
    if not row:
        return None
    try:
        contract = json.loads(row["contract_json"] or "{}")
    except (json.JSONDecodeError, TypeError) as exc:
        raise HTTPException(500, "案件关联的保险合同记录损坏") from exc
    if not isinstance(contract, dict) or contract.get("contract_sha256") != row["contract_sha256"]:
        raise HTTPException(500, "案件关联的保险合同完整性校验失败")
    contract["artifact_relative_path"] = row["artifact_relative_path"]
    contract["artifact_sha256"] = row["artifact_sha256"]
    contract["created_at"] = row["created_at"]
    return contract


def _ensure_case_contract(
    claim_id: str, policy: dict[str, Any], loss_date: str
) -> dict[str, Any]:
    """Freeze the policy contract selected by an individual claim's loss year."""
    existing = _load_case_contract(claim_id)
    if existing:
        return existing

    base_version_id = str(policy.get("policy_version_id") or f"{policy['policy_id']}:v1")
    uploaded = _load_policy_contract(str(policy["policy_id"]), base_version_id)
    if uploaded and uploaded.get("source_type") == "uploaded_contract":
        contract = uploaded
    else:
        insurance_year = _contract_year_from_loss_date(loss_date)
        contract = _ensure_policy_contract(policy, insurance_year=insurance_year)
    con = _db()
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT 1 FROM case_contract_bindings WHERE claim_id = ?", (claim_id,)
        ).fetchone()
        if not row:
            con.execute(
                "INSERT INTO case_contract_bindings "
                "(claim_id, policy_id, policy_version_id, contract_id, contract_sha256, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    claim_id,
                    str(policy["policy_id"]),
                    str(contract["policy_version_id"]),
                    str(contract["contract_id"]),
                    str(contract["contract_sha256"]),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return _load_case_contract(claim_id) or contract


def _preferred_policy_contract(policy: dict[str, Any]) -> dict[str, Any]:
    """Show the latest claim year's contract when a policy is viewed in the UI."""
    base_version_id = str(policy.get("policy_version_id") or f"{policy['policy_id']}:v1")
    base_contract = _load_policy_contract(str(policy["policy_id"]), base_version_id)
    if base_contract and base_contract.get("source_type") == "uploaded_contract":
        return base_contract
    con = _db()
    try:
        row = con.execute(
            "SELECT loss_date FROM cases WHERE policy_id = ? "
            "ORDER BY reported_at DESC LIMIT 1",
            (str(policy["policy_id"]),),
        ).fetchone()
    finally:
        con.close()
    if row and row["loss_date"]:
        return _ensure_policy_contract(
            policy, insurance_year=_contract_year_from_loss_date(str(row["loss_date"]))
        )
    return _ensure_policy_contract(policy)


def _assemble_case_data(claim_id: str) -> dict | None:
    """在一个 SQLite 读事务中组装报告快照，避免跨连接拼出混合版本。"""
    con = _db()
    try:
        con.execute("BEGIN")
        row = con.execute(
            "SELECT c.*, p.holder_name, p.address AS policy_address, p.area_mu AS policy_area_mu, "
            "p.policy_version_id AS policy_version_id, "
            "p.boundary_geojson AS policy_boundary_geojson "
            "FROM cases c LEFT JOIN policies p ON c.policy_id = p.policy_id "
            "WHERE c.claim_id = ?",
            (claim_id,),
        ).fetchone()
        if not row:
            con.commit()
            return None
        contract_row = con.execute(
            "SELECT pc.contract_json, pc.contract_sha256, pc.artifact_relative_path, "
            "pc.artifact_sha256, pc.created_at "
            "FROM case_contract_bindings cb JOIN policy_contracts pc "
            "ON pc.policy_id = cb.policy_id AND pc.policy_version_id = cb.policy_version_id "
            "WHERE cb.claim_id = ?",
            (claim_id,),
        ).fetchone()
        result_rows = con.execute(
            "SELECT step, result_json, created_at FROM case_results WHERE claim_id = ?",
            (claim_id,),
        ).fetchall()
        document_rows = con.execute(
            "SELECT document_id, document_type, original_filename, media_type, size_bytes, "
            "sha256, parse_status, uploaded_at FROM case_documents "
            "WHERE claim_id = ? ORDER BY uploaded_at",
            (claim_id,),
        ).fetchall()
        field_rows = con.execute(
            "SELECT f.document_id, f.field_name, f.normalized_value_json, f.confidence, "
            "f.page_number, f.source_ref, f.extractor, f.created_at "
            "FROM document_fields f JOIN document_extraction_runs r "
            "ON r.extraction_id = f.extraction_id "
            "WHERE f.claim_id = ? AND r.status = 'completed' ORDER BY f.created_at",
            (claim_id,),
        ).fetchall()
        finding_rows = con.execute(
            "SELECT code, severity, field_name, expected_value_json, actual_value_json, "
            "source_ref, message, status, created_at FROM document_findings "
            "WHERE claim_id = ? ORDER BY created_at",
            (claim_id,),
        ).fetchall()
        con.commit()
    finally:
        con.close()

    results: dict[str, dict] = {}
    revisions: dict[str, str | None] = {}
    contract: dict[str, Any] = {}
    if contract_row:
        try:
            contract = json.loads(contract_row["contract_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            contract = {}
        if isinstance(contract, dict):
            contract["artifact_relative_path"] = contract_row["artifact_relative_path"]
            contract["artifact_sha256"] = contract_row["artifact_sha256"]
            revisions["policy_contract"] = contract_row["created_at"]
        else:
            contract = {}
    for result_row in result_rows:
        revisions[result_row["step"]] = result_row["created_at"]
        try:
            parsed = json.loads(result_row["result_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            parsed = {}
        results[result_row["step"]] = parsed if isinstance(parsed, dict) else {}

    for analysis_step in _ANALYSIS_RESULT_KINDS:
        pointer = results.get(analysis_step) or {}
        if pointer:
            results[analysis_step] = _authoritative_analysis_result(
                claim_id, analysis_step, pointer
            )

    policy_boundary_sha256 = None
    if row["policy_boundary_geojson"]:
        try:
            policy_boundary_sha256 = _geometry_sha256(json.loads(row["policy_boundary_geojson"]))
        except (json.JSONDecodeError, TypeError, ValueError):
            policy_boundary_sha256 = None

    materials = {
        "documents": [dict(item) for item in document_rows],
        "fields": [
            {
                **{
                    key: item[key]
                    for key in (
                        "document_id",
                        "field_name",
                        "confidence",
                        "page_number",
                        "source_ref",
                        "extractor",
                        "created_at",
                    )
                },
                "normalized_value": json.loads(item["normalized_value_json"]),
            }
            for item in field_rows
        ],
        "findings": [
            {
                **{
                    key: item[key]
                    for key in (
                        "code",
                        "severity",
                        "field_name",
                        "source_ref",
                        "message",
                        "status",
                        "created_at",
                    )
                },
                "expected_value": json.loads(item["expected_value_json"])
                if item["expected_value_json"]
                else None,
                "actual_value": json.loads(item["actual_value_json"])
                if item["actual_value_json"]
                else None,
            }
            for item in finding_rows
        ],
    }

    if not contract:
        contract = _ensure_case_contract(
            claim_id,
            {
                "policy_id": row["policy_id"],
                "policy_version_id": row["policy_version_id"] or f"{row['policy_id']}:v1",
                "holder_name": row["holder_name"],
                "crop_type": row["crop_type"],
                "address": row["policy_address"],
                "area_mu": row["policy_area_mu"],
            },
            str(row["loss_date"] or ""),
        )
        revisions["policy_contract"] = contract.get("created_at")

    return {
        "case": {
            "claim_id": claim_id,
            "policy_id": row["policy_id"],
            "policy_version_id": row["policy_version_id"] or f"{row['policy_id']}:v1",
            "holder_name": row["holder_name"],
            "policy_address": row["policy_address"],
            "policy_area_mu": row["policy_area_mu"],
            "policy_boundary_sha256": policy_boundary_sha256,
            "policy_boundary_hash_scheme": "canonical_geometry_v1" if policy_boundary_sha256 else None,
            "disaster_type": row["disaster_type"],
            "loss_date": row["loss_date"],
            "crop_type": row["crop_type"],
            "plot_id": row["plot_id"],
            "case_created_at": row["reported_at"],
        },
        "satellite": results.get("satellite") or {},
        "growth": results.get("growth") or {},
        "historical_ndvi": results.get("historical_ndvi") or {},
        "parcel_growth": results.get("parcel_growth") or {},
        "loss_assessment": results.get("loss_assessment") or {},
        "compliance": results.get("compliance") or {},
        "payout": results.get("payout") or {},
        "rule": results.get("rule") or {},
        "contract": contract,
        "materials": materials,
        "_snapshot_revisions": revisions,
    }


def _normalize_geometry(gj: dict) -> dict:
    """把 FeatureCollection/Feature 归一化为完整合并几何，不丢失多宗地。"""
    if not isinstance(gj, dict):
        return gj
    if gj.get("type") == "FeatureCollection" and gj.get("features"):
        geometries = [
            feature.get("geometry") for feature in gj["features"]
            if isinstance(feature, dict) and feature.get("geometry")
        ]
        if not geometries:
            return gj
        try:
            from shapely import make_valid, union_all
            from shapely.geometry import mapping, shape

            return mapping(union_all([make_valid(shape(geometry)) for geometry in geometries]))
        except Exception:  # noqa: BLE001 保底仍保留全部几何，绝不退化为首要素
            return {"type": "GeometryCollection", "geometries": geometries}
    if gj.get("type") == "Feature":
        return gj.get("geometry", gj)
    return gj


def _canonical_geometry_json(gj: dict) -> str:
    """Return a stable JSON representation of the insured union geometry."""
    normalized = _normalize_geometry(gj)
    try:
        from shapely import make_valid
        from shapely.geometry import mapping, shape

        geometry = make_valid(shape(normalized))
        if hasattr(geometry, "normalize"):
            geometry = geometry.normalize()
        normalized = mapping(geometry)
    except Exception:  # noqa: BLE001 - hashing still works for valid JSON without Shapely
        pass
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _geometry_sha256(gj: dict) -> str:
    return hashlib.sha256(_canonical_geometry_json(gj).encode("utf-8")).hexdigest()


def _geojson_area_mu(gj: dict) -> float | None:
    """由 GeoJSON 估算面积（亩）。失败返回 None（不阻断登记）。"""
    try:
        import geopandas as gpd
        from shapely.geometry import shape
        geom = shape(_normalize_geometry(gj))
        gdf = gpd.GeoDataFrame(geometry=[geom], crs="EPSG:4326")
        utm = gdf.estimate_utm_crs() or "EPSG:6933"
        return round(float(gdf.to_crs(utm).geometry.area.iloc[0]) * 0.0015, 2)
    except Exception:  # noqa: BLE001
        return None


def _policy_roi(policy_id: str) -> dict | None:
    """读取保单在册地块边界，归一化为几何（roi_geojson）。未登记返回 None。"""
    if not policy_id:
        return None
    con = _db()
    row = con.execute(
        "SELECT boundary_geojson FROM policies p WHERE policy_id = ? "
        "AND NOT EXISTS (SELECT 1 FROM deleted_records d "
        "WHERE d.entity_type = 'policy' AND d.entity_id = p.policy_id)",
        (policy_id,),
    ).fetchone()
    con.close()
    if not row or not row["boundary_geojson"]:
        return None
    try:
        return _normalize_geometry(json.loads(row["boundary_geojson"]))
    except (json.JSONDecodeError, TypeError):
        return None


def _case_policy_id(claim_id: str) -> str | None:
    con = _db()
    row = con.execute(
        "SELECT policy_id FROM cases c WHERE claim_id = ? "
        "AND NOT EXISTS (SELECT 1 FROM deleted_records d "
        "WHERE d.entity_type = 'case' AND d.entity_id = c.claim_id)",
        (claim_id,),
    ).fetchone()
    con.close()
    return str(row["policy_id"]) if row and row["policy_id"] else None


def _output_artifact_path(value: str | None) -> Path | None:
    """Resolve a persisted /outputs URL without allowing traversal outside OUTPUT_ROOT."""
    if not value or not isinstance(value, str):
        return None
    root = OUTPUT_ROOT.resolve()
    if value.startswith("/outputs/"):
        candidate = root / value[len("/outputs/"):]
    else:
        raw = Path(value)
        candidate = raw if raw.is_absolute() else root / raw
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    if not resolved.is_relative_to(root) or not resolved.is_file():
        return None
    return resolved


_GROWTH_PUBLIC_OUTPUT_KEYS = {
    "ndvi_preview_png",
    "class_preview_png",
    "ndvi_overlay_png",
    "class_overlay_png",
    "report_growth_map_png",
    "report_crop_map_png",
    "summary_csv",
    "summary_json",
    "map_html",
    "class_geojson",
    "report_docx",
}
_GROWTH_REQUIRED_INTEGRITY_KEYS = {"ndvi_clip_tif", "class_preview_png", "report_docx"}


def _bind_growth_artifact_integrity(result: dict[str, Any], task_dir: Path) -> None:
    """Bind every produced growth output (except self-referential result.json) by hash."""
    task_id = str(result.get("task_id") or "")
    expected_root = (OUTPUT_ROOT / "growth" / task_id).resolve()
    if not _SAFE_TASK_ID.fullmatch(task_id) or task_dir.resolve() != expected_root:
        raise RuntimeError("长势任务目录与任务号不一致")
    integrity: dict[str, dict[str, Any]] = {}
    for key, value in sorted((result.get("outputs") or {}).items()):
        if key == "result_json" or not value:
            continue
        path = _output_artifact_path(str(value))
        if not path or path.parent.resolve() != expected_root:
            raise RuntimeError(f"长势输出 {key} 路径越界或缺失")
        integrity[key] = {
            "filename": path.name,
            "size_bytes": path.stat().st_size,
            "sha256": _sha256_file(path),
        }
    if not _GROWTH_REQUIRED_INTEGRITY_KEYS.issubset(integrity):
        raise RuntimeError("长势权威输出的摘要登记不完整")
    result["artifact_integrity"] = integrity


def _verified_growth_artifacts(growth: dict[str, Any]) -> dict[str, Path]:
    """Re-hash a growth task and reject path substitution or stale algorithms."""
    from growth_analysis import (
        GROWTH_ANALYSIS_ALGORITHM_VERSION,
        GROWTH_ANALYSIS_SCHEMA_VERSION,
    )

    if (
        growth.get("schema_version") != GROWTH_ANALYSIS_SCHEMA_VERSION
        or growth.get("algorithm_version") != GROWTH_ANALYSIS_ALGORITHM_VERSION
    ):
        raise HTTPException(409, "长势结果缺少受支持的算法/架构版本，请重新计算")
    task_id = str(growth.get("task_id") or "")
    if not _SAFE_TASK_ID.fullmatch(task_id):
        raise HTTPException(409, "长势任务号缺失或无效")
    expected_root = (OUTPUT_ROOT / "growth" / task_id).resolve()
    integrity = growth.get("artifact_integrity")
    if not isinstance(integrity, dict) or not _GROWTH_REQUIRED_INTEGRITY_KEYS.issubset(integrity):
        raise HTTPException(409, "长势结果未绑定完整的源栅格与报告附件摘要，请重新计算")
    verified: dict[str, Path] = {}
    outputs = growth.get("outputs") or {}
    for key, entry in integrity.items():
        if not isinstance(entry, dict):
            raise HTTPException(409, "长势附件摘要登记损坏")
        value = outputs.get(key)
        path = _output_artifact_path(value)
        expected_hash = str(entry.get("sha256") or "")
        expected_size = entry.get("size_bytes")
        if (
            not path
            or path.parent.resolve() != expected_root
            or path.name != entry.get("filename")
            or not isinstance(expected_size, int)
            or path.stat().st_size != expected_size
            or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)
            or not secrets.compare_digest(expected_hash, _sha256_file(path))
        ):
            raise HTTPException(409, f"长势附件 {key} 缺失、被替换或摘要不一致")
        verified[key] = path
    raster = growth.get("raster") or {}
    ndvi_path = verified["ndvi_clip_tif"]
    if (
        raster.get("ndvi_clip_size_bytes") != ndvi_path.stat().st_size
        or not secrets.compare_digest(
            str(raster.get("ndvi_clip_sha256") or ""), _sha256_file(ndvi_path)
        )
    ):
        raise HTTPException(409, "长势 NDVI 栅格未在生成时绑定或已被替换")
    return verified


def _case_growth_result_response(claim_id: str, growth: dict[str, Any] | None) -> dict[str, Any] | None:
    """Expose only case-scoped, non-raw growth previews through controlled URLs."""
    if not growth:
        return None
    public = json.loads(json.dumps(growth, ensure_ascii=False, default=str))
    outputs = public.get("outputs") or {}
    try:
        verified = _verified_growth_artifacts(growth)
    except HTTPException:
        public["outputs"] = {key: None for key in outputs}
        public["message"] = f"{public.get('message') or ''}（历史附件未绑定摘要，已隐藏）".strip()
        return public
    task_id = str(growth.get("task_id") or "")
    for key in list(outputs):
        path = verified.get(key)
        outputs[key] = (
            f"/api/v1/cases/{claim_id}/evidence/growth/{task_id}/{path.name}"
            if key in _GROWTH_PUBLIC_OUTPUT_KEYS and path
            else None
        )
    public["outputs"] = outputs
    return public


_STANDALONE_GROWTH_OUTPUT_KEYS = _GROWTH_PUBLIC_OUTPUT_KEYS | {
    "ndvi_clip_tif",
    "classified_tif",
    "boundary_geojson",
    "auto_ndvi_tif",
    "result_json",
}


def _request_principal(request: Request) -> str:
    username = getattr(request.state, "agrisky_user", None)
    if username:
        return f"session:{username}"
    api_key = _extract_api_key(request)
    if api_key:
        fingerprint = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:24]
        return f"api-key-sha256:{fingerprint}"
    return "development" if not AUTH_ENABLED else "unidentified"


def _growth_media_type(path: Path) -> str:
    return {
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
        ".png": "image/png",
        ".html": "text/html; charset=utf-8",
        ".json": "application/json",
        ".geojson": "application/geo+json",
        ".csv": "text/csv; charset=utf-8",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }.get(path.suffix.lower(), "application/octet-stream")


def _growth_html_security_headers(path: Path) -> dict[str, str]:
    """Build a strict CSP whose only inline script hash matches this immutable map."""
    try:
        document = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise HTTPException(409, "长势地图 HTML 无法读取") from exc
    scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", document, re.I | re.S)
    if len(scripts) != 1:
        raise HTTPException(409, "长势地图 HTML 内联脚本结构无效")
    script_hash = base64.b64encode(hashlib.sha256(scripts[0].encode("utf-8")).digest()).decode("ascii")
    frame_ancestors = ["'self'"]
    for configured_origin in CORS_ORIGINS:
        parsed = urlsplit(configured_origin)
        if (
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and parsed.netloc
            and parsed.path in {"", "/"}
            and not parsed.query
            and not parsed.fragment
            and "'" not in configured_origin
            and "*" not in configured_origin
        ):
            origin = f"{parsed.scheme}://{parsed.netloc}"
            if origin not in frame_ancestors:
                frame_ancestors.append(origin)
    return {
        "Content-Security-Policy": (
            "default-src 'none'; "
            f"script-src https://unpkg.com 'sha256-{script_hash}'; "
            "style-src 'unsafe-inline' https://unpkg.com; "
            "img-src 'self' data: https://unpkg.com https://tile.openstreetmap.org "
            "https://server.arcgisonline.com; "
            "connect-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'; "
            f"frame-ancestors {' '.join(frame_ancestors)}"
        ),
        "Referrer-Policy": "no-referrer",
    }


def _growth_runtime_map_response(
    path: Path,
    source_digest: str,
    local_basemap: dict[str, object] | None = None,
) -> HTMLResponse:
    """Render a stored map with same-origin Leaflet assets without mutating evidence bytes."""
    try:
        document = build_runtime_document(path, local_basemap)
        security_headers = runtime_security_headers(document, CORS_ORIGINS)
    except MapRuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    runtime_digest = hashlib.sha256(document.encode("utf-8")).hexdigest()
    return HTMLResponse(
        content=document,
        headers={
            **security_headers,
            "X-Source-Content-SHA256": source_digest,
            "X-Runtime-Content-SHA256": runtime_digest,
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _register_standalone_growth_task(
    task_id: str,
    result: dict[str, Any],
    task_dir: Path,
    request: Request,
) -> None:
    """Register a private standalone task and every declared output immutably."""
    expected_root = (OUTPUT_ROOT / "growth" / task_id).resolve()
    if not _SAFE_TASK_ID.fullmatch(task_id) or task_dir.resolve() != expected_root:
        raise RuntimeError("独立长势任务目录无效")
    result_path = expected_root / "result.json"
    raw_result = result_path.read_text(encoding="utf-8")
    parsed_result = json.loads(raw_result)
    if parsed_result != result:
        raise RuntimeError("独立长势任务结果文件与内存结果不一致")
    entries: list[tuple[str, str, str, int, str]] = []
    for output_key, value in sorted((result.get("outputs") or {}).items()):
        if output_key not in _STANDALONE_GROWTH_OUTPUT_KEYS or not value:
            continue
        path = _output_artifact_path(str(value))
        if not path or path.parent.resolve() != expected_root:
            raise RuntimeError(f"独立长势附件 {output_key} 缺失或路径越界")
        entries.append(
            (
                output_key,
                path.relative_to(OUTPUT_ROOT.resolve()).as_posix(),
                _growth_media_type(path),
                path.stat().st_size,
                _sha256_file(path),
            )
        )
    if not entries or "result_json" not in {item[0] for item in entries}:
        raise RuntimeError("独立长势任务缺少 result_json 登记")
    created_at = datetime.now(timezone.utc).isoformat()
    con = _db()
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            "INSERT INTO growth_tasks "
            "(task_id, creator_principal, result_json, result_sha256, created_at) "
            "VALUES (?,?,?,?,?)",
            (
                task_id,
                _request_principal(request),
                raw_result,
                hashlib.sha256(raw_result.encode("utf-8")).hexdigest(),
                created_at,
            ),
        )
        for output_key, relative_path, media_type, size_bytes, sha256 in entries:
            con.execute(
                "INSERT INTO growth_task_artifacts "
                "(task_id, output_key, relative_path, media_type, size_bytes, sha256, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    task_id,
                    output_key,
                    relative_path,
                    media_type,
                    size_bytes,
                    sha256,
                    created_at,
                ),
            )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _registered_standalone_growth_task(
    task_id: str,
    request: Request,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    if not _SAFE_TASK_ID.fullmatch(task_id or ""):
        raise HTTPException(400, "长势任务号无效")
    con = _db()
    task = con.execute(
        "SELECT creator_principal, result_json, result_sha256 FROM growth_tasks WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    rows = con.execute(
        "SELECT output_key, relative_path, media_type, size_bytes, sha256 "
        "FROM growth_task_artifacts WHERE task_id = ? ORDER BY output_key",
        (task_id,),
    ).fetchall()
    con.close()
    if not task:
        raise HTTPException(404, "长势任务不存在或属于未登记的旧版本")
    principal = _request_principal(request)
    if (
        task["creator_principal"] != principal
        and getattr(request.state, "agrisky_role", None) != "admin"
    ):
        raise HTTPException(403, "无权访问其他操作者的独立长势任务")
    raw_result = str(task["result_json"] or "")
    if not secrets.compare_digest(
        str(task["result_sha256"] or ""),
        hashlib.sha256(raw_result.encode("utf-8")).hexdigest(),
    ):
        raise HTTPException(409, "长势任务结果登记摘要损坏")
    try:
        result = json.loads(raw_result)
    except (json.JSONDecodeError, TypeError) as exc:
        raise HTTPException(409, "长势任务结果无法解析") from exc
    artifacts: dict[str, dict[str, Any]] = {}
    task_root = (OUTPUT_ROOT / "growth" / task_id).resolve()
    for row in rows:
        path = (OUTPUT_ROOT.resolve() / row["relative_path"]).resolve()
        if (
            row["output_key"] not in _STANDALONE_GROWTH_OUTPUT_KEYS
            or not path.is_relative_to(task_root)
            or path.parent.resolve() != task_root
            or not path.is_file()
            or path.stat().st_size != row["size_bytes"]
            or not secrets.compare_digest(row["sha256"], _sha256_file(path))
        ):
            raise HTTPException(409, f"长势任务附件 {row['output_key']} 完整性校验失败")
        artifacts[row["output_key"]] = {
            "path": path,
            "filename": path.name,
            "media_type": row["media_type"],
            "size_bytes": row["size_bytes"],
            "sha256": row["sha256"],
        }
    result_file = artifacts.get("result_json")
    if not result_file or result_file["path"].read_text(encoding="utf-8") != raw_result:
        raise HTTPException(409, "长势任务结果文件与不可变登记不一致")
    return result, artifacts


def _standalone_growth_result_response(
    task_id: str,
    result: dict[str, Any],
    artifacts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    public = json.loads(json.dumps(result, ensure_ascii=False, default=str))
    outputs = public.get("outputs") or {}
    for key in list(outputs):
        artifact = artifacts.get(key)
        outputs[key] = (
            f"/api/v1/tools/growth_analysis/{task_id}/artifacts/{artifact['filename']}"
            if artifact
            else None
        )
    public["outputs"] = outputs
    return public


_ANALYSIS_STEPS = {"historical_ndvi", "parcel_growth"}
_SAFE_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _analysis_task_root(claim_id: str, step: str, task_id: str) -> Path:
    """Resolve one private analysis directory without exposing claim ids in its path."""
    if step not in _ANALYSIS_STEPS or not _SAFE_TASK_ID.fullmatch(task_id or ""):
        raise HTTPException(400, "分析任务标识无效")
    claim_key = hashlib.sha256(claim_id.encode("utf-8")).hexdigest()[:20]
    root = (OUTPUT_ROOT / step / claim_key / task_id).resolve()
    namespace = (OUTPUT_ROOT / step).resolve()
    if not root.is_relative_to(namespace):
        raise HTTPException(400, "分析任务路径无效")
    return root


def _remove_failed_analysis_task(claim_id: str, step: str, task_id: str, path: Path) -> None:
    """Remove only an unregistered failed task directory inside its exact namespace."""
    try:
        expected = _analysis_task_root(claim_id, step, task_id)
        if path.resolve() == expected and expected.is_dir():
            shutil.rmtree(expected)
    except (OSError, HTTPException):
        logger.warning("清理失败的分析任务目录时发生异常", exc_info=True)


def _register_analysis_artifacts(
    claim_id: str,
    step: str,
    task_id: str,
    root: Path,
    entries: list[tuple[str, str, str]],
) -> list[dict[str, Any]]:
    """Register immutable private artifacts after validating path, size and digest."""
    expected_root = _analysis_task_root(claim_id, step, task_id)
    if root.resolve() != expected_root:
        raise RuntimeError("分析产物目录与任务登记不一致")
    output_root = OUTPUT_ROOT.resolve()
    registered: list[dict[str, Any]] = []
    seen_kinds: set[str] = set()
    seen_paths: set[str] = set()
    for kind, relative_name, media_type in entries:
        relative = Path(relative_name)
        if (
            not kind
            or kind in seen_kinds
            or relative.is_absolute()
            or ".." in relative.parts
            or relative.as_posix() in seen_paths
        ):
            raise RuntimeError("分析产物登记项重复或路径无效")
        path = (root / relative).resolve()
        if not path.is_relative_to(expected_root) or not path.is_file():
            raise RuntimeError(f"分析产物缺失: {relative.as_posix()}")
        stored_relative = path.relative_to(output_root).as_posix()
        registered.append(
            {
                "claim_id": claim_id,
                "step": step,
                "task_id": task_id,
                "kind": kind,
                "relative_path": stored_relative,
                "filename": path.name,
                "media_type": media_type,
                "size_bytes": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
        )
        seen_kinds.add(kind)
        seen_paths.add(relative.as_posix())

    con = _db()
    try:
        con.execute("BEGIN IMMEDIATE")
        for artifact in registered:
            existing = con.execute(
                "SELECT relative_path, size_bytes, sha256 FROM analysis_artifacts "
                "WHERE claim_id = ? AND step = ? AND task_id = ? AND kind = ?",
                (claim_id, step, task_id, artifact["kind"]),
            ).fetchone()
            if existing:
                if (
                    existing["relative_path"] != artifact["relative_path"]
                    or existing["size_bytes"] != artifact["size_bytes"]
                    or not secrets.compare_digest(existing["sha256"], artifact["sha256"])
                ):
                    raise HTTPException(409, "同一分析任务的不可变附件登记发生冲突")
                continue
            con.execute(
                "INSERT INTO analysis_artifacts "
                "(claim_id, step, task_id, kind, relative_path, media_type, size_bytes, sha256, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    claim_id,
                    step,
                    task_id,
                    artifact["kind"],
                    artifact["relative_path"],
                    artifact["media_type"],
                    artifact["size_bytes"],
                    artifact["sha256"],
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return registered


def _registered_analysis_artifacts(
    claim_id: str,
    step: str,
    task_id: str,
) -> list[dict[str, Any]]:
    """Load and re-verify the immutable artifact registry for a task."""
    root = OUTPUT_ROOT.resolve()
    con = _db()
    rows = con.execute(
        "SELECT kind, relative_path, media_type, size_bytes, sha256 FROM analysis_artifacts "
        "WHERE claim_id = ? AND step = ? AND task_id = ? ORDER BY kind",
        (claim_id, step, task_id),
    ).fetchall()
    con.close()
    verified: list[dict[str, Any]] = []
    for row in rows:
        path = (root / row["relative_path"]).resolve()
        if (
            not path.is_relative_to(_analysis_task_root(claim_id, step, task_id))
            or not path.is_file()
            or path.stat().st_size != row["size_bytes"]
            or not secrets.compare_digest(row["sha256"], _sha256_file(path))
        ):
            raise HTTPException(409, f"分析附件 {row['kind']} 缺失或完整性校验失败")
        verified.append(
            {
                "kind": row["kind"],
                "relative_path": row["relative_path"],
                "path": path,
                "filename": path.name,
                "media_type": row["media_type"],
                "size_bytes": row["size_bytes"],
                "sha256": row["sha256"],
                "download_url": (
                    f"/api/v1/cases/{claim_id}/analysis-artifacts/"
                    f"{step}/{task_id}/{path.name}"
                ),
            }
        )
    return verified


_ANALYSIS_RESULT_KINDS = {
    "historical_ndvi": "historical_ndvi_result",
    "parcel_growth": "parcel_growth_result",
}


def _authoritative_analysis_result(
    claim_id: str,
    step: str,
    pointer: dict[str, Any],
) -> dict[str, Any]:
    """Load an analysis result from its immutable registered JSON.

    ``case_results`` is only a latest-task pointer.  Business fields are never
    trusted from that mutable row: the pointer must match the registered JSON
    byte-for-byte at the JSON value level, apart from the registry digest fields
    that can only be added after the file is registered.
    """
    if step not in _ANALYSIS_RESULT_KINDS or not isinstance(pointer, dict):
        raise HTTPException(409, "案件分析指针缺失或类型无效")
    task_id = str(pointer.get("task_id") or "")
    artifacts = _registered_analysis_artifacts(claim_id, step, task_id)
    result_artifact = next(
        (item for item in artifacts if item["kind"] == _ANALYSIS_RESULT_KINDS[step]),
        None,
    )
    if (
        not result_artifact
        or pointer.get("_result_artifact_sha256") != result_artifact["sha256"]
        or pointer.get("_result_artifact_size_bytes") != result_artifact["size_bytes"]
    ):
        raise HTTPException(409, "案件分析指针与不可变 JSON 附件摘要不一致")
    try:
        source = json.loads(result_artifact["path"].read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise HTTPException(409, "不可变分析 JSON 无法解析") from exc
    if not isinstance(source, dict) or str(source.get("task_id") or "") != task_id:
        raise HTTPException(409, "不可变分析 JSON 与任务标识不一致")
    if step == "historical_ndvi":
        try:
            from historical_ndvi import HistoricalNDVIResult

            source = HistoricalNDVIResult.model_validate(source).model_dump(mode="json")
        except Exception as exc:
            raise HTTPException(409, "不可变历史 NDVI JSON 的架构或场景溯源无效") from exc

    pointer_business = dict(pointer)
    pointer_business.pop("_result_artifact_sha256", None)
    pointer_business.pop("_result_artifact_size_bytes", None)
    pointer_business.pop("artifacts", None)
    if pointer_business != source:
        raise HTTPException(409, "案件分析指针业务内容与不可变 JSON 不一致")
    return {
        **source,
        "_result_artifact_sha256": result_artifact["sha256"],
        "_result_artifact_size_bytes": result_artifact["size_bytes"],
    }


def _assert_case_download_access(claim_id: str, request: Request) -> dict | None:
    """Apply policyholder ownership checks in addition to middleware authentication."""
    portal_token = (
        request.headers.get("x-agrisky-token", "").strip()
        or request.cookies.get(SESSION_COOKIE_NAME, "").strip()
    )
    portal_user = _auth_user(portal_token) if portal_token else None
    if portal_token and not portal_user and not getattr(request.state, "agrisky_role", None):
        raise HTTPException(401, "未登录或会话已失效")
    if portal_user and portal_user.get("role") != "admin":
        con = _db()
        owner = con.execute(
            "SELECT p.holder_account FROM cases c JOIN policies p ON c.policy_id = p.policy_id "
            "WHERE c.claim_id = ?",
            (claim_id,),
        ).fetchone()
        con.close()
        if not owner or owner["holder_account"] != portal_user.get("username"):
            raise HTTPException(403, "无权访问其他投保人案件的分析附件")
    return portal_user


# ═══════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════

def _require_state(claim_id: str, required_state: S) -> dict:
    """校验案件存在且处于指定状态，返回案件行。"""
    con = _db()
    row = con.execute(
        "SELECT * FROM cases c WHERE claim_id = ? "
        "AND NOT EXISTS (SELECT 1 FROM deleted_records d "
        "WHERE d.entity_type = 'case' AND d.entity_id = c.claim_id)",
        (claim_id,),
    ).fetchone()
    con.close()
    if not row:
        raise HTTPException(404, f"案件不存在: {claim_id}")
    if row["state"] != required_state.value:
        raise HTTPException(400, f"案件状态为 {row['state']}，要求 {required_state.value}")
    return row


def _advance_state(claim_id: str, to_state: S) -> None:
    """推进案件状态，含合法性校验。"""
    con = _db()
    row = con.execute(
        "SELECT state FROM cases c WHERE claim_id = ? "
        "AND NOT EXISTS (SELECT 1 FROM deleted_records d "
        "WHERE d.entity_type = 'case' AND d.entity_id = c.claim_id)",
        (claim_id,),
    ).fetchone()
    if not row: con.close(); raise HTTPException(404, f"案件不存在: {claim_id}")
    from_state = S(row["state"])
    result = can_transition(from_state, S(to_state.value))
    if not result.allowed: con.close(); raise HTTPException(400, f"状态流转非法: {result.reason}")
    con.execute("UPDATE cases SET state = ? WHERE claim_id = ?", (to_state.value, claim_id))
    con.commit()
    con.close()
    logger.info(f"案件 {claim_id}: {from_state.value} -> {to_state.value}")


def _advance_state_with_result(
    claim_id: str,
    to_state: S,
    step: str,
    payload: dict,
    *,
    expected_revisions: dict[str, str | None] | None = None,
) -> None:
    """Atomically persist an authoritative result and advance workflow state."""
    con = _db()
    try:
        row = con.execute(
            "SELECT state FROM cases c WHERE claim_id = ? "
            "AND NOT EXISTS (SELECT 1 FROM deleted_records d "
            "WHERE d.entity_type = 'case' AND d.entity_id = c.claim_id)",
            (claim_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, f"案件不存在: {claim_id}")
        from_state = S(row["state"])
        transition = can_transition(from_state, to_state)
        if not transition.allowed:
            raise HTTPException(400, f"状态流转非法: {transition.reason}")
        if expected_revisions is not None:
            current_rows = con.execute(
                "SELECT step, created_at FROM case_results WHERE claim_id = ?",
                (claim_id,),
            ).fetchall()
            current_revisions = {row["step"]: row["created_at"] for row in current_rows}
            # policy_contract is a separately persisted immutable policy artifact,
            # not a case_results step. Its digest is still included in report
            # snapshots, but it must not participate in this row-for-row CAS.
            expected_case_revisions = {
                key: value
                for key, value in expected_revisions.items()
                if key != "policy_contract"
            }
            if current_revisions != expected_case_revisions:
                raise HTTPException(409, "报告生成期间案件权威结果发生变化，请重新生成最新快照")
        con.execute(
            "INSERT OR REPLACE INTO case_results (claim_id, step, result_json, created_at) VALUES (?,?,?,?)",
            (claim_id, step, json.dumps(payload, ensure_ascii=False, default=str), datetime.now().isoformat()),
        )
        updated = con.execute(
            "UPDATE cases SET state = ? WHERE claim_id = ? AND state = ?",
            (to_state.value, claim_id, from_state.value),
        )
        if updated.rowcount != 1:
            raise HTTPException(409, "案件状态已被其他请求更新，请刷新后重试")
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    logger.info(f"案件 {claim_id}: {from_state.value} -> {to_state.value}; result={step}")


_REVIEW_RETURN_INVALIDATIONS: dict[S, tuple[str, ...]] = {
    S.RULE_DONE: ("report", "excel_report"),
    S.COMPLIANCE_DONE: ("rule", "payout", "report", "excel_report"),
    S.NDVI_DONE: ("compliance", "rule", "payout", "report", "excel_report"),
    S.SCREENING_DONE: (
        "growth", "parcel_growth", "loss_assessment", "compliance", "rule", "payout", "report", "excel_report"
    ),
    S.PREPROCESS_READY: (
        "satellite", "growth", "parcel_growth", "loss_assessment", "compliance", "rule", "payout", "report", "excel_report"
    ),
    S.MATERIAL_CHECK: (
        "satellite", "growth", "parcel_growth", "loss_assessment", "compliance", "rule", "payout", "report", "excel_report"
    ),
}


def _review_result(receipt: dict, receipt_sha256: str) -> dict:
    review_id = str(receipt["review_id"])
    return {
        "status": "success",
        "claim_id": receipt["claim_id"],
        "state": receipt["final_state"],
        "action": receipt["decision"],
        "review_id": review_id,
        "receipt_sha256": receipt_sha256,
        "receipt_download_url": (
            f"/api/v1/cases/{receipt['claim_id']}/review-receipts/{review_id}"
        ),
        "receipt": receipt,
    }


def _verified_review_receipt(
    row: sqlite3.Row,
    *,
    expected_claim_id: str,
    expected_generation_id: str | None = None,
    require_approved: bool = False,
) -> dict[str, Any]:
    """Verify the append-only row, its digest and all identity fields."""
    raw = str(row["receipt_json"] or "")
    actual_sha256 = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    if not secrets.compare_digest(str(row["receipt_sha256"] or ""), actual_sha256):
        raise HTTPException(409, "审核回执摘要校验失败")
    try:
        receipt = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise HTTPException(409, "审核回执无法解析") from exc
    if not isinstance(receipt, dict):
        raise HTTPException(409, "审核回执顶层结构无效")
    generation_id = str(row["generation_id"] or "")
    report = receipt.get("report") or {}
    if (
        receipt.get("review_id") != row["review_id"]
        or receipt.get("claim_id") != row["claim_id"]
        or row["claim_id"] != expected_claim_id
        or receipt.get("decision") != row["decision"]
        or report.get("generation_id") != generation_id
        or (expected_generation_id is not None and generation_id != expected_generation_id)
    ):
        raise HTTPException(409, "审核回执与数据库案件、结论或报告版本不一致")
    if require_approved and row["decision"] != "approved":
        raise HTTPException(409, "归档附件必须由审核通过回执锚定")
    artifacts = report.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise HTTPException(409, "审核回执缺少附件登记")
    seen: set[tuple[str, str]] = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise HTTPException(409, "审核回执附件登记损坏")
        kind = str(artifact.get("kind") or "")
        filename = str(artifact.get("filename") or "")
        sha256 = str(artifact.get("sha256") or "")
        size_bytes = artifact.get("size_bytes")
        key = (kind, filename)
        if (
            not _report_artifact_kind_allowed(kind)
            or Path(filename).name != filename
            or not filename
            or key in seen
            or not re.fullmatch(r"[0-9a-f]{64}", sha256)
            or not isinstance(size_bytes, int)
            or size_bytes <= 0
        ):
            raise HTTPException(409, "审核回执附件名称、类型、大小或摘要无效")
        seen.add(key)
    return receipt


def _record_human_review_decision(
    claim_id: str,
    *,
    decision: str,
    actor: str,
    generation_id: str,
    comment: str = "",
    return_to: str | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """Atomically bind a review decision to a verified report generation and its receipt."""
    if decision not in {"approved", "rejected"}:
        raise HTTPException(422, "审核结论必须为 approved 或 rejected")
    normalized_comment = comment.strip()
    if decision == "rejected" and not normalized_comment:
        raise HTTPException(422, "驳回时必须填写审核意见")
    if decision == "approved" and return_to:
        raise HTTPException(422, "审核通过时不能指定回退状态")
    try:
        final_state = S.ARCHIVED if decision == "approved" else S(return_to or S.RULE_DONE.value)
    except ValueError as exc:
        raise HTTPException(422, "驳回回退状态不受支持") from exc
    if decision == "rejected" and final_state not in _REVIEW_RETURN_INVALIDATIONS:
        raise HTTPException(422, "驳回回退状态不受支持")

    # Hash and inspect the potentially large report bundle without holding
    # SQLite's database-wide writer reservation.  The short write transaction
    # below performs an exact CAS over the report row and every authority revision.
    precon = _db()
    try:
        if idempotency_key:
            existing = precon.execute(
                "SELECT review_id, claim_id, decision, generation_id, receipt_json, receipt_sha256 "
                "FROM review_receipts WHERE claim_id = ? AND idempotency_key = ?",
                (claim_id, idempotency_key),
            ).fetchone()
            if existing:
                if existing["decision"] != decision or existing["generation_id"] != generation_id:
                    raise HTTPException(409, "幂等键已用于另一审核结论或报告版本")
                receipt = _verified_review_receipt(
                    existing,
                    expected_claim_id=claim_id,
                    expected_generation_id=generation_id,
                )
                return _review_result(receipt, existing["receipt_sha256"])
        pre_state_row = precon.execute(
            "SELECT state FROM cases WHERE claim_id = ?", (claim_id,)
        ).fetchone()
        pre_report_row = precon.execute(
            "SELECT result_json, created_at FROM case_results "
            "WHERE claim_id = ? AND step = 'report'",
            (claim_id,),
        ).fetchone()
        pre_revision_rows = precon.execute(
            "SELECT step, created_at FROM case_results WHERE claim_id = ?",
            (claim_id,),
        ).fetchall()
    finally:
        precon.close()
    if not pre_state_row:
        raise HTTPException(404, "案件不存在")
    if S(pre_state_row["state"]) not in {S.REPORT_DRAFTED, S.HUMAN_REVIEW}:
        raise HTTPException(400, f"current state {pre_state_row['state']} does not allow human review")
    if not pre_report_row:
        raise HTTPException(409, "待审核报告记录缺失，禁止归档")
    try:
        pre_report = json.loads(pre_report_row["result_json"] or "{}")
    except (json.JSONDecodeError, TypeError) as exc:
        raise HTTPException(409, "待审核报告记录损坏，禁止归档") from exc
    evidence = _verify_report_for_review(claim_id, pre_report, generation_id)
    expected_authority_revisions = evidence.pop(
        "_authority_revisions",
        {
            item["step"]: item["created_at"]
            for item in pre_revision_rows
            if item["step"] not in {"report", "excel_report"}
        },
    )
    expected_report_json = pre_report_row["result_json"]
    expected_report_revision = pre_report_row["created_at"]

    con = _db()
    try:
        con.execute("BEGIN IMMEDIATE")
        if idempotency_key:
            existing = con.execute(
                "SELECT review_id, claim_id, decision, generation_id, receipt_json, receipt_sha256 "
                "FROM review_receipts WHERE claim_id = ? AND idempotency_key = ?",
                (claim_id, idempotency_key),
            ).fetchone()
            if existing:
                if existing["decision"] != decision or existing["generation_id"] != generation_id:
                    raise HTTPException(409, "幂等键已用于另一审核结论或报告版本")
                receipt = _verified_review_receipt(
                    existing,
                    expected_claim_id=claim_id,
                    expected_generation_id=generation_id,
                )
                con.commit()
                return _review_result(receipt, existing["receipt_sha256"])

        row = con.execute("SELECT state FROM cases WHERE claim_id = ?", (claim_id,)).fetchone()
        if not row:
            raise HTTPException(404, "案件不存在")

        current_state = S(row["state"])
        if current_state not in {S.REPORT_DRAFTED, S.HUMAN_REVIEW}:
            raise HTTPException(400, f"current state {current_state.value} does not allow human review")

        report_row = con.execute(
            "SELECT result_json, created_at FROM case_results WHERE claim_id = ? AND step = 'report'",
            (claim_id,),
        ).fetchone()
        if not report_row:
            raise HTTPException(409, "待审核报告记录缺失，禁止归档")
        if (
            report_row["result_json"] != expected_report_json
            or report_row["created_at"] != expected_report_revision
        ):
            raise HTTPException(409, "报告在审核校验期间发生变化，请重新审核")
        revision_rows = con.execute(
            "SELECT step, created_at FROM case_results WHERE claim_id = ?",
            (claim_id,),
        ).fetchall()
        current_authority_revisions = {
            item["step"]: item["created_at"]
            for item in revision_rows
            if item["step"] not in {"report", "excel_report"}
        }
        if current_authority_revisions != expected_authority_revisions:
            raise HTTPException(409, "审核校验期间案件权威结果发生变化，请重新生成报告")

        reviewed_at = datetime.now(timezone.utc).isoformat()
        review_id = uuid.uuid4().hex
        receipt = {
            "schema_version": "1.0",
            "review_id": review_id,
            "claim_id": claim_id,
            "decision": decision,
            "comment": normalized_comment,
            "actor": actor,
            "reviewed_at_utc": reviewed_at,
            "final_state": final_state.value,
            "return_to": final_state.value if decision == "rejected" else None,
            "report": evidence,
        }
        receipt_json = json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2)
        receipt_sha256 = hashlib.sha256(receipt_json.encode("utf-8")).hexdigest()
        result_payload = _review_result(receipt, receipt_sha256)

        if current_state == S.REPORT_DRAFTED:
            transition = can_transition(current_state, S.HUMAN_REVIEW)
            if not transition.allowed:
                raise HTTPException(400, f"状态流转非法: {transition.reason}")
            updated = con.execute(
                "UPDATE cases SET state = ? WHERE claim_id = ? AND state = ?",
                (S.HUMAN_REVIEW.value, claim_id, current_state.value),
            )
            if updated.rowcount != 1:
                raise HTTPException(409, "案件状态已被其他审核请求更新，请刷新后重试")
            current_state = S.HUMAN_REVIEW

        transition = can_transition(current_state, final_state)
        if not transition.allowed:
            raise HTTPException(400, f"状态流转非法: {transition.reason}")
        updated = con.execute(
            "UPDATE cases SET state = ? WHERE claim_id = ? AND state = ?",
            (final_state.value, claim_id, current_state.value),
        )
        if updated.rowcount != 1:
            raise HTTPException(409, "案件状态已被其他审核请求更新，请刷新后重试")

        if decision == "rejected":
            invalidated_steps = _REVIEW_RETURN_INVALIDATIONS[final_state]
            placeholders = ",".join("?" for _ in invalidated_steps)
            con.execute(
                f"DELETE FROM case_results WHERE claim_id = ? AND step IN ({placeholders})",
                (claim_id, *invalidated_steps),
            )

        con.execute(
            "INSERT INTO review_receipts "
            "(review_id, claim_id, decision, generation_id, receipt_json, receipt_sha256, idempotency_key, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                review_id,
                claim_id,
                decision,
                generation_id,
                receipt_json,
                receipt_sha256,
                idempotency_key,
                reviewed_at,
            ),
        )
        con.execute(
            "INSERT OR REPLACE INTO case_results (claim_id, step, result_json, created_at) VALUES (?,?,?,?)",
            (claim_id, "human_review", json.dumps(result_payload, ensure_ascii=False), reviewed_at),
        )

        action = "human_review_approved" if decision == "approved" else "human_review_rejected"
        summary = (
            "approved and archived"
            if decision == "approved"
            else f"rejected and returned to {final_state.value}"
        )
        con.execute(
            "INSERT INTO audit_log VALUES (?,?,?,?,?,?,?,?,?)",
            (
                uuid.uuid4().hex,
                claim_id,
                action,
                "human_review",
                json.dumps(
                    {
                        "decision": decision,
                        "generation_id": generation_id,
                        "comment": normalized_comment,
                        "return_to": final_state.value if decision == "rejected" else None,
                        "review_id": review_id,
                        "receipt_sha256": receipt_sha256,
                        "idempotency_key": idempotency_key,
                    },
                    ensure_ascii=False,
                    default=str,
                ),
                summary,
                None,
                actor,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    logger.info(
        "案件 %s: human review %s -> %s; actor=%s",
        claim_id,
        decision,
        final_state.value,
        actor,
    )
    return result_payload


# ═══════════════════════════════════════════════════════════
# 1. create_claim — 建案
# ═══════════════════════════════════════════════════════════

@app.post("/api/v1/tools/create_claim", response_model=CreateClaimResponse)
async def create_claim(req: CreateClaimRequest) -> CreateClaimResponse:
    """新建案件。状态: INIT → MATERIAL_CHECK。"""
    claim_id = f"CLAIM-{datetime.now().strftime('%Y%m%d')}-{str(uuid.uuid4())[:6].upper()}"

    con = _db()
    policy = con.execute(
        "SELECT * FROM policies p WHERE p.policy_id = ? "
        "AND NOT EXISTS (SELECT 1 FROM deleted_records d "
        "WHERE d.entity_type = 'policy' AND d.entity_id = p.policy_id)",
        (req.policy_id,),
    ).fetchone()
    if not policy:
        con.close()
        raise HTTPException(409, f"保单未登记或已删除: {req.policy_id}")
    con.close()
    con = _db()
    try:
        con.execute("INSERT INTO cases VALUES (?,?,?,?,?,?,?,?)", (
            claim_id, req.policy_id, "MATERIAL_CHECK", req.disaster_type.value,
            str(req.loss_date), req.crop_type, req.plot_id, datetime.now().isoformat(),
        ))
        con.commit()
    finally:
        con.close()
    _ensure_case_contract(claim_id, dict(policy), str(req.loss_date))

    _audit(claim_id, "create_claim", "create_claim",
           req.model_dump(mode="json"), f"案件创建成功，进入 MATERIAL_CHECK")

    return CreateClaimResponse(
        claim_id=claim_id,
        state=CaseState.MATERIAL_CHECK,
        created_at=datetime.now(),
    )


# ═══════════════════════════════════════════════════════════
# 2. validate_materials — 材料校验
# ═══════════════════════════════════════════════════════════

@app.post("/api/v1/tools/validate_materials", response_model=MaterialCheckResult)
async def validate_materials(req: ValidateMaterialsRequest) -> MaterialCheckResult:
    """校验案件材料的完整性。状态: MATERIAL_CHECK。"""
    case = _require_state(req.claim_id, S.MATERIAL_CHECK)

    missing: list[str] = []
    file_errors: list[dict] = []
    policy_boundary = _policy_roi(case["policy_id"])
    material_review = _material_review_summary(req.claim_id)

    # 文件路径是兼容入口；没有文件时必须能从案件关联保单读取在册边界。
    if req.insured_geom_path:
        path = Path(req.insured_geom_path)
        if not path.exists():
            file_errors.append({
                "file_path": str(path),
                "error_type": "MISSING",
                "detail": "承保边界文件不存在",
            })
        elif path.suffix.lower() not in (".geojson", ".json", ".shp"):
            file_errors.append({
                "file_path": str(path),
                "error_type": "WRONG_FORMAT",
                "detail": f"不支持的文件格式: {path.suffix}，需为 GeoJSON 或 SHP",
            })
    if not policy_boundary:
        missing.append("insured_geom")
    if REQUIRE_CLAIM_DOCUMENTS:
        for item in material_review["missing_document_types"]:
            missing.append(f"document:{item['code']}")
    for document in material_review["documents"]:
        if document["parse_status"] == "failed":
            file_errors.append({
                "file_path": document["original_filename"],
                "error_type": "UNREADABLE",
                "detail": "材料解析失败，请重新上传清晰文件或重新执行解析",
            })
    if material_review["blocking_count"]:
        missing.append("material_findings_resolution")

    passed = not missing and not file_errors

    if passed:
        _advance_state(req.claim_id, S.PREPROCESS_READY)

    result = MaterialCheckResult(
        passed=passed,
        missing_fields=missing,
        file_errors=[
            {"file_path": e["file_path"], "error_type": e["error_type"], "detail": e["detail"]}
            for e in file_errors
        ],
        quality_flags={
            "overall": "pass" if passed else "fail",
            "boundary_source": "policy_registry" if policy_boundary else "missing",
            "uploaded_path_checked": "true" if req.insured_geom_path else "false",
            "document_understanding": "pass" if material_review["passed"] else "needs_review",
            "document_count": str(len(material_review["documents"])),
            "blocking_findings": str(material_review["blocking_count"]),
        },
    )

    _audit(req.claim_id, "validate_materials", "validate_materials",
           req.model_dump(mode="json"), f"passed={passed}")

    return result


# ═══════════════════════════════════════════════════════════
# 3. run_satellite_screening — 卫星初筛
# ═══════════════════════════════════════════════════════════

# ── GEE 配置 ──────────────────────────────────────────────
GEE_PROJECT_ID = os.getenv("GEE_PROJECT_ID", "evimodis")
ALLOW_MOCK_REMOTE_SENSING = os.getenv("AGRISKY_ALLOW_MOCK_REMOTE_SENSING", "false").lower() in {"1", "true", "yes", "on"}


def _mock_satellite_result(reason: str) -> dict:
    return {
        "status": "success",
        "flooded_area_mu": 25.0,
        "damage_ratio": 0.25,
        "confidence": "mock",
        "reference_assets": [f"mock_sentinel1_flood_ratio:{reason}"],
        "image_count": 0,
        "thumbnail_url": None,
        "s2_thumbnail_url": None,
    }


def _ensure_screening_thumbnails(
    claim_id: str,
    roi_geojson: dict | None,
    sar_url: str | None,
    optical_url: str | None,
) -> tuple[str | None, str | None]:
    """Materialize remote thumbnails locally without persisting capability URLs."""
    screening_dir = (OUTPUT_ROOT / "screening" / claim_id).resolve()
    screening_dir.mkdir(parents=True, exist_ok=True)

    def materialize(value: str | None, label: str) -> str | None:
        if not value:
            return None
        if value.startswith("/outputs/screening/"):
            path = _output_artifact_path(value)
            return value if path and path.parent.resolve() == screening_dir else None
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.netloc or len(value) > 8192:
            return None
        artifact_token = uuid.uuid4().hex
        temporary = screening_dir / f".{label}.{artifact_token}.tmp"
        destination = screening_dir / f"{label}-evidence-{artifact_token}.png"
        try:
            import requests
            from PIL import Image

            with requests.get(value, stream=True, timeout=(5, 20)) as response:
                response.raise_for_status()
                content_type = (response.headers.get("content-type") or "").lower()
                if not content_type.startswith("image/"):
                    return None
                total = 0
                with temporary.open("wb") as output:
                    for chunk in response.iter_content(64 * 1024):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > 8 * 1024 * 1024:
                            raise ValueError("screening thumbnail exceeds size limit")
                        output.write(chunk)
            with Image.open(temporary) as source:
                source.convert("RGB").save(destination, format="PNG", optimize=True)
            return f"/outputs/screening/{claim_id}/{destination.name}"
        except Exception:  # noqa: BLE001 - never persist/log the signed source URL
            logger.warning("初筛远程缩略图下载失败: kind=%s", label)
            return None
        finally:
            temporary.unlink(missing_ok=True)

    sar_url = materialize(sar_url, "sar")
    optical_url = materialize(optical_url, "optical")
    if (sar_url and optical_url) or not roi_geojson:
        return sar_url, optical_url
    fallback_token = uuid.uuid4().hex
    fallback_dir = screening_dir / f".fallback-{fallback_token}.tmp"
    try:
        from growth_analysis import render_screening_thumbnails

        thumbs = render_screening_thumbnails(roi_geojson, fallback_dir)
        prefix = f"/outputs/screening/{claim_id}"
        if not optical_url and thumbs.get("optical"):
            source = (fallback_dir / thumbs["optical"]).resolve()
            destination = screening_dir / f"optical-context-{fallback_token}.png"
            source.replace(destination)
            optical_url = f"{prefix}/{destination.name}"
        if not sar_url and thumbs.get("sar"):
            source = (fallback_dir / thumbs["sar"]).resolve()
            destination = screening_dir / f"sar-context-{fallback_token}.png"
            source.replace(destination)
            sar_url = f"{prefix}/{destination.name}"
    except Exception:  # noqa: BLE001
        logger.warning("初筛缩略图兜底失败", exc_info=True)
    finally:
        if fallback_dir.is_dir() and fallback_dir.resolve().is_relative_to(screening_dir):
            shutil.rmtree(fallback_dir, ignore_errors=True)
    return sar_url, optical_url


def _bind_screening_thumbnail_integrity(
    claim_id: str, result: SatelliteScreeningResult
) -> None:
    expected_root = (OUTPUT_ROOT / "screening" / claim_id).resolve()
    integrity: dict[str, dict[str, Any]] = {}
    for field in ("thumbnail_url", "s2_thumbnail_url"):
        value = getattr(result, field)
        if not value:
            continue
        path = _output_artifact_path(value)
        if not path or path.parent.resolve() != expected_root:
            setattr(result, field, None)
            continue
        integrity[field] = {
            "filename": path.name,
            "size_bytes": path.stat().st_size,
            "sha256": _sha256_file(path),
            "provenance": (
                "downloaded_remote_observation_thumbnail"
                if "-evidence-" in path.name
                else "context_only_fallback_not_observation_evidence"
            ),
        }
    result.thumbnail_integrity = integrity


def _case_satellite_result_response(
    claim_id: str, satellite: dict[str, Any] | None
) -> dict[str, Any] | None:
    if not satellite:
        return None
    public = json.loads(json.dumps(satellite, ensure_ascii=False, default=str))
    integrity = satellite.get("thumbnail_integrity") or {}
    for field in ("thumbnail_url", "s2_thumbnail_url"):
        entry = integrity.get(field) or {}
        filename = str(entry.get("filename") or "")
        public[field] = (
            f"/api/v1/cases/{claim_id}/evidence/screening/{filename}"
            if filename and re.fullmatch(r"[0-9a-f]{64}", str(entry.get("sha256") or ""))
            else None
        )
    return public


def _case_local_context_basemap(claim_id: str) -> dict[str, object] | None:
    """Build an embedded, verified optical fallback for the interactive evidence map."""
    satellite = _get_result(claim_id, "satellite") or {}
    integrity = satellite.get("thumbnail_integrity") or {}
    expected_root = (OUTPUT_ROOT / "screening" / claim_id).resolve()
    for field in ("s2_thumbnail_url", "thumbnail_url"):
        entry = integrity.get(field) or {}
        path = _output_artifact_path(satellite.get(field))
        expected_hash = str(entry.get("sha256") or "")
        expected_size = entry.get("size_bytes")
        if (
            not path
            or path.parent.resolve() != expected_root
            or path.name != entry.get("filename")
            or not isinstance(expected_size, int)
            or path.stat().st_size != expected_size
            or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)
            or not secrets.compare_digest(expected_hash, _sha256_file(path))
        ):
            continue
        boundary = _policy_roi(_case_policy_id(claim_id) or "")
        if not boundary:
            return None
        try:
            return embedded_local_basemap(path, boundary)
        except MapRuntimeError:
            logger.warning("案件本地地图底图无法嵌入: claim_id=%s", claim_id)
            return None
    return None


@app.post("/api/v1/tools/run_satellite_screening", response_model=SatelliteScreeningResult)
def run_satellite_screening(req: SatelliteScreeningRequest) -> SatelliteScreeningResult:
    """卫星遥感初筛。状态: PREPROCESS_READY。"""
    case = _require_state(req.claim_id, S.PREPROCESS_READY)
    roi_geojson = _policy_roi(case["policy_id"])
    if not roi_geojson:
        raise HTTPException(400, "保单未登记有效地块边界，无法进行卫星筛查")

    cached = _matching_cached_result(
        req.claim_id,
        "satellite",
        start_date=req.start_date,
        end_date=req.end_date,
    )
    if cached:
        source_claim_id, payload = cached
        # A cached result belongs to another claim. Rebuild only the local map
        # context and artifact integrity for this claim; retain the verified GEE
        # numerical evidence, source assets and exact observation window.
        payload = dict(payload)
        thumb_url, s2_thumb_url = _ensure_screening_thumbnails(req.claim_id, roi_geojson, None, None)
        payload.update(
            {
                "thumbnail_url": thumb_url,
                "s2_thumbnail_url": s2_thumb_url,
                "thumbnail_integrity": {},
                "boundary_sha256": _geometry_sha256(roi_geojson),
                "boundary_hash_scheme": "canonical_geometry_v1",
            }
        )
        cached_result = SatelliteScreeningResult(**payload)
        _bind_screening_thumbnail_integrity(req.claim_id, cached_result)
        payload = cached_result.model_dump(mode="json")
        _advance_state_with_result(req.claim_id, S.SCREENING_DONE, "satellite", payload)
        _audit(
            req.claim_id,
            "run_satellite_screening",
            "run_satellite_screening",
            req.model_dump(mode="json"),
            f"cache_hit={source_claim_id}, damage_ratio={payload.get('damage_ratio')}",
        )
        return SatelliteScreeningResult(
            **(_case_satellite_result_response(req.claim_id, payload) or payload)
        )

    try:
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from space_engine.sar_flood import calculate_flood_ratio

        gee_result = calculate_flood_ratio(
            roi_geojson=roi_geojson,
            start_date=req.start_date,
            end_date=req.end_date,
            project_id=GEE_PROJECT_ID,
        )
    except ImportError as e:
        if not ALLOW_MOCK_REMOTE_SENSING:
            _audit(req.claim_id, "run_satellite_screening", "run_satellite_screening",
                   req.model_dump(mode="json"), f"error=GEE engine unavailable: {e}")
            return SatelliteScreeningResult(
                status="error",
                suspected_damage_area_mu=0,
                damage_ratio=0,
                confidence="low",
                reference_assets=[],
                error_message=f"GEE engine unavailable: {e}",
            )
        logger.warning(f"GEE engine unavailable, using mock satellite result: {e}")
        _audit(req.claim_id, "run_satellite_screening", "run_satellite_screening",
               req.model_dump(mode="json"), f"mock=GEE engine unavailable: {e}")
        gee_result = _mock_satellite_result("import_error")

    if gee_result["status"] != "success":
        if ALLOW_MOCK_REMOTE_SENSING:
            reason = gee_result.get("error_code") or gee_result.get("error_message") or "gee_unavailable"
            logger.warning(f"GEE calculation failed, using mock satellite result: {reason}")
            _audit(req.claim_id, "run_satellite_screening", "run_satellite_screening",
                   req.model_dump(mode="json"), f"mock={reason}")
            gee_result = _mock_satellite_result(str(reason))
        else:
            error_message = (
                gee_result.get("error_message")
                or gee_result.get("message")
                or gee_result.get("error_code")
                or "卫星初筛失败"
            )
            _audit(req.claim_id, "run_satellite_screening", "run_satellite_screening",
                   req.model_dump(mode="json"), f"error={error_message}")
            return SatelliteScreeningResult(
                status="error",
                suspected_damage_area_mu=0,
                damage_ratio=0,
                confidence="low",
                reference_assets=[],
                error_message=error_message,
            )

    thumb_url, s2_thumb_url = _ensure_screening_thumbnails(
        req.claim_id, roi_geojson, gee_result.get("thumbnail_url"), gee_result.get("s2_thumbnail_url")
    )

    result = SatelliteScreeningResult(
        status="success",
        suspected_damage_area_mu=gee_result.get("flooded_area_mu", 0),
        damage_ratio=gee_result.get("damage_ratio", 0),
        confidence=gee_result.get("confidence", "low"),
        reference_assets=gee_result.get("reference_assets", []),
        image_count=gee_result.get("image_count", 0),
        thumbnail_url=thumb_url,
        s2_thumbnail_url=s2_thumb_url,
        source="synthetic" if gee_result.get("confidence") == "mock" else "gee",
        source_label="模拟 Sentinel-1 SAR" if gee_result.get("confidence") == "mock" else "GEE Sentinel-1 SAR",
        start_date=req.start_date,
        end_date=req.end_date,
        is_mock=gee_result.get("confidence") == "mock",
        boundary_sha256=_geometry_sha256(roi_geojson),
        boundary_hash_scheme="canonical_geometry_v1",
    )
    _bind_screening_thumbnail_integrity(req.claim_id, result)

    _advance_state_with_result(
        req.claim_id, S.SCREENING_DONE, "satellite", result.model_dump(mode="json")
    )
    _audit(req.claim_id, "run_satellite_screening", "run_satellite_screening",
           req.model_dump(mode="json"), f"damage_ratio={result.damage_ratio}")

    return SatelliteScreeningResult(
        **(_case_satellite_result_response(req.claim_id, result.model_dump(mode="json")) or {})
    )


# ═══════════════════════════════════════════════════════════
# 3b. run_satellite_screening_upload — 上传边界直接SAR初筛
# ═══════════════════════════════════════════════════════════

@app.post("/api/v1/tools/run_satellite_screening_upload", response_model=SatelliteScreeningResult)
async def run_satellite_screening_upload(
    claim_id: str = Form(...),
    boundary_file: UploadFile = File(...),
    start_date: str = Form(...),
    end_date: str = Form(...),
) -> SatelliteScreeningResult:
    """上传地块边界文件，自动提取几何并执行卫星SAR初筛。"""
    case = _require_state(claim_id, S.PREPROCESS_READY)

    from spatial_utils import load_boundary_from_upload
    boundary_bytes = await boundary_file.read()
    br = load_boundary_from_upload(boundary_bytes, boundary_file.filename or "b.geojson")
    if br["status"] != "success":
        return SatelliteScreeningResult(status="error", error_message=f"边界加载失败: {br.get('error_message')}")

    roi = _policy_roi(case["policy_id"])
    if not roi:
        raise HTTPException(400, "保单未登记有效地块边界，无法进行卫星筛查")

    logger.info(f"边界: {br['source_format']}, {br['feature_count']}要素")

    cached = _matching_cached_result(
        claim_id,
        "satellite",
        start_date=start_date,
        end_date=end_date,
    )
    if cached:
        source_claim_id, payload = cached
        payload = dict(payload)
        thumb_url, s2_thumb_url = _ensure_screening_thumbnails(claim_id, roi, None, None)
        payload.update(
            {
                "thumbnail_url": thumb_url,
                "s2_thumbnail_url": s2_thumb_url,
                "thumbnail_integrity": {},
                "boundary_sha256": _geometry_sha256(roi),
                "boundary_hash_scheme": "canonical_geometry_v1",
            }
        )
        cached_result = SatelliteScreeningResult(**payload)
        _bind_screening_thumbnail_integrity(claim_id, cached_result)
        payload = cached_result.model_dump(mode="json")
        _advance_state_with_result(claim_id, S.SCREENING_DONE, "satellite", payload)
        _audit(
            claim_id,
            "run_satellite_screening",
            "run_satellite_screening_upload",
            {"claim_id": claim_id, "file": boundary_file.filename},
            f"cache_hit={source_claim_id}, damage_ratio={payload.get('damage_ratio')}",
        )
        return SatelliteScreeningResult(
            **(_case_satellite_result_response(claim_id, payload) or payload)
        )

    try:
        import sys; sys.path.insert(0, str(Path(__file__).parent.parent))
        from space_engine.sar_flood import calculate_flood_ratio
        gr = calculate_flood_ratio(roi_geojson=roi, start_date=start_date, end_date=end_date,
                                    project_id=GEE_PROJECT_ID)
    except ImportError as e:
        if not ALLOW_MOCK_REMOTE_SENSING:
            return SatelliteScreeningResult(status="error", error_message=f"GEE dependency unavailable: {e}")
        logger.warning(f"GEE engine unavailable, using mock satellite result: {e}")
        gr = _mock_satellite_result("import_error")

    if gr["status"] != "success":
        if not ALLOW_MOCK_REMOTE_SENSING:
            return SatelliteScreeningResult(status="error", error_message=gr.get("error_message", "GEE calculation failed"))
        reason = gr.get("error_code") or gr.get("error_message") or "gee_unavailable"
        logger.warning(f"GEE calculation failed, using mock satellite result: {reason}")
        gr = _mock_satellite_result(str(reason))

    thumb_url, s2_thumb_url = _ensure_screening_thumbnails(
        claim_id, roi, gr.get("thumbnail_url"), gr.get("s2_thumbnail_url")
    )

    result = SatelliteScreeningResult(
        status="success",
        suspected_damage_area_mu=gr.get("flooded_area_mu", 0),
        damage_ratio=gr.get("damage_ratio", 0),
        confidence=gr.get("confidence", "low"),
        reference_assets=gr.get("reference_assets", []),
        image_count=gr.get("image_count", 0),
        thumbnail_url=thumb_url,
        s2_thumbnail_url=s2_thumb_url,
        source="synthetic" if gr.get("confidence") == "mock" else "gee",
        source_label="模拟 Sentinel-1 SAR" if gr.get("confidence") == "mock" else "GEE Sentinel-1 SAR",
        start_date=start_date,
        end_date=end_date,
        is_mock=gr.get("confidence") == "mock",
        boundary_sha256=_geometry_sha256(roi),
        boundary_hash_scheme="canonical_geometry_v1",
    )
    _bind_screening_thumbnail_integrity(claim_id, result)
    _advance_state_with_result(
        claim_id, S.SCREENING_DONE, "satellite", result.model_dump(mode="json")
    )
    _audit(claim_id, "run_satellite_screening", "run_satellite_screening_upload",
           {"claim_id": claim_id, "file": boundary_file.filename}, f"ratio={result.damage_ratio}")
    return SatelliteScreeningResult(
        **(_case_satellite_result_response(claim_id, result.model_dump(mode="json")) or {})
    )


# ═══════════════════════════════════════════════════════════
# 5a. 文件上传方式合规核验（支持 GeoJSON/SHP/KML/GPKG）
# ═══════════════════════════════════════════════════════════

@app.post("/api/v1/tools/run_compliance_calc_upload", response_model=ComplianceCalcResult)
async def run_compliance_calc_upload(
    claim_id: str = Form(...),
    insured_file: UploadFile | None = File(None),
    damage_file: UploadFile | None = File(None),
    damage_ratio: float | None = Form(None),
) -> ComplianceCalcResult:
    """文件上传方式的空间合规核验。状态: SCREENING_DONE/NDVI_DONE。"""
    # 检查状态
    con = _db()
    row = con.execute("SELECT state, policy_id FROM cases WHERE claim_id = ?", (claim_id,)).fetchone()
    con.close()
    if not row: raise HTTPException(404, "案件不存在")
    st = row["state"]
    # 幂等：返回首次计算的权威结果，不构造虚假的全零结果。
    if st in ("COMPLIANCE_DONE", "RULE_DONE", "REPORT_DRAFTED", "HUMAN_REVIEW", "ARCHIVED"):
        existing = _get_result(claim_id, "compliance")
        if existing:
            return ComplianceCalcResult(**existing)
        raise HTTPException(409, "案件状态显示合规已完成，但权威结果缺失")
    if st not in ("SCREENING_DONE", "NDVI_DONE"):
        raise HTTPException(400, f"当前状态 {st} 不允许合规核验")

    from spatial_utils import load_boundary_from_upload

    # 承保边界只能来自保单库。上传文件仅做格式校验，不能覆盖权威边界。
    if insured_file is not None:
        insured_bytes = await insured_file.read()
        uploaded = load_boundary_from_upload(insured_bytes, insured_file.filename or "b.geojson")
        if uploaded["status"] != "success":
            raise HTTPException(400, f"边界加载失败: {uploaded.get('error_message')}")
    insured_geojson = _policy_roi(row["policy_id"])
    if not insured_geojson:
        raise HTTPException(400, "保单未登记有效地块边界，无法进行合规核验")

    # 上传的受灾范围只能进入待审核材料流，不能绕过遥感/查勘来源审查成为赔付权威值。
    if damage_file is not None:
        raise HTTPException(
            409,
            "上传受灾边界尚未经过来源登记与人工确认，不能直接驱动权威赔付；"
            "请先完成服务端灾损评估，再使用免上传合规核验",
        )

    # 用 UTM 投影估算面积
    import geopandas as gpd
    from shapely.geometry import shape
    geom = shape(insured_geojson)
    gdf = gpd.GeoDataFrame(geometry=[geom], crs="EPSG:4326")
    utm = gdf.estimate_utm_crs() or "EPSG:6933"
    area_sqm = gdf.to_crs(utm).geometry.area.iloc[0]
    insured_mu = round(area_sqm * 0.0015, 2)

    loss_ratio, ratio_source = _authoritative_damage_ratio(claim_id)
    if loss_ratio is None:
        raise HTTPException(409, "缺少服务端灾损评估或卫星初筛结果，无法进行合规核验")

    # 合规受灾 = 承保面积 × 受损比例
    valid_mu = round(insured_mu * loss_ratio, 2)
    unaffected_mu = round(insured_mu * (1 - loss_ratio), 2)

    result = ComplianceCalcResult(
        status="success",
        valid_damage_area_mu=valid_mu,
        damage_ratio=round(loss_ratio, 4),
        excluded_area_mu=unaffected_mu,
        insured_area_mu=insured_mu,
        clip_log=[
            f"承保面积: {insured_mu} 亩",
            f"受损比例来源: {ratio_source} = {loss_ratio*100:.1f}%",
            f"合规受灾 = {insured_mu} × {loss_ratio*100:.1f}% = {valid_mu} 亩",
            f"承保红线内未受灾: {unaffected_mu} 亩（未上传受灾范围时按服务端权威受损比例估算）",
        ],
        boundary_sha256=_geometry_sha256(insured_geojson),
        boundary_hash_scheme="canonical_geometry_v1",
    )
    _advance_state_with_result(
        claim_id, S.COMPLIANCE_DONE, "compliance", result.model_dump(mode="json")
    )
    _audit(claim_id, "run_compliance_calc", "run_compliance_calc_upload",
           {"claim_id": claim_id, "file": insured_file.filename if insured_file else None,
            "ratio": loss_ratio, "ratio_source": ratio_source, "client_ratio_ignored": damage_ratio},
           f"valid={valid_mu}mu, loss_ratio={loss_ratio}")
    return result


# ═══════════════════════════════════════════════════════════
# 8. run_growth_analysis_upload — NDVI 作物长势分析
# ═══════════════════════════════════════════════════════════

@app.post("/api/v1/tools/run_growth_analysis_upload", response_model=GrowthAnalysisResult)
def run_growth_analysis_upload(
    request: Request,
    ndvi_file: UploadFile = File(..., description="NDVI GeoTIFF (.tif/.tiff)"),
    boundary_file: UploadFile = File(..., description="地块边界 (.geojson/.json/.gpkg/.kml 或 SHP zip)"),
    total_area_mu: float = Form(..., description="用于按比例折算的总面积（亩）"),
    crop_label: str = Form("玉米"),
    insurer: str = Form(""),
    method: str = Form("jenks"),
    n_classes: int = Form(5),
    boundary_crs: str = Form("", description="边界缺少 .prj 时可填写，如 EPSG:4526"),
) -> GrowthAnalysisResult:
    """上传 NDVI GeoTIFF 和地块边界，执行长势分析并返回前端可视化结果。"""
    if total_area_mu <= 0:
        raise HTTPException(400, "total_area_mu 必须大于 0")
    if n_classes < 2 or n_classes > 9:
        raise HTTPException(400, "n_classes 需在 2 到 9 之间")

    ndvi_suffix = Path(ndvi_file.filename or "").suffix.lower()
    if ndvi_suffix not in {".tif", ".tiff"}:
        raise HTTPException(400, "NDVI 文件需为 .tif 或 .tiff")

    boundary_suffix = Path(boundary_file.filename or "").suffix.lower()
    if boundary_suffix == ".shp":
        raise HTTPException(400, "Shapefile 请将 .shp/.shx/.dbf/.prj 打包为 .zip 上传")
    if boundary_suffix not in {".geojson", ".json", ".gpkg", ".kml", ".zip"}:
        raise HTTPException(400, "边界文件需为 .geojson/.json/.gpkg/.kml 或 SHP zip")

    task_id = f"growth-{datetime.now().strftime('%Y%m%d%H%M%S')}-{str(uuid.uuid4())[:6]}"
    task_dir = OUTPUT_ROOT / "growth" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)

    src_tif = task_dir / f"input_ndvi{ndvi_suffix}"
    boundary_path = task_dir / f"boundary{boundary_suffix}"
    with src_tif.open("wb") as f:
        shutil.copyfileobj(ndvi_file.file, f)
    with boundary_path.open("wb") as f:
        shutil.copyfileobj(boundary_file.file, f)

    try:
        from growth_analysis import run_growth_analysis

        result = run_growth_analysis(
            task_id=task_id,
            src_tif=src_tif,
            boundary_path=boundary_path,
            output_dir=task_dir,
            total_area_mu=total_area_mu,
            crop_label=crop_label,
            insurer=insurer,
            method=method,
            n_classes=n_classes,
            boundary_crs=boundary_crs.strip() or None,
            report_context={
                "ndvi_meta": {
                    "source": "upload",
                    "source_label": "用户上传 NDVI GeoTIFF",
                    "source_filename": Path(ndvi_file.filename or src_tif.name).name,
                    "source_sha256": _sha256_file(src_tif),
                    "boundary_filename": Path(boundary_file.filename or boundary_path.name).name,
                    "boundary_sha256": _sha256_file(boundary_path),
                    "formula": "源文件已提供 NDVI 值；原始波段公式与观测日期未随文件提供",
                }
            },
            url_prefix=f"/outputs/growth/{task_id}",
        )
    except ImportError as e:
        logger.exception("长势分析依赖缺失")
        raise HTTPException(
            500,
            f"长势分析依赖缺失: {e}. 请安装 api_gateway/requirements.txt 中的 rasterio/geopandas/matplotlib/mapclassify/python-docx",
        ) from e
    except Exception as e:
        logger.exception("长势分析失败")
        raise HTTPException(500, f"长势分析失败: {e}") from e

    result["outputs"]["result_json"] = f"/outputs/growth/{task_id}/result.json"
    _bind_growth_artifact_integrity(result, task_dir)
    (task_dir / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    try:
        _register_standalone_growth_task(task_id, result, task_dir, request)
    except Exception as exc:
        logger.exception("独立长势任务附件登记失败")
        raise HTTPException(500, "长势结果生成成功但私有附件登记失败") from exc

    _audit(
        task_id,
        "run_growth_analysis_upload",
        "run_growth_analysis_upload",
        {
            "ndvi_file": ndvi_file.filename,
            "boundary_file": boundary_file.filename,
            "total_area_mu": total_area_mu,
            "crop_label": crop_label,
            "method": method,
            "n_classes": n_classes,
            "boundary_crs": boundary_crs,
        },
        f"valid_pixels={result['valid_pixel_count']}",
    )

    registered_result, artifacts = _registered_standalone_growth_task(task_id, request)
    return GrowthAnalysisResult(
        **_standalone_growth_result_response(task_id, registered_result, artifacts)
    )


@app.get("/api/v1/tools/growth_analysis/{task_id}", response_model=GrowthAnalysisResult)
async def get_growth_analysis(task_id: str, request: Request) -> GrowthAnalysisResult:
    """读取属于当前操作者的不可变独立长势任务结果。"""
    result, artifacts = _registered_standalone_growth_task(task_id, request)
    return GrowthAnalysisResult(**_standalone_growth_result_response(task_id, result, artifacts))


@app.get("/api/v1/map-runtime/vendor/{filename:path}", include_in_schema=False)
async def download_map_runtime_vendor(filename: str) -> FileResponse:
    """Serve the pinned Leaflet runtime from the API origin."""
    try:
        path = vendor_asset_path(filename)
    except MapRuntimeError as exc:
        raise HTTPException(404, str(exc)) from exc
    media_type = {
        ".css": "text/css; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
        ".png": "image/png",
    }.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(
        path=str(path),
        media_type=media_type,
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/api/v1/tools/growth_analysis/{task_id}/map-runtime.html")
async def view_standalone_growth_map(task_id: str, request: Request) -> HTMLResponse:
    """Render the registered standalone map with local frontend dependencies."""
    _result, artifacts = _registered_standalone_growth_task(task_id, request)
    artifact = artifacts.get("map_html")
    if not artifact:
        raise HTTPException(404, "长势地图未登记")
    _audit(
        task_id,
        "view_standalone_growth_map",
        "view_standalone_growth_map",
        {"principal": _request_principal(request)},
        f"source_sha256={artifact['sha256']}",
        actor=_request_principal(request),
    )
    return _growth_runtime_map_response(artifact["path"], artifact["sha256"])


@app.get("/api/v1/tools/growth_analysis/{task_id}/artifacts/{filename}")
async def download_standalone_growth_artifact(
    task_id: str,
    filename: str,
    request: Request,
) -> FileResponse:
    """Download one creator-scoped standalone growth artifact after digest verification."""
    if Path(filename).name != filename or not filename:
        raise HTTPException(400, "长势附件文件名无效")
    _result, artifacts = _registered_standalone_growth_task(task_id, request)
    match = next((item for item in artifacts.values() if item["filename"] == filename), None)
    if not match:
        raise HTTPException(404, "长势附件未登记")
    suffix = match["path"].suffix.lower()
    inline = suffix in {".png", ".html"}
    headers = {
        "X-Content-SHA256": match["sha256"],
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    }
    if suffix == ".html":
        headers.update(_growth_html_security_headers(match["path"]))
    _audit(
        task_id,
        "download_standalone_growth_artifact",
        "download_standalone_growth_artifact",
        {"filename": filename, "principal": _request_principal(request)},
        f"sha256={match['sha256']}",
        actor=_request_principal(request),
    )
    return FileResponse(
        path=str(match["path"]),
        media_type=match["media_type"],
        filename=None if inline else filename,
        headers=headers,
    )


# ═══════════════════════════════════════════════════════════
# 8b. run_growth_analysis_boundary_upload — 仅上传边界自动解译
# ═══════════════════════════════════════════════════════════

@app.post("/api/v1/tools/run_growth_analysis_boundary_upload", response_model=GrowthAnalysisResult)
def run_growth_analysis_boundary_upload(
    request: Request,
    boundary_file: UploadFile = File(..., description="地块边界 (.shp/.geojson/.json/.gpkg/.kml 或 SHP zip)"),
    total_area_mu: float | None = Form(None, description="可选；留空时由边界自动计算"),
    crop_label: str = Form("玉米"),
    insurer: str = Form(""),
    method: str = Form("jenks"),
    n_classes: int = Form(5),
    boundary_crs: str = Form("", description="边界缺少 .prj 时可填写，如 EPSG:4526"),
    ndvi_source: str = Form("auto", description="auto/gee/synthetic；仅显式 Mock 模式允许模拟数据"),
    start_date: str = Form("2025-08-30"),
    end_date: str = Form("2025-09-15"),
    max_cloud_pct: float = Form(30.0),
    claim_id: str = Form("", description="遗留字段；案件分析必须改用 run_growth_analysis_by_claim"),
) -> GrowthAnalysisResult:
    """只上传地块边界，自动准备 NDVI 并执行长势分析。"""
    if claim_id:
        raise HTTPException(
            400,
            "案件长势分析禁止使用临时上传边界覆盖在册 ROI，请调用 run_growth_analysis_by_claim",
        )
    if total_area_mu is not None and total_area_mu <= 0:
        raise HTTPException(400, "total_area_mu 留空或填写大于 0 的数值")
    if n_classes < 2 or n_classes > 9:
        raise HTTPException(400, "n_classes 需在 2 到 9 之间")
    if max_cloud_pct < 0 or max_cloud_pct > 100:
        raise HTTPException(400, "max_cloud_pct 需在 0 到 100 之间")

    boundary_suffix = Path(boundary_file.filename or "").suffix.lower()
    if boundary_suffix not in {".shp", ".geojson", ".json", ".gpkg", ".kml", ".zip"}:
        raise HTTPException(400, "边界文件需为 .shp/.geojson/.json/.gpkg/.kml 或 SHP zip")

    task_id = f"growth-{datetime.now().strftime('%Y%m%d%H%M%S')}-{str(uuid.uuid4())[:6]}"
    task_dir = OUTPUT_ROOT / "growth" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)

    boundary_path = task_dir / f"boundary{boundary_suffix}"
    with boundary_path.open("wb") as f:
        shutil.copyfileobj(boundary_file.file, f)

    try:
        from growth_analysis import run_growth_analysis_from_boundary

        result = run_growth_analysis_from_boundary(
            task_id=task_id,
            boundary_path=boundary_path,
            output_dir=task_dir,
            total_area_mu=total_area_mu,
            crop_label=crop_label,
            insurer=insurer,
            method=method,
            n_classes=n_classes,
            boundary_crs=boundary_crs.strip() or None,
            ndvi_source=ndvi_source,
            start_date=start_date,
            end_date=end_date,
            max_cloud_pct=max_cloud_pct,
            gee_project_id=GEE_PROJECT_ID,
            url_prefix=f"/outputs/growth/{task_id}",
            source_provenance={
                "boundary_filename": Path(boundary_file.filename or boundary_path.name).name,
                "boundary_sha256": _sha256_file(boundary_path),
            },
        )
    except ImportError as e:
        logger.exception("边界自动解译依赖缺失")
        raise HTTPException(
            500,
            f"边界自动解译依赖缺失: {e}. 请安装 api_gateway/requirements.txt",
        ) from e
    except Exception as e:
        logger.exception("边界自动解译失败")
        raise HTTPException(500, f"边界自动解译失败: {e}") from e

    result["outputs"]["result_json"] = f"/outputs/growth/{task_id}/result.json"
    _bind_growth_artifact_integrity(result, task_dir)
    (task_dir / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    try:
        _register_standalone_growth_task(task_id, result, task_dir, request)
    except Exception as exc:
        logger.exception("独立边界长势任务附件登记失败")
        raise HTTPException(500, "长势结果生成成功但私有附件登记失败") from exc

    _audit(
        task_id,
        "run_growth_analysis_boundary_upload",
        "run_growth_analysis_boundary_upload",
        {
            "boundary_file": boundary_file.filename,
            "total_area_mu": total_area_mu,
            "crop_label": crop_label,
            "method": method,
            "n_classes": n_classes,
            "boundary_crs": boundary_crs,
            "ndvi_source": ndvi_source,
            "start_date": start_date,
            "end_date": end_date,
        },
        f"valid_pixels={result['valid_pixel_count']}, source={result['raster'].get('ndvi_source')}",
    )

    # 挂到案件：供从案件队列重新打开时回填长势分级图（长势分析本身按 task_id 独立存储）
    if claim_id:
        _save_result(claim_id, "growth", result)

    registered_result, artifacts = _registered_standalone_growth_task(task_id, request)
    return GrowthAnalysisResult(
        **_standalone_growth_result_response(task_id, registered_result, artifacts)
    )


# ═══════════════════════════════════════════════════════════
# 8bb. run_growth_analysis_by_claim — Agent 免上传长势查询
# ═══════════════════════════════════════════════════════════

class GrowthByClaimRequest(BaseModel):
    claim_id: str = Field(..., description="案件编号")
    start_date: str | None = Field(None, description="NDVI 起算日期；留空按出险日期所在季度")
    end_date: str | None = Field(None, description="NDVI 截止日期（含）；留空按季度末/当前日")
    ndvi_source: str = Field("auto", description="auto/gee/synthetic")
    max_cloud_pct: float = Field(30.0, description="最大云量百分比")
    method: str = Field("fixed", description="fixed/jenks/equalinterval/quantile/std")


def _growth_observation_window(
    loss_date: str | None, requested_start: str | None, requested_end: str | None
) -> tuple[str, str]:
    """Use an explicit inclusive window or derive the loss-date calendar quarter."""
    if bool(requested_start) != bool(requested_end):
        raise HTTPException(400, "NDVI 起止日期必须同时提供或同时留空")
    try:
        if requested_start and requested_end:
            start = date.fromisoformat(requested_start)
            end = date.fromisoformat(requested_end)
        else:
            observed = date.fromisoformat(str(loss_date)[:10])
            quarter_month = ((observed.month - 1) // 3) * 3 + 1
            start = date(observed.year, quarter_month, 1)
            if quarter_month == 10:
                next_quarter = date(observed.year + 1, 1, 1)
            else:
                next_quarter = date(observed.year, quarter_month + 3, 1)
            end = next_quarter - timedelta(days=1)
            today = date.today()
            if start <= today < end:
                end = today
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "案件出险日期或 NDVI 日期无效") from exc
    if start > end:
        raise HTTPException(400, "NDVI 起始日期不能晚于截止日期")
    return start.isoformat(), end.isoformat()


@app.post("/api/v1/tools/run_growth_analysis_by_claim", response_model=GrowthAnalysisResult)
def run_growth_analysis_by_claim(req: GrowthByClaimRequest) -> GrowthAnalysisResult:
    """Agent 专用：凭 claim_id 自动取保单在册地块边界执行长势分析，无需上传文件。"""
    from pathlib import Path as _Path
    from growth_analysis import run_growth_analysis_from_boundary

    # 查案件状态
    con = _db()
    row = con.execute(
        "SELECT c.state, c.policy_id, c.loss_date, c.crop_type, p.boundary_geojson, p.holder_name "
        "FROM cases c LEFT JOIN policies p ON c.policy_id = p.policy_id "
        "WHERE c.claim_id = ?", (req.claim_id,)
    ).fetchone()
    con.close()
    if not row:
        raise HTTPException(404, f"案件不存在: {req.claim_id}")
    if not row["boundary_geojson"]:
        raise HTTPException(400, f"保单 {row['policy_id']} 未登记地块边界，请先在保单库登记。")
    state = row["state"]
    if state == "NDVI_DONE":
        existing = _get_result(req.claim_id, "growth")
        if existing:
            return GrowthAnalysisResult(**(_case_growth_result_response(req.claim_id, existing) or existing))
        raise HTTPException(409, "案件状态显示长势分析已完成，但权威结果缺失")
    if state != "SCREENING_DONE":
        raise HTTPException(409, f"当前状态 {state} 已冻结长势结果；只有 SCREENING_DONE 可执行长势分析")

    # 修复极少数“结果已落库但状态未推进”的历史中断，不重复计算遥感结果。
    existing = _get_result(req.claim_id, "growth")
    if existing:
        _advance_state_with_result(req.claim_id, S.NDVI_DONE, "growth", existing)
        return GrowthAnalysisResult(**(_case_growth_result_response(req.claim_id, existing) or existing))

    start_date, end_date = _growth_observation_window(
        row["loss_date"], req.start_date, req.end_date
    )

    cached = _matching_cached_result(
        req.claim_id,
        "growth",
        start_date=start_date,
        end_date=end_date,
    )
    if cached:
        source_claim_id, payload = cached
        _advance_state_with_result(req.claim_id, S.NDVI_DONE, "growth", payload)
        _audit(
            req.claim_id,
            "run_growth_analysis",
            "run_growth_analysis_by_claim",
            {"start_date": start_date, "end_date": end_date, "method": req.method},
            f"cache_hit={source_claim_id}, task={payload.get('task_id')}",
        )
        return GrowthAnalysisResult(
            **(_case_growth_result_response(req.claim_id, payload) or payload)
        )

    # 写边界到临时 GeoJSON
    task_id = f"growth-{datetime.now().strftime('%Y%m%d%H%M%S')}-{str(uuid.uuid4())[:6]}"
    task_dir = OUTPUT_ROOT / "growth" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    boundary_path = task_dir / "boundary.geojson"
    roi = row["boundary_geojson"]
    if isinstance(roi, str):
        roi = json.loads(roi)
    roi = _normalize_geometry(roi)
    boundary_path.write_text(_canonical_geometry_json(roi), encoding="utf-8")

    crop_label = row["crop_type"] or "玉米"
    try:
        result = run_growth_analysis_from_boundary(
            task_id=task_id,
            boundary_path=boundary_path,
            output_dir=task_dir,
            total_area_mu=None,
            crop_label=crop_label,
            insurer=row["holder_name"] or "",
            method=req.method,
            n_classes=5,
            boundary_crs=None,
            ndvi_source=req.ndvi_source,
            start_date=start_date,
            end_date=end_date,
            max_cloud_pct=req.max_cloud_pct,
            gee_project_id=GEE_PROJECT_ID,
            url_prefix=f"/outputs/growth/{task_id}",
            source_provenance={
                "boundary_source": f"保单 {row['policy_id']} 在册地块边界",
                "boundary_sha256": _sha256_file(boundary_path),
                "boundary_hash_scheme": "canonical_geometry_v1",
            },
        )
    except ImportError as e:
        logger.exception("长势分析依赖缺失")
        raise HTTPException(500, f"长势分析依赖缺失: {e}") from e
    except Exception as e:
        logger.exception("长势分析失败")
        raise HTTPException(500, f"长势分析失败: {e}") from e

    result["outputs"]["result_json"] = f"/outputs/growth/{task_id}/result.json"
    _bind_growth_artifact_integrity(result, task_dir)
    (task_dir / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _advance_state_with_result(req.claim_id, S.NDVI_DONE, "growth", result)

    _audit(
        req.claim_id,
        "run_growth_analysis",
        "run_growth_analysis_by_claim",
        {"start_date": start_date, "end_date": end_date, "method": req.method},
        f"task={task_id}, source={result['raster'].get('ndvi_source')}, pixels={result['valid_pixel_count']}",
    )

    return GrowthAnalysisResult(**(_case_growth_result_response(req.claim_id, result) or result))


# ═══════════════════════════════════════════════════════════
# 8bc. 历史季度 NDVI 与已确认分地块长势
# ═══════════════════════════════════════════════════════════

_FROZEN_ANALYSIS_STATES = {S.REPORT_DRAFTED.value, S.HUMAN_REVIEW.value, S.ARCHIVED.value}
_ANALYSIS_CACHE_MAX_AGE_SECONDS = _bounded_int_env(
    "AGRISKY_ANALYSIS_CACHE_MAX_AGE_SECONDS", 86400, 0, 7 * 24 * 60 * 60
)


def _historical_cache_reusable(result: dict[str, Any]) -> bool:
    """Reuse only recent, fully versioned observations; never pin failures/backfills forever."""
    try:
        from historical_ndvi import (
            AGGREGATION_METHOD,
            ALGORITHM_ID,
            ALGORITHM_VERSION,
            SCHEMA_VERSION,
            SOURCE_PROVENANCE_SCHEMA_VERSION,
        )

        source = result.get("source") or {}
        if (
            result.get("schema_version") != SCHEMA_VERSION
            or source.get("provenance_schema_version") != SOURCE_PROVENANCE_SCHEMA_VERSION
            or source.get("algorithm_id") != ALGORITHM_ID
            or source.get("algorithm_version") != ALGORITHM_VERSION
            or source.get("aggregation_method") != AGGREGATION_METHOD
        ):
            return False
        generated_at = datetime.fromisoformat(str(result.get("generated_at") or "").replace("Z", "+00:00"))
        if generated_at.tzinfo is None:
            return False
        age = (datetime.now(timezone.utc) - generated_at.astimezone(timezone.utc)).total_seconds()
        if age < 0 or age > _ANALYSIS_CACHE_MAX_AGE_SECONDS:
            return False
        quarters = result.get("quarters") or []
        if not quarters:
            return False
        for quarter in quarters:
            disposition = (quarter or {}).get("cache_disposition")
            if disposition in {"retryable_failure", "negative_no_imagery"}:
                return False
            if disposition not in {"observation", "negative_no_valid_pixels", "not_reached"}:
                return False
        return True
    except (ImportError, TypeError, ValueError):
        return False


def _run_historical_ndvi_job(**kwargs: Any) -> dict[str, Any]:
    """Run the real GEE history pipeline and its formal artifacts; never synthesize."""
    from historical_ndvi import run_gee_historical_ndvi
    from historical_ndvi_report import generate_historical_ndvi_report

    result = run_gee_historical_ndvi(**kwargs)
    reachable = [item for item in result.quarters if item.status != "not_reached"]
    if reachable and all(item.status == "failed" for item in reachable):
        failures = "; ".join(
            item.error_message or item.status for item in reachable[:4]
        )
        raise RuntimeError(f"历史季度 NDVI 没有任何可用季度：{failures}")
    updated = generate_historical_ndvi_report(
        result,
        kwargs["output_dir"],
        artifact_root=kwargs["output_dir"],
    )
    return updated.model_dump(mode="json")


def _historical_artifact_entries(result: dict[str, Any]) -> list[tuple[str, str, str]]:
    outputs = result.get("outputs") or {}
    entries: list[tuple[str, str, str]] = [
        ("historical_ndvi_result", str(outputs.get("result_json") or "result.json"), "application/json"),
        (
            "historical_ndvi_report",
            str(outputs.get("report_docx") or ""),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        ("historical_ndvi_trend", str(outputs.get("trend_chart_png") or ""), "image/png"),
    ]
    panels = outputs.get("yearly_panel_pngs") or {}
    for year, relative_path in sorted(panels.items(), key=lambda item: str(item[0])):
        entries.append((f"historical_ndvi_year_panel_{year}", str(relative_path), "image/png"))
    if any(not relative for _, relative, _ in entries):
        raise RuntimeError("历史 NDVI 必需附件登记不完整")
    return entries


def _analysis_result_response(claim_id: str, step: str, result: dict[str, Any]) -> dict[str, Any]:
    result = _authoritative_analysis_result(claim_id, step, result)
    task_id = str(result.get("task_id") or "")
    artifacts = _registered_analysis_artifacts(claim_id, step, task_id)
    public_artifacts = [
        {key: value for key, value in artifact.items() if key not in {"path", "relative_path"}}
        for artifact in artifacts
    ]
    return {**result, "artifacts": public_artifacts}


@app.post("/api/v1/tools/run_historical_ndvi_by_claim")
async def run_historical_ndvi_by_claim(req: HistoricalNDVIByClaimRequest) -> dict:
    """Generate a persisted, boundary-bound quarterly history from real GEE data."""
    claim_id = _safe_claim_file_id(req.claim_id)
    con = _db()
    row = con.execute(
        "SELECT c.state, c.policy_id, c.crop_type, p.holder_name, p.boundary_geojson, "
        "p.policy_version_id FROM cases c LEFT JOIN policies p ON c.policy_id = p.policy_id "
        "WHERE c.claim_id = ?",
        (claim_id,),
    ).fetchone()
    con.close()
    if not row:
        raise HTTPException(404, "案件不存在")
    if not row["boundary_geojson"]:
        raise HTTPException(409, "案件保单没有在册承保边界")
    if req.end_year < req.start_year or req.end_year - req.start_year + 1 > 20:
        raise HTTPException(422, "历史季度范围必须按升序且不超过 20 年")

    try:
        boundary = _normalize_geometry(json.loads(row["boundary_geojson"]))
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise HTTPException(409, "在册承保边界损坏") from exc
    boundary_sha256 = _geometry_sha256(boundary)
    as_of = req.as_of_date or date.today()
    if as_of > date.today():
        raise HTTPException(422, "历史 NDVI 截止日不能晚于服务器当前日期")
    if req.start_year > as_of.year or req.end_year > as_of.year:
        raise HTTPException(422, "历史 NDVI 年份范围不能晚于统计截止日所在年份")
    existing_pointer, historical_revision = _get_result_with_revision(
        claim_id, "historical_ndvi"
    )
    existing = (
        _authoritative_analysis_result(claim_id, "historical_ndvi", existing_pointer)
        if existing_pointer
        else {}
    )
    exact_existing = (
        existing.get("boundary_geometry_sha256") == boundary_sha256
        and existing.get("start_year") == req.start_year
        and existing.get("end_year") == req.end_year
        and existing.get("as_of_date") == as_of.isoformat()
        and float((existing.get("source") or {}).get("max_cloud_pct", -1)) == req.max_cloud_pct
        and int((existing.get("source") or {}).get("scale_m", -1)) == req.scale_m
        and _historical_cache_reusable(existing)
    )
    if exact_existing:
        return _analysis_result_response(claim_id, "historical_ndvi", existing)
    if row["state"] in _FROZEN_ANALYSIS_STATES:
        raise HTTPException(409, "报告已冻结，不能替换历史 NDVI 快照")

    task_id = f"history-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
    task_dir = _analysis_task_root(claim_id, "historical_ndvi", task_id)
    task_dir.mkdir(parents=True, exist_ok=False)
    boundary_path = task_dir / "boundary.geojson"
    boundary_path.write_text(_canonical_geometry_json(boundary), encoding="utf-8")
    scope_label = f"{row['holder_name'] or row['policy_id']} {row['crop_type'] or '承保作物'}地块"
    try:
        result = await asyncio.to_thread(
            _run_historical_ndvi_job,
            task_id=task_id,
            scope_label=scope_label,
            boundary_path=boundary_path,
            work_dir=task_dir,
            output_dir=task_dir,
            start_year=req.start_year,
            end_year=req.end_year,
            as_of_date=as_of,
            parcel_id=None,
            feature_id=None,
            boundary_geometry_sha256=boundary_sha256,
            source_crs=None,
            max_cloud_pct=req.max_cloud_pct,
            scale_m=req.scale_m,
            project_id=GEE_PROJECT_ID,
            download_maps=True,
        )
    except (ValueError, TypeError) as exc:
        _remove_failed_analysis_task(claim_id, "historical_ndvi", task_id, task_dir)
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        _remove_failed_analysis_task(claim_id, "historical_ndvi", task_id, task_dir)
        logger.exception("历史季度 NDVI 计算失败")
        raise HTTPException(502, "历史季度 NDVI 计算失败（HISTORICAL_NDVI_JOB_FAILED），未生成模拟结果") from exc
    if result.get("boundary_geometry_sha256") != boundary_sha256:
        _remove_failed_analysis_task(claim_id, "historical_ndvi", task_id, task_dir)
        raise HTTPException(409, "历史 NDVI 结果与当前在册边界哈希不一致")
    try:
        registered_artifacts = _register_analysis_artifacts(
            claim_id,
            "historical_ndvi",
            task_id,
            task_dir,
            _historical_artifact_entries(result),
        )
    except Exception:
        _remove_failed_analysis_task(claim_id, "historical_ndvi", task_id, task_dir)
        raise
    result_artifact = next(
        item for item in registered_artifacts if item["kind"] == "historical_ndvi_result"
    )
    result["_result_artifact_sha256"] = result_artifact["sha256"]
    result["_result_artifact_size_bytes"] = result_artifact["size_bytes"]
    _publish_analysis_result(
        claim_id,
        "historical_ndvi",
        result,
        boundary_sha256=boundary_sha256,
        expected_pointer_revision=historical_revision,
        enforce_pointer_revision=True,
    )
    _audit(
        claim_id,
        "run_historical_ndvi",
        "run_historical_ndvi_by_claim",
        req.model_dump(mode="json"),
        f"task={task_id}, boundary={boundary_sha256}, range={req.start_year}-{req.end_year}",
    )
    return _analysis_result_response(claim_id, "historical_ndvi", result)


@app.get("/api/v1/cases/{claim_id}/historical-ndvi")
async def get_historical_ndvi_by_claim(claim_id: str) -> dict:
    claim_id = _safe_claim_file_id(claim_id)
    result = _get_result(claim_id, "historical_ndvi")
    if not result:
        raise HTTPException(404, "案件尚未生成历史季度 NDVI")
    current = _policy_roi(_case_policy_id(claim_id) or "")
    if not current or result.get("boundary_geometry_sha256") != _geometry_sha256(current):
        raise HTTPException(409, "历史 NDVI 与当前在册边界哈希不一致")
    return _analysis_result_response(claim_id, "historical_ndvi", result)


def _confirmed_parcel_rows(policy_id: str, policy_version_id: str) -> list[sqlite3.Row]:
    con = _db()
    rows = con.execute(
        "SELECT * FROM parcel_confirmations WHERE policy_id = ? AND policy_version_id = ? "
        "ORDER BY parcel_id",
        (policy_id, policy_version_id),
    ).fetchall()
    con.close()
    return rows


def _confirmation_set_sha256(rows: list[sqlite3.Row]) -> str:
    evidence = [
        {
            "confirmation_id": row["confirmation_id"],
            "confirmation_batch_id": row["confirmation_batch_id"],
            "receipt_sha256": row["receipt_sha256"],
            "manifest_sha256": row["manifest_sha256"],
            "policy_id": row["policy_id"],
            "policy_version_id": row["policy_version_id"],
            "parcel_id": row["parcel_id"],
            "parcel_boundary_sha256": row["parcel_boundary_sha256"],
        }
        for row in rows
    ]
    return hashlib.sha256(
        json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _verify_confirmation_receipts(rows: list[sqlite3.Row]) -> None:
    batches = {
        str(row["confirmation_batch_id"]): str(row["receipt_sha256"])
        for row in rows
        if row["confirmation_batch_id"] and row["receipt_sha256"]
    }
    if rows and len(batches) != 1:
        raise HTTPException(409, "同一保单版本的地块确认批次不唯一或回执绑定缺失")
    con = _db()
    try:
        for batch_id, expected_hash in batches.items():
            batch = con.execute(
                "SELECT receipt_json, receipt_sha256 FROM parcel_confirmation_batches "
                "WHERE confirmation_batch_id = ?",
                (batch_id,),
            ).fetchone()
            if not batch:
                raise HTTPException(409, "地块确认回执记录缺失")
            receipt = json.loads(batch["receipt_json"] or "{}")
            claimed_hash = str(receipt.pop("receipt_sha256", ""))
            actual_hash = hashlib.sha256(
                json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                    "utf-8"
                )
            ).hexdigest()
            if not (
                secrets.compare_digest(expected_hash, actual_hash)
                and secrets.compare_digest(str(batch["receipt_sha256"]), actual_hash)
                and secrets.compare_digest(claimed_hash, actual_hash)
            ):
                raise HTTPException(409, "地块确认回执完整性校验失败")
    except (json.JSONDecodeError, TypeError) as exc:
        raise HTTPException(409, "地块确认回执损坏") from exc
    finally:
        con.close()


def _publish_analysis_result(
    claim_id: str,
    step: str,
    result: dict[str, Any],
    *,
    boundary_sha256: str,
    expected_growth_task_id: str | None = None,
    expected_growth_revision: str | None = None,
    expected_growth_raster_sha256: str | None = None,
    expected_confirmation_set_sha256: str | None = None,
    expected_pointer_revision: str | None = None,
    enforce_pointer_revision: bool = False,
) -> None:
    """Publish the latest pointer only if boundary, source revisions and state are unchanged."""
    con = _db()
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT c.state, c.policy_id, p.policy_version_id, p.boundary_geojson "
            "FROM cases c LEFT JOIN policies p ON c.policy_id = p.policy_id WHERE c.claim_id = ?",
            (claim_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "案件不存在")
        if row["state"] in _FROZEN_ANALYSIS_STATES:
            raise HTTPException(409, "报告已冻结，分析任务结果不得再发布到案件快照")
        current_boundary = _normalize_geometry(json.loads(row["boundary_geojson"] or "{}"))
        if not secrets.compare_digest(boundary_sha256, _geometry_sha256(current_boundary)):
            raise HTTPException(409, "分析期间在册承保边界发生变化，结果已拒绝发布")

        if enforce_pointer_revision:
            current_pointer = con.execute(
                "SELECT created_at FROM case_results WHERE claim_id = ? AND step = ?",
                (claim_id, step),
            ).fetchone()
            current_revision = current_pointer["created_at"] if current_pointer else None
            if current_revision != expected_pointer_revision:
                raise HTTPException(409, "并发分析已先发布新版本，较旧任务不得覆盖最新指针")

        if expected_growth_task_id is not None:
            growth_row = con.execute(
                "SELECT result_json, created_at FROM case_results WHERE claim_id = ? AND step = 'growth'",
                (claim_id,),
            ).fetchone()
            if not growth_row or growth_row["created_at"] != expected_growth_revision:
                raise HTTPException(409, "分析期间案件长势源版本发生变化，结果已拒绝发布")
            growth_payload = json.loads(growth_row["result_json"] or "{}")
            if growth_payload.get("task_id") != expected_growth_task_id:
                raise HTTPException(409, "分析期间案件长势任务发生变化，结果已拒绝发布")
            if expected_growth_raster_sha256 is not None:
                registered_digest = str(
                    ((growth_payload.get("artifact_integrity") or {}).get("ndvi_clip_tif") or {}).get(
                        "sha256"
                    )
                    or ""
                )
                raster_digest = str(
                    ((growth_payload.get("raster") or {}).get("ndvi_clip_sha256") or "")
                )
                if not (
                    secrets.compare_digest(registered_digest, expected_growth_raster_sha256)
                    and secrets.compare_digest(raster_digest, expected_growth_raster_sha256)
                ):
                    raise HTTPException(409, "案件长势栅格登记摘要在分地块分析期间发生变化")

        if expected_confirmation_set_sha256 is not None:
            version = row["policy_version_id"] or f"{row['policy_id']}:v1"
            confirmation_rows = con.execute(
                "SELECT * FROM parcel_confirmations WHERE policy_id = ? AND policy_version_id = ? "
                "ORDER BY parcel_id",
                (row["policy_id"], version),
            ).fetchall()
            if not secrets.compare_digest(
                expected_confirmation_set_sha256,
                _confirmation_set_sha256(confirmation_rows),
            ):
                raise HTTPException(409, "分析期间人工确认地块集合发生变化，结果已拒绝发布")

        con.execute(
            "INSERT OR REPLACE INTO case_results (claim_id, step, result_json, created_at) VALUES (?,?,?,?)",
            (
                claim_id,
                step,
                json.dumps(result, ensure_ascii=False, default=str),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _growth_ndvi_raster_source(growth: dict[str, Any]) -> Path:
    verified = _verified_growth_artifacts(growth)
    path = verified["ndvi_clip_tif"]
    expected = (OUTPUT_ROOT / "growth" / str(growth.get("task_id")) / "ndvi_clip.tif").resolve()
    if path.resolve() != expected:
        raise HTTPException(409, "长势 NDVI 栅格文件名或任务路径登记异常")
    return path


def _run_parcel_growth_job(raster_path: Path, manifest: dict[str, Any], class_breaks: list[float]) -> dict[str, Any]:
    from parcel_growth import analyze_parcel_growth

    return analyze_parcel_growth(
        raster_path=raster_path,
        manifest=manifest,
        class_breaks=class_breaks,
    )


def _assert_parcel_raster_binding(
    result: dict[str, Any],
    raster_path: Path,
    expected_sha256: str,
) -> None:
    analyzer_raster = result.get("raster") or {}
    actual_sha256 = _sha256_file(raster_path)
    if not (
        analyzer_raster.get("filename") == raster_path.name
        and re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
        and secrets.compare_digest(str(analyzer_raster.get("sha256") or ""), expected_sha256)
        and secrets.compare_digest(actual_sha256, expected_sha256)
    ):
        raise HTTPException(409, "分地块分析所读栅格与生成时绑定的 NDVI 摘要不一致")


@app.post("/api/v1/tools/run_parcel_growth_by_claim")
async def run_parcel_growth_by_claim(req: ParcelGrowthByClaimRequest) -> dict:
    """Aggregate the case NDVI raster by separately confirmed parcel and feature."""
    claim_id = _safe_claim_file_id(req.claim_id)
    con = _db()
    row = con.execute(
        "SELECT c.state, c.policy_id, p.policy_version_id, p.boundary_geojson, p.area_mu "
        "FROM cases c LEFT JOIN policies p ON c.policy_id = p.policy_id WHERE c.claim_id = ?",
        (claim_id,),
    ).fetchone()
    con.close()
    if not row:
        raise HTTPException(404, "案件不存在")
    if not row["boundary_geojson"]:
        raise HTTPException(409, "案件保单没有在册承保边界")
    policy_version_id = row["policy_version_id"] or f"{row['policy_id']}:v1"
    boundary = _normalize_geometry(json.loads(row["boundary_geojson"]))
    boundary_sha256 = _geometry_sha256(boundary)
    growth = _get_result(claim_id, "growth") or {}
    if not growth:
        raise HTTPException(409, "请先完成案件 NDVI 长势分析")
    growth_meta = ((growth.get("raster") or {}).get("ndvi_meta") or {})
    if growth_meta.get("boundary_sha256") != boundary_sha256:
        raise HTTPException(409, "长势栅格与当前在册边界哈希不一致")
    if (growth.get("raster") or {}).get("ndvi_source") == "synthetic" or growth_meta.get("source") == "synthetic":
        raise HTTPException(409, "模拟 NDVI 不得生成分地块权威统计")
    raster_path = _growth_ndvi_raster_source(growth)
    source_ndvi_sha256 = _sha256_file(raster_path)
    class_breaks = [float(value) for value in (growth.get("class_breaks") or [])]
    if len(class_breaks) < 2 or any(not math.isfinite(value) for value in class_breaks):
        raise HTTPException(409, "长势结果缺少有效分级阈值")
    class_breaks_sha256 = hashlib.sha256(
        json.dumps(class_breaks, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    from parcel_growth import (
        CLASSIFICATION_SCHEME_VERSION,
        PARCEL_GROWTH_ALGORITHM_VERSION,
        PARCEL_GROWTH_SCHEMA_VERSION,
    )

    con = _db()
    growth_revision_row = con.execute(
        "SELECT created_at FROM case_results WHERE claim_id = ? AND step = 'growth'",
        (claim_id,),
    ).fetchone()
    con.close()
    growth_revision = growth_revision_row["created_at"] if growth_revision_row else None

    confirmed = _confirmed_parcel_rows(row["policy_id"], policy_version_id)
    if not confirmed:
        raise HTTPException(409, "当前保单版本没有经人工确认的分地块映射")
    _verify_confirmation_receipts(confirmed)
    confirmation_set_sha256 = _confirmation_set_sha256(confirmed)
    existing_pointer, parcel_growth_revision = _get_result_with_revision(
        claim_id, "parcel_growth"
    )
    existing = (
        _authoritative_analysis_result(claim_id, "parcel_growth", existing_pointer)
        if existing_pointer
        else {}
    )
    if (
        existing.get("boundary_sha256") == boundary_sha256
        and existing.get("source_growth_task_id") == growth.get("task_id")
        and existing.get("confirmation_set_sha256") == confirmation_set_sha256
        and existing.get("schema_version") == PARCEL_GROWTH_SCHEMA_VERSION
        and existing.get("algorithm_version") == PARCEL_GROWTH_ALGORITHM_VERSION
        and existing.get("classification_scheme_version") == CLASSIFICATION_SCHEME_VERSION
        and (existing.get("raster") or {}).get("source_ndvi_sha256") == source_ndvi_sha256
        and (existing.get("raster") or {}).get("sha256") == source_ndvi_sha256
        and existing.get("class_breaks_sha256") == class_breaks_sha256
    ):
        return _analysis_result_response(claim_id, "parcel_growth", existing)
    if row["state"] in _FROZEN_ANALYSIS_STATES:
        raise HTTPException(409, "报告已冻结，不能替换分地块长势快照")
    parcels: list[dict[str, Any]] = []
    confirmation_provenance: list[dict[str, str]] = []
    for confirmed_row in confirmed:
        parcel = json.loads(confirmed_row["parcel_json"])
        mapping = json.loads(confirmed_row["mapping_json"])
        parcel["parcel_id"] = confirmed_row["parcel_id"]
        parcel["mapping"] = mapping
        parcels.append(parcel)
        confirmation_provenance.append(
            {
                "confirmation_id": confirmed_row["confirmation_id"],
                "confirmation_batch_id": confirmed_row["confirmation_batch_id"],
                "confirmation_receipt_sha256": confirmed_row["receipt_sha256"],
                "confirmation_receipt_download_url": (
                    f"/api/v1/policies/parcels/preflights/{confirmed_row['preflight_id']}/"
                    f"confirmation-receipts/{confirmed_row['confirmation_batch_id']}"
                ),
                "preflight_id": confirmed_row["preflight_id"],
                "manifest_sha256": confirmed_row["manifest_sha256"],
                "policy_id": confirmed_row["policy_id"],
                "policy_version_id": confirmed_row["policy_version_id"],
                "parcel_id": confirmed_row["parcel_id"],
                "parcel_boundary_sha256": confirmed_row["parcel_boundary_sha256"],
                "confirmed_by": confirmed_row["confirmed_by"],
                "confirmed_at": confirmed_row["confirmed_at"],
            }
        )
    manifest_digest = hashlib.sha256(
        json.dumps(confirmation_provenance, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    manifest = {
        "schema_version": "parcel_preflight_v1",
        "algorithm_version": "multi_kml_preflight_v1",
        "id_scheme": "source_path_geometry_v1",
        "geometry_hash_scheme": "canonical_geometry_v1",
        "preflight_status": "human_confirmed",
        "manifest_sha256": manifest_digest,
        "source_crs": "EPSG:4326",
        "total_area_mu": row["area_mu"],
        "parcels": parcels,
    }
    task_id = f"parcel-growth-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
    task_dir = _analysis_task_root(claim_id, "parcel_growth", task_id)
    task_dir.mkdir(parents=True, exist_ok=False)
    try:
        result = await asyncio.to_thread(_run_parcel_growth_job, raster_path, manifest, class_breaks)
    except Exception as exc:
        _remove_failed_analysis_task(claim_id, "parcel_growth", task_id, task_dir)
        logger.exception("分地块长势分析失败")
        raise HTTPException(500, f"分地块长势分析失败: {exc}") from exc
    try:
        _assert_parcel_raster_binding(result, raster_path, source_ndvi_sha256)
    except HTTPException:
        _remove_failed_analysis_task(claim_id, "parcel_growth", task_id, task_dir)
        raise
    if result.get("boundary_sha256") != boundary_sha256:
        _remove_failed_analysis_task(claim_id, "parcel_growth", task_id, task_dir)
        raise HTTPException(409, "分地块集合与当前在册边界哈希不一致")
    result.update(
        {
            "task_id": task_id,
            "claim_id": claim_id,
            "policy_id": row["policy_id"],
            "policy_version_id": policy_version_id,
            "source_growth_task_id": growth.get("task_id"),
            "confirmation_provenance": confirmation_provenance,
            "confirmation_set_sha256": confirmation_set_sha256,
            "class_breaks_sha256": class_breaks_sha256,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    result["raster"] = {
        key: value for key, value in (result.get("raster") or {}).items() if key != "path"
    }
    result["raster"].update(
        {
            "source_growth_task_id": growth.get("task_id"),
            "source_ndvi_sha256": source_ndvi_sha256,
        }
    )
    output_path = task_dir / "parcel_growth.json"
    payload_bytes = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    temporary = task_dir / f".parcel_growth.{uuid.uuid4().hex}.tmp"
    temporary.write_bytes(payload_bytes)
    temporary.replace(output_path)
    try:
        registered_artifacts = _register_analysis_artifacts(
            claim_id,
            "parcel_growth",
            task_id,
            task_dir,
            [("parcel_growth_result", "parcel_growth.json", "application/json")],
        )
    except Exception:
        _remove_failed_analysis_task(claim_id, "parcel_growth", task_id, task_dir)
        raise
    result_artifact = next(
        item for item in registered_artifacts if item["kind"] == "parcel_growth_result"
    )
    result["_result_artifact_sha256"] = result_artifact["sha256"]
    result["_result_artifact_size_bytes"] = result_artifact["size_bytes"]
    if not secrets.compare_digest(_sha256_file(raster_path), source_ndvi_sha256):
        raise HTTPException(409, "分地块结果登记后源 NDVI 栅格摘要发生变化，拒绝发布指针")
    _publish_analysis_result(
        claim_id,
        "parcel_growth",
        result,
        boundary_sha256=boundary_sha256,
        expected_growth_task_id=str(growth.get("task_id") or ""),
        expected_growth_revision=growth_revision,
        expected_growth_raster_sha256=source_ndvi_sha256,
        expected_confirmation_set_sha256=confirmation_set_sha256,
        expected_pointer_revision=parcel_growth_revision,
        enforce_pointer_revision=True,
    )
    _audit(
        claim_id,
        "run_parcel_growth",
        "run_parcel_growth_by_claim",
        {"policy_id": row["policy_id"], "policy_version_id": policy_version_id},
        f"task={task_id}, parcels={len(result.get('parcels') or [])}, boundary={boundary_sha256}",
    )
    return _analysis_result_response(claim_id, "parcel_growth", result)


@app.get("/api/v1/cases/{claim_id}/parcel-growth")
async def get_parcel_growth_by_claim(claim_id: str) -> dict:
    claim_id = _safe_claim_file_id(claim_id)
    result = _get_result(claim_id, "parcel_growth")
    if not result:
        raise HTTPException(404, "案件尚未生成分地块长势结果")
    current = _policy_roi(_case_policy_id(claim_id) or "")
    if not current or result.get("boundary_sha256") != _geometry_sha256(current):
        raise HTTPException(409, "分地块长势与当前在册边界哈希不一致")
    authoritative = _authoritative_analysis_result(claim_id, "parcel_growth", result)
    growth = _get_result(claim_id, "growth") or {}
    verified_growth = _verified_growth_artifacts(growth)
    parcel_digest = str((authoritative.get("raster") or {}).get("sha256") or "")
    if not (
        authoritative.get("source_growth_task_id") == growth.get("task_id")
        and secrets.compare_digest(
            parcel_digest,
            str((authoritative.get("raster") or {}).get("source_ndvi_sha256") or ""),
        )
        and secrets.compare_digest(
            parcel_digest,
            str(
                ((growth.get("artifact_integrity") or {}).get("ndvi_clip_tif") or {}).get(
                    "sha256"
                )
                or ""
            ),
        )
        and verified_growth.get("ndvi_clip_tif") is not None
    ):
        raise HTTPException(409, "分地块长势与当前长势源栅格摘要不一致")
    return _analysis_result_response(claim_id, "parcel_growth", result)


# ═══════════════════════════════════════════════════════════
# 8c. run_loss_assessment — 多源遥感灾损评估（减产率，替代单期 NDVI）
# ═══════════════════════════════════════════════════════════

# 早期状态（尚未完成卫星初筛）不允许做灾损评估
_PRE_SCREENING_STATES = {"INIT", "MATERIAL_CHECK", "PREPROCESS_READY"}


@app.post("/api/v1/tools/run_loss_assessment", response_model=LossAssessmentResult)
def run_loss_assessment(req: LossAssessmentRequest) -> LossAssessmentResult:
    """多源遥感灾损评估：按灾种加权融合 NDVI/EVI2/NDWI/NDRE + MODIS LAI/GPP/NPP，
    产出减产率 + 置信度 + 各产品贡献。只允许在 SCREENING_DONE/NDVI_DONE 阶段首次生成；
    进入合规、评级、报告或归档阶段后结果冻结。

    这是对"单期 NDVI 分级"的替代；结果持久化为服务端权威副本，供合规与报告引用。
    """
    con = _db()
    row = con.execute("SELECT state, policy_id, disaster_type, loss_date FROM cases WHERE claim_id = ?", (req.claim_id,)).fetchone()
    con.close()
    if not row:
        raise HTTPException(404, f"案件不存在: {req.claim_id}")
    state = row["state"]
    if state in _PRE_SCREENING_STATES:
        raise HTTPException(400, f"当前状态 {row['state']} 不允许灾损评估，需先完成卫星初筛")
    existing = _get_result(req.claim_id, "loss_assessment")
    if existing:
        return LossAssessmentResult(**existing)
    if state not in {"SCREENING_DONE", "NDVI_DONE"}:
        raise HTTPException(409, f"当前状态 {state} 已冻结灾损评估；缺失结果需走管理员修复流程")

    disaster_type = row["disaster_type"] or "other"
    roi_geojson = _policy_roi(row["policy_id"])
    if not roi_geojson:
        raise HTTPException(400, "保单未登记有效地块边界，无法进行定损")

    # 灾前/灾后窗口：请求未提供时从灾害日期推导，给 GEE 留出找无云影像的余量。
    # 关键：没有窗口则 compute_product 会回退合成——前端不传日期，故必须在此兜底。
    pre_start, pre_end = req.pre_start, req.pre_end
    post_start, post_end = req.post_start, req.post_end
    if not (pre_start and post_start):
        from datetime import date as _date, timedelta
        try:
            ld = _date.fromisoformat(str(row["loss_date"])[:10])
        except (TypeError, ValueError):
            ld = None
        if ld:
            pre_start = pre_start or (ld - timedelta(days=60)).isoformat()
            pre_end = pre_end or (ld - timedelta(days=15)).isoformat()
            post_start = post_start or (ld - timedelta(days=5)).isoformat()
            post_end = post_end or (ld + timedelta(days=30)).isoformat()

    expected_window = {
        "pre": [pre_start, pre_end],
        "post": [post_start, post_end],
    }
    cached = _matching_cached_result(
        req.claim_id,
        "loss_assessment",
        window=expected_window,
    )
    if cached:
        source_claim_id, assessment = cached
        _save_result(req.claim_id, "loss_assessment", assessment)
        _audit(
            req.claim_id,
            "run_loss_assessment",
            "run_loss_assessment",
            {"disaster_type": disaster_type, "products": req.products},
            f"cache_hit={source_claim_id}, yield_loss={assessment.get('yield_loss_ratio')}",
        )
        return LossAssessmentResult(**assessment)

    try:
        from loss_engine import assess_yield_loss

        assessment = assess_yield_loss(
            roi_geojson,
            disaster_type,
            pre_start=pre_start,
            pre_end=pre_end,
            post_start=post_start,
            post_end=post_end,
            project_id=GEE_PROJECT_ID,
            products=req.products,
        )
    except Exception as e:
        logger.exception("灾损评估失败")
        raise HTTPException(500, f"灾损评估失败: {e}") from e

    assessment["boundary_sha256"] = _geometry_sha256(roi_geojson)
    assessment["boundary_hash_scheme"] = "canonical_geometry_v1"
    _save_result(req.claim_id, "loss_assessment", assessment)
    _audit(
        req.claim_id, "run_loss_assessment", "run_loss_assessment",
        {"disaster_type": disaster_type, "products": req.products},
        f"yield_loss={assessment['yield_loss_ratio']}, conf={assessment['confidence']}, src={assessment['data_sources']}",
    )

    return LossAssessmentResult(**assessment)


@app.post("/api/v1/tools/run_compliance_estimate", response_model=ComplianceCalcResult)
def run_compliance_estimate(req: ComplianceEstimateRequest) -> ComplianceCalcResult:
    """免上传合规核验：保单在册面积 × 服务端已持久化受损比例。
    供对话智能体使用，免文件上传。状态 SCREENING_DONE/NDVI_DONE → COMPLIANCE_DONE。"""
    con = _db()
    row = con.execute("SELECT state, policy_id FROM cases WHERE claim_id = ?", (req.claim_id,)).fetchone()
    con.close()
    if not row:
        raise HTTPException(404, f"案件不存在: {req.claim_id}")
    st = row["state"]
    if st in ("COMPLIANCE_DONE", "RULE_DONE", "REPORT_DRAFTED", "HUMAN_REVIEW", "ARCHIVED"):
        existing = _get_result(req.claim_id, "compliance")
        if existing:
            return ComplianceCalcResult(**existing)
        raise HTTPException(409, "案件状态显示合规已完成，但权威结果缺失")
    if st not in ("SCREENING_DONE", "NDVI_DONE"):
        raise HTTPException(400, f"当前状态 {st} 不允许合规核验")

    import geopandas as gpd
    from shapely.geometry import shape
    roi_geojson = _policy_roi(row["policy_id"])
    if not roi_geojson:
        raise HTTPException(400, "保单未登记有效地块边界，无法进行合规核验")
    geom = shape(roi_geojson)
    gdf = gpd.GeoDataFrame(geometry=[geom], crs="EPSG:4326")
    utm = gdf.estimate_utm_crs() or "EPSG:6933"
    area_sqm = float(gdf.to_crs(utm).geometry.area.iloc[0])
    insured_mu = round(area_sqm * 0.0015, 2)

    damage_area_ratio, ratio_source = _authoritative_damage_area_ratio(req.claim_id)
    if damage_area_ratio is None:
        raise HTTPException(409, "缺少服务端灾损评估或卫星初筛结果，无法进行合规核验")

    policy_con = _db()
    try:
        policy_row = policy_con.execute(
            "SELECT * FROM policies WHERE policy_id = ?", (row["policy_id"],)
        ).fetchone()
    finally:
        policy_con.close()
    if not policy_row:
        raise HTTPException(409, "案件关联保单不存在")
    case_con = _db()
    try:
        case_terms = case_con.execute(
            "SELECT crop_type, disaster_type, loss_date FROM cases WHERE claim_id = ?",
            (req.claim_id,),
        ).fetchone()
    finally:
        case_con.close()
    contract = _ensure_case_contract(
        req.claim_id, dict(policy_row), str(case_terms["loss_date"] or "")
    )
    eligibility = evaluate_contract_eligibility(
        contract,
        crop_type=str(case_terms["crop_type"] or ""),
        disaster_type=str(case_terms["disaster_type"] or ""),
        loss_date=str(case_terms["loss_date"] or ""),
    )
    if not eligibility["eligible"]:
        failed = "; ".join(
            item["name"] for item in eligibility["checks"] if not item["passed"]
        )
        raise HTTPException(409, f"合同条款校验未通过：{failed}")

    valid_mu = round(insured_mu * damage_area_ratio, 2)
    unaffected_mu = round(insured_mu * (1 - damage_area_ratio), 2)
    result = ComplianceCalcResult(
        status="success", valid_damage_area_mu=valid_mu, damage_ratio=round(damage_area_ratio, 4),
        excluded_area_mu=unaffected_mu, insured_area_mu=insured_mu,
        contract_id=contract.get("contract_id"),
        contract_version=contract.get("contract_version"),
        contract_checks=eligibility["checks"],
        clip_log=[
            f"承保面积: {insured_mu} 亩（由边界框计算）",
            f"受灾面积比例来源: {ratio_source} = {damage_area_ratio * 100:.1f}%",
            f"合同 C6 核定受灾面积 = {insured_mu} × {damage_area_ratio * 100:.1f}% = {valid_mu} 亩",
            f"合同 {contract.get('contract_number')} / {contract.get('contract_version')} 已通过条款校验",
        ],
        boundary_sha256=_geometry_sha256(roi_geojson),
        boundary_hash_scheme="canonical_geometry_v1",
    )
    _advance_state_with_result(
        req.claim_id, S.COMPLIANCE_DONE, "compliance", result.model_dump(mode="json")
    )
    _audit(req.claim_id, "run_compliance_calc", "run_compliance_estimate",
           {"claim_id": req.claim_id, "ratio": damage_area_ratio, "contract_id": contract.get("contract_id")}, f"valid={valid_mu}mu")
    return result


# ═══════════════════════════════════════════════════════════
# 5b. JSON 方式合规核验（原有接口）
# ═══════════════════════════════════════════════════════════

@app.post("/api/v1/tools/run_compliance_calc", response_model=ComplianceCalcResult)
def run_compliance_calc(req: ComplianceCalcRequest) -> ComplianceCalcResult:
    """拒绝把未经审核的客户端受灾几何写入权威赔付链。"""
    case = _require_state(req.claim_id, S.SCREENING_DONE)

    if case['state'] not in (CaseState.SCREENING_DONE, CaseState.NDVI_DONE):
        raise HTTPException(400, f"当前状态 {case['state']} 不允许调用合规核验")
    raise HTTPException(
        409,
        "客户端受灾几何仅可作为待审核材料，不能直接写入权威合规/赔付结果；"
        "请使用 run_compliance_estimate 读取服务端遥感灾损证据",
    )


# ═══════════════════════════════════════════════════════════
# 5c. run_payout_estimate — 赔付测算（减产率 → 预估赔款）
# ═══════════════════════════════════════════════════════════

def _compute_and_store_payout(
    claim_id: str,
    *,
    allowed_states: set[str] | None = None,
) -> dict:
    """Compute and publish payout under one state/compliance write transaction."""
    allowed = allowed_states or {S.COMPLIANCE_DONE.value, S.RULE_DONE.value}
    if not _load_case_contract(claim_id):
        bootstrap = _db()
        try:
            policy_row = bootstrap.execute(
                "SELECT p.*, c.loss_date FROM cases c JOIN policies p ON p.policy_id = c.policy_id "
                "WHERE c.claim_id = ?",
                (claim_id,),
            ).fetchone()
        finally:
            bootstrap.close()
        if policy_row:
            _ensure_case_contract(claim_id, dict(policy_row), str(policy_row["loss_date"] or ""))
    con = _db()
    try:
        con.execute("BEGIN IMMEDIATE")
        case_row = con.execute(
            "SELECT state, crop_type, policy_id, loss_date FROM cases WHERE claim_id = ?", (claim_id,)
        ).fetchone()
        if not case_row:
            raise HTTPException(404, f"案件不存在: {claim_id}")
        if case_row["state"] in {
            S.REPORT_DRAFTED.value,
            S.HUMAN_REVIEW.value,
            S.ARCHIVED.value,
        }:
            raise HTTPException(409, "报告已冻结，赔付结果不得重新计算或覆盖")
        if case_row["state"] not in allowed:
            raise HTTPException(409, f"当前状态 {case_row['state']} 不允许写入赔付结果")
        compliance_row = con.execute(
            "SELECT result_json FROM case_results WHERE claim_id = ? AND step = 'compliance'",
            (claim_id,),
        ).fetchone()
        if not compliance_row:
            raise HTTPException(409, "缺少合规核验结果，无法测算赔款")
        try:
            compliance = json.loads(compliance_row["result_json"] or "{}")
        except (json.JSONDecodeError, TypeError) as exc:
            raise HTTPException(409, "合规核验结果损坏，无法测算赔款") from exc
        insured_area_mu = compliance.get("insured_area_mu")
        affected_area_mu = compliance.get("valid_damage_area_mu")
        if not _valid_nonnegative_number(insured_area_mu) or not _valid_nonnegative_number(affected_area_mu):
            raise HTTPException(409, "合规面积或核定受灾面积无效，无法测算赔款")
        loss_row = con.execute(
            "SELECT result_json FROM case_results WHERE claim_id = ? AND step = 'loss_assessment'",
            (claim_id,),
        ).fetchone()
        try:
            loss_assessment = json.loads(loss_row["result_json"] or "{}") if loss_row else {}
        except (json.JSONDecodeError, TypeError):
            loss_assessment = {}
        yield_loss_ratio = loss_assessment.get("yield_loss_ratio")
        if not _valid_ratio(yield_loss_ratio):
            yield_loss_ratio = compliance.get("damage_ratio")
        if not _valid_ratio(yield_loss_ratio):
            raise HTTPException(409, "缺少有效减产率，无法依据合同计算赔款")
        contract_row = con.execute(
            "SELECT pc.contract_json FROM case_contract_bindings cb "
            "JOIN policy_contracts pc ON pc.policy_id = cb.policy_id "
            "AND pc.policy_version_id = cb.policy_version_id "
            "WHERE cb.claim_id = ?",
            (claim_id,),
        ).fetchone()
        if not contract_row:
            raise HTTPException(409, "案件关联保单缺少冻结的保险合同版本")
        try:
            contract = json.loads(contract_row["contract_json"] or "{}")
        except (json.JSONDecodeError, TypeError) as exc:
            raise HTTPException(409, "保险合同版本损坏，无法计算赔款") from exc
        payout = estimate_contract_payout(
            contract,
            loss_date=str(case_row["loss_date"] or ""),
            loss_ratio=float(yield_loss_ratio),
            affected_area_mu=float(affected_area_mu),
        )
        con.execute(
            "INSERT OR REPLACE INTO case_results (claim_id, step, result_json, created_at) "
            "VALUES (?,?,?,?)",
            (
                claim_id,
                "payout",
                json.dumps(payout, ensure_ascii=False, default=str),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        con.commit()
        return payout
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


@app.post("/api/v1/tools/run_payout_estimate", response_model=PayoutResult)
def run_payout_estimate(req: PayoutEstimateRequest) -> PayoutResult:
    """根据合规面积与减产率测算预估赔款。状态需 COMPLIANCE_DONE 及以后。"""
    payout = _compute_and_store_payout(
        req.claim_id,
        allowed_states={S.COMPLIANCE_DONE.value, S.RULE_DONE.value},
    )

    _audit(req.claim_id, "run_payout_estimate", "run_payout_estimate",
           {"claim_id": req.claim_id}, f"payout={payout['payout_amount_yuan']}元, factor={payout['payout_factor']}")
    return PayoutResult(**payout)


# ═══════════════════════════════════════════════════════════
# 6. run_rule_engine — 规则判断
# ═══════════════════════════════════════════════════════════

RISK_RULES = load_risk_rules()
RISK_THRESHOLDS = RISK_RULES["thresholds"]
RISK_RULE_VERSION = str(RISK_RULES["version"])
HIGH_PAYOUT_THRESHOLD_YUAN = float(RISK_RULES["high_payout_threshold_yuan"])


def evaluate_risk(
    damage_ratio: float,
    crop_type: str,
    disaster_type: str = "other",
    estimated_payout_yuan: float = 0.0,
    threshold_set: str = "default_v1",
) -> RuleEngineResult:
    """根据受损比例 + 灾害类型 + 预估金额，判定风险等级。

    阈值按灾害类型（flood/drought/...）取，而非作物类型——
    RISK_THRESHOLDS 的键就是灾害类型。
    """
    thresholds = RISK_THRESHOLDS.get(disaster_type, RISK_THRESHOLDS["other"])
    rule_trace = [
        f"threshold_set={threshold_set}",
        f"damage_ratio={damage_ratio}",
        f"disaster_type={disaster_type}",
        f"crop_type={crop_type}",
    ]

    # 基础等级
    if damage_ratio >= thresholds["high"]:
        risk_level = RiskLevel.HIGH
        rule_trace.append(f"受损比例 >= {thresholds['high']} → 高风险")
    elif damage_ratio >= thresholds["medium"]:
        risk_level = RiskLevel.MEDIUM
        rule_trace.append(f"受损比例 >= {thresholds['medium']} → 中风险")
    else:
        risk_level = RiskLevel.LOW
        rule_trace.append("受损比例低于中风险阈值 → 低风险")

    # 大额赔款：触发强制人工/财务复核，但【不】改变以受损严重度为准的风险等级。
    # （此前是直接顶成高风险，导致几乎所有案件——农险赔款普遍超阈值——都成高风险。）
    large_payout = estimated_payout_yuan >= HIGH_PAYOUT_THRESHOLD_YUAN
    if large_payout:
        rule_trace.append(
            f"预估赔款 {estimated_payout_yuan:,.0f} >= {HIGH_PAYOUT_THRESHOLD_YUAN:,.0f} 元 → 大额案件，"
            f"强制人工/财务复核（风险等级仍以受损严重度为准）"
        )

    # 复核条件：受损严重度为高，或属于大额赔款
    review_required = risk_level == RiskLevel.HIGH or large_payout

    return RuleEngineResult(
        risk_level=risk_level,
        review_required=review_required,
        rule_trace=rule_trace,
        rule_version=RISK_RULE_VERSION,
    )


@app.post("/api/v1/tools/run_rule_engine", response_model=RuleEngineResult)
def run_rule_engine(req: RuleEngineRequest) -> RuleEngineResult:
    """规则引擎。状态: COMPLIANCE_DONE。"""
    con = _db()
    row = con.execute(
        "SELECT state, disaster_type, crop_type FROM cases WHERE claim_id = ?", (req.claim_id,)
    ).fetchone()
    con.close()
    if not row: raise HTTPException(404, "案件不存在")
    st = row["state"]
    # 幂等
    if st in ("RULE_DONE", "REPORT_DRAFTED", "HUMAN_REVIEW", "ARCHIVED"):
        existing = _get_result(req.claim_id, "rule")
        if existing:
            return RuleEngineResult(**existing)
        raise HTTPException(409, "案件状态显示规则判断已完成，但权威结果缺失")
    if st != "COMPLIANCE_DONE":
        raise HTTPException(400, f"当前状态 {st} 不允许规则判断")

    # 预估赔款用于"大额预警"：优先已持久化的赔付测算，否则用合规结果现算
    payout = _get_result(req.claim_id, "payout") or _compute_and_store_payout(req.claim_id)
    estimated_payout = float(payout["payout_amount_yuan"]) if payout else 0.0

    compliance = _get_result(req.claim_id, "compliance")
    damage_ratio = compliance.get("damage_ratio") if compliance else None
    if not isinstance(damage_ratio, (int, float)) or not 0.0 <= float(damage_ratio) <= 1.0:
        raise HTTPException(409, "缺少有效的服务端合规受损比例，无法执行规则判断")

    result = evaluate_risk(
        damage_ratio=float(damage_ratio),
        crop_type=row["crop_type"] or "other",
        disaster_type=row["disaster_type"] or "other",
        estimated_payout_yuan=estimated_payout,
        threshold_set=RISK_RULE_VERSION,
    )

    _advance_state_with_result(
        req.claim_id, S.RULE_DONE, "rule", result.model_dump(mode="json")
    )

    _audit(req.claim_id, "run_rule_engine", "run_rule_engine",
           {**req.model_dump(mode="json"), "authoritative_damage_ratio": float(damage_ratio),
            "authoritative_crop_type": row["crop_type"], "estimated_payout_yuan": estimated_payout},
           f"risk={result.risk_level.value}, review={result.review_required}, payout={estimated_payout}")

    return result


# ═══════════════════════════════════════════════════════════
# 7. generate_report — 报告生成
# ═══════════════════════════════════════════════════════════

def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _output_url(path: Path) -> str:
    resolved = path.resolve()
    root = OUTPUT_ROOT.resolve()
    if not resolved.is_relative_to(root):
        raise RuntimeError("报告产物路径越出输出目录")
    return f"/outputs/{resolved.relative_to(root).as_posix()}"


def _atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
    try:
        shutil.copy2(source, temporary)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def _safe_remove_report_staging(path: Path) -> None:
    reports_root = (OUTPUT_ROOT / "reports").resolve()
    resolved = path.resolve()
    if resolved.is_relative_to(reports_root) and path.name.startswith(".") and path.is_dir():
        shutil.rmtree(path, ignore_errors=True)


def _artifact_metadata(kind: str, path: Path, media_type: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "filename": path.name,
        "url": _output_url(path),
        "media_type": media_type,
        "size_bytes": path.stat().st_size,
        "sha256": _sha256_file(path),
    }


def _report_snapshot_sha256(claim_id: str, data: dict, template_version: str) -> str:
    case = data.get("case") or {}
    contract = data.get("contract") or {}
    compliance_basis = {
        "rows": report_basis_rows(contract, str(case.get("crop_type") or "")),
        "references": reference_index(contract, str(case.get("crop_type") or "")),
    }
    canonical = json.dumps(
        {
            "claim_id": claim_id,
            "template_version": template_version,
            "data": data,
            "compliance_basis": compliance_basis,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _valid_ratio(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and 0 <= float(value) <= 1
    )


def _valid_nonnegative_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) >= 0
    )


def _validate_report_snapshot(data: dict) -> None:
    """Reject incomplete legacy/corrupt cases instead of rendering missing values as zero/low."""
    satellite = data.get("satellite") or {}
    compliance = data.get("compliance") or {}
    rule = data.get("rule") or {}
    payout = data.get("payout") or {}
    contract = data.get("contract") or {}
    case = data.get("case") or {}
    missing: list[str] = []
    current_boundary_sha256 = case.get("policy_boundary_sha256")
    if not re.fullmatch(r"[0-9a-f]{64}", str(current_boundary_sha256 or "")):
        missing.append("有效在册承保边界")
    if not satellite or not _valid_ratio(satellite.get("damage_ratio")):
        missing.append("有效卫星初筛")
    if not compliance or not _valid_ratio(compliance.get("damage_ratio")):
        missing.append("有效合规核验")
    if not rule or rule.get("risk_level") not in {"low", "medium", "high"}:
        missing.append("有效规则评级")
    if not payout or not _valid_nonnegative_number(payout.get("payout_amount_yuan")):
        missing.append("有效赔付测算")
    if not re.fullmatch(r"[0-9a-f]{64}", str(contract.get("contract_sha256") or "")):
        missing.append("冻结的保险合同版本")
    if missing:
        raise HTTPException(409, f"报告数据快照不完整：{'、'.join(missing)}；请先修复权威结果")
    if payout.get("contract_sha256") != contract.get("contract_sha256"):
        raise HTTPException(409, "赔款测算引用的合同版本与保单冻结合同不一致")

    provenance: list[tuple[str, dict]] = [
        ("卫星初筛", satellite),
        ("合规核验", compliance),
    ]
    if data.get("loss_assessment"):
        provenance.append(("多源灾损评估", data["loss_assessment"]))
    growth_meta = (((data.get("growth") or {}).get("raster") or {}).get("ndvi_meta") or {})
    if data.get("growth"):
        provenance.append(("NDVI 长势分析", growth_meta))
    mismatches = [
        label
        for label, payload in provenance
        if payload.get("boundary_hash_scheme") != "canonical_geometry_v1"
        or payload.get("boundary_sha256") != current_boundary_sha256
    ]
    if mismatches:
        raise HTTPException(
            409,
            f"分析结果与当前在册承保边界哈希不一致：{'、'.join(mismatches)}；禁止混合版本生成报告",
        )
    verified_growth = (
        _verified_growth_artifacts(data["growth"]) if data.get("growth") else {}
    )
    historical = data.get("historical_ndvi") or {}
    if historical:
        if historical.get("boundary_geometry_sha256") != current_boundary_sha256:
            raise HTTPException(409, "历史季度 NDVI 与当前在册承保边界哈希不一致")
        if not re.fullmatch(r"[0-9a-f]{64}", str(historical.get("_result_artifact_sha256") or "")):
            raise HTTPException(409, "历史季度 NDVI 未绑定不可变 JSON 摘要")
    parcel_growth = data.get("parcel_growth") or {}
    if parcel_growth:
        if parcel_growth.get("boundary_sha256") != current_boundary_sha256:
            raise HTTPException(409, "分地块长势结果与当前在册承保边界哈希不一致")
        if parcel_growth.get("source_growth_task_id") != (data.get("growth") or {}).get("task_id"):
            raise HTTPException(409, "分地块长势与当前案件长势栅格任务不一致")
        source_digest = str((parcel_growth.get("raster") or {}).get("source_ndvi_sha256") or "")
        analyzer_digest = str((parcel_growth.get("raster") or {}).get("sha256") or "")
        growth_digest = str(
            (((data.get("growth") or {}).get("artifact_integrity") or {}).get("ndvi_clip_tif") or {}).get(
                "sha256"
            )
            or ""
        )
        if not (
            re.fullmatch(r"[0-9a-f]{64}", source_digest)
            and secrets.compare_digest(source_digest, analyzer_digest)
            and secrets.compare_digest(source_digest, growth_digest)
            and verified_growth.get("ndvi_clip_tif") is not None
        ):
            raise HTTPException(409, "分地块长势未绑定当前案件长势源栅格摘要")
        if not re.fullmatch(r"[0-9a-f]{64}", str(parcel_growth.get("_result_artifact_sha256") or "")):
            raise HTTPException(409, "分地块长势未绑定不可变 JSON 摘要")
        policy_id = str(case.get("policy_id") or "")
        policy_version_id = str(case.get("policy_version_id") or f"{policy_id}:v1")
        confirmation_rows = _confirmed_parcel_rows(policy_id, policy_version_id)
        _verify_confirmation_receipts(confirmation_rows)
        current_confirmation_digest = _confirmation_set_sha256(confirmation_rows)
        if parcel_growth.get("confirmation_set_sha256") != current_confirmation_digest:
            raise HTTPException(409, "分地块长势绑定的人工确认集合已发生变化")


def _growth_report_source(growth: dict) -> Path | None:
    """Only trust the report belonging to the persisted growth task for this case."""
    task_id = str(growth.get("task_id") or "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", task_id):
        return None
    expected = (OUTPUT_ROOT / "growth" / task_id / "growth_report.docx").resolve()
    try:
        source = _verified_growth_artifacts(growth).get("report_docx")
    except HTTPException:
        return None
    if not source or source.resolve() != expected or not report_file_ready(source):
        return None
    return source


def _report_payload_ready(payload: dict) -> bool:
    claim_id = str(payload.get("claim_id") or "")
    generation_id = str(payload.get("generation_id") or "")
    snapshot_sha256 = str(payload.get("snapshot_sha256") or "")
    bundle_sha256 = str(payload.get("bundle_sha256") or "")
    if (
        not _SAFE_CLAIM_ID.fullmatch(claim_id)
        or not _SAFE_TASK_ID.fullmatch(generation_id)
        or not re.fullmatch(r"[0-9a-f]{64}", snapshot_sha256)
        or not re.fullmatch(r"[0-9a-f]{64}", bundle_sha256)
    ):
        return False
    report_path = _output_artifact_path(payload.get("report_docx_url"))
    excel_path = _output_artifact_path(payload.get("excel_report_url"))
    bundle_path = _output_artifact_path(payload.get("bundle_zip_url"))
    if not report_path or not report_file_ready(report_path):
        return False
    if not excel_path or not _xlsx_file_ready(excel_path):
        return False
    if not bundle_path or not _zip_file_ready(bundle_path):
        return False
    if payload.get("growth_status") in {"included", "summary_rebuilt"}:
        growth_path = _output_artifact_path(payload.get("growth_report_docx_url"))
        if not growth_path or not report_file_ready(growth_path):
            return False
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        return False
    required_kinds = {"claim_report", "assessment_excel", "bundle"}
    if payload.get("template_version") == "v1.3-legal-basis":
        required_kinds.add("legal_basis")
    if payload.get("growth_status") in {"included", "summary_rebuilt"}:
        required_kinds.add("growth_report")
    if payload.get("historical_ndvi_status") in {"included", "included_no_usable_data"}:
        required_kinds.update(
            {"historical_ndvi_report", "historical_ndvi_result", "historical_ndvi_trend"}
        )
    if payload.get("parcel_growth_status") == "included":
        required_kinds.add("parcel_growth_result")
    seen_kinds: set[str] = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            return False
        kind = str(artifact.get("kind") or "")
        expected_size = artifact.get("size_bytes")
        expected_hash = str(artifact.get("sha256") or "")
        path = _output_artifact_path(artifact.get("url"))
        if (
            not _report_artifact_kind_allowed(kind)
            or kind in seen_kinds
            or not path
            or not isinstance(expected_size, int)
            or expected_size <= 0
            or path.stat().st_size != expected_size
            or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)
            or not secrets.compare_digest(expected_hash, _sha256_file(path))
        ):
            return False
        seen_kinds.add(kind)
    return required_kinds.issubset(seen_kinds) and secrets.compare_digest(
        bundle_sha256, _sha256_file(bundle_path)
    )


def _report_artifact_kind_allowed(kind: str) -> bool:
    return kind in {
        "claim_report",
        "growth_report",
        "assessment_excel",
        "legal_basis",
        "bundle",
        "historical_ndvi_report",
        "historical_ndvi_result",
        "historical_ndvi_trend",
        "parcel_growth_result",
    } or re.fullmatch(r"historical_ndvi_year_panel_[0-9]{4}", kind) is not None


def _verify_report_for_review(claim_id: str, report: dict, generation_id: str) -> dict:
    """Verify every registered artifact and the ZIP manifest before a review can bind to it."""
    if not isinstance(report, dict) or report.get("generation_id") != generation_id:
        raise HTTPException(409, "审核报告版本与当前登记版本不一致，请刷新后重试")
    snapshot_sha256 = str(report.get("snapshot_sha256") or "")
    bundle_sha256 = str(report.get("bundle_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", snapshot_sha256):
        raise HTTPException(409, "报告快照哈希缺失或无效")
    if not re.fullmatch(r"[0-9a-f]{64}", bundle_sha256):
        raise HTTPException(409, "报告包哈希缺失或无效")

    claim_key = hashlib.sha256(claim_id.encode("utf-8")).hexdigest()[:20]
    expected_directory = (OUTPUT_ROOT / "reports" / claim_key / generation_id).resolve()
    required_kinds = {"claim_report", "assessment_excel", "bundle"}
    if report.get("template_version") == "v1.3-legal-basis":
        required_kinds.add("legal_basis")
    if report.get("growth_status") in {"included", "summary_rebuilt"}:
        required_kinds.add("growth_report")
    if report.get("historical_ndvi_status") in {"included", "included_no_usable_data"}:
        required_kinds.update(
            {"historical_ndvi_report", "historical_ndvi_result", "historical_ndvi_trend"}
        )
        required_kinds.update(
            f"historical_ndvi_year_panel_{year}"
            for year in (report.get("historical_ndvi_years") or [])
        )
    if report.get("parcel_growth_status") == "included":
        required_kinds.add("parcel_growth_result")

    artifacts = report.get("artifacts")
    if not isinstance(artifacts, list):
        raise HTTPException(409, "报告附件登记信息缺失")
    verified_artifacts: list[dict[str, Any]] = []
    seen_kinds: set[str] = set()
    seen_filenames: set[str] = set()
    paths_by_kind: dict[str, Path] = {}
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise HTTPException(409, "报告附件登记信息损坏")
        kind = str(artifact.get("kind") or "")
        filename = str(artifact.get("filename") or "")
        expected_hash = str(artifact.get("sha256") or "")
        if not _report_artifact_kind_allowed(kind) or kind in seen_kinds:
            raise HTTPException(409, "报告附件类型缺失、重复或未受支持")
        if Path(filename).name != filename or not filename or filename in seen_filenames:
            raise HTTPException(409, "报告附件名称缺失、重复或无效")
        if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            raise HTTPException(409, f"报告附件 {filename} 的哈希缺失或无效")
        path = _output_artifact_path(artifact.get("url"))
        if not path or path.parent.resolve() != expected_directory or path.name != filename:
            raise HTTPException(409, f"报告附件 {filename} 缺失或路径登记异常")
        actual_hash = _sha256_file(path)
        if not secrets.compare_digest(expected_hash, actual_hash):
            raise HTTPException(409, f"报告附件 {filename} 完整性校验失败")
        expected_size = artifact.get("size_bytes")
        if not isinstance(expected_size, int) or expected_size != path.stat().st_size:
            raise HTTPException(409, f"报告附件 {filename} 大小校验失败")
        seen_kinds.add(kind)
        seen_filenames.add(filename)
        paths_by_kind[kind] = path
        verified_artifacts.append(
            {
                "kind": kind,
                "filename": filename,
                "media_type": artifact.get("media_type") or "application/octet-stream",
                "size_bytes": expected_size,
                "sha256": expected_hash,
            }
        )
    if not required_kinds.issubset(seen_kinds):
        raise HTTPException(409, "待审核报告缺少必需附件")

    bundle_path = paths_by_kind["bundle"]
    if not secrets.compare_digest(bundle_sha256, _sha256_file(bundle_path)):
        raise HTTPException(409, "报告包哈希与登记快照不一致")
    manifest_path = expected_directory / "manifest.json"
    if not manifest_path.is_file():
        raise HTTPException(409, "报告清单文件缺失")
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        with zipfile.ZipFile(bundle_path) as archive:
            if archive.testzip() is not None:
                raise HTTPException(409, "报告包 CRC 校验失败")
            names = archive.namelist()
            if len(names) != len(set(names)) or "manifest.json" not in names:
                raise HTTPException(409, "报告包成员重复或清单缺失")
            if archive.read("manifest.json") != manifest_bytes:
                raise HTTPException(409, "报告包内外清单不一致")
            files = manifest.get("files") or {}
            expected_members = {name for name in files.values() if isinstance(name, str) and name}
            expected_members.add("manifest.json")
            if set(names) != expected_members:
                raise HTTPException(409, "报告包成员与清单声明不一致")
            integrity_entries = manifest.get("file_integrity")
            if not isinstance(integrity_entries, list):
                raise HTTPException(409, "报告清单缺少附件完整性信息")
            integrity_by_name: dict[str, dict] = {}
            for entry in integrity_entries:
                if not isinstance(entry, dict):
                    raise HTTPException(409, "报告清单完整性条目损坏")
                filename = str(entry.get("filename") or "")
                if filename in integrity_by_name or filename not in expected_members:
                    raise HTTPException(409, "报告清单完整性条目重复或引用无效成员")
                content = archive.read(filename)
                if entry.get("size_bytes") != len(content):
                    raise HTTPException(409, f"报告包成员 {filename} 大小校验失败")
                if not secrets.compare_digest(str(entry.get("sha256") or ""), hashlib.sha256(content).hexdigest()):
                    raise HTTPException(409, f"报告包成员 {filename} 哈希校验失败")
                integrity_by_name[filename] = entry
            if set(integrity_by_name) != expected_members - {"manifest.json"}:
                raise HTTPException(409, "报告清单未覆盖全部业务附件")
    except HTTPException:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, zipfile.BadZipFile, KeyError) as exc:
        raise HTTPException(409, "报告包或清单无法解析，禁止审核归档") from exc

    if (
        manifest.get("claim_id") != claim_id
        or manifest.get("generation_id") != generation_id
        or manifest.get("snapshot_sha256") != snapshot_sha256
        or manifest.get("template_version") != report.get("template_version")
        or manifest.get("report_status") != "draft_pending_human_review"
    ):
        raise HTTPException(409, "报告清单与案件、版本或快照不一致")
    if (
        manifest.get("historical_ndvi_status", "not_available")
        != report.get("historical_ndvi_status", "not_available")
        or manifest.get("parcel_growth_status", "not_available")
        != report.get("parcel_growth_status", "not_available")
        or (manifest.get("historical_ndvi_years") or [])
        != (report.get("historical_ndvi_years") or [])
    ):
        raise HTTPException(409, "报告清单的历史 NDVI 或分地块附件状态与登记快照不一致")
    for artifact in verified_artifacts:
        if artifact["kind"] == "bundle":
            continue
        entry = integrity_by_name.get(artifact["filename"])
        if (
            not entry
            or entry.get("sha256") != artifact["sha256"]
            or (manifest.get("files") or {}).get(artifact["kind"]) != artifact["filename"]
        ):
            raise HTTPException(409, f"登记附件 {artifact['filename']} 与报告包内容不一致")

    current_data = _assemble_case_data(claim_id) or {}
    expected_revisions = manifest.get("snapshot_revisions") or {}
    current_revisions = current_data.get("_snapshot_revisions") or {}
    ignored_revision_steps = {"report", "excel_report", "policy_contract"}
    if (
        {key: value for key, value in current_revisions.items() if key not in ignored_revision_steps}
        != {key: value for key, value in expected_revisions.items() if key not in ignored_revision_steps}
    ):
        raise HTTPException(409, "报告生成后案件权威结果版本发生变化，必须重新生成报告")
    current_data["_snapshot_revisions"] = expected_revisions
    _validate_report_snapshot(current_data)
    current_snapshot_sha256 = _report_snapshot_sha256(
        claim_id,
        current_data,
        str(report.get("template_version") or ""),
    )
    if not secrets.compare_digest(snapshot_sha256, current_snapshot_sha256):
        raise HTTPException(409, "报告快照与当前案件权威数据不一致，禁止审核归档")

    return {
        "generation_id": generation_id,
        "template_version": report.get("template_version"),
        "snapshot_sha256": snapshot_sha256,
        "bundle_sha256": bundle_sha256,
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "artifacts": sorted(verified_artifacts, key=lambda item: (item["kind"], item["filename"])),
        "_authority_revisions": {
            key: value
            for key, value in current_revisions.items()
            if key not in ignored_revision_steps
        },
    }


def _report_readme(
    claim_id: str,
    template_version: str,
    generation_id: str,
    generated_at: str,
    growth_status: str,
    historical_ndvi_status: str,
    parcel_growth_status: str,
) -> str:
    growth_text = {
        "included": "已包含由当前服务端快照标准化生成的长势报告。",
        "summary_rebuilt": "原始长势 DOCX 不可用，已依据服务端持久化摘要重建附件。",
        "not_available": "本案件没有可用的长势分析附件。",
    }[growth_status]
    return (
        "Agrisky AI 农业保险查勘定损报告包\n"
        f"案件编号：{claim_id}\n"
        f"报告生成号：{generation_id}\n"
        f"模板版本：{template_version}\n"
        f"生成时间：{generated_at}\n"
        f"长势附件：{growth_text}\n\n"
        f"历史季度 NDVI：{('已包含真实遥感时序附件。' if historical_ndvi_status == 'included' else '已包含附件，但没有可用季度统计。') if historical_ndvi_status != 'not_available' else '未生成。'}\n"
        f"分地块长势：{'已包含经人工映射确认的 JSON 明细。' if parcel_growth_status == 'included' else '未生成。'}\n\n"
        "合规依据：已包含案件级 legal-basis.json，记录结论、合同条款、法规条文、技术依据与适用边界。\n\n"
        "说明：报告中的遥感筛查、模型估算和规则评级用于辅助查勘，最终理赔结论须经人工审核。\n"
        "请使用 manifest.json 中的 SHA-256 校验各附件完整性。\n"
    )


def _build_report_generation(
    claim_id: str,
    server_data: dict,
    template_version: str,
    sections: list[str],
) -> dict[str, Any]:
    """Build and atomically publish one immutable report generation."""
    safe_claim_id = _safe_claim_file_id(claim_id)
    snapshot_sha256 = _report_snapshot_sha256(claim_id, server_data, template_version)
    version_slug = re.sub(r"[^A-Za-z0-9._-]", "-", template_version).strip(".-") or "template"
    generation_id = f"{version_slug}-{snapshot_sha256[:16]}"
    claim_key = hashlib.sha256(claim_id.encode("utf-8")).hexdigest()[:20]
    reports_root = OUTPUT_ROOT / "reports"
    generation_parent = reports_root / claim_key
    generation_dir = generation_parent / generation_id
    generation_parent.mkdir(parents=True, exist_ok=True)

    generated_at_dt = datetime.now(timezone(timedelta(hours=8)))
    generated_at = generated_at_dt.isoformat()
    report_data = json.loads(json.dumps(server_data, ensure_ascii=False, default=str))
    report_data["_report_meta"] = {
        "snapshot_sha256": snapshot_sha256,
        "generation_id": generation_id,
        "generated_at": generated_at,
        "template_version": template_version,
    }

    claim_name = f"{safe_claim_id}.docx"
    excel_name = f"{safe_claim_id}.xlsx"
    growth_name = f"{safe_claim_id}-growth.docx"
    historical_report_name = f"{safe_claim_id}-historical-ndvi.docx"
    historical_result_name = f"{safe_claim_id}-historical-ndvi.json"
    historical_trend_name = f"{safe_claim_id}-historical-ndvi-trend.png"
    parcel_growth_name = f"{safe_claim_id}-parcel-growth.json"
    legal_basis_name = f"{safe_claim_id}-legal-basis.json"
    bundle_name = f"{safe_claim_id}-reports.zip"
    readme_name = "报告包说明.txt"
    manifest_name = "manifest.json"

    staging = generation_parent / f".{generation_id}.{uuid.uuid4().hex}.tmp"
    staging.mkdir(parents=False, exist_ok=False)
    warnings: list[str] = []
    growth_status = "not_available"
    historical_ndvi_status = "not_available"
    parcel_growth_status = "not_available"
    historical_ndvi_years: list[int] = []
    try:
        from excel_report import generate_excel_report
        from report_generator import generate_claim_report, generate_growth_appendix_report

        claim_path = staging / claim_name
        excel_path = staging / excel_name
        growth_path = staging / growth_name
        historical_report_path = staging / historical_report_name
        historical_result_path = staging / historical_result_name
        historical_trend_path = staging / historical_trend_name
        parcel_growth_path = staging / parcel_growth_name
        legal_basis_path = staging / legal_basis_name
        readme_path = staging / readme_name
        manifest_path = staging / manifest_name
        bundle_path = staging / bundle_name

        growth = server_data.get("growth") or {}
        if growth:
            verified_growth = _verified_growth_artifacts(growth)
            preview_source = verified_growth.get("class_preview_png")
            if not preview_source:
                raise RuntimeError("长势快照缺少已绑定摘要的分级预览图")
            trusted_preview = staging / "trusted-growth-class-preview.png"
            shutil.copy2(preview_source, trusted_preview)
            trusted_sha256 = _sha256_file(trusted_preview)
            expected_preview_sha256 = str(
                ((growth.get("artifact_integrity") or {}).get("class_preview_png") or {}).get(
                    "sha256"
                )
                or ""
            )
            if not secrets.compare_digest(trusted_sha256, expected_preview_sha256):
                raise RuntimeError("长势预览图复制期间摘要发生变化")
            report_data.setdefault("growth", {}).setdefault("outputs", {})[
                "class_preview_png"
            ] = _output_url(trusted_preview)

        generate_claim_report(
            claim_id,
            report_data,
            str(claim_path),
            template_version=template_version,
            generated_at=generated_at_dt,
        )
        generate_excel_report(
            claim_id,
            report_data,
            str(excel_path),
            template_version=template_version,
            generated_at=generated_at_dt,
        )

        if growth.get("summary"):
            growth_source = _growth_report_source(growth)
            # 标准附件始终从同一服务端快照重建，避免把旧模板、旧警示或旧分页混入新报告包。
            generate_growth_appendix_report(
                claim_id,
                report_data,
                str(growth_path),
                template_version=template_version,
                generated_at=generated_at_dt,
            )
            if growth_source:
                growth_status = "included"
            else:
                growth_status = "summary_rebuilt"
                warnings.append("原始长势报告不可用，已根据服务端长势摘要重建独立附件")
        else:
            warnings.append("案件没有可用的 NDVI 长势分析结果")
        if growth:
            trusted_preview.unlink(missing_ok=True)

        if (server_data.get("satellite") or {}).get("is_mock") or (server_data.get("satellite") or {}).get("confidence") == "mock":
            warnings.append("卫星初筛包含模拟数据，不可作为定损证据")
        growth_raster = (growth.get("raster") or {})
        growth_meta = growth_raster.get("ndvi_meta") or {}
        if (growth_raster.get("ndvi_source") or growth_meta.get("source")) == "synthetic":
            warnings.append("NDVI 长势分析包含模拟数据，不可作为定损证据")

        historical_panel_paths: list[tuple[int, Path]] = []
        historical = server_data.get("historical_ndvi") or {}
        if historical:
            historical_task_id = str(historical.get("task_id") or "")
            if historical.get("boundary_geometry_sha256") != (server_data.get("case") or {}).get("policy_boundary_sha256"):
                raise RuntimeError("历史季度 NDVI 与报告在册边界哈希不一致")
            registered = {
                item["kind"]: item
                for item in _registered_analysis_artifacts(
                    claim_id, "historical_ndvi", historical_task_id
                )
            }
            required_history = {
                "historical_ndvi_report",
                "historical_ndvi_result",
                "historical_ndvi_trend",
            }
            historical_ndvi_years = list(
                range(int(historical["start_year"]), int(historical["end_year"]) + 1)
            )
            required_history.update(
                f"historical_ndvi_year_panel_{year}" for year in historical_ndvi_years
            )
            if not required_history.issubset(registered):
                raise RuntimeError("历史季度 NDVI 已登记结果缺少完整 DOCX、JSON、趋势图或年度图")
            result_registration = registered["historical_ndvi_result"]
            if (
                historical.get("_result_artifact_sha256") != result_registration["sha256"]
                or historical.get("_result_artifact_size_bytes") != result_registration["size_bytes"]
            ):
                raise RuntimeError("历史季度 NDVI 案件指针未绑定已登记 JSON 摘要")
            from historical_ndvi import (
                HistoricalNDVIOutputs,
                load_historical_ndvi_result,
                save_historical_ndvi_result,
            )

            validated_history_model = load_historical_ndvi_result(
                registered["historical_ndvi_result"]["path"]
            )
            validated_history = validated_history_model.model_dump(mode="json")
            if (
                validated_history.get("task_id") != historical_task_id
                or validated_history.get("boundary_geometry_sha256")
                != historical.get("boundary_geometry_sha256")
            ):
                raise RuntimeError("历史季度 NDVI JSON 与案件权威快照不一致")
            historical = {
                **validated_history,
                "_result_artifact_sha256": result_registration["sha256"],
                "_result_artifact_size_bytes": result_registration["size_bytes"],
            }
            shutil.copy2(registered["historical_ndvi_report"]["path"], historical_report_path)
            shutil.copy2(registered["historical_ndvi_trend"]["path"], historical_trend_path)
            exported_panel_names: dict[str, str] = {}
            for year in historical_ndvi_years:
                panel_name = f"{safe_claim_id}-historical-ndvi-{year}-quarters.png"
                panel_path = staging / panel_name
                shutil.copy2(registered[f"historical_ndvi_year_panel_{year}"]["path"], panel_path)
                historical_panel_paths.append((year, panel_path))
                exported_panel_names[str(year)] = panel_name
            exported_history = validated_history_model.model_copy(deep=True)
            if any(item.quarter_map_png for item in exported_history.quarters):
                for observation in exported_history.quarters:
                    observation.quarter_map_png = None
                    observation.quarter_map_sha256 = None
                exported_history.warnings = list(
                    dict.fromkeys(
                        [
                            *exported_history.warnings,
                            "报告包提供趋势图和年度 2×2 图组；任务级单季度源图未作为独立附件导出。",
                        ]
                    )
                )
            exported_history.outputs = HistoricalNDVIOutputs(
                result_json=historical_result_name,
                trend_chart_png=historical_trend_name,
                yearly_panel_pngs=exported_panel_names,
                report_docx=historical_report_name,
            )
            exported_history = type(exported_history).model_validate(
                exported_history.model_dump(mode="json")
            )
            save_historical_ndvi_result(exported_history, historical_result_path)
            if not report_file_ready(historical_report_path):
                raise RuntimeError("历史季度 NDVI DOCX 结构校验失败")
            has_usable_history = any(
                quarter.get("status") in {"complete", "partial"}
                and quarter.get("median_ndvi") is not None
                for quarter in (historical.get("quarters") or [])
            )
            historical_ndvi_status = (
                "included" if has_usable_history else "included_no_usable_data"
            )
            if not has_usable_history:
                warnings.append("历史季度 NDVI 没有可用季度统计，附件仅记录缺测/质量状态")
        else:
            warnings.append("案件没有历史季度 NDVI 时序结果")

        parcel_growth = server_data.get("parcel_growth") or {}
        if parcel_growth:
            parcel_task_id = str(parcel_growth.get("task_id") or "")
            if parcel_growth.get("boundary_sha256") != (server_data.get("case") or {}).get("policy_boundary_sha256"):
                raise RuntimeError("分地块长势与报告在册边界哈希不一致")
            registered_parcel = {
                item["kind"]: item
                for item in _registered_analysis_artifacts(
                    claim_id, "parcel_growth", parcel_task_id
                )
            }
            if "parcel_growth_result" not in registered_parcel:
                raise RuntimeError("分地块长势已登记结果缺少 JSON 附件")
            parcel_registration = registered_parcel["parcel_growth_result"]
            if (
                parcel_growth.get("_result_artifact_sha256") != parcel_registration["sha256"]
                or parcel_growth.get("_result_artifact_size_bytes") != parcel_registration["size_bytes"]
            ):
                raise RuntimeError("分地块长势案件指针未绑定已登记 JSON 摘要")
            source_payload = json.loads(
                registered_parcel["parcel_growth_result"]["path"].read_text(encoding="utf-8")
            )
            if (
                source_payload.get("task_id") != parcel_task_id
                or source_payload.get("boundary_sha256") != parcel_growth.get("boundary_sha256")
                or source_payload.get("source_growth_task_id") != (server_data.get("growth") or {}).get("task_id")
            ):
                raise RuntimeError("分地块长势 JSON 与案件权威快照不一致")
            parcel_growth = {
                **source_payload,
                "_result_artifact_sha256": parcel_registration["sha256"],
                "_result_artifact_size_bytes": parcel_registration["size_bytes"],
            }
            shutil.copy2(registered_parcel["parcel_growth_result"]["path"], parcel_growth_path)
            parcel_growth_status = "included"
        else:
            warnings.append("案件没有经人工映射确认的分地块长势结果")

        case = server_data.get("case") or {}
        contract = server_data.get("contract") or {}
        legal_basis = report_basis_rows(contract, str(case.get("crop_type") or ""))
        legal_references = reference_index(contract, str(case.get("crop_type") or ""))
        legal_basis_payload = {
            "schema_version": "agrisky-case-legal-basis-v1.0",
            "claim_id": claim_id,
            "generation_id": generation_id,
            "template_version": template_version,
            "generated_at": generated_at,
            "contract": {
                "contract_number": contract.get("contract_number"),
                "contract_version": contract.get("contract_version"),
                "contract_sha256": contract.get("contract_sha256"),
            },
            "basis": legal_basis,
            "references": legal_references,
            "disclosure": (
                "法规摘要仅用于说明报告结论的依据链，不替代官方文本；"
                "单案赔付以冻结合同、案件证据、固定计算规则和人工审核为准。"
            ),
        }
        legal_basis_path.write_text(
            json.dumps(legal_basis_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        readme_path.write_text(
            _report_readme(
                claim_id,
                template_version,
                generation_id,
                generated_at,
                growth_status,
                historical_ndvi_status,
                parcel_growth_status,
            ),
            encoding="utf-8-sig",
        )

        if not report_file_ready(claim_path):
            raise RuntimeError("综合 DOCX 结构校验失败")
        if not _xlsx_file_ready(excel_path):
            raise RuntimeError("Excel 结构校验失败")
        if growth_status != "not_available" and not report_file_ready(growth_path):
            raise RuntimeError("长势 DOCX 结构校验失败")

        file_entries = [
            _artifact_metadata("claim_report", claim_path, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            _artifact_metadata("assessment_excel", excel_path, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
            _artifact_metadata("legal_basis", legal_basis_path, "application/json"),
            _artifact_metadata("readme", readme_path, "text/plain; charset=utf-8"),
        ]
        if growth_status != "not_available":
            file_entries.insert(
                1,
                _artifact_metadata("growth_report", growth_path, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            )
        if historical_ndvi_status != "not_available":
            file_entries.extend(
                [
                    _artifact_metadata(
                        "historical_ndvi_report",
                        historical_report_path,
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    ),
                    _artifact_metadata(
                        "historical_ndvi_result", historical_result_path, "application/json"
                    ),
                    _artifact_metadata(
                        "historical_ndvi_trend", historical_trend_path, "image/png"
                    ),
                ]
            )
            file_entries.extend(
                _artifact_metadata(
                    f"historical_ndvi_year_panel_{year}", panel_path, "image/png"
                )
                for year, panel_path in historical_panel_paths
            )
        if parcel_growth_status == "included":
            file_entries.append(
                _artifact_metadata("parcel_growth_result", parcel_growth_path, "application/json")
            )
        # ZIP 内只使用文件名和校验信息；url 在发布到最终目录后重算。
        for entry in file_entries:
            entry.pop("url", None)

        manifest_files: dict[str, str | None] = {
            "claim_report": claim_name,
            "growth_report": growth_name if growth_status != "not_available" else None,
            "assessment_excel": excel_name,
            "legal_basis": legal_basis_name,
            "historical_ndvi_report": (
                historical_report_name if historical_ndvi_status != "not_available" else None
            ),
            "historical_ndvi_result": (
                historical_result_name if historical_ndvi_status != "not_available" else None
            ),
            "historical_ndvi_trend": (
                historical_trend_name if historical_ndvi_status != "not_available" else None
            ),
            "parcel_growth_result": (
                parcel_growth_name if parcel_growth_status == "included" else None
            ),
            "readme": readme_name,
        }
        for year, panel_path in historical_panel_paths:
            manifest_files[f"historical_ndvi_year_panel_{year}"] = panel_path.name

        manifest = {
            "schema_version": "1.3",
            "claim_id": claim_id,
            "generation_id": generation_id,
            "template_version": template_version,
            "generated_at": generated_at,
            "report_status": "draft_pending_human_review",
            "snapshot_sha256": snapshot_sha256,
            "snapshot_revisions": server_data.get("_snapshot_revisions") or {},
            "files": manifest_files,
            "file_integrity": file_entries,
            "growth_status": growth_status,
            "historical_ndvi_status": historical_ndvi_status,
            "historical_ndvi_years": historical_ndvi_years,
            "parcel_growth_status": parcel_growth_status,
            "growth_provenance": {
                "task_id": growth.get("task_id"),
                "method": growth.get("method"),
                "class_breaks": growth.get("class_breaks") or [],
                "summary": growth.get("summary") or [],
                "raster": growth.get("raster") or {},
            },
            "historical_ndvi_provenance": {
                "task_id": historical.get("task_id"),
                "schema_version": historical.get("schema_version"),
                "boundary_geometry_sha256": historical.get("boundary_geometry_sha256"),
                "start_year": historical.get("start_year"),
                "end_year": historical.get("end_year"),
                "as_of_date": historical.get("as_of_date"),
                "source_result_artifact_sha256": historical.get("_result_artifact_sha256"),
                "source": historical.get("source") or {},
                "warnings": historical.get("warnings") or [],
            },
            "parcel_growth_provenance": {
                "task_id": parcel_growth.get("task_id"),
                "schema_version": parcel_growth.get("schema_version"),
                "boundary_sha256": parcel_growth.get("boundary_sha256"),
                "source_growth_task_id": parcel_growth.get("source_growth_task_id"),
                "confirmation_set_sha256": parcel_growth.get("confirmation_set_sha256"),
                "source_result_artifact_sha256": parcel_growth.get("_result_artifact_sha256"),
                "confirmation_receipts": [
                    {
                        "confirmation_batch_id": batch_id,
                        "receipt_sha256": receipt_sha256,
                        "download_url": download_url,
                    }
                    for batch_id, receipt_sha256, download_url in sorted(
                        {
                            (
                                str(item.get("confirmation_batch_id") or ""),
                                str(item.get("confirmation_receipt_sha256") or ""),
                                str(item.get("confirmation_receipt_download_url") or ""),
                            )
                            for item in (parcel_growth.get("confirmation_provenance") or [])
                        }
                    )
                ],
                "manifest": parcel_growth.get("manifest") or {},
                "parcel_count": len(parcel_growth.get("parcels") or []),
                "feature_count": len(parcel_growth.get("features") or []),
                "overall": parcel_growth.get("overall") or {},
            },
            "rule_version": (server_data.get("rule") or {}).get("rule_version"),
            "legal_basis": legal_basis,
            "warnings": warnings,
        }
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

        bundle_members = [
            claim_path,
            excel_path,
            legal_basis_path,
            readme_path,
            manifest_path,
        ]
        if growth_status != "not_available":
            bundle_members.append(growth_path)
        if historical_ndvi_status != "not_available":
            bundle_members.extend(
                [historical_report_path, historical_result_path, historical_trend_path]
            )
            bundle_members.extend(path for _, path in historical_panel_paths)
        if parcel_growth_status == "included":
            bundle_members.append(parcel_growth_path)
        bundle_members.sort(key=lambda path: path.name)
        names = [path.name for path in bundle_members]
        if len(names) != len(set(names)):
            raise RuntimeError("报告包成员名称重复")
        with zipfile.ZipFile(bundle_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for member in bundle_members:
                archive.write(member, arcname=member.name)
        if not _zip_file_ready(bundle_path):
            raise RuntimeError("报告 ZIP 结构校验失败")

        try:
            staging.replace(generation_dir)
        except OSError:
            # 另一 worker 可能已发布同一快照；只接受完整、同名的不可变目录。
            existing_claim = generation_dir / claim_name
            existing_excel = generation_dir / excel_name
            existing_bundle = generation_dir / bundle_name
            if not (
                report_file_ready(existing_claim)
                and _xlsx_file_ready(existing_excel)
                and _zip_file_ready(existing_bundle)
            ):
                raise
            _safe_remove_report_staging(staging)
    except Exception:
        _safe_remove_report_staging(staging)
        raise

    claim_path = generation_dir / claim_name
    excel_path = generation_dir / excel_name
    growth_path = generation_dir / growth_name
    bundle_path = generation_dir / bundle_name
    manifest_path = generation_dir / manifest_name
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        manifest.get("claim_id") != claim_id
        or manifest.get("generation_id") != generation_id
        or manifest.get("snapshot_sha256") != snapshot_sha256
        or manifest.get("template_version") != template_version
    ):
        raise RuntimeError("同名报告目录的案件、生成号、模板或快照身份不一致")
    growth_status = manifest.get("growth_status") or "not_available"
    historical_ndvi_status = manifest.get("historical_ndvi_status") or "not_available"
    parcel_growth_status = manifest.get("parcel_growth_status") or "not_available"
    historical_ndvi_years = manifest.get("historical_ndvi_years") or []
    warnings = manifest.get("warnings") or []

    artifacts: list[dict[str, Any]] = []
    for entry in manifest.get("file_integrity") or []:
        kind = str(entry.get("kind") or "")
        filename = str(entry.get("filename") or "")
        if not _report_artifact_kind_allowed(kind):
            continue
        path = generation_dir / filename
        artifact = _artifact_metadata(
            kind,
            path,
            entry.get("media_type") or "application/octet-stream",
        )
        if (
            artifact["size_bytes"] != entry.get("size_bytes")
            or artifact["sha256"] != entry.get("sha256")
        ):
            raise RuntimeError(f"发布后的报告附件 {filename} 与清单不一致")
        artifacts.append(artifact)
    artifacts.append(_artifact_metadata("bundle", bundle_path, "application/zip"))
    for artifact in artifacts:
        artifact["download_url"] = (
            f"/api/v1/cases/{claim_id}/artifacts/{generation_id}/{artifact['filename']}"
        )

    # Backward-compatible aliases remain available, while API responses point to immutable generation paths.
    stable_dir = OUTPUT_ROOT / "reports"
    _atomic_copy(claim_path, stable_dir / claim_name)
    _atomic_copy(excel_path, stable_dir / excel_name)
    _atomic_copy(bundle_path, stable_dir / bundle_name)
    stable_growth = stable_dir / growth_name
    if growth_status != "not_available":
        _atomic_copy(growth_path, stable_growth)
    else:
        stable_growth.unlink(missing_ok=True)

    payload = {
        "status": "success",
        "claim_id": claim_id,
        "report_docx_url": _output_url(claim_path),
        "growth_report_docx_url": _output_url(growth_path) if growth_status != "not_available" else None,
        "excel_report_url": _output_url(excel_path),
        "bundle_zip_url": _output_url(bundle_path),
        "template_version": template_version,
        "generation_id": generation_id,
        "generated_at": manifest.get("generated_at") or generated_at,
        "snapshot_sha256": snapshot_sha256,
        "bundle_sha256": _sha256_file(bundle_path),
        "growth_status": growth_status,
        "historical_ndvi_status": historical_ndvi_status,
        "historical_ndvi_years": historical_ndvi_years,
        "parcel_growth_status": parcel_growth_status,
        "legal_basis": manifest.get("legal_basis") or [],
        "warnings": warnings,
        "artifacts": artifacts,
        "sections": sections,
    }
    return payload

@app.post("/api/v1/tools/generate_report", response_model=ReportGenerateResult)
def generate_report(req: ReportGenerateRequest) -> ReportGenerateResult:
    """生成固定模板报告草稿。状态: RULE_DONE。幂等。

    案件、卫星、长势、灾损、合规、规则和赔付均取自服务端 case_results，
    不信任前端回传的数据或图件。
    """
    claim_id = _safe_claim_file_id(req.claim_id)
    con = _db()
    row = con.execute("SELECT * FROM cases WHERE claim_id = ?", (claim_id,)).fetchone()
    con.close()
    if not row: raise HTTPException(404, "案件不存在")
    st = row["state"]
    if st in ("REPORT_DRAFTED", "HUMAN_REVIEW", "ARCHIVED"):
        existing = _get_result(claim_id, "report") or {}
        if existing and _report_payload_ready(existing):
            return ReportGenerateResult(**existing)
        raise HTTPException(409, "报告状态已冻结，但已登记产物缺失或损坏；请走管理员修复/版本迁移流程")
    if st != "RULE_DONE":
        raise HTTPException(400, f"当前状态 {st} 不允许生成报告，需要 RULE_DONE")

    # 报告需要赔付测算；若此前未单独调用，则在此补算
    _assemble_case_data(claim_id)
    existing_payout = _get_result(claim_id, "payout") or {}
    if not existing_payout or not existing_payout.get("contract_sha256"):
        _compute_and_store_payout(claim_id)

    # 所有报告数据（包括长势）都取服务端 case_results，不信任前端回传。
    server_data = _assemble_case_data(claim_id) or {}
    _validate_report_snapshot(server_data)

    sections = ["案件基础信息", "保险合同与规则依据", "材料理解与一致性核验", "卫星遥感初筛", "NDVI 作物长势分析", "历史季度 NDVI 时序附件",
                "分地块/要素长势明细", "多源灾损评估", "合规面积核验",
                "规则引擎建议", "赔付测算", "结论与合规依据映射", "依据与证据索引",
                "风险提示", "人工审核意见区"]
    try:
        payload = _build_report_generation(claim_id, server_data, REPORT_TEMPLATE_VERSION, sections)
        logger.info("报告已生成: claim=%s generation=%s", claim_id, payload["generation_id"])
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("报告生成失败")
        raise HTTPException(500, f"报告生成失败: {e}") from e

    try:
        _advance_state_with_result(
            claim_id,
            S.REPORT_DRAFTED,
            "report",
            payload,
            expected_revisions=server_data.get("_snapshot_revisions") or {},
        )
    except HTTPException:
        # 并发请求中另一 worker 可能先完成 CAS；只返回已持久化且校验通过的同一报告。
        existing = _get_result(claim_id, "report") or {}
        if existing and _report_payload_ready(existing):
            return ReportGenerateResult(**existing)
        raise
    excel_artifact = next((item for item in payload["artifacts"] if item.get("kind") == "assessment_excel"), {})
    _save_result(
        claim_id,
        "excel_report",
        {
            "excel_url": excel_artifact.get("download_url"),
            "storage_url": payload["excel_report_url"],
            "generation_id": payload["generation_id"],
            "sha256": excel_artifact.get("sha256"),
            "size_bytes": excel_artifact.get("size_bytes"),
        },
    )
    _audit(
        claim_id,
        "generate_report",
        "generate_report",
        {"claim_id": claim_id, "template_version": REPORT_TEMPLATE_VERSION},
        f"generation={payload['generation_id']}, snapshot={payload['snapshot_sha256']}, bundle={payload['bundle_sha256']}",
    )

    return ReportGenerateResult(**payload)


@app.get("/api/v1/cases/{claim_id}/analysis-artifacts/{step}/{task_id}/{filename}")
async def download_analysis_artifact(
    claim_id: str,
    step: str,
    task_id: str,
    filename: str,
    request: Request,
) -> FileResponse:
    """Download only a hash-registered private analysis artifact for this case."""
    claim_id = _safe_claim_file_id(claim_id)
    if Path(filename).name != filename or not filename:
        raise HTTPException(400, "分析附件名称无效")
    portal_user = _assert_case_download_access(claim_id, request)
    artifact = next(
        (
            item for item in _registered_analysis_artifacts(claim_id, step, task_id)
            if item["filename"] == filename
        ),
        None,
    )
    if not artifact:
        raise HTTPException(404, "分析附件不存在或未登记")
    actor = (
        f"session:{portal_user['username']}" if portal_user
        else f"api-key:{getattr(request.state, 'agrisky_role', 'development')}"
    )
    _audit(
        claim_id,
        "download_analysis_artifact",
        "download_analysis_artifact",
        {"step": step, "task_id": task_id, "filename": filename},
        f"sha256={artifact['sha256']}",
        actor=actor,
    )
    return FileResponse(
        path=str(artifact["path"]),
        media_type=artifact["media_type"],
        filename=filename,
        headers={"X-Content-SHA256": artifact["sha256"]},
    )


@app.get("/api/v1/cases/{claim_id}/evidence/growth/{task_id}/map-runtime.html")
async def view_case_growth_map(
    claim_id: str,
    task_id: str,
    request: Request,
) -> HTMLResponse:
    """Render the case-scoped map with local dependencies after evidence verification."""
    claim_id = _safe_claim_file_id(claim_id)
    if not _SAFE_TASK_ID.fullmatch(task_id or ""):
        raise HTTPException(400, "长势附件任务号无效")
    portal_user = _assert_case_download_access(claim_id, request)
    growth = _get_result(claim_id, "growth") or {}
    if growth.get("task_id") != task_id:
        raise HTTPException(404, "长势附件不属于当前案件任务")
    verified = _verified_growth_artifacts(growth)
    path = verified.get("map_html")
    if not path:
        raise HTTPException(404, "长势地图未列入受控预览白名单")
    digest = str((growth.get("artifact_integrity") or {}).get("map_html", {}).get("sha256") or "")
    actor = (
        f"session:{portal_user['username']}"
        if portal_user
        else f"api-key:{getattr(request.state, 'agrisky_role', 'development')}"
    )
    _audit(
        claim_id,
        "view_growth_map",
        "view_case_growth_map",
        {"task_id": task_id, "kind": "map_html"},
        f"source_sha256={digest}",
        actor=actor,
    )
    return _growth_runtime_map_response(path, digest, _case_local_context_basemap(claim_id))


@app.get("/api/v1/cases/{claim_id}/evidence/growth/{task_id}/{filename}")
async def download_case_growth_evidence(
    claim_id: str,
    task_id: str,
    filename: str,
    request: Request,
) -> FileResponse:
    """Serve an allowlisted growth preview with case ownership and hash validation."""
    claim_id = _safe_claim_file_id(claim_id)
    if not _SAFE_TASK_ID.fullmatch(task_id or "") or Path(filename).name != filename:
        raise HTTPException(400, "长势附件任务号或文件名无效")
    portal_user = _assert_case_download_access(claim_id, request)
    growth = _get_result(claim_id, "growth") or {}
    if growth.get("task_id") != task_id:
        raise HTTPException(404, "长势附件不属于当前案件任务")
    verified = _verified_growth_artifacts(growth)
    match = next(
        (
            (key, path)
            for key, path in verified.items()
            if key in _GROWTH_PUBLIC_OUTPUT_KEYS and path.name == filename
        ),
        None,
    )
    if not match:
        raise HTTPException(404, "长势附件未列入受控预览白名单")
    key, path = match
    digest = str((growth.get("artifact_integrity") or {}).get(key, {}).get("sha256") or "")
    media_type = {
        ".png": "image/png",
        ".html": "text/html; charset=utf-8",
        ".json": "application/json",
        ".geojson": "application/geo+json",
        ".csv": "text/csv; charset=utf-8",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }.get(path.suffix.lower(), "application/octet-stream")
    actor = (
        f"session:{portal_user['username']}"
        if portal_user
        else f"api-key:{getattr(request.state, 'agrisky_role', 'development')}"
    )
    _audit(
        claim_id,
        "download_growth_evidence",
        "download_case_growth_evidence",
        {"task_id": task_id, "kind": key, "filename": filename},
        f"sha256={digest}",
        actor=actor,
    )
    response_headers = {
        "X-Content-SHA256": digest,
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    }
    if path.suffix.lower() == ".html":
        response_headers.update(_growth_html_security_headers(path))
    return FileResponse(
        path=str(path),
        media_type=media_type,
        filename=filename if path.suffix.lower() == ".docx" else None,
        headers=response_headers,
    )


@app.get("/api/v1/cases/{claim_id}/evidence/screening/{filename}")
async def download_case_screening_evidence(
    claim_id: str,
    filename: str,
    request: Request,
) -> FileResponse:
    """Serve a locally materialized screening image with ownership and digest checks."""
    claim_id = _safe_claim_file_id(claim_id)
    if Path(filename).name != filename or not filename:
        raise HTTPException(400, "初筛附件文件名无效")
    portal_user = _assert_case_download_access(claim_id, request)
    satellite = _get_result(claim_id, "satellite") or {}
    integrity = satellite.get("thumbnail_integrity") or {}
    match = next(
        ((field, entry) for field, entry in integrity.items() if entry.get("filename") == filename),
        None,
    )
    if not match:
        raise HTTPException(404, "初筛附件未登记")
    field, entry = match
    path = _output_artifact_path(satellite.get(field))
    expected_root = (OUTPUT_ROOT / "screening" / claim_id).resolve()
    expected_hash = str(entry.get("sha256") or "")
    expected_size = entry.get("size_bytes")
    if (
        not path
        or path.parent.resolve() != expected_root
        or path.name != filename
        or not isinstance(expected_size, int)
        or path.stat().st_size != expected_size
        or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)
        or not secrets.compare_digest(expected_hash, _sha256_file(path))
    ):
        raise HTTPException(409, "初筛附件缺失、被替换或摘要不一致")
    actor = (
        f"session:{portal_user['username']}"
        if portal_user
        else f"api-key:{getattr(request.state, 'agrisky_role', 'development')}"
    )
    _audit(
        claim_id,
        "download_screening_evidence",
        "download_case_screening_evidence",
        {"field": field, "filename": filename},
        f"sha256={expected_hash}",
        actor=actor,
    )
    return FileResponse(
        path=str(path),
        media_type="image/png",
        headers={"X-Content-SHA256": expected_hash, "Cache-Control": "private, no-store"},
    )


@app.get("/api/v1/cases/{claim_id}/artifacts/{generation_id}/{filename}")
async def download_case_artifact(
    claim_id: str,
    generation_id: str,
    filename: str,
    request: Request,
) -> FileResponse:
    """Download a report artifact after generation and case-ownership validation."""
    claim_id = _safe_claim_file_id(claim_id)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", generation_id or ""):
        raise HTTPException(400, "报告生成号无效")
    if Path(filename).name != filename or not filename:
        raise HTTPException(400, "附件名称无效")

    portal_token = (
        request.headers.get("x-agrisky-token", "").strip()
        or request.cookies.get(SESSION_COOKIE_NAME, "").strip()
    )
    portal_user = _auth_user(portal_token) if portal_token else None
    if portal_token and not portal_user and not getattr(request.state, "agrisky_role", None):
        raise HTTPException(401, "未登录或会话已失效")
    if portal_user and portal_user.get("role") != "admin":
        con = _db()
        owner = con.execute(
            "SELECT p.holder_account FROM cases c "
            "JOIN policies p ON c.policy_id = p.policy_id WHERE c.claim_id = ?",
            (claim_id,),
        ).fetchone()
        con.close()
        if not owner or owner["holder_account"] != portal_user.get("username"):
            raise HTTPException(403, "无权下载其他投保人案件的报告")

    con = _db()
    case_row = con.execute("SELECT state FROM cases WHERE claim_id = ?", (claim_id,)).fetchone()
    receipt_rows = con.execute(
        "SELECT review_id, claim_id, decision, generation_id, receipt_json, receipt_sha256 "
        "FROM review_receipts WHERE claim_id = ? AND generation_id = ? "
        "ORDER BY created_at DESC",
        (claim_id, generation_id),
    ).fetchall()
    con.close()
    if not case_row:
        raise HTTPException(404, "案件不存在")

    artifact = None
    # Reviewed generations are always resolved from the immutable receipt first.
    if receipt_rows:
        receipt = _verified_review_receipt(
            receipt_rows[0],
            expected_claim_id=claim_id,
            expected_generation_id=generation_id,
            require_approved=True,
        )
        artifact = next(
            (
                item
                for item in ((receipt.get("report") or {}).get("artifacts") or [])
                if item.get("filename") == filename
                and _report_artifact_kind_allowed(str(item.get("kind") or ""))
            ),
            None,
        )
    elif case_row["state"] == S.ARCHIVED.value:
        raise HTTPException(409, "归档报告缺少审核通过回执锚点，禁止下载")
    else:
        report = _get_result(claim_id, "report") or {}
        if report.get("generation_id") == generation_id and _report_payload_ready(report):
            artifact = next(
                (
                    item
                    for item in report.get("artifacts", [])
                    if item.get("filename") == filename
                    and _report_artifact_kind_allowed(str(item.get("kind") or ""))
                ),
                None,
            )
    if not artifact:
        raise HTTPException(404, "报告附件不存在")
    claim_key = hashlib.sha256(claim_id.encode("utf-8")).hexdigest()[:20]
    expected_directory = (OUTPUT_ROOT / "reports" / claim_key / generation_id).resolve()
    path = _output_artifact_path(artifact.get("url"))
    if path is None:
        candidate = (expected_directory / filename).resolve()
        path = candidate if candidate.is_relative_to(expected_directory) and candidate.is_file() else None
    if not path or path.name != filename or path.parent.resolve() != expected_directory:
        raise HTTPException(409, "报告附件缺失或路径登记异常")
    expected_hash = str(artifact.get("sha256") or "")
    expected_size = artifact.get("size_bytes")
    if (
        not re.fullmatch(r"[0-9a-f]{64}", expected_hash)
        or not isinstance(expected_size, int)
        or expected_size <= 0
        or path.stat().st_size != expected_size
        or not secrets.compare_digest(expected_hash, _sha256_file(path))
    ):
        raise HTTPException(409, "报告附件完整性校验失败")

    actor = (
        f"session:{portal_user['username']}" if portal_user
        else f"api-key:{getattr(request.state, 'agrisky_role', 'development')}"
    )
    _audit(
        claim_id,
        "download_report_artifact",
        "download_case_artifact",
        {"generation_id": generation_id, "filename": filename, "actor": actor},
        f"sha256={expected_hash}",
    )
    return FileResponse(
        path=str(path),
        media_type=artifact.get("media_type") or "application/octet-stream",
        filename=filename,
    )


@app.get("/api/v1/cases/{claim_id}/review-receipts/{review_id}")
async def download_review_receipt(claim_id: str, review_id: str, request: Request) -> Response:
    """Download an immutable human-review receipt bound to one verified report generation."""
    claim_id = _safe_claim_file_id(claim_id)
    if not re.fullmatch(r"[0-9a-f]{32}", review_id or ""):
        raise HTTPException(400, "审核回执编号无效")

    portal_token = (
        request.headers.get("x-agrisky-token", "").strip()
        or request.cookies.get(SESSION_COOKIE_NAME, "").strip()
    )
    portal_user = _auth_user(portal_token) if portal_token else None
    if portal_token and not portal_user and not getattr(request.state, "agrisky_role", None):
        raise HTTPException(401, "未登录或会话已失效")
    if portal_user and portal_user.get("role") != "admin":
        con = _db()
        owner = con.execute(
            "SELECT p.holder_account FROM cases c "
            "JOIN policies p ON c.policy_id = p.policy_id WHERE c.claim_id = ?",
            (claim_id,),
        ).fetchone()
        con.close()
        if not owner or owner["holder_account"] != portal_user.get("username"):
            raise HTTPException(403, "无权下载其他投保人案件的审核回执")

    con = _db()
    row = con.execute(
        "SELECT review_id, claim_id, decision, generation_id, receipt_json, receipt_sha256 "
        "FROM review_receipts "
        "WHERE claim_id = ? AND review_id = ?",
        (claim_id, review_id),
    ).fetchone()
    con.close()
    if not row:
        raise HTTPException(404, "审核回执不存在")
    _verified_review_receipt(row, expected_claim_id=claim_id)
    content = row["receipt_json"].encode("utf-8")
    actual_hash = hashlib.sha256(content).hexdigest()
    if not secrets.compare_digest(str(row["receipt_sha256"]), actual_hash):
        raise HTTPException(409, "审核回执完整性校验失败")
    actor = (
        f"session:{portal_user['username']}" if portal_user
        else f"api-key:{getattr(request.state, 'agrisky_role', 'development')}"
    )
    _audit(
        claim_id,
        "download_review_receipt",
        "download_review_receipt",
        {"review_id": review_id, "actor": actor},
        f"sha256={actual_hash}",
    )
    return Response(
        content=content,
        media_type="application/vnd.agrisky.review-receipt+json",
        headers={
            "Content-Disposition": f'attachment; filename="review-receipt-{review_id}.json"',
            "X-Content-SHA256": actual_hash,
        },
    )


@app.get("/api/v1/cases/{claim_id}/preliminary-excel")
async def download_preliminary_excel(claim_id: str, request: Request) -> FileResponse:
    """受控下载尚未组成完整报告包的 Excel，兼容合规/规则阶段导出。"""
    claim_id = _safe_claim_file_id(claim_id)
    portal_token = (
        request.headers.get("x-agrisky-token", "").strip()
        or request.cookies.get(SESSION_COOKIE_NAME, "").strip()
    )
    portal_user = _auth_user(portal_token) if portal_token else None
    if portal_token and not portal_user and not getattr(request.state, "agrisky_role", None):
        raise HTTPException(401, "未登录或会话已失效")
    if portal_user and portal_user.get("role") != "admin":
        con = _db()
        owner = con.execute(
            "SELECT p.holder_account FROM cases c "
            "JOIN policies p ON c.policy_id = p.policy_id WHERE c.claim_id = ?",
            (claim_id,),
        ).fetchone()
        con.close()
        if not owner or owner["holder_account"] != portal_user.get("username"):
            raise HTTPException(403, "无权下载其他投保人案件的评估表")

    con = _db()
    case_row = con.execute("SELECT state FROM cases WHERE claim_id = ?", (claim_id,)).fetchone()
    con.close()
    if not case_row:
        raise HTTPException(404, "案件不存在")
    if case_row["state"] in {S.REPORT_DRAFTED.value, S.HUMAN_REVIEW.value, S.ARCHIVED.value}:
        raise HTTPException(409, "报告已冻结，请使用报告 generation 的受控附件地址下载 Excel")

    stored = _get_result(claim_id, "excel_report") or {}
    path = _output_artifact_path(stored.get("storage_url") or stored.get("excel_url"))
    if not path or not _xlsx_file_ready(path):
        raise HTTPException(404, "案件评估表不存在或已损坏")
    generation_id = stored.get("generation_id")
    if generation_id:
        claim_key = hashlib.sha256(claim_id.encode("utf-8")).hexdigest()[:20]
        expected_path = (OUTPUT_ROOT / "reports" / claim_key / str(generation_id) / path.name).resolve()
    else:
        expected_path = (OUTPUT_ROOT / "reports" / f"{claim_id}.xlsx").resolve()
    if path.resolve() != expected_path:
        raise HTTPException(409, "案件评估表路径登记异常")
    expected_hash = str(stored.get("sha256") or "")
    expected_size = stored.get("size_bytes")
    if (
        not re.fullmatch(r"[0-9a-f]{64}", expected_hash)
        or not isinstance(expected_size, int)
        or expected_size <= 0
        or path.stat().st_size != expected_size
        or not secrets.compare_digest(expected_hash, _sha256_file(path))
    ):
        raise HTTPException(409, "案件评估表完整性校验失败")

    actor = (
        f"session:{portal_user['username']}" if portal_user
        else f"api-key:{getattr(request.state, 'agrisky_role', 'development')}"
    )
    _audit(
        claim_id,
        "download_preliminary_excel",
        "download_preliminary_excel",
        {"actor": actor},
        f"sha256={expected_hash}",
    )
    return FileResponse(
        path=str(path),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=path.name,
    )


@app.post("/api/v1/tools/generate_excel_report")
def generate_excel_report_ep(req: ReportGenerateRequest) -> dict:
    """生成受灾评估 Excel；报告形成后只返回冻结快照，不在下载时重算。"""
    claim_id = _safe_claim_file_id(req.claim_id)
    con = _db()
    row = con.execute("SELECT state FROM cases WHERE claim_id = ?", (claim_id,)).fetchone()
    con.close()
    if not row:
        raise HTTPException(404, f"案件不存在: {claim_id}")
    state = row["state"]
    if state in {"REPORT_DRAFTED", "HUMAN_REVIEW", "ARCHIVED"}:
        report = _get_result(claim_id, "report") or {}
        excel_path = _output_artifact_path(report.get("excel_report_url"))
        if excel_path and _xlsx_file_ready(excel_path):
            excel_artifact = next(
                (item for item in report.get("artifacts", []) if item.get("kind") == "assessment_excel"),
                {},
            )
            return {
                "status": "success",
                "excel_url": excel_artifact.get("download_url")
                or f"/api/v1/cases/{claim_id}/preliminary-excel",
                "claim_id": claim_id,
                "generation_id": report.get("generation_id"),
                "sha256": _sha256_file(excel_path),
            }
        raise HTTPException(409, "报告已冻结，但 Excel 产物缺失或损坏；不能在下载时覆盖重建")
    if state not in {"COMPLIANCE_DONE", "RULE_DONE"}:
        raise HTTPException(400, f"当前状态 {state} 不允许导出评估表，需要 COMPLIANCE_DONE 或 RULE_DONE")

    data = _assemble_case_data(claim_id)
    if data is None:
        raise HTTPException(404, f"案件不存在: {claim_id}")
    # 若已合规但未测算赔付，补算一次，让评估表含赔款
    if not data["payout"] and data["compliance"]:
        p = _compute_and_store_payout(claim_id)
        if p:
            data["payout"] = p
    reports_dir = OUTPUT_ROOT / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    xlsx_path = reports_dir / f"{claim_id}.xlsx"
    temporary = reports_dir / f".{claim_id}.{uuid.uuid4().hex}.xlsx.tmp"
    try:
        from excel_report import generate_excel_report
        generate_excel_report(claim_id, data, str(temporary), template_version=REPORT_TEMPLATE_VERSION)
        if not _xlsx_file_ready(temporary):
            raise RuntimeError("Excel 结构校验失败")
        temporary.replace(xlsx_path)
    except Exception as e:
        temporary.unlink(missing_ok=True)
        logger.exception("Excel 报表生成失败")
        raise HTTPException(500, f"Excel 报表生成失败: {e}") from e
    storage_url = f"/outputs/reports/{claim_id}.xlsx"
    download_url = f"/api/v1/cases/{claim_id}/preliminary-excel"
    digest = _sha256_file(xlsx_path)
    _save_result(
        claim_id,
        "excel_report",
        {
            "excel_url": download_url,
            "storage_url": storage_url,
            "sha256": digest,
            "size_bytes": xlsx_path.stat().st_size,
            "template_version": REPORT_TEMPLATE_VERSION,
        },
    )
    _audit(claim_id, "generate_excel_report", "generate_excel_report", {"claim_id": claim_id}, f"excel={digest}")
    return {"status": "success", "excel_url": download_url, "claim_id": claim_id, "sha256": digest}


# ═══════════════════════════════════════════════════════════
# 管理接口
# ═══════════════════════════════════════════════════════════

@app.get("/api/v1/cases")
async def list_cases(state: str | None = None, limit: int = 200) -> dict:
    """案件队列：列出全部案件（可按状态过滤），附队列汇总统计。"""
    limit = min(500, max(1, limit))
    con = _db()
    select_sql = (
        "SELECT c.*, p.policy_version_id, p.area_mu AS policy_area_mu, "
        "p.boundary_geojson AS policy_boundary_geojson "
        "FROM cases c LEFT JOIN policies p ON c.policy_id = p.policy_id "
        "WHERE NOT EXISTS (SELECT 1 FROM deleted_records d "
        "WHERE d.entity_type = 'case' AND d.entity_id = c.claim_id) "
    )
    if state:
        rows = con.execute(
            select_sql + "AND c.state = ? ORDER BY c.reported_at DESC LIMIT ?", (state, limit)
        ).fetchall()
    else:
        rows = con.execute(
            select_sql + "ORDER BY c.reported_at DESC LIMIT ?", (limit,)
        ).fetchall()
    con.close()

    items: list[dict] = []
    by_state: dict[str, int] = {}
    total_payout = 0.0
    high_risk = 0
    for row in rows:
        rule = _get_result(row["claim_id"], "rule") or {}
        payout = _get_result(row["claim_id"], "payout") or {}
        loss = _get_result(row["claim_id"], "loss_assessment") or {}
        risk = rule.get("risk_level")
        amount = payout.get("payout_amount_yuan")
        boundary_sha256 = None
        if row["policy_boundary_geojson"]:
            try:
                boundary_sha256 = _geometry_sha256(json.loads(row["policy_boundary_geojson"]))
            except (json.JSONDecodeError, TypeError, ValueError):
                boundary_sha256 = None
        items.append({
            "claim_id": row["claim_id"],
            "policy_id": row["policy_id"],
            "policy_version_id": row["policy_version_id"]
            or (f"{row['policy_id']}:v1" if row["policy_id"] else None),
            "state": row["state"],
            "disaster_type": row["disaster_type"],
            "crop_type": row["crop_type"],
            "plot_id": row["plot_id"],
            "loss_date": row["loss_date"],
            "reported_at": row["reported_at"],
            "risk_level": risk,
            "yield_loss_ratio": loss.get("yield_loss_ratio"),
            "payout_amount_yuan": amount,
            "policy_area_mu": row["policy_area_mu"],
            "boundary_registered": boundary_sha256 is not None,
            "boundary_sha256": boundary_sha256,
            "boundary_hash_scheme": "canonical_geometry_v1" if boundary_sha256 else None,
        })
        by_state[row["state"]] = by_state.get(row["state"], 0) + 1
        if isinstance(amount, (int, float)):
            total_payout += amount
        if risk == "high":
            high_risk += 1

    return {
        "total": len(items),
        "by_state": by_state,
        "total_payout_yuan": round(total_payout, 2),
        "high_risk_count": high_risk,
        "items": items,
    }


@app.delete("/api/v1/cases/{claim_id}")
async def delete_case(claim_id: str, request: Request) -> dict:
    """Hide a case from active workflows while retaining immutable evidence."""
    claim_id = _safe_claim_file_id(claim_id)
    actor = _require_admin_actor(request)
    con = _db()
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT claim_id, policy_id, state FROM cases WHERE claim_id = ?",
            (claim_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "案件不存在")
        if _is_deleted(con, "case", claim_id):
            con.rollback()
            return {
                "status": "success",
                "claim_id": claim_id,
                "already_deleted": True,
                "evidence_retained": True,
            }
        con.execute(
            "INSERT INTO deleted_records (entity_type, entity_id, deleted_at, deleted_by) "
            "VALUES ('case', ?, ?, ?)",
            (claim_id, datetime.now().isoformat(), actor),
        )
        con.commit()
    except HTTPException:
        con.rollback()
        raise
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    _audit(
        claim_id,
        "delete_case",
        "delete_case",
        {"claim_id": claim_id},
        "案件已从活动队列移除；证据、结果和审计记录保留",
        actor=actor,
    )
    return {
        "status": "success",
        "claim_id": claim_id,
        "already_deleted": False,
        "evidence_retained": True,
    }


@app.get("/api/v1/cases/{claim_id}/full")
async def get_case_full(claim_id: str) -> dict:
    """单案件完整数据（案件信息 + 各步持久化结果），供前端从队列打开时回填工作台。"""
    con = _db()
    row = con.execute(
        "SELECT * FROM cases c WHERE claim_id = ? "
        "AND NOT EXISTS (SELECT 1 FROM deleted_records d "
        "WHERE d.entity_type = 'case' AND d.entity_id = c.claim_id)",
        (claim_id,),
    ).fetchone()
    con.close()
    if not row:
        raise HTTPException(404, "案件不存在")
    return {
        "claim_id": row["claim_id"],
        "state": row["state"],
        "case": {
            "policy_id": row["policy_id"],
            "disaster_type": row["disaster_type"],
            "loss_date": row["loss_date"],
            "crop_type": row["crop_type"],
            "plot_id": row["plot_id"],
        },
        "results": {
            "satellite": _case_satellite_result_response(
                claim_id, _get_result(claim_id, "satellite")
            ),
            "growth": _case_growth_result_response(claim_id, _get_result(claim_id, "growth")),
            "compliance": _get_result(claim_id, "compliance"),
            "rule": _get_result(claim_id, "rule"),
            "loss_assessment": _get_result(claim_id, "loss_assessment"),
            "payout": _get_result(claim_id, "payout"),
            "report": _get_result(claim_id, "report"),
            "excel_report": _get_result(claim_id, "excel_report"),
            "human_review": _get_result(claim_id, "human_review"),
        },
    }


# ═══════════════════════════════════════════════════════════
# 投保人登录门户：账号登录 + 仅见自己名下保单/理赔
# ═══════════════════════════════════════════════════════════

@app.post("/api/v1/auth/login")
async def auth_login(req: LoginRequest, response: Response, request: Request) -> dict:
    """登录并设置 HttpOnly 会话 Cookie；响应正文不暴露可重放 token。"""
    rate_keys = _login_rate_keys(request, req.username)
    retry_after = _login_retry_after(rate_keys)
    if retry_after:
        raise HTTPException(
            429,
            "登录尝试过多，请稍后再试",
            headers={"Retry-After": str(retry_after)},
        )
    con = _db()
    acc = con.execute("SELECT * FROM accounts WHERE username = ?", (req.username,)).fetchone()
    salt = acc["salt"] if acc else _DUMMY_AUTH_SALT
    expected_hash = acc["pwd_hash"] if acc else _DUMMY_AUTH_HASH
    # PBKDF2 is intentionally expensive; run it off the event loop so login traffic
    # cannot stall unrelated async API requests in the same worker.
    candidate_hash = await asyncio.to_thread(_hash_pwd, req.password, salt)
    valid_password = secrets.compare_digest(candidate_hash, expected_hash) and acc is not None
    if not valid_password:
        con.close()
        retry_after = _register_login_failure(rate_keys)
        if retry_after:
            raise HTTPException(
                429,
                "登录尝试过多，请稍后再试",
                headers={"Retry-After": str(retry_after)},
            )
        raise HTTPException(401, "用户名或密码错误")
    _clear_login_account_failures(req.username)
    token = secrets.token_hex(24)
    # 每个账号仅保留最新会话，降低遗留设备或泄露 token 的可重放窗口。
    con.execute("DELETE FROM sessions WHERE username = ?", (acc["username"],))
    con.execute("INSERT INTO sessions VALUES (?,?,?)", (token, acc["username"], datetime.now().isoformat()))
    con.commit()
    con.close()
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=max(60, SESSION_MAX_AGE_SECONDS),
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        path="/",
    )
    return {"username": acc["username"], "holder_name": acc["holder_name"], "role": acc["role"]}


@app.post("/api/v1/auth/logout")
async def auth_logout(request: Request, response: Response) -> dict:
    token = (
        request.headers.get("x-agrisky-token", "").strip()
        or request.cookies.get(SESSION_COOKIE_NAME, "").strip()
    )
    if token:
        con = _db()
        con.execute("DELETE FROM sessions WHERE token = ?", (token,))
        con.commit()
        con.close()
    response.delete_cookie(SESSION_COOKIE_NAME, path="/", secure=COOKIE_SECURE, samesite="lax")
    return {"status": "success"}


@app.get("/api/v1/me")
async def get_me(request: Request) -> dict:
    """当前投保人信息 + 仅其名下的保单与理赔（按 holder_account 隔离）。"""
    tok = request.headers.get("x-agrisky-token", "") or request.cookies.get(SESSION_COOKIE_NAME, "")
    user = _auth_user(tok)
    if not user:
        raise HTTPException(401, "未登录或会话已失效")
    con = _db()
    pols = con.execute(
        "SELECT policy_id, crop_type, address, area_mu FROM policies p "
        "WHERE holder_account = ? AND NOT EXISTS (SELECT 1 FROM deleted_records d "
        "WHERE d.entity_type = 'policy' AND d.entity_id = p.policy_id)",
        (user["username"],),
    ).fetchall()
    pol_ids = [p["policy_id"] for p in pols]
    cases: list[dict] = []
    if pol_ids:
        placeholders = ",".join("?" * len(pol_ids))
        rows = con.execute(
            f"SELECT claim_id, policy_id, state, disaster_type, loss_date, crop_type, reported_at "
            f"FROM cases c WHERE policy_id IN ({placeholders}) "
            "AND NOT EXISTS (SELECT 1 FROM deleted_records d "
            "WHERE d.entity_type = 'case' AND d.entity_id = c.claim_id) "
            "ORDER BY reported_at DESC",
            pol_ids,
        ).fetchall()
        con.close()
        for r in rows:
            payout = _get_result(r["claim_id"], "payout") or {}
            rule = _get_result(r["claim_id"], "rule") or {}
            cases.append({**dict(r),
                          "payout_amount_yuan": payout.get("payout_amount_yuan"),
                          "risk_level": rule.get("risk_level")})
    else:
        con.close()
    return {"username": user["username"], "holder_name": user["holder_name"], "role": user["role"],
            "policies": [dict(p) for p in pols], "cases": cases}


# ═══════════════════════════════════════════════════════════
# 保单-地块库：承保边界在册，理赔凭保单号自动取用
# ═══════════════════════════════════════════════════════════

def _save_policy(
    policy_id: str,
    holder_name: str,
    crop_type: str,
    address: str,
    boundary_geojson: dict,
    policy_version_id: str | None = None,
    uploaded_contract: tuple[dict[str, Any], bytes, str] | None = None,
) -> dict:
    area_mu = _geojson_area_mu(boundary_geojson)
    new_boundary_sha256 = _geometry_sha256(boundary_geojson)
    resolved_version_id = (policy_version_id or f"{policy_id}:v1").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", resolved_version_id):
        raise HTTPException(422, "policy_version_id 格式无效")
    if uploaded_contract:
        frozen_contract = _load_policy_contract(policy_id, resolved_version_id)
        uploaded_sha256 = sha256_bytes(uploaded_contract[1])
        if frozen_contract and frozen_contract.get("source_sha256") != uploaded_sha256:
            raise HTTPException(409, "该保单版本已冻结其他合同原件；请使用新的保单号或版本登记")
    con = _db()
    try:
        # Acquire the writer reservation before checking references.  A claim or
        # parcel confirmation cannot slip in between the check and the upsert.
        con.execute("BEGIN IMMEDIATE")
        if _is_deleted(con, "policy", policy_id):
            raise HTTPException(
                409,
                f"保单 {policy_id} 已删除；为保持审计可追溯性不能复用该编号，请使用新的 policy_id",
            )
        existing = con.execute(
            "SELECT holder_name, crop_type, address, boundary_geojson, policy_version_id "
            "FROM policies WHERE policy_id = ?",
            (policy_id,),
        ).fetchone()
        terms_changed = True
        if existing:
            try:
                existing_boundary_sha256 = _geometry_sha256(
                    json.loads(existing["boundary_geojson"] or "{}")
                )
            except (json.JSONDecodeError, TypeError, ValueError):
                existing_boundary_sha256 = ""
            terms_changed = any(
                (
                    existing["holder_name"] != holder_name,
                    existing["crop_type"] != crop_type,
                    existing["address"] != address,
                    existing_boundary_sha256 != new_boundary_sha256,
                    (existing["policy_version_id"] or f"{policy_id}:v1")
                    != resolved_version_id,
                )
            )
            referenced = con.execute(
                "SELECT claim_id FROM cases WHERE policy_id = ? LIMIT 1", (policy_id,)
            ).fetchone()
            parcel_reference = con.execute(
                "SELECT confirmation_id FROM parcel_confirmations WHERE policy_id = ? LIMIT 1",
                (policy_id,),
            ).fetchone()
            if (referenced or parcel_reference) and terms_changed:
                reference_label = (
                    f"案件 {referenced['claim_id']}" if referenced
                    else f"地块确认 {parcel_reference['confirmation_id']}"
                )
                raise HTTPException(
                    409,
                    f"保单 {policy_id} 已被{reference_label}引用，不能覆盖承保人、作物、地址、版本标签或地块边界；"
                    "当前存储模型要求使用新的 policy_id 登记新版本",
                )
        # An idempotent save does not rewrite referenced rows or their timestamps.
        if not existing or terms_changed:
            con.execute(
                "INSERT INTO policies "
                "(policy_id, holder_name, crop_type, address, boundary_geojson, area_mu, created_at, policy_version_id) "
                "VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(policy_id) DO UPDATE SET holder_name=excluded.holder_name, "
                "crop_type=excluded.crop_type, address=excluded.address, "
                "boundary_geojson=excluded.boundary_geojson, area_mu=excluded.area_mu, "
                "created_at=excluded.created_at, policy_version_id=excluded.policy_version_id",
                (
                    policy_id,
                    holder_name,
                    crop_type,
                    address,
                    json.dumps(boundary_geojson, ensure_ascii=False),
                    area_mu,
                    datetime.now().isoformat(),
                    resolved_version_id,
                ),
            )
        con.commit()
    except sqlite3.IntegrityError as exc:
        con.rollback()
        if "referenced policy terms are immutable" in str(exc):
            raise HTTPException(409, "已被案件或地块确认引用的保单条款不可覆盖") from exc
        raise
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    policy_record = {
            "policy_id": policy_id,
            "policy_version_id": resolved_version_id,
            "holder_name": holder_name,
            "crop_type": crop_type,
            "address": address,
            "area_mu": area_mu,
        }
    if uploaded_contract:
        contract_data, artifact_data, artifact_filename = uploaded_contract
        contract = _store_uploaded_policy_contract(
            policy_record, contract_data, artifact_data, artifact_filename
        )
    else:
        # Legacy JSON integrations remain readable; the user-facing registration
        # endpoint always supplies and freezes an uploaded contract.
        contract = _ensure_policy_contract(policy_record)
    return {"status": "success", "policy_id": policy_id, "policy_version_id": resolved_version_id,
            "holder_name": holder_name,
            "crop_type": crop_type, "address": address, "area_mu": area_mu,
            "boundary_sha256": new_boundary_sha256,
            "boundary_hash_scheme": "canonical_geometry_v1",
            "contract": contract_summary(
                contract, f"/api/v1/policies/{policy_id}/contract/download"
            )}


def _store_uploaded_policy_contract(
    policy: dict[str, Any], contract: dict[str, Any], artifact_data: bytes, filename: str
) -> dict[str, Any]:
    policy_id = str(policy["policy_id"])
    version_id = str(policy.get("policy_version_id") or f"{policy_id}:v1")
    existing = _load_policy_contract(policy_id, version_id)
    if existing:
        if existing.get("source_sha256") == sha256_bytes(artifact_data):
            return existing
        raise HTTPException(409, "该保单版本已冻结其他合同原件；请使用新的保单号或版本登记")

    suffix = Path(filename).suffix.lower()
    if suffix not in {".pdf", ".docx"}:
        raise HTTPException(422, "保险合同原件仅支持 PDF 或 DOCX")
    contract_key = hashlib.sha256(f"{policy_id}|{version_id}".encode("utf-8")).hexdigest()[:20]
    stored_name = f"{contract.get('contract_number') or 'contract'}{suffix}"
    relative_path = (Path("contracts") / contract_key / safe_filename(stored_name)).as_posix()
    artifact_path = _contract_artifact_path(relative_path)
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_bytes(artifact_data)
    artifact_sha256 = _sha256_file(artifact_path)
    if artifact_sha256 != sha256_bytes(artifact_data):
        artifact_path.unlink(missing_ok=True)
        raise HTTPException(500, "保险合同原件保存后完整性校验失败")
    created_at = datetime.now(timezone.utc).isoformat()
    contract = {
        **contract,
        "policy_id": policy_id,
        "policy_version_id": version_id,
        "artifact_sha256": artifact_sha256,
    }
    contract["contract_sha256"] = contract_sha256(contract)
    con = _db()
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            "INSERT INTO policy_contracts "
            "(policy_id, policy_version_id, contract_id, contract_version, contract_json, "
            "contract_sha256, artifact_relative_path, artifact_sha256, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                policy_id,
                version_id,
                contract["contract_id"],
                contract["contract_version"],
                json.dumps(contract, ensure_ascii=False, sort_keys=True),
                contract["contract_sha256"],
                relative_path,
                artifact_sha256,
                created_at,
            ),
        )
        con.commit()
    except sqlite3.IntegrityError as exc:
        con.rollback()
        artifact_path.unlink(missing_ok=True)
        raise HTTPException(409, "该保单版本的合同已经冻结") from exc
    except Exception:
        con.rollback()
        artifact_path.unlink(missing_ok=True)
        raise
    finally:
        con.close()
    contract["artifact_relative_path"] = relative_path
    contract["created_at"] = created_at
    return contract


def _request_actor(request: Request) -> str:
    username = getattr(request.state, "agrisky_user", None)
    role = getattr(request.state, "agrisky_role", "development")
    return f"session:{username}" if username else f"api-key:{role}"


def _load_preflight_manifest(preflight_id: str) -> tuple[dict[str, Any], sqlite3.Row]:
    if not re.fullmatch(r"preflight-[0-9a-f]{24}", preflight_id or ""):
        raise HTTPException(400, "地块预检编号无效")
    con = _db()
    row = con.execute(
        "SELECT * FROM parcel_preflight_manifests WHERE preflight_id = ?",
        (preflight_id,),
    ).fetchone()
    con.close()
    if not row:
        raise HTTPException(404, "地块预检清单不存在")
    try:
        manifest = json.loads(row["manifest_json"])
    except (json.JSONDecodeError, TypeError) as exc:
        raise HTTPException(409, "地块预检清单损坏") from exc
    from parcel_preflight import (
        BOUNDARY_HASH_SCHEME,
        PREFLIGHT_ALGORITHM_VERSION,
        PREFLIGHT_SCHEMA_VERSION,
        PROPOSED_ID_SCHEME,
    )
    if (
        manifest.get("schema_version") != PREFLIGHT_SCHEMA_VERSION
        or manifest.get("algorithm_version") != PREFLIGHT_ALGORITHM_VERSION
        or manifest.get("id_scheme") != PROPOSED_ID_SCHEME
        or manifest.get("geometry_hash_scheme") != BOUNDARY_HASH_SCHEME
    ):
        raise HTTPException(409, "地块预检清单使用了未知架构、算法、编号或几何哈希方案")
    digest_payload = dict(manifest)
    digest_payload.pop("manifest_sha256", None)
    canonical = json.dumps(digest_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if not secrets.compare_digest(row["manifest_sha256"], hashlib.sha256(canonical.encode("utf-8")).hexdigest()):
        raise HTTPException(409, "地块预检清单完整性校验失败")
    return manifest, row


@app.post("/api/v1/policies/parcels/preflight")
async def create_parcel_preflight(request: Request, archive_file: UploadFile = File(...)) -> dict:
    """Safely inspect a multi-KML ZIP and persist its manifest without importing geometry."""
    from parcel_preflight import MAX_ARCHIVE_BYTES, preflight_kml_zip

    data = await archive_file.read(MAX_ARCHIVE_BYTES + 1)
    if len(data) > MAX_ARCHIVE_BYTES:
        raise HTTPException(413, "地块 ZIP 超过 50 MB 上限")
    archive_sha256 = hashlib.sha256(data).hexdigest()
    con = _db()
    existing = con.execute(
        "SELECT preflight_id, manifest_json FROM parcel_preflight_manifests WHERE archive_sha256 = ?",
        (archive_sha256,),
    ).fetchone()
    con.close()
    if existing:
        return {"preflight_id": existing["preflight_id"], **json.loads(existing["manifest_json"])}

    manifest = await asyncio.to_thread(
        preflight_kml_zip,
        data,
        archive_file.filename or "parcels.zip",
    )
    if manifest.get("status") != "success":
        error_code = str(manifest.get("error_code") or "PREFLIGHT_FAILED")
        status_code = 413 if error_code in {
            "ARCHIVE_TOO_LARGE",
            "TOO_MANY_ZIP_MEMBERS",
            "TOO_MANY_KML_FILES",
            "ZIP_TOO_LARGE_UNCOMPRESSED",
            "KML_MEMBER_TOO_LARGE",
            "SUSPICIOUS_COMPRESSION_RATIO",
        } else 400
        raise HTTPException(status_code, f"{error_code}: {manifest.get('error_message') or '地块预检失败'}")
    manifest_sha256 = str(manifest.get("manifest_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", manifest_sha256):
        raise HTTPException(500, "地块预检未生成有效清单哈希")
    preflight_id = f"preflight-{manifest_sha256[:24]}"
    manifest_json = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    con = _db()
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            "INSERT INTO parcel_preflight_manifests "
            "(preflight_id, manifest_json, manifest_sha256, archive_sha256, created_by, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                preflight_id,
                manifest_json,
                manifest_sha256,
                archive_sha256,
                _request_actor(request),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        con.commit()
    except sqlite3.IntegrityError:
        con.rollback()
        existing = con.execute(
            "SELECT preflight_id, manifest_json FROM parcel_preflight_manifests WHERE archive_sha256 = ?",
            (archive_sha256,),
        ).fetchone()
        if not existing:
            raise
        preflight_id = existing["preflight_id"]
        manifest = json.loads(existing["manifest_json"])
    finally:
        con.close()
    _audit(
        preflight_id,
        "parcel_preflight",
        "create_parcel_preflight",
        {"filename": archive_file.filename, "archive_sha256": archive_sha256},
        f"status={manifest['preflight_status']}, parcels={manifest['summary']['parcel_file_count']}",
        actor=_request_actor(request),
    )
    return {"preflight_id": preflight_id, **manifest}


@app.get("/api/v1/policies/parcels/preflights/{preflight_id}")
async def get_parcel_preflight(preflight_id: str) -> dict:
    manifest, _ = _load_preflight_manifest(preflight_id)
    return {"preflight_id": preflight_id, **manifest}


def _parcel_warning_codes(parcel: dict[str, Any]) -> set[str]:
    warnings = list(((parcel.get("validation") or {}).get("warnings") or []))
    for feature in parcel.get("features") or []:
        warnings.extend(((feature.get("validation") or {}).get("warnings") or []))
    return {str(item.get("code")) for item in warnings if isinstance(item, dict) and item.get("code")}


@app.post("/api/v1/policies/parcels/preflights/{preflight_id}/confirm")
async def confirm_parcel_preflight(
    preflight_id: str,
    confirmation: ParcelPreflightConfirmRequest,
    request: Request,
) -> dict:
    """Bind every preflight parcel to an existing immutable policy version; never import it."""
    from parcel_preflight import canonical_geometry_sha256

    manifest, stored = _load_preflight_manifest(preflight_id)
    if not secrets.compare_digest(confirmation.manifest_sha256, stored["manifest_sha256"]):
        raise HTTPException(409, "确认请求绑定的 manifest_sha256 已过期或不一致")
    if manifest.get("preflight_status") == "blocked":
        raise HTTPException(409, "预检清单包含阻断错误，不能确认映射")
    if manifest.get("direct_import_allowed") is not False:
        raise HTTPException(409, "预检清单缺少禁止直接导入标记")

    parcels = manifest.get("parcels") or []
    parcel_by_id = {str(item.get("proposed_parcel_id") or item.get("parcel_id")): item for item in parcels}
    mappings = confirmation.mappings
    mapped_ids = [item.proposed_parcel_id for item in mappings]
    if len(mapped_ids) != len(set(mapped_ids)):
        raise HTTPException(422, "proposed_parcel_id 不能重复")
    if set(mapped_ids) != set(parcel_by_id):
        missing = sorted(set(parcel_by_id) - set(mapped_ids))
        extra = sorted(set(mapped_ids) - set(parcel_by_id))
        raise HTTPException(422, f"必须逐一映射全部预检地块；缺失={missing}，多余={extra}")
    final_keys = [(item.policy_id, item.policy_version_id, item.parcel_id) for item in mappings]
    if len(final_keys) != len(set(final_keys)):
        raise HTTPException(422, "同一保单版本下 parcel_id 不能重复")

    request_payload = confirmation.model_dump(mode="json")
    request_sha256 = hashlib.sha256(
        json.dumps(request_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    con = _db()
    try:
        con.execute("BEGIN IMMEDIATE")
        current_manifest = con.execute(
            "SELECT manifest_sha256, manifest_json FROM parcel_preflight_manifests "
            "WHERE preflight_id = ?",
            (preflight_id,),
        ).fetchone()
        if (
            not current_manifest
            or not secrets.compare_digest(
                current_manifest["manifest_sha256"], stored["manifest_sha256"]
            )
            or current_manifest["manifest_json"] != stored["manifest_json"]
        ):
            raise HTTPException(409, "地块预检清单在确认前发生变化，已拒绝写入")
        if confirmation.idempotency_key:
            existing_batch = con.execute(
                "SELECT request_sha256, receipt_json FROM parcel_confirmation_batches "
                "WHERE preflight_id = ? AND idempotency_key = ?",
                (preflight_id, confirmation.idempotency_key),
            ).fetchone()
            if existing_batch:
                if not secrets.compare_digest(existing_batch["request_sha256"], request_sha256):
                    raise HTTPException(409, "幂等键已用于另一份地块确认请求")
                con.commit()
                return json.loads(existing_batch["receipt_json"])
        if con.execute(
            "SELECT 1 FROM parcel_confirmations WHERE preflight_id = ? LIMIT 1",
            (preflight_id,),
        ).fetchone():
            raise HTTPException(409, "该预检清单已经确认，确认记录不可覆盖")

        policies: dict[tuple[str, str], sqlite3.Row] = {}
        mapped_parcels: list[tuple[Any, dict[str, Any], dict[str, Any]]] = []
        for mapping in mappings:
            parcel = json.loads(json.dumps(parcel_by_id[mapping.proposed_parcel_id], ensure_ascii=False))
            warning_codes = _parcel_warning_codes(parcel)
            if warning_codes and not mapping.resolution_note.strip():
                raise HTTPException(
                    422,
                    f"地块 {mapping.proposed_parcel_id} 存在预检警告，必须填写 resolution_note",
                )
            policy = con.execute(
                "SELECT policy_id, policy_version_id, boundary_geojson FROM policies p "
                "WHERE policy_id = ? AND NOT EXISTS (SELECT 1 FROM deleted_records d "
                "WHERE d.entity_type = 'policy' AND d.entity_id = p.policy_id)",
                (mapping.policy_id,),
            ).fetchone()
            if not policy:
                raise HTTPException(409, f"保单未登记: {mapping.policy_id}")
            registered_version = policy["policy_version_id"] or f"{mapping.policy_id}:v1"
            if registered_version != mapping.policy_version_id:
                raise HTTPException(
                    409,
                    f"保单 {mapping.policy_id} 当前登记版本为 {registered_version}，不能映射到 {mapping.policy_version_id}",
                )
            policy_key = (mapping.policy_id, mapping.policy_version_id)
            if policy_key not in policies:
                existing_version_mapping = con.execute(
                    "SELECT confirmation_batch_id FROM parcel_confirmations "
                    "WHERE policy_id = ? AND policy_version_id = ? LIMIT 1",
                    policy_key,
                ).fetchone()
                if existing_version_mapping:
                    raise HTTPException(
                        409,
                        f"保单 {mapping.policy_id} 版本 {mapping.policy_version_id} 已有不可变地块确认批次，"
                        "变更映射必须登记新的 policy_id/版本",
                    )
                frozen_case = con.execute(
                    "SELECT claim_id, state FROM cases WHERE policy_id = ? "
                    "AND state IN ('REPORT_DRAFTED','HUMAN_REVIEW','ARCHIVED') LIMIT 1",
                    (mapping.policy_id,),
                ).fetchone()
                if frozen_case:
                    raise HTTPException(
                        409,
                        f"保单已被冻结案件 {frozen_case['claim_id']}（{frozen_case['state']}）引用，"
                        "不能追加地块确认",
                    )
            for feature in parcel.get("features") or []:
                geometry = feature.get("geometry")
                expected_hash = feature.get("geometry_sha256")
                if not isinstance(geometry, dict) or not expected_hash:
                    raise HTTPException(409, f"地块 {mapping.proposed_parcel_id} 缺少有效几何")
                if not secrets.compare_digest(expected_hash, canonical_geometry_sha256(geometry)):
                    raise HTTPException(409, f"地块 {mapping.proposed_parcel_id} 几何哈希校验失败")
            policies[policy_key] = policy
            mapping_json = {
                **mapping.model_dump(mode="json"),
                "confirmed": True,
                "confirmed_by": _request_actor(request),
            }
            mapped_parcels.append((mapping, parcel, mapping_json))

        for policy_key, policy in policies.items():
            grouped = [
                parcel
                for mapping, parcel, _ in mapped_parcels
                if (mapping.policy_id, mapping.policy_version_id) == policy_key
            ]
            features = [
                {"type": "Feature", "properties": {}, "geometry": feature["geometry"]}
                for parcel in grouped
                for feature in parcel.get("features") or []
            ]
            mapped_hash = _geometry_sha256({"type": "FeatureCollection", "features": features})
            registered_boundary = json.loads(policy["boundary_geojson"] or "{}")
            registered_hash = _geometry_sha256(registered_boundary)
            if not secrets.compare_digest(mapped_hash, registered_hash):
                raise HTTPException(
                    409,
                    f"映射到保单 {policy_key[0]} 版本 {policy_key[1]} 的地块集合与在册边界不一致；"
                    "请先走独立保单版本登记流程，禁止覆盖已有边界",
                )

        confirmed_at = datetime.now(timezone.utc).isoformat()
        actor = _request_actor(request)
        batch_id = uuid.uuid4().hex
        prepared_rows: list[dict[str, Any]] = []
        receipt_mappings: list[dict[str, Any]] = []
        for mapping, parcel, mapping_json in mapped_parcels:
            parcel_features = [
                {"type": "Feature", "properties": {}, "geometry": feature["geometry"]}
                for feature in parcel.get("features") or []
            ]
            parcel_hash = _geometry_sha256({"type": "FeatureCollection", "features": parcel_features})
            confirmation_id = uuid.uuid4().hex
            mapping_json.update(
                {
                    "confirmed_at": confirmed_at,
                    "confirmation_id": confirmation_id,
                    "confirmation_batch_id": batch_id,
                }
            )
            warning_codes = sorted(_parcel_warning_codes(parcel))
            prepared_rows.append(
                {
                    "confirmation_id": confirmation_id,
                    "mapping": mapping,
                    "parcel": parcel,
                    "mapping_json": mapping_json,
                    "parcel_hash": parcel_hash,
                }
            )
            receipt_mappings.append(
                {
                    "confirmation_id": confirmation_id,
                    "proposed_parcel_id": mapping.proposed_parcel_id,
                    "policy_id": mapping.policy_id,
                    "policy_version_id": mapping.policy_version_id,
                    "parcel_id": mapping.parcel_id,
                    "parcel_boundary_sha256": parcel_hash,
                    "warning_codes": warning_codes,
                    "resolution_note": mapping.resolution_note.strip(),
                }
            )
        receipt = {
            "schema_version": "agrisky.parcel-confirmation-receipt/v1",
            "status": "confirmed",
            "confirmation_batch_id": batch_id,
            "preflight_id": preflight_id,
            "manifest_sha256": stored["manifest_sha256"],
            "archive_sha256": stored["archive_sha256"],
            "confirmed_at": confirmed_at,
            "confirmed_by": actor,
            "confirmation_statement": confirmation.confirmation_statement,
            "comment": confirmation.comment.strip(),
            "request_sha256": request_sha256,
            "idempotency_key": confirmation.idempotency_key,
            "direct_import_performed": False,
            "receipt_download_url": (
                f"/api/v1/policies/parcels/preflights/{preflight_id}/"
                f"confirmation-receipts/{batch_id}"
            ),
            "mappings": receipt_mappings,
        }
        receipt_without_hash = json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        receipt_sha256 = hashlib.sha256(receipt_without_hash.encode("utf-8")).hexdigest()
        receipt["receipt_sha256"] = receipt_sha256
        receipt_json = json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for prepared in prepared_rows:
            mapping = prepared["mapping"]
            con.execute(
                "INSERT INTO parcel_confirmations "
                "(confirmation_id, preflight_id, manifest_sha256, proposed_parcel_id, policy_id, "
                "policy_version_id, parcel_id, parcel_json, mapping_json, parcel_boundary_sha256, "
                "confirmation_batch_id, receipt_sha256, confirmed_by, confirmed_at, comment) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    prepared["confirmation_id"],
                    preflight_id,
                    stored["manifest_sha256"],
                    mapping.proposed_parcel_id,
                    mapping.policy_id,
                    mapping.policy_version_id,
                    mapping.parcel_id,
                    json.dumps(
                        prepared["parcel"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    ),
                    json.dumps(
                        prepared["mapping_json"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    ),
                    prepared["parcel_hash"],
                    batch_id,
                    receipt_sha256,
                    actor,
                    confirmed_at,
                    confirmation.comment.strip(),
                ),
            )
        con.execute(
            "INSERT INTO parcel_confirmation_batches "
            "(confirmation_batch_id, preflight_id, manifest_sha256, idempotency_key, request_sha256, "
            "receipt_json, receipt_sha256, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                batch_id,
                preflight_id,
                stored["manifest_sha256"],
                confirmation.idempotency_key,
                request_sha256,
                receipt_json,
                receipt_sha256,
                confirmed_at,
            ),
        )
        con.execute(
            "INSERT INTO audit_log VALUES (?,?,?,?,?,?,?,?,?)",
            (
                uuid.uuid4().hex,
                preflight_id,
                "confirm_parcel_mappings",
                "confirm_parcel_preflight",
                json.dumps(
                    {
                        "manifest_sha256": stored["manifest_sha256"],
                        "mapping_count": len(mappings),
                        "confirmation_batch_id": batch_id,
                        "receipt_sha256": receipt_sha256,
                    },
                    ensure_ascii=False,
                ),
                f"batch={batch_id}, direct_import=false",
                None,
                actor,
                confirmed_at,
            ),
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return receipt


@app.get(
    "/api/v1/policies/parcels/preflights/{preflight_id}/"
    "confirmation-receipts/{confirmation_batch_id}"
)
async def download_parcel_confirmation_receipt(
    preflight_id: str,
    confirmation_batch_id: str,
    request: Request,
) -> Response:
    """Download an immutable parcel-mapping receipt after integrity and ownership checks."""
    _load_preflight_manifest(preflight_id)
    if not re.fullmatch(r"[0-9a-f]{32}", confirmation_batch_id or ""):
        raise HTTPException(400, "地块确认批次编号无效")
    portal_token = (
        request.headers.get("x-agrisky-token", "").strip()
        or request.cookies.get(SESSION_COOKIE_NAME, "").strip()
    )
    portal_user = _auth_user(portal_token) if portal_token else None
    con = _db()
    row = con.execute(
        "SELECT receipt_json, receipt_sha256 FROM parcel_confirmation_batches "
        "WHERE preflight_id = ? AND confirmation_batch_id = ?",
        (preflight_id, confirmation_batch_id),
    ).fetchone()
    if portal_user and portal_user.get("role") != "admin":
        owners = con.execute(
            "SELECT DISTINCT p.holder_account FROM parcel_confirmations pc "
            "JOIN policies p ON pc.policy_id = p.policy_id "
            "WHERE pc.preflight_id = ? AND pc.confirmation_batch_id = ?",
            (preflight_id, confirmation_batch_id),
        ).fetchall()
        if not owners or any(owner["holder_account"] != portal_user.get("username") for owner in owners):
            con.close()
            raise HTTPException(403, "无权下载其他投保人的地块确认回执")
    con.close()
    if not row:
        raise HTTPException(404, "地块确认回执不存在")
    try:
        receipt = json.loads(row["receipt_json"])
    except (json.JSONDecodeError, TypeError) as exc:
        raise HTTPException(409, "地块确认回执损坏") from exc
    claimed_hash = str(receipt.pop("receipt_sha256", ""))
    canonical = json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    actual_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if (
        not secrets.compare_digest(str(row["receipt_sha256"]), actual_hash)
        or not secrets.compare_digest(claimed_hash, actual_hash)
    ):
        raise HTTPException(409, "地块确认回执完整性校验失败")
    receipt["receipt_sha256"] = actual_hash
    content = json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    content_sha256 = hashlib.sha256(content).hexdigest()
    _audit(
        preflight_id,
        "download_parcel_confirmation_receipt",
        "download_parcel_confirmation_receipt",
        {"confirmation_batch_id": confirmation_batch_id},
        f"sha256={actual_hash}",
        actor=_request_actor(request),
    )
    return Response(
        content=content,
        media_type="application/vnd.agrisky.parcel-confirmation-receipt+json",
        headers={
            "Content-Disposition": (
                f'attachment; filename="parcel-confirmation-{confirmation_batch_id}.json"'
            ),
            "X-Content-SHA256": content_sha256,
            "X-Receipt-SHA256": actual_hash,
        },
    )


def _confirmed_parcel_payload(rows: list[sqlite3.Row]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for row in rows:
        items.append(
            {
                "confirmation_id": row["confirmation_id"],
                "preflight_id": row["preflight_id"],
                "manifest_sha256": row["manifest_sha256"],
                "policy_id": row["policy_id"],
                "policy_version_id": row["policy_version_id"],
                "parcel_id": row["parcel_id"],
                "parcel_boundary_sha256": row["parcel_boundary_sha256"],
                "confirmation_batch_id": row["confirmation_batch_id"],
                "receipt_sha256": row["receipt_sha256"],
                "receipt_download_url": (
                    f"/api/v1/policies/parcels/preflights/{row['preflight_id']}/"
                    f"confirmation-receipts/{row['confirmation_batch_id']}"
                ),
                "mapping": json.loads(row["mapping_json"]),
                "parcel": json.loads(row["parcel_json"]),
                "confirmed_by": row["confirmed_by"],
                "confirmed_at": row["confirmed_at"],
            }
        )
    return {"total": len(items), "direct_import_performed": False, "items": items}


@app.get("/api/v1/policies/{policy_id}/parcels")
async def get_confirmed_policy_parcels(policy_id: str, policy_version_id: str | None = None) -> dict:
    con = _db()
    policy = con.execute(
        "SELECT policy_version_id FROM policies p WHERE policy_id = ? "
        "AND NOT EXISTS (SELECT 1 FROM deleted_records d "
        "WHERE d.entity_type = 'policy' AND d.entity_id = p.policy_id)",
        (policy_id,),
    ).fetchone()
    con.close()
    if not policy:
        raise HTTPException(404, "保单未登记")
    version = policy_version_id or policy["policy_version_id"] or f"{policy_id}:v1"
    return _confirmed_parcel_payload(_confirmed_parcel_rows(policy_id, version))


@app.get("/api/v1/cases/{claim_id}/parcels")
async def get_confirmed_case_parcels(claim_id: str) -> dict:
    claim_id = _safe_claim_file_id(claim_id)
    con = _db()
    row = con.execute(
        "SELECT c.policy_id, p.policy_version_id FROM cases c LEFT JOIN policies p "
        "ON c.policy_id = p.policy_id WHERE c.claim_id = ?",
        (claim_id,),
    ).fetchone()
    con.close()
    if not row:
        raise HTTPException(404, "案件不存在")
    version = row["policy_version_id"] or f"{row['policy_id']}:v1"
    return _confirmed_parcel_payload(_confirmed_parcel_rows(row["policy_id"], version))


@app.post("/api/v1/policies")
async def register_policy(req: PolicyRegisterRequest) -> dict:
    """登记/更新一个保单的承保地块边界（JSON）。"""
    if not req.boundary_geojson:
        raise HTTPException(400, "缺少 boundary_geojson")
    return _save_policy(
        req.policy_id,
        req.holder_name,
        req.crop_type,
        req.address,
        req.boundary_geojson,
        req.policy_version_id,
    )


@app.post("/api/v1/policies/upload")
async def register_policy_upload(
    policy_id: str = Form(...),
    policy_version_id: str | None = Form(None),
    boundary_file: UploadFile = File(...),
    contract_file: UploadFile = File(...),
    holder_name: str = Form(""),
    crop_type: str = Form(""),
    address: str = Form(""),
) -> dict:
    """同时上传合同原件与边界，登记后冻结合同版本和文件哈希。"""
    from spatial_utils import load_boundary_from_upload
    data = await boundary_file.read()
    br = load_boundary_from_upload(data, boundary_file.filename or "b.geojson")
    if br["status"] != "success":
        raise HTTPException(400, f"边界加载失败: {br.get('error_message')}")
    contract_data = await contract_file.read()
    resolved_version_id = (policy_version_id or f"{policy_id}:v1").strip()
    try:
        parsed_contract = parse_uploaded_contract(
            {
                "policy_id": policy_id,
                "policy_version_id": resolved_version_id,
                "holder_name": holder_name,
                "crop_type": crop_type,
                "address": address,
                "area_mu": _geojson_area_mu(br["geojson"]),
            },
            filename=contract_file.filename or "contract.docx",
            data=contract_data,
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(422, f"保险合同解析失败: {exc}") from exc
    return _save_policy(
        policy_id,
        holder_name,
        crop_type,
        address,
        br["geojson"],
        policy_version_id,
        (parsed_contract, contract_data, contract_file.filename or "contract.docx"),
    )


@app.get("/api/v1/contracts/template/download")
async def download_contract_template(
    policy_id: str = "POL-DEMO-001",
    holder_name: str = "示范种植合作社",
    crop_type: str = "rice",
    address: str = "示范承保地块",
    area_mu: float = 1000.0,
    insurance_year: int = 2025,
) -> FileResponse:
    """Download a crop-specific simulated contract that can be signed and re-uploaded."""
    if crop_type.lower() not in {"rice", "corn", "maize", "wheat"}:
        raise HTTPException(422, "示范合同模板仅支持水稻、玉米和小麦")
    contract = build_simulated_contract(
        {
            "policy_id": policy_id,
            "policy_version_id": f"{policy_id}:v1",
            "holder_name": holder_name,
            "crop_type": crop_type,
            "address": address,
            "area_mu": area_mu,
        },
        insurance_year=insurance_year,
    )
    key = hashlib.sha256(
        f"{policy_id}|{holder_name}|{crop_type}|{address}|{area_mu}|{insurance_year}".encode("utf-8")
    ).hexdigest()[:16]
    path = (OUTPUT_ROOT / "contract_templates" / f"{key}.docx").resolve()
    templates_root = (OUTPUT_ROOT / "contract_templates").resolve()
    if not path.is_relative_to(templates_root):
        raise HTTPException(500, "合同模板路径无效")
    render_contract_docx(contract, path)
    return FileResponse(
        path,
        filename=f"Agrisky-{contract.get('crop_name')}-保险合同模板-{policy_id}.docx",
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/v1/compliance/library")
async def get_compliance_library() -> dict[str, Any]:
    return public_library_payload()


@app.get("/api/v1/compliance/documents/{document_id}/download")
async def download_compliance_document(document_id: str) -> FileResponse:
    resolved = legal_document_path(document_id)
    if not resolved:
        raise HTTPException(404, "法规文件不存在")
    item, path = resolved
    media_type = "application/pdf" if path.suffix.lower() == ".pdf" else "text/html; charset=utf-8"
    return FileResponse(
        path,
        filename=path.name,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=3600", "X-Content-SHA256": item["sha256"]},
    )


@app.get("/api/v1/policies")
async def list_policies() -> dict:
    """保单库列表（不含完整边界，含面积/作物/投保人）。首次访问时懒补算缺失面积。"""
    con = _db()
    # 懒补算：种子/历史行可能 area_mu 为空，首次列出时算一次并落库
    for r in con.execute(
        "SELECT policy_id, boundary_geojson FROM policies p WHERE area_mu IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM deleted_records d "
        "WHERE d.entity_type = 'policy' AND d.entity_id = p.policy_id)"
    ).fetchall():
        try:
            a = _geojson_area_mu(json.loads(r["boundary_geojson"]))
            if a is not None:
                con.execute("UPDATE policies SET area_mu = ? WHERE policy_id = ?", (a, r["policy_id"]))
        except Exception:  # noqa: BLE001
            continue
    con.commit()
    rows = con.execute(
        "SELECT policy_id, policy_version_id, holder_name, crop_type, address, area_mu, created_at "
        "FROM policies p WHERE NOT EXISTS (SELECT 1 FROM deleted_records d "
        "WHERE d.entity_type = 'policy' AND d.entity_id = p.policy_id) "
        "ORDER BY created_at DESC"
    ).fetchall()
    con.close()
    items = []
    for row in rows:
        item = dict(row)
        item["policy_version_id"] = item.get("policy_version_id") or f"{item['policy_id']}:v1"
        items.append(item)
    return {"total": len(items), "items": items}


@app.delete("/api/v1/policies/{policy_id}")
async def delete_policy(policy_id: str, request: Request) -> dict:
    """Hide an unreferenced policy while retaining its registered evidence."""
    actor = _require_admin_actor(request)
    con = _db()
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT policy_id FROM policies WHERE policy_id = ?",
            (policy_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "保单未登记")
        if _is_deleted(con, "policy", policy_id):
            con.rollback()
            return {
                "status": "success",
                "policy_id": policy_id,
                "already_deleted": True,
                "evidence_retained": True,
            }
        active_cases = con.execute(
            "SELECT claim_id FROM cases c WHERE c.policy_id = ? "
            "AND NOT EXISTS (SELECT 1 FROM deleted_records d "
            "WHERE d.entity_type = 'case' AND d.entity_id = c.claim_id) "
            "ORDER BY c.reported_at DESC",
            (policy_id,),
        ).fetchall()
        if active_cases:
            raise HTTPException(
                409,
                f"该保单仍被 {len(active_cases)} 个未删除案件引用，请先在案件队列删除关联案件",
            )
        con.execute(
            "INSERT INTO deleted_records (entity_type, entity_id, deleted_at, deleted_by) "
            "VALUES ('policy', ?, ?, ?)",
            (policy_id, datetime.now().isoformat(), actor),
        )
        con.commit()
    except HTTPException:
        con.rollback()
        raise
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    _audit(
        f"POLICY:{policy_id}",
        "delete_policy",
        "delete_policy",
        {"policy_id": policy_id},
        "保单已从在册列表移除；边界、地块确认和审计记录保留",
        actor=actor,
    )
    return {
        "status": "success",
        "policy_id": policy_id,
        "already_deleted": False,
        "evidence_retained": True,
    }


@app.get("/api/v1/policies/{policy_id}/contract")
async def get_policy_contract(policy_id: str) -> dict:
    con = _db()
    try:
        row = con.execute(
            "SELECT * FROM policies p WHERE policy_id = ? "
            "AND NOT EXISTS (SELECT 1 FROM deleted_records d "
            "WHERE d.entity_type = 'policy' AND d.entity_id = p.policy_id)",
            (policy_id,),
        ).fetchone()
    finally:
        con.close()
    if not row:
        raise HTTPException(404, f"保单未登记: {policy_id}")
    contract = _preferred_policy_contract(dict(row))
    return {
        "contract": contract_summary(
            contract, f"/api/v1/policies/{policy_id}/contract/download"
        ),
        "clauses": contract.get("clauses") or [],
    }


@app.get("/api/v1/policies/{policy_id}/contract/download")
async def download_policy_contract(policy_id: str) -> FileResponse:
    con = _db()
    try:
        row = con.execute(
            "SELECT * FROM policies p WHERE policy_id = ? "
            "AND NOT EXISTS (SELECT 1 FROM deleted_records d "
            "WHERE d.entity_type = 'policy' AND d.entity_id = p.policy_id)",
            (policy_id,),
        ).fetchone()
    finally:
        con.close()
    if not row:
        raise HTTPException(404, f"保单未登记: {policy_id}")
    contract = _preferred_policy_contract(dict(row))
    path = _contract_artifact_path(str(contract.get("artifact_relative_path") or ""))
    if not path.is_file():
        raise HTTPException(404, "保险合同附件不存在")
    if _sha256_file(path) != contract.get("artifact_sha256"):
        raise HTTPException(409, "保险合同附件完整性校验失败")
    headers = {
        "Cache-Control": "no-store",
        "X-Content-SHA256": str(contract.get("artifact_sha256") or ""),
        "X-Contract-Version": str(contract.get("contract_version") or ""),
    }
    return FileResponse(
        path,
        filename=path.name,
        media_type=(
            "application/pdf"
            if path.suffix.lower() == ".pdf"
            else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        headers=headers,
    )


@app.get("/api/v1/policies/{policy_id}")
async def get_policy(policy_id: str) -> dict:
    """单个保单详情，含原始边界与归一化 roi_geojson（供理赔流程直接取用）。"""
    con = _db()
    row = con.execute(
        "SELECT * FROM policies p WHERE policy_id = ? "
        "AND NOT EXISTS (SELECT 1 FROM deleted_records d "
        "WHERE d.entity_type = 'policy' AND d.entity_id = p.policy_id)",
        (policy_id,),
    ).fetchone()
    con.close()
    if not row:
        raise HTTPException(404, f"保单未登记: {policy_id}")
    try:
        raw = json.loads(row["boundary_geojson"]) if row["boundary_geojson"] else None
    except (json.JSONDecodeError, TypeError):
        raw = None
    boundary_sha256 = None
    if raw:
        try:
            boundary_sha256 = _geometry_sha256(raw)
        except (TypeError, ValueError):
            boundary_sha256 = None
    contract = _preferred_policy_contract(dict(row))
    return {
        "policy_id": row["policy_id"], "policy_version_id": row["policy_version_id"] or f"{policy_id}:v1",
        "holder_name": row["holder_name"], "crop_type": row["crop_type"],
        "address": row["address"], "area_mu": row["area_mu"], "created_at": row["created_at"],
        "boundary_geojson": raw, "roi_geojson": _normalize_geometry(raw) if raw else None,
        "boundary_registered": boundary_sha256 is not None,
        "boundary_sha256": boundary_sha256,
        "boundary_hash_scheme": "canonical_geometry_v1" if boundary_sha256 else None,
        "contract": contract_summary(
            contract, f"/api/v1/policies/{policy_id}/contract/download"
        ),
    }


# ═══════════════════════════════════════════════════════════
# 理赔材料理解：私有上传、字段引用、冲突核验与人工确认
# ═══════════════════════════════════════════════════════════

MATERIALS_ROOT = OUTPUT_ROOT / "materials"
MATERIALS_ROOT.mkdir(parents=True, exist_ok=True)
REQUIRE_CLAIM_DOCUMENTS = os.getenv(
    "AGRISKY_REQUIRE_CLAIM_DOCUMENTS", "false"
).lower() in {"1", "true", "yes", "on"}


def _material_actor(request: Request) -> str:
    username = getattr(request.state, "agrisky_user", None)
    role = getattr(request.state, "agrisky_role", None) or "unknown"
    return f"session:{username}" if username else f"api-key:{role}"


def _document_storage_path(claim_id: str, document_id: str, filename: str) -> tuple[Path, str]:
    claim_key = hashlib.sha256(claim_id.encode("utf-8")).hexdigest()[:20]
    relative = Path("materials") / claim_key / document_id / safe_filename(filename)
    path = (OUTPUT_ROOT / relative).resolve()
    if not path.is_relative_to(MATERIALS_ROOT.resolve()):
        raise HTTPException(400, "材料存储路径非法")
    return path, relative.as_posix()


def _document_row(document_id: str, claim_id: str | None = None) -> sqlite3.Row:
    con = _db()
    if claim_id:
        row = con.execute(
            "SELECT * FROM case_documents WHERE document_id = ? AND claim_id = ?",
            (document_id, claim_id),
        ).fetchone()
    else:
        row = con.execute(
            "SELECT * FROM case_documents WHERE document_id = ?", (document_id,)
        ).fetchone()
    con.close()
    if not row:
        raise HTTPException(404, "材料不存在")
    return row


def _material_case_and_policy(claim_id: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    con = _db()
    case_row = con.execute(
        "SELECT * FROM cases c WHERE claim_id = ? "
        "AND NOT EXISTS (SELECT 1 FROM deleted_records d "
        "WHERE d.entity_type = 'case' AND d.entity_id = c.claim_id)",
        (claim_id,),
    ).fetchone()
    if not case_row:
        con.close()
        raise HTTPException(404, f"案件不存在: {claim_id}")
    policy_row = con.execute(
        "SELECT * FROM policies p WHERE p.policy_id = ? "
        "AND NOT EXISTS (SELECT 1 FROM deleted_records d "
        "WHERE d.entity_type = 'policy' AND d.entity_id = p.policy_id)",
        (case_row["policy_id"],),
    ).fetchone()
    con.close()
    return dict(case_row), dict(policy_row) if policy_row else None


def _serialize_document(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    return {
        key: item.get(key)
        for key in (
            "document_id",
            "claim_id",
            "document_type",
            "original_filename",
            "media_type",
            "size_bytes",
            "sha256",
            "parse_status",
            "uploaded_by",
            "uploaded_at",
        )
    }


def _latest_material_fields(con: sqlite3.Connection, claim_id: str) -> list[dict[str, Any]]:
    rows = con.execute(
        "SELECT f.* FROM document_fields f "
        "JOIN document_extraction_runs r ON r.extraction_id = f.extraction_id "
        "WHERE f.claim_id = ? AND r.status = 'completed' "
        "AND r.extraction_id = ("
        "SELECT r2.extraction_id FROM document_extraction_runs r2 "
        "WHERE r2.document_id = r.document_id AND r2.status = 'completed' "
        "ORDER BY COALESCE(r2.completed_at, r2.started_at) DESC, r2.started_at DESC "
        "LIMIT 1"
        ") "
        "ORDER BY f.created_at DESC",
        (claim_id,),
    ).fetchall()
    seen: set[tuple[str, str]] = set()
    fields: list[dict[str, Any]] = []
    for row in rows:
        key = (row["document_id"], row["field_name"])
        if key in seen:
            continue
        seen.add(key)
        fields.append(
            {
                "field_id": row["field_id"],
                "extraction_id": row["extraction_id"],
                "document_id": row["document_id"],
                "field_name": row["field_name"],
                "normalized_value": json.loads(row["normalized_value_json"]),
                "raw_text": row["raw_text"],
                "confidence": row["confidence"],
                "page_number": row["page_number"],
                "bbox": json.loads(row["bbox_json"]) if row["bbox_json"] else None,
                "source_ref": row["source_ref"],
                "extractor": row["extractor"],
                "created_at": row["created_at"],
            }
        )
    return fields


def _material_review_summary(claim_id: str) -> dict[str, Any]:
    case, _ = _material_case_and_policy(claim_id)
    con = _db()
    documents = con.execute(
        "SELECT * FROM case_documents WHERE claim_id = ? ORDER BY uploaded_at DESC",
        (claim_id,),
    ).fetchall()
    fields = _latest_material_fields(con, claim_id)
    findings = [
        dict(row)
        for row in con.execute(
            "SELECT * FROM document_findings WHERE claim_id = ? AND status = 'open' "
            "ORDER BY CASE severity WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, created_at DESC",
            (claim_id,),
        ).fetchall()
    ]
    confirmation_rows = con.execute(
        "SELECT target_type, target_id, decision, confirmed_value_json, note, confirmed_by, confirmed_at "
        "FROM document_confirmations WHERE claim_id = ? ORDER BY confirmed_at DESC",
        (claim_id,),
    ).fetchall()
    con.close()
    confirmations = []
    for row in confirmation_rows:
        item = dict(row)
        item["confirmed_value"] = (
            json.loads(item.pop("confirmed_value_json"))
            if item.get("confirmed_value_json")
            else None
        )
        confirmations.append(item)

    present_types = {row["document_type"] for row in documents if row["parse_status"] == "completed"}
    required_types = (
        required_document_types(case.get("disaster_type"))
        if REQUIRE_CLAIM_DOCUMENTS
        else []
    )
    missing_types = [item for item in required_types if item not in present_types]
    blocking = [
        item
        for item in findings
        if item["severity"] == "high" and item["status"] == "open"
    ]
    failed_documents = [
        _serialize_document(row)
        for row in documents
        if row["parse_status"] == "failed"
    ]
    passed = not missing_types and not blocking and not failed_documents
    return {
        "claim_id": claim_id,
        "required": REQUIRE_CLAIM_DOCUMENTS,
        "passed": passed,
        "required_document_types": [
            {"code": item, "label": DOCUMENT_TYPES[item]} for item in required_types
        ],
        "missing_document_types": [
            {"code": item, "label": DOCUMENT_TYPES[item]} for item in missing_types
        ],
        "documents": [_serialize_document(row) for row in documents],
        "fields": fields,
        "findings": findings,
        "confirmations": confirmations,
        "blocking_count": len(blocking) + len(failed_documents),
    }


def _extract_document(document_id: str) -> dict[str, Any]:
    row = _document_row(document_id)
    path = (OUTPUT_ROOT / row["storage_relpath"]).resolve()
    if not path.is_file() or not path.is_relative_to(MATERIALS_ROOT.resolve()):
        raise HTTPException(409, "材料文件缺失或路径非法")

    extraction_id = f"DEX-{uuid.uuid4().hex[:16].upper()}"
    started_at = datetime.now().isoformat()
    con = _db()
    con.execute(
        "INSERT INTO document_extraction_runs "
        "(extraction_id, document_id, status, started_at) VALUES (?,?,?,?)",
        (extraction_id, document_id, "running", started_at),
    )
    con.execute(
        "UPDATE case_documents SET parse_status = 'processing' WHERE document_id = ?",
        (document_id,),
    )
    con.commit()
    con.close()

    try:
        lines, text_engine = extract_evidence(path, row["media_type"])
        deterministic = deterministic_fields(lines)
        semantic, llm_error = llm_fields(lines, row["document_type"])
        fields = normalize_fields_for_document_type(
            merge_fields(deterministic, semantic), row["document_type"]
        )
        case, policy = _material_case_and_policy(row["claim_id"])
        findings = compare_material_fields(case, policy, fields)
        if not lines:
            findings.append(
                {
                    "code": "NO_TEXT_EXTRACTED",
                    "severity": "high",
                    "field_name": None,
                    "expected_value": "可读取的材料内容",
                    "actual_value": None,
                    "source_ref": None,
                    "message": "材料未提取到可读文字，需要重新扫描或人工处理",
                }
            )
        result_payload = {
            "document_id": document_id,
            "line_count": len(lines),
            "field_count": len(fields),
            "finding_count": len(findings),
            "text_engine": text_engine,
            "semantic_engine": "agent-model" if semantic else "not-used",
            "semantic_warning": llm_error,
        }
        result_sha = hashlib.sha256(
            json.dumps(
                {"lines": [line.__dict__ for line in lines], "fields": fields, "findings": findings},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        completed_at = datetime.now().isoformat()
        con = _db()
        con.execute(
            "UPDATE document_extraction_runs SET status = 'completed', engine = ?, model = ?, "
            "result_sha256 = ?, error_message = ?, completed_at = ? WHERE extraction_id = ?",
            (
                text_engine,
                os.getenv("AGENT_MODEL") if semantic else None,
                result_sha,
                llm_error,
                completed_at,
                extraction_id,
            ),
        )
        con.execute(
            "UPDATE case_documents SET parse_status = 'completed' WHERE document_id = ?",
            (document_id,),
        )
        con.execute(
            "UPDATE document_findings SET status = 'superseded' "
            "WHERE status = 'open' AND extraction_id IN "
            "(SELECT extraction_id FROM document_extraction_runs "
            "WHERE document_id = ? AND extraction_id <> ?)",
            (document_id, extraction_id),
        )
        for item in fields:
            con.execute(
                "INSERT INTO document_fields "
                "(field_id, extraction_id, document_id, claim_id, field_name, normalized_value_json, "
                "raw_text, confidence, page_number, bbox_json, source_ref, extractor, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"FIELD-{uuid.uuid4().hex[:16].upper()}",
                    extraction_id,
                    document_id,
                    row["claim_id"],
                    item["field_name"],
                    json.dumps(item["normalized_value"], ensure_ascii=False),
                    item["raw_text"],
                    item["confidence"],
                    item["page_number"],
                    json.dumps(item.get("bbox"), ensure_ascii=False) if item.get("bbox") else None,
                    item["source_ref"],
                    item["extractor"],
                    completed_at,
                ),
            )
        for item in findings:
            con.execute(
                "INSERT INTO document_findings "
                "(finding_id, claim_id, extraction_id, code, severity, field_name, expected_value_json, "
                "actual_value_json, source_ref, message, status, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"FIND-{uuid.uuid4().hex[:16].upper()}",
                    row["claim_id"],
                    extraction_id,
                    item["code"],
                    item["severity"],
                    item.get("field_name"),
                    json.dumps(item.get("expected_value"), ensure_ascii=False),
                    json.dumps(item.get("actual_value"), ensure_ascii=False),
                    item.get("source_ref"),
                    item["message"],
                    "open",
                    completed_at,
                ),
            )
        con.commit()
        con.close()
        _audit(
            row["claim_id"],
            "extract_document",
            "document_intelligence",
            {"document_id": document_id, "document_sha256": row["sha256"]},
            f"fields={len(fields)}; findings={len(findings)}; result_sha256={result_sha}",
        )
        return {**result_payload, "extraction_id": extraction_id, "result_sha256": result_sha}
    except Exception as exc:
        completed_at = datetime.now().isoformat()
        con = _db()
        con.execute(
            "UPDATE document_extraction_runs SET status = 'failed', error_message = ?, "
            "completed_at = ? WHERE extraction_id = ?",
            (str(exc)[:1000], completed_at, extraction_id),
        )
        con.execute(
            "UPDATE case_documents SET parse_status = 'failed' WHERE document_id = ?",
            (document_id,),
        )
        con.commit()
        con.close()
        _audit(
            row["claim_id"],
            "extract_document_failed",
            "document_intelligence",
            {"document_id": document_id},
            str(exc)[:500],
        )
        raise HTTPException(422, f"材料解析失败: {exc}") from exc


@app.post("/api/v1/cases/{claim_id}/documents")
async def upload_case_document(
    claim_id: str,
    request: Request,
    document_type: str = Form(...),
    document: UploadFile = File(...),
) -> dict:
    _assert_case_download_access(claim_id, request)
    _material_case_and_policy(claim_id)
    if document_type not in DOCUMENT_TYPES:
        raise HTTPException(400, "材料类型不受支持")
    data = await document.read()
    filename = safe_filename(document.filename or "document")
    try:
        media_type = detect_media_type(filename, data)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    digest = sha256_bytes(data)
    con = _db()
    existing = con.execute(
        "SELECT * FROM case_documents WHERE claim_id = ? AND sha256 = ?",
        (claim_id, digest),
    ).fetchone()
    con.close()
    if existing:
        return {
            "status": "duplicate",
            "document": _serialize_document(existing),
            "review": _material_review_summary(claim_id),
        }

    document_id = f"DOC-{uuid.uuid4().hex[:16].upper()}"
    path, relpath = _document_storage_path(claim_id, document_id, filename)
    path.parent.mkdir(parents=True, exist_ok=False)
    path.write_bytes(data)
    uploaded_at = datetime.now().isoformat()
    actor = _material_actor(request)
    con = _db()
    con.execute(
        "INSERT INTO case_documents "
        "(document_id, claim_id, document_type, original_filename, stored_filename, media_type, "
        "size_bytes, sha256, storage_relpath, parse_status, uploaded_by, uploaded_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            document_id,
            claim_id,
            document_type,
            filename,
            path.name,
            media_type,
            len(data),
            digest,
            relpath,
            "pending",
            actor,
            uploaded_at,
        ),
    )
    con.commit()
    con.close()
    _audit(
        claim_id,
        "upload_document",
        "document_intelligence",
        {"document_id": document_id, "document_type": document_type, "sha256": digest},
        f"uploaded={filename}; bytes={len(data)}",
    )
    extraction = await asyncio.to_thread(_extract_document, document_id)
    return {
        "status": "success",
        "document": _serialize_document(_document_row(document_id)),
        "extraction": extraction,
        "review": _material_review_summary(claim_id),
    }


@app.get("/api/v1/cases/{claim_id}/documents")
def list_case_documents(claim_id: str, request: Request) -> dict:
    _assert_case_download_access(claim_id, request)
    return _material_review_summary(claim_id)


@app.get("/api/v1/cases/{claim_id}/documents/{document_id}/content")
def download_case_document(claim_id: str, document_id: str, request: Request) -> FileResponse:
    _assert_case_download_access(claim_id, request)
    row = _document_row(document_id, claim_id)
    path = (OUTPUT_ROOT / row["storage_relpath"]).resolve()
    if not path.is_file() or not path.is_relative_to(MATERIALS_ROOT.resolve()):
        raise HTTPException(404, "材料文件不存在")
    _audit(
        claim_id,
        "download_document",
        "document_intelligence",
        {"document_id": document_id},
        f"sha256={row['sha256']}",
    )
    return FileResponse(path, media_type=row["media_type"], filename=row["original_filename"])


@app.post("/api/v1/cases/{claim_id}/documents/{document_id}/extract")
async def reextract_case_document(
    claim_id: str, document_id: str, request: Request
) -> dict:
    _assert_case_download_access(claim_id, request)
    _document_row(document_id, claim_id)
    extraction = await asyncio.to_thread(_extract_document, document_id)
    return {"status": "success", "extraction": extraction, "review": _material_review_summary(claim_id)}


@app.get("/api/v1/cases/{claim_id}/material-review")
def get_material_review(claim_id: str, request: Request) -> dict:
    _assert_case_download_access(claim_id, request)
    return _material_review_summary(claim_id)


@app.post("/api/v1/cases/{claim_id}/material-findings/{finding_id}/resolve")
def resolve_material_finding(
    claim_id: str,
    finding_id: str,
    req: MaterialFindingResolutionRequest,
    request: Request,
) -> dict:
    _assert_case_download_access(claim_id, request)
    actor = _material_actor(request)
    con = _db()
    finding = con.execute(
        "SELECT * FROM document_findings WHERE finding_id = ? AND claim_id = ?",
        (finding_id, claim_id),
    ).fetchone()
    if not finding:
        con.close()
        raise HTTPException(404, "材料问题不存在")
    if finding["status"] != "open":
        con.close()
        raise HTTPException(409, "材料问题已经处理")
    if req.decision == "corrected" and req.corrected_value is None:
        con.close()
        raise HTTPException(400, "修正材料问题时必须提供修正值")
    confirmed_at = datetime.now().isoformat()
    confirmation_id = f"CONF-{uuid.uuid4().hex[:16].upper()}"
    receipt = {
        "confirmation_id": confirmation_id,
        "claim_id": claim_id,
        "target_type": "finding",
        "target_id": finding_id,
        "decision": req.decision,
        "corrected_value": req.corrected_value,
        "note": req.note,
        "confirmed_by": actor,
        "confirmed_at": confirmed_at,
    }
    receipt_sha = hashlib.sha256(
        json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    con.execute(
        "INSERT INTO document_confirmations "
        "(confirmation_id, claim_id, target_type, target_id, decision, confirmed_value_json, note, "
        "confirmed_by, confirmed_at, receipt_sha256) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            confirmation_id,
            claim_id,
            "finding",
            finding_id,
            req.decision,
            json.dumps(req.corrected_value, ensure_ascii=False)
            if req.corrected_value is not None
            else None,
            req.note,
            actor,
            confirmed_at,
            receipt_sha,
        ),
    )
    con.execute(
        "UPDATE document_findings SET status = 'resolved', resolved_by = ?, resolved_at = ?, "
        "resolution_note = ? WHERE finding_id = ?",
        (actor, confirmed_at, req.note, finding_id),
    )
    con.commit()
    con.close()
    _audit(
        claim_id,
        "resolve_material_finding",
        "document_intelligence",
        {"finding_id": finding_id, "decision": req.decision},
        f"receipt_sha256={receipt_sha}",
    )
    return {
        "status": "success",
        "confirmation": {**receipt, "receipt_sha256": receipt_sha},
        "review": _material_review_summary(claim_id),
    }


# ═══════════════════════════════════════════════════════════
# 对话式智能体（OpenAI-compatible Function Calling）
# ═══════════════════════════════════════════════════════════

def _agent_scope_from_request(request: Request) -> tuple[dict | None, dict | None]:
    portal_token = (
        request.headers.get("x-agrisky-token", "").strip()
        or request.cookies.get(SESSION_COOKIE_NAME, "").strip()
    )
    user = _auth_user(portal_token)
    if portal_token and not user:
        raise HTTPException(401, "未登录或会话已失效")
    scope = None
    if user and user.get("role") != "admin":
        con = _db()
        rows = con.execute(
            "SELECT policy_id FROM policies p WHERE holder_account = ? "
            "AND NOT EXISTS (SELECT 1 FROM deleted_records d "
            "WHERE d.entity_type = 'policy' AND d.entity_id = p.policy_id)",
            (user["username"],),
        ).fetchall()
        con.close()
        scope = {
            "holder_name": user["holder_name"],
            "username": user["username"],
            "policy_ids": [row["policy_id"] for row in rows],
        }
    return scope, user


@app.get("/api/v1/agent/health")
def agent_health(request: Request, probe: bool = False) -> dict:
    import sys
    if str(BASE_DIR) not in sys.path:
        sys.path.insert(0, str(BASE_DIR))
    from agent.agent_runtime import provider_health

    scope, user = _agent_scope_from_request(request)
    del scope
    provider = provider_health(probe=probe)
    return {
        **provider,
        "gateway": "ready",
        "actor": user["username"] if user else "service-client",
        "document_intelligence": {
            "status": "ready",
            "required_for_new_claims": REQUIRE_CLAIM_DOCUMENTS,
            "supported_types": DOCUMENT_TYPES,
        },
    }


def _append_agent_run_event(run_id: str, event_type: str, payload: dict[str, Any]) -> None:
    con = _db()
    row = con.execute(
        "SELECT COALESCE(MAX(sequence_no), 0) AS value FROM agent_run_events WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    sequence = int(row["value"]) + 1
    con.execute(
        "INSERT INTO agent_run_events "
        "(event_id, run_id, sequence_no, event_type, event_json, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (
            f"EVT-{uuid.uuid4().hex[:16].upper()}",
            run_id,
            sequence,
            event_type,
            json.dumps(payload, ensure_ascii=False, default=str),
            datetime.now().isoformat(),
        ),
    )
    con.commit()
    con.close()


def _agent_run_cancel_requested(run_id: str) -> bool:
    con = _db()
    row = con.execute(
        "SELECT cancel_requested FROM agent_runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    con.close()
    return bool(row and row["cancel_requested"])


def _execute_agent_run(run_id: str) -> None:
    con = _db()
    row = con.execute("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
    if not row:
        con.close()
        return
    con.execute(
        "UPDATE agent_runs SET status = 'running', started_at = ? WHERE run_id = ?",
        (datetime.now().isoformat(), run_id),
    )
    con.commit()
    con.close()
    _append_agent_run_event(run_id, "run_started", {})
    try:
        import sys
        if str(BASE_DIR) not in sys.path:
            sys.path.insert(0, str(BASE_DIR))
        from agent.agent_runtime import run_agent_api

        request_payload = json.loads(row["request_json"])
        scope = json.loads(row["scope_json"]) if row["scope_json"] else None
        result = run_agent_api(
            request_payload.get("messages") or [],
            scope=scope,
            event_callback=lambda event_type, payload: _append_agent_run_event(
                run_id, event_type, payload
            ),
            cancel_check=lambda: _agent_run_cancel_requested(run_id),
        )
        if result.get("cancelled"):
            status = "cancelled"
        elif result.get("error"):
            status = "failed"
        else:
            status = "completed"
        _append_agent_run_event(
            run_id,
            "run_completed",
            {"status": status, "error": bool(result.get("error"))},
        )
        con = _db()
        con.execute(
            "UPDATE agent_runs SET status = ?, response_json = ?, error_message = ?, "
            "completed_at = ? WHERE run_id = ?",
            (
                status,
                json.dumps(result, ensure_ascii=False, default=str),
                result.get("reply") if result.get("error") else None,
                datetime.now().isoformat(),
                run_id,
            ),
        )
        con.commit()
        con.close()
    except Exception as exc:  # noqa: BLE001
        _append_agent_run_event(run_id, "run_failed", {"error_message": str(exc)[:500]})
        con = _db()
        con.execute(
            "UPDATE agent_runs SET status = 'failed', error_message = ?, completed_at = ? "
            "WHERE run_id = ?",
            (str(exc)[:1000], datetime.now().isoformat(), run_id),
        )
        con.commit()
        con.close()


def _agent_run_access(row: sqlite3.Row, request: Request) -> None:
    role = getattr(request.state, "agrisky_role", None)
    if role == "admin":
        return
    actor = _material_actor(request)
    if row["actor"] != actor:
        raise HTTPException(403, "无权访问其他用户的 Agent 任务")


@app.post("/api/v1/agent/runs", status_code=202)
def create_agent_run(req: AgentRunCreateRequest, request: Request) -> dict:
    scope, user = _agent_scope_from_request(request)
    actor = (
        f"session:{user['username']}"
        if user
        else f"api-key:{getattr(request.state, 'agrisky_role', 'unknown')}"
    )
    run_id = f"ARUN-{uuid.uuid4().hex[:16].upper()}"
    created_at = datetime.now().isoformat()
    con = _db()
    con.execute(
        "INSERT INTO agent_runs "
        "(run_id, actor, scope_json, request_json, status, created_at) VALUES (?,?,?,?,?,?)",
        (
            run_id,
            actor,
            json.dumps(scope, ensure_ascii=False) if scope else None,
            json.dumps({"messages": req.messages}, ensure_ascii=False),
            "queued",
            created_at,
        ),
    )
    con.commit()
    con.close()
    threading.Thread(target=_execute_agent_run, args=(run_id,), daemon=True).start()
    return {"run_id": run_id, "status": "queued", "created_at": created_at}


@app.get("/api/v1/agent/runs/{run_id}")
def get_agent_run(run_id: str, request: Request) -> dict:
    con = _db()
    row = con.execute("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
    if not row:
        con.close()
        raise HTTPException(404, "Agent 任务不存在")
    events = con.execute(
        "SELECT sequence_no, event_type, event_json, created_at "
        "FROM agent_run_events WHERE run_id = ? ORDER BY sequence_no",
        (run_id,),
    ).fetchall()
    con.close()
    _agent_run_access(row, request)
    return {
        "run_id": row["run_id"],
        "status": row["status"],
        "response": json.loads(row["response_json"]) if row["response_json"] else None,
        "error_message": row["error_message"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "events": [
            {
                "sequence_no": event["sequence_no"],
                "event_type": event["event_type"],
                "payload": json.loads(event["event_json"]),
                "created_at": event["created_at"],
            }
            for event in events
        ],
    }


@app.post("/api/v1/agent/runs/{run_id}/cancel")
def cancel_agent_run(run_id: str, request: Request) -> dict:
    con = _db()
    row = con.execute("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
    if not row:
        con.close()
        raise HTTPException(404, "Agent 任务不存在")
    _agent_run_access(row, request)
    if row["status"] in {"completed", "failed", "cancelled"}:
        con.close()
        return {"run_id": run_id, "status": row["status"]}
    con.execute(
        "UPDATE agent_runs SET cancel_requested = 1 WHERE run_id = ?", (run_id,)
    )
    con.commit()
    con.close()
    _append_agent_run_event(run_id, "cancel_requested", {})
    return {"run_id": run_id, "status": "cancelling"}


@app.post("/api/v1/agent/chat")
def agent_chat(req: AgentChatRequest, request: Request) -> dict:
    """对话式智能体：跑一个回合（含其中多次工具调用），返回助手回复 + 工具调用明细 + 新对话历史。

    若带投保人登录会话，智能体按其名下保单限权（只能办理自己的保单）。
    用同步 def，让 FastAPI 在线程池执行（内部 curl 子进程为阻塞调用）。
    """
    import sys
    if str(BASE_DIR) not in sys.path:
        sys.path.insert(0, str(BASE_DIR))
    try:
        from agent.agent_runtime import run_agent_api
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"智能体运行时不可用: {e}") from e

    scope, user = _agent_scope_from_request(request)
    out = run_agent_api(req.messages or [], scope=scope)
    actor = user["username"] if user else "agent"
    _audit(actor, "agent_chat", "agent_chat",
           {"turns": len(req.messages or []), "scoped": bool(scope),
            "role": user.get("role") if user else getattr(request.state, "agrisky_role", None)},
           f"tools={[t['tool'] for t in out.get('trace', [])]}, error={out.get('error')}",
           actor=f"session:{actor}" if user else "agent")
    return out


@app.get("/api/v1/cases/{claim_id}")
async def get_case(claim_id: str) -> dict:
    con = _db()
    row = con.execute(
        "SELECT * FROM cases c WHERE claim_id = ? "
        "AND NOT EXISTS (SELECT 1 FROM deleted_records d "
        "WHERE d.entity_type = 'case' AND d.entity_id = c.claim_id)",
        (claim_id,),
    ).fetchone()
    con.close()
    if not row: raise HTTPException(404, "案件不存在")
    return {"claim_id":row["claim_id"],"policy_id":row["policy_id"],"state":row["state"],"disaster_type":row["disaster_type"],"loss_date":row["loss_date"],"crop_type":row["crop_type"],"plot_id":row["plot_id"],"reported_at":row["reported_at"],"allowed_tools":get_allowed_tools(S(row["state"]))}

@app.get("/api/v1/audit/{claim_id}")
async def get_audit_log(claim_id: str) -> list[dict]:
    con = _db()
    rows = con.execute("SELECT * FROM audit_log WHERE claim_id = ? ORDER BY created_at DESC", (claim_id,)).fetchall()
    con.close()
    return [dict(r) for r in rows]

@app.post("/api/v1/cases/{claim_id}/human_review")
async def human_review(
    claim_id: str, review: HumanReviewRequest, request: Request
) -> dict:
    """Bind an authenticated human decision to one exact, integrity-checked report generation."""
    portal_token = (
        request.headers.get("x-agrisky-token", "").strip()
        or request.cookies.get(SESSION_COOKIE_NAME, "").strip()
    )
    user = _auth_user(portal_token) if portal_token else None
    if portal_token and not user:
        raise HTTPException(401, "未登录或会话已失效")
    if AUTH_ENABLED and not user:
        raise HTTPException(403, "人工审核必须使用可识别的管理员会话，不能使用共享 API key")
    if user and user["role"] != "admin":
        raise HTTPException(403, "仅管理员可执行人工审核")
    actor = user["username"] if user else f"api-key:{getattr(request.state, 'agrisky_role', 'unauthenticated')}"
    return _record_human_review_decision(
        claim_id,
        decision=review.decision,
        actor=actor,
        generation_id=review.generation_id,
        comment=review.comment,
        return_to=review.return_to,
        idempotency_key=review.idempotency_key,
    )

@app.get("/health")
async def health():
    con = _db()
    c = con.execute("SELECT COUNT(*) FROM cases").fetchone()[0]
    a = con.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    con.close()
    return {"status":"ok","version":"1.0.0","cases_count":c,"audit_logs_count":a}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
