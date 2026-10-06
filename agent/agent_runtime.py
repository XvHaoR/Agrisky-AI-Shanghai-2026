"""
Agrisky AI Agent — Web 对话运行时（供 FastAPI /api/v1/agent/chat 调用）

OpenAI-compatible Function Calling，工具映射到网关白名单接口。两处网络都用 curl
子进程，绕开 Anaconda 自带 OpenSSL 在本机的握手问题。

地块边界已在"保单-地块库"登记：理赔时凭保单号自动取在册边界，用户无需提供任何经纬度。
智能体到"报告草稿"即止，归档须人工审核（无 human_review 工具）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlencode

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:
    pass

AGENT_PROVIDER = os.getenv("AGENT_PROVIDER", "openai_compatible").strip() or "openai_compatible"
AGENT_API_KEY = os.getenv("AGENT_API_KEY", "").strip()
AGENT_BASE_URL = (
    os.getenv("AGENT_BASE_URL", "").strip() or "https://api.deepseek.com"
).rstrip("/")
AGENT_MODEL = os.getenv("AGENT_MODEL", "").strip() or "deepseek-v4-pro"
AGENT_FALLBACK_MODEL = os.getenv("AGENT_FALLBACK_MODEL", "").strip()
try:
    AGENT_TIMEOUT_SECONDS = min(600, max(10, int(os.getenv("AGENT_TIMEOUT_SECONDS", "120"))))
except ValueError:
    AGENT_TIMEOUT_SECONDS = 120
try:
    AGENT_MAX_TOOL_ROUNDS = min(20, max(1, int(os.getenv("AGENT_MAX_TOOL_ROUNDS", "10"))))
except ValueError:
    AGENT_MAX_TOOL_ROUNDS = 10


def _normalize_api_base_url(value: str | None) -> str:
    """Accept either a gateway origin or the full versioned API base."""
    base = (value or "http://127.0.0.1:8000/api/v1").strip().rstrip("/")
    if not base:
        base = "http://127.0.0.1:8000/api/v1"
    if not base.endswith("/api/v1"):
        base = f"{base}/api/v1"
    return base


API_BASE = _normalize_api_base_url(os.getenv("AGENT_API_BASE_URL"))
GATEWAY_API_KEY = os.getenv("AGRISKY_AGENT_API_KEY", "")

SYSTEM_PROMPT = """你是 Agrisky AI Agent，农业保险灾后查勘定损的受控调度智能体。

铁律：
1. 只能通过工具推进，不得自行估算受灾面积/减产率/赔款/风险等级；所有数值原样引用工具返回并标注来源。
2. 严格按状态机顺序：建案→材料校验→卫星初筛→当前长势分析→多源减产率评估→合规核验→赔付测算→规则评级→报告草稿。
   卫星初筛完成后必须接着调用 run_growth_analysis 生成当前长势分级图，不得跳过——缺少长势结果时，理赔驾驶舱的「长势」图层和报告附图都将缺失。
   用户还可在任意步骤后要求查看当前长势(run_growth_analysis)或历史季度长势(run_historical_ndvi)；两者都凭 claim_id 使用在册边界，不要求上传文件。run_parcel_growth 仅在当前长势和人工确认地块均已存在时调用。历史与分地块分析不改变主状态机；缺少前置数据时原样说明，不得改用模拟数据。
3. 到"报告草稿"必须停下，提示用户到理赔调度台做人工审核——你绝不能归档、绝不替人审核。
4. 地块边界已在保单库登记：理赔凭保单号自动取用，**不要向用户索要经纬度坐标**。建案只需 policy_id、disaster_type(flood/drought/hail/typhoon/pest/frost/other)、loss_date(YYYY-MM-DD)；作物 crop_type 可从保单带出，用户没说就别追问。
5. 若工具报"保单未登记地块边界"，提示用户先在保单库登记该保单的边界。
6. 用户询问“所有案件/全局案件/调度队列/哪些案件优先”时必须调用 analyze_claims；管理员和分析员可读取全部案件，投保人只会得到名下案件。不要把管理员误判成无保单的投保人。
7. 用户询问本地边界是否可用、边界面积或边界哈希时调用 inspect_policy_boundary。坐标只供后端引擎内部计算，不在对话中展开原始坐标。
8. 用户明确要求“一键走完全流程/自动完成理赔调度”时调用 run_claim_workflow。该工具会使用在册边界并默认先跑当前长势，然后按状态机推进到报告草稿；任何生产分析失败都必须停下并如实报告，绝不改用模拟数据。
9. 用户消息中只要出现“材料、资料、附件、文件、字段来源、字段冲突、是否齐全、缺少什么”等材料核验意图，必须先调用 compare_case_materials；需要逐份清单时再调用 list_case_documents，需要解释开放问题时再调用 explain_material_findings。不得仅调用 get_case_details 代替材料核验。只能引用工具返回的结构化字段和 source_ref；原始文件、身份证号、手机号、账户号和完整地块坐标不得进入回复。
10. 当前流程不要求上传 policy_document、claim_notice 或 damage_certificate。validate_materials 只校验案件要素、在册保单边界及已有材料中的阻断问题；不得主动把这三类文件列为建案或推进流程的必需项。
11. 用户询问保险合同、理赔依据、保额、起赔点、生育期系数或赔款公式时，必须调用 inspect_policy_contract。回答必须以登记时上传并冻结的合同原件及结构化条款为准，引用合同版本、原件哈希和条款编号；示范合同须说明用于产品规则验证，不得把 Agrisky AI 说成保险人。
12. 用户询问法律法规、合规依据或作物技术方案时，必须优先引用知识库中的 L-01/L-02 与 T-01 至 T-04 编号，说明官方来源；不得编造法律条文。合同与法规冲突、证据不足或高风险情形必须提示人工复核。

风格：每步简短说明在做什么、关键数值多少(标注来自哪个工具)；中文回复。"""

TOOLS = [
    {"type": "function", "function": {
        "name": "analyze_claims",
        "description": "读取本地案件库并分析当前可见的全部案件：状态分布、边界就绪情况、风险、赔付与下一步调度优先级。管理员/分析员看全局，投保人仅看名下案件。只读。",
        "parameters": {"type": "object", "properties": {
            "state": {"type": "string", "description": "可选；仅分析指定状态"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500, "description": "最多读取案件数，默认 200"},
        }}}},
    {"type": "function", "function": {
        "name": "get_case_details",
        "description": "读取一个案件及其已持久化分析结果的安全摘要，包括长势、减产率、合规、赔付、风险和报告状态。只读。",
        "parameters": {"type": "object", "properties": {
            "claim_id": {"type": "string"},
        }, "required": ["claim_id"]}}},
    {"type": "function", "function": {
        "name": "inspect_policy_boundary",
        "description": "读取本地保单库中的在册边界摘要：版本、面积、几何类型、要素数和 canonical_geometry_v1 SHA-256；原始坐标不进入模型上下文。只读。",
        "parameters": {"type": "object", "properties": {
            "policy_id": {"type": "string"},
        }, "required": ["policy_id"]}}},
    {"type": "function", "function": {
        "name": "inspect_policy_contract",
        "description": "读取登记时上传并冻结的保险合同摘要、原件哈希、保险期间、保险责任、赔款参数、合同版本与条款编号；用于解释理赔依据。只读。",
        "parameters": {"type": "object", "properties": {
            "policy_id": {"type": "string"},
        }, "required": ["policy_id"]}}},
    {"type": "function", "function": {
        "name": "list_case_documents",
        "description": "材料清单专用工具：读取案件已上传材料的类型、解析状态、缺失材料和阻断数量。用户询问逐份附件/文件清单时调用。只读，不向模型发送原始文件或敏感全文。",
        "parameters": {"type": "object", "properties": {
            "claim_id": {"type": "string"},
        }, "required": ["claim_id"]}}},
    {"type": "function", "function": {
        "name": "compare_case_materials",
        "description": "材料核验首选工具：用户询问材料/资料是否齐全、缺少什么、字段来源、字段冲突或资料风险时必须调用。核对提取字段与在册保单/案件字段的一致性，返回缺失项、冲突、置信度和证据引用。不得用 get_case_details 替代。只读。",
        "parameters": {"type": "object", "properties": {
            "claim_id": {"type": "string"},
        }, "required": ["claim_id"]}}},
    {"type": "function", "function": {
        "name": "explain_material_findings",
        "description": "解释案件材料中的开放问题及下一步人工处理要求；只能引用服务端已有 findings，不得自行判定材料有效。",
        "parameters": {"type": "object", "properties": {
            "claim_id": {"type": "string"},
        }, "required": ["claim_id"]}}},
    {"type": "function", "function": {
        "name": "run_claim_workflow",
        "description": "按状态机自动调度一个已有案件：材料校验→卫星初筛→当前长势→多源减产率→合规→赔付→规则评级→报告草稿。默认执行当前长势；到人工审核前停止，绝不自动归档。",
        "parameters": {"type": "object", "properties": {
            "claim_id": {"type": "string"},
            "include_growth": {"type": "boolean", "description": "是否运行当前长势分析，默认 true"},
            "include_historical": {"type": "boolean", "description": "是否同时运行历史季度长势，默认 false"},
        }, "required": ["claim_id"]}}},
    {"type": "function", "function": {
        "name": "create_claim",
        "description": "新建理赔案件（INIT→MATERIAL_CHECK）。作物可从保单库带出。",
        "parameters": {"type": "object", "properties": {
            "policy_id": {"type": "string"},
            "disaster_type": {"type": "string", "enum": ["flood", "drought", "hail", "typhoon", "pest", "frost", "other"]},
            "loss_date": {"type": "string", "description": "YYYY-MM-DD"},
            "crop_type": {"type": "string", "description": "可省略，将从保单库带出"},
            "plot_id": {"type": "string"},
        }, "required": ["policy_id", "disaster_type", "loss_date"]}}},
    {"type": "function", "function": {
        "name": "validate_materials",
        "description": "校验案件要素与在册保单边界（MATERIAL_CHECK→PREPROCESS_READY）；当前流程不要求上传理赔文件。",
        "parameters": {"type": "object", "properties": {"claim_id": {"type": "string"}}, "required": ["claim_id"]}}},
    {"type": "function", "function": {
        "name": "run_satellite_screening",
        "description": "卫星 SAR 初筛（用保单在册地块边界，PREPROCESS_READY→SCREENING_DONE）。",
        "parameters": {"type": "object", "properties": {"claim_id": {"type": "string"}}, "required": ["claim_id"]}}},
    {"type": "function", "function": {
        "name": "run_loss_assessment",
        "description": "多源遥感减产率评估(NDVI/EVI2/NDWI/NDRE + GPP/NPP 异常 + VCI)，用保单在册边界。返回减产率与各产品明细。",
        "parameters": {"type": "object", "properties": {"claim_id": {"type": "string"}}, "required": ["claim_id"]}}},
    {"type": "function", "function": {
        "name": "run_compliance",
        "description": "合规面积核验：承保面积(在册边界)×减产率→合规受灾面积（→COMPLIANCE_DONE）。",
        "parameters": {"type": "object", "properties": {"claim_id": {"type": "string"}}, "required": ["claim_id"]}}},
    {"type": "function", "function": {
        "name": "run_payout_estimate",
        "description": "赔付测算：按保额/起赔点/赔付曲线得预估赔款（COMPLIANCE_DONE 后）。",
        "parameters": {"type": "object", "properties": {"claim_id": {"type": "string"}}, "required": ["claim_id"]}}},
    {"type": "function", "function": {
        "name": "run_rule_engine",
        "description": "规则评级：服务端读取已保存的合规比例、案件作物和赔付结果（COMPLIANCE_DONE→RULE_DONE）。",
        "parameters": {"type": "object", "properties": {
            "claim_id": {"type": "string"},
        }, "required": ["claim_id"]}}},
    {"type": "function", "function": {
        "name": "generate_report",
        "description": "生成报告草稿（RULE_DONE→REPORT_DRAFTED）。之后必须停下交人工审核。",
        "parameters": {"type": "object", "properties": {"claim_id": {"type": "string"}}, "required": ["claim_id"]}}},
    {"type": "function", "function": {
        "name": "generate_excel_report",
        "description": "生成受灾评估 Excel(.xlsx) 表，含各源减产率/合规/赔付明细。合规之后可随时出表。",
        "parameters": {"type": "object", "properties": {"claim_id": {"type": "string"}}, "required": ["claim_id"]}}},
    {"type": "function", "function": {
        "name": "run_growth_analysis",
        "description": "作物长势分析（由系统内置的『长势监测 / NDVI 分级引擎』执行）：凭 claim_id 自动取保单在册地块边界，"
                       "运行 NDVI 长势分级（差/一般/中/良/优）并产出可视化长势分级图与各等级面积占比。"
                       "用户说『跑一下长势 / 看看长势 / 分析长势 / 长势怎么样』时直接调用即可，无需上传文件；"
                       "卫星初筛后随时可调，不改变案件状态。",
        "parameters": {"type": "object", "properties": {
            "claim_id": {"type": "string"},
            "start_date": {"type": "string", "description": "可选；留空按出险日期所在季度"},
            "end_date": {"type": "string", "description": "可选；与 start_date 同时提供，截止日按包含处理"},
        }, "required": ["claim_id"]}}},
    {"type": "function", "function": {
        "name": "run_historical_ndvi",
        "description": "历史季度长势：按案件在册边界计算 2022 至指定年份的真实 Sentinel-2 季度 NDVI，并生成趋势、年度 2×2 图和 DOCX。生产失败绝不降级模拟。",
        "parameters": {"type": "object", "properties": {
            "claim_id": {"type": "string"},
            "start_year": {"type": "integer", "minimum": 2017, "description": "可选，默认 2022"},
            "end_year": {"type": "integer", "minimum": 2017, "description": "可选，默认服务器当前年份"},
            "max_cloud_pct": {"type": "number", "minimum": 0, "maximum": 100, "description": "可选，默认 30"},
        }, "required": ["claim_id"]}}},
    {"type": "function", "function": {
        "name": "run_parcel_growth",
        "description": "分地块长势：使用案件已登记 NDVI 栅格和经人工确认的地块映射，输出 parcel_id/feature_id 级覆盖率、分级和异常。",
        "parameters": {"type": "object", "properties": {
            "claim_id": {"type": "string"},
        }, "required": ["claim_id"]}}},
]


def _curl(method: str, url: str, headers: dict | None = None, body: dict | None = None, timeout: int = 180) -> dict:
    curl_cmd = shutil.which("curl.exe") or shutil.which("curl")
    if not curl_cmd:
        return {"status": "error", "error_message": "curl 不可用"}
    cmd = [curl_cmd, "-s", "-X", method, url]
    subprocess_env = None
    if url == AGENT_BASE_URL or url.startswith(f"{AGENT_BASE_URL}/"):
        # GEE may install a process-wide proxy. Model traffic must stay direct.
        cmd += [
            "--noproxy", "*",
            "--http1.1",
            "--retry", "2",
            "--retry-all-errors",
            "--retry-delay", "1",
        ]
        subprocess_env = os.environ.copy()
        for name in (
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
            "http_proxy", "https_proxy", "all_proxy",
        ):
            subprocess_env.pop(name, None)
    request_headers = dict(headers or {})
    if GATEWAY_API_KEY and (url == API_BASE or url.startswith(f"{API_BASE}/")):
        request_headers.setdefault("x-agrisky-api-key", GATEWAY_API_KEY)
    for k, v in request_headers.items():
        cmd += ["-H", f"{k}: {v}"]
    if body is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(body, ensure_ascii=False)]
    cmd += ["--connect-timeout", "30", "--max-time", str(timeout)]
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout + 15,
            env=subprocess_env,
        )
        out = r.stdout.decode("utf-8", errors="replace")
        if r.returncode != 0:
            return {"status": "error", "error_message": r.stderr.decode("utf-8", errors="replace") or f"curl {r.returncode}"}
        if not out.strip():
            return {"status": "error", "error_message": "模型服务返回空响应"}
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return {
                "status": "error",
                "error_message": "模型服务返回了无法解析的响应",
            }
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "error_message": str(e)}


def _resolve_case_roi(claim_id: str) -> tuple[dict | None, str, str | None]:
    """凭 claim → policy → 在册边界。返回 (roi_geojson, loss_date, error)。"""
    case = _curl("GET", f"{API_BASE}/cases/{claim_id}")
    pid = case.get("policy_id") if isinstance(case, dict) else None
    loss_date = str(case.get("loss_date", ""))[:10] if isinstance(case, dict) else ""
    if not pid:
        return None, loss_date, "无法获取案件保单号"
    pol = _curl("GET", f"{API_BASE}/policies/{pid}")
    roi = pol.get("roi_geojson") if isinstance(pol, dict) else None
    if not roi:
        return None, loss_date, f"保单 {pid} 未登记地块边界，请先在保单库登记后再处理。"
    return roi, loss_date, None


def _scope_claim_error(claim_id: str, scope: dict | None) -> str | None:
    """投保人智能体在调用任一案件工具前，强制核验案件所属保单。"""
    if not scope:
        return None
    case = _curl("GET", f"{API_BASE}/cases/{claim_id}")
    policy_id = case.get("policy_id") if isinstance(case, dict) else None
    if not policy_id:
        detail = case.get("detail") if isinstance(case, dict) else None
        return str(detail or f"无法核验案件 {claim_id} 的保单归属")
    allowed = set(scope.get("policy_ids") or [])
    if policy_id not in allowed:
        return f"案件 {claim_id} 不属于当前投保人名下保单，拒绝调用。"
    return None


def _scope_policy_error(policy_id: str, scope: dict | None) -> str | None:
    """投保人只能读取或使用自己名下保单；管理员/分析员的 scope 为 None。"""
    if not scope:
        return None
    if policy_id not in set(scope.get("policy_ids") or []):
        return f"保单 {policy_id} 不属于当前投保人，拒绝读取。"
    return None


def _tool_error_message(result: object) -> str | None:
    if not isinstance(result, dict):
        return "工具返回格式异常"
    if result.get("status") == "error":
        return str(result.get("error_message") or result.get("detail") or "工具执行失败")
    if result.get("error") is True:
        return str(result.get("error_message") or result.get("detail") or "工具执行失败")
    if result.get("detail") and not result.get("status"):
        return str(result["detail"])
    return None


def _geometry_summary(value: object) -> tuple[str | None, int]:
    """只提取几何类型和要素数，不把坐标传给大模型。"""
    if not isinstance(value, dict):
        return None, 0
    kind = value.get("type")
    if kind == "FeatureCollection":
        features = [item for item in value.get("features", []) if isinstance(item, dict)]
        geometry_types = sorted({
            str((item.get("geometry") or {}).get("type"))
            for item in features
            if isinstance(item.get("geometry"), dict) and (item.get("geometry") or {}).get("type")
        })
        return "/".join(geometry_types) or "FeatureCollection", len(features)
    if kind == "Feature":
        geometry = value.get("geometry") if isinstance(value.get("geometry"), dict) else {}
        return str(geometry.get("type") or "Feature"), 1 if geometry else 0
    return (str(kind) if kind else None), 1 if kind else 0


_NEXT_ACTIONS = {
    "INIT": "补齐建案信息",
    "MATERIAL_CHECK": "校验材料与在册边界",
    "PREPROCESS_READY": "运行卫星初筛",
    "SCREENING_DONE": "运行长势/减产率评估并做合规核验",
    "NDVI_DONE": "完成减产率评估并做合规核验",
    "COMPLIANCE_DONE": "完成赔付测算与规则评级",
    "RULE_DONE": "生成报告草稿",
    "REPORT_DRAFTED": "等待具名管理员人工审核",
    "HUMAN_REVIEW": "依据人工审核决定归档或退回",
    "ARCHIVED": "已归档，无需调度",
}

_STATE_PRIORITY = {
    "REPORT_DRAFTED": 90,
    "HUMAN_REVIEW": 85,
    "RULE_DONE": 80,
    "COMPLIANCE_DONE": 70,
    "SCREENING_DONE": 60,
    "NDVI_DONE": 60,
    "PREPROCESS_READY": 50,
    "MATERIAL_CHECK": 40,
    "INIT": 30,
    "ARCHIVED": 0,
}


def _analyze_claims(args: dict, scope: dict | None) -> dict:
    try:
        limit = min(500, max(1, int(args.get("limit") or 200)))
    except (TypeError, ValueError):
        limit = 200
    params: dict[str, object] = {"limit": limit}
    if args.get("state"):
        params["state"] = str(args["state"])
    payload = _curl("GET", f"{API_BASE}/cases?{urlencode(params)}")
    error = _tool_error_message(payload)
    if error:
        return {"status": "error", "error_message": error}

    items = payload.get("items") if isinstance(payload, dict) else []
    if not isinstance(items, list):
        return {"status": "error", "error_message": "案件队列返回格式异常"}
    if scope:
        allowed = set(scope.get("policy_ids") or [])
        items = [item for item in items if isinstance(item, dict) and item.get("policy_id") in allowed]
    else:
        items = [item for item in items if isinstance(item, dict)]

    by_state: dict[str, int] = {}
    total_payout = 0.0
    high_risk = 0
    missing_boundary = 0
    action_queue: list[dict] = []
    for item in items:
        state = str(item.get("state") or "UNKNOWN")
        by_state[state] = by_state.get(state, 0) + 1
        amount = item.get("payout_amount_yuan")
        if isinstance(amount, (int, float)):
            total_payout += float(amount)
        risk = item.get("risk_level")
        if risk == "high":
            high_risk += 1
        boundary_ready = item.get("boundary_registered") is True
        if not boundary_ready:
            missing_boundary += 1
        priority = _STATE_PRIORITY.get(state, 20)
        reasons: list[str] = []
        if not boundary_ready and state != "ARCHIVED":
            priority += 100
            reasons.append("缺少可验证在册边界")
        if risk == "high":
            priority += 20
            reasons.append("高风险")
        if state == "REPORT_DRAFTED":
            reasons.append("等待人工审核")
        action_queue.append({
            "claim_id": item.get("claim_id"),
            "policy_id": item.get("policy_id"),
            "state": state,
            "risk_level": risk,
            "yield_loss_ratio": item.get("yield_loss_ratio"),
            "payout_amount_yuan": amount,
            "boundary_registered": boundary_ready,
            "boundary_sha256": item.get("boundary_sha256"),
            "priority": priority,
            "priority_reasons": reasons,
            "next_action": (
                "先补登记或修复在册边界，禁止进入遥感分析"
                if not boundary_ready and state != "ARCHIVED"
                else _NEXT_ACTIONS.get(state, "人工核查状态")
            ),
        })
    action_queue.sort(key=lambda item: (-int(item["priority"]), str(item.get("claim_id") or "")))
    return {
        "status": "success",
        "source": "本地 SQLite 案件库与保单在册边界（经业务网关读取）",
        "scope": "policyholder" if scope else "global",
        "total": len(items),
        "by_state": by_state,
        "high_risk_count": high_risk,
        "missing_boundary_count": missing_boundary,
        "total_payout_yuan": round(total_payout, 2),
        "action_queue": action_queue,
    }


def _inspect_policy_boundary(policy_id: str, scope: dict | None) -> dict:
    scope_error = _scope_policy_error(policy_id, scope)
    if scope_error:
        return {"status": "error", "error_message": scope_error}
    policy = _curl("GET", f"{API_BASE}/policies/{policy_id}")
    error = _tool_error_message(policy)
    if error:
        return {"status": "error", "error_message": error}
    geometry = policy.get("roi_geojson") or policy.get("boundary_geojson")
    geometry_type, feature_count = _geometry_summary(geometry)
    return {
        "status": "success",
        "source": "本地保单库在册边界",
        "policy_id": policy.get("policy_id"),
        "policy_version_id": policy.get("policy_version_id"),
        "crop_type": policy.get("crop_type"),
        "address": policy.get("address"),
        "area_mu": policy.get("area_mu"),
        "boundary_registered": policy.get("boundary_registered") is True or bool(geometry),
        "boundary_sha256": policy.get("boundary_sha256"),
        "boundary_hash_scheme": policy.get("boundary_hash_scheme"),
        "geometry_type": geometry_type,
        "feature_count": feature_count,
        "raw_geometry_exposed_to_model": False,
    }


def _inspect_policy_contract(policy_id: str, scope: dict | None) -> dict:
    scope_error = _scope_policy_error(policy_id, scope)
    if scope_error:
        return {"status": "error", "error_message": scope_error}
    payload = _curl("GET", f"{API_BASE}/policies/{policy_id}/contract")
    error = _tool_error_message(payload)
    if error:
        return {"status": "error", "error_message": error}
    contract = payload.get("contract") if isinstance(payload.get("contract"), dict) else {}
    clauses = payload.get("clauses") if isinstance(payload.get("clauses"), list) else []
    return {
        "status": "success",
        "source": "保单库登记时上传并冻结的保险合同版本",
        "policy_id": policy_id,
        "simulation": contract.get("simulation") is True,
        "simulation_disclosure": contract.get("simulation_disclosure"),
        "source_type": contract.get("source_type"),
        "source_filename": contract.get("source_filename"),
        "source_sha256": contract.get("source_sha256"),
        "human_confirmed": contract.get("human_confirmed"),
        "contract_id": contract.get("contract_id"),
        "contract_number": contract.get("contract_number"),
        "contract_version": contract.get("contract_version"),
        "contract_sha256": contract.get("contract_sha256"),
        "insurer": contract.get("insurer"),
        "policyholder": contract.get("policyholder"),
        "insurance_period": contract.get("insurance_period"),
        "payout_terms": contract.get("payout_terms"),
        "clauses": [
            {"id": item.get("id"), "title": item.get("title"), "text": item.get("text")}
            for item in clauses
            if isinstance(item, dict)
        ],
        "download_url": contract.get("artifact_url"),
    }


def _get_case_details(claim_id: str, scope: dict | None) -> dict:
    scope_error = _scope_claim_error(claim_id, scope)
    if scope_error:
        return {"status": "error", "error_message": scope_error}
    payload = _curl("GET", f"{API_BASE}/cases/{claim_id}/full")
    error = _tool_error_message(payload)
    if error:
        return {"status": "error", "error_message": error}
    case = payload.get("case") if isinstance(payload.get("case"), dict) else {}
    results = payload.get("results") if isinstance(payload.get("results"), dict) else {}
    satellite = results.get("satellite") if isinstance(results.get("satellite"), dict) else {}
    growth = results.get("growth") if isinstance(results.get("growth"), dict) else {}
    loss = results.get("loss_assessment") if isinstance(results.get("loss_assessment"), dict) else {}
    compliance = results.get("compliance") if isinstance(results.get("compliance"), dict) else {}
    payout = results.get("payout") if isinstance(results.get("payout"), dict) else {}
    rule = results.get("rule") if isinstance(results.get("rule"), dict) else {}
    report = results.get("report") if isinstance(results.get("report"), dict) else {}
    return {
        "status": "success",
        "source": "本地案件库与已持久化服务端分析结果",
        "claim_id": payload.get("claim_id") or claim_id,
        "state": payload.get("state"),
        "case": case,
        "metrics": {
            "satellite_damage_ratio": satellite.get("damage_ratio"),
            "growth_valid_pixel_coverage": growth.get("valid_pixel_coverage"),
            "growth_summary": growth.get("summary"),
            "yield_loss_ratio": loss.get("yield_loss_ratio"),
            "compliance_ratio": (
                compliance.get("damage_ratio")
                if compliance.get("damage_ratio") is not None
                else compliance.get("loss_ratio")
            ),
            "valid_claim_area_mu": compliance.get("valid_claim_area_mu"),
            "payout_amount_yuan": payout.get("payout_amount_yuan"),
            "risk_level": rule.get("risk_level"),
        },
        "result_status": {name: bool(value) for name, value in results.items()},
        "report_generation_id": report.get("generation_id"),
        "next_action": _NEXT_ACTIONS.get(str(payload.get("state") or ""), "人工核查状态"),
    }


def _compact_step_result(tool_name: str, result: dict) -> dict:
    keys = (
        "status", "claim_id", "state", "task_id", "generation_id", "damage_ratio",
        "yield_loss_ratio", "payout_amount_yuan", "risk_level", "boundary_sha256",
        "message", "detail", "error_message",
    )
    compact = {key: result[key] for key in keys if key in result}
    return {"tool": tool_name, "result": compact or {"status": "success"}}


def _run_claim_workflow(args: dict, scope: dict | None) -> dict:
    claim_id = str(args.get("claim_id") or "").strip()
    if not claim_id:
        return {"status": "error", "error_message": "缺少 claim_id"}
    scope_error = _scope_claim_error(claim_id, scope)
    if scope_error:
        return {"status": "error", "error_message": scope_error}
    include_growth = args.get("include_growth", True) is not False
    include_historical = args.get("include_historical", False) is True
    steps: list[dict] = []

    for _ in range(12):
        case = _curl("GET", f"{API_BASE}/cases/{claim_id}")
        error = _tool_error_message(case)
        if error:
            return {"status": "error", "claim_id": claim_id, "steps": steps, "error_message": error}
        state = str(case.get("state") or "")
        if state in {"REPORT_DRAFTED", "HUMAN_REVIEW", "ARCHIVED"}:
            return {
                "status": "success",
                "claim_id": claim_id,
                "final_state": state,
                "steps": steps,
                "stopped_before_human_review": state == "REPORT_DRAFTED",
                "message": (
                    "已生成报告草稿，必须由具名管理员人工审核后才能归档。"
                    if state == "REPORT_DRAFTED"
                    else "案件已进入人工审核或归档阶段，智能体不再推进。"
                ),
            }
        if state == "INIT":
            return {"status": "error", "claim_id": claim_id, "steps": steps,
                    "error_message": "已有案件仍处于 INIT，缺少建案数据，不能自动跳过。"}

        action_names: list[str]
        if state == "MATERIAL_CHECK":
            action_names = ["validate_materials"]
        elif state == "PREPROCESS_READY":
            action_names = ["run_satellite_screening"]
        elif state in {"SCREENING_DONE", "NDVI_DONE"}:
            full = _curl("GET", f"{API_BASE}/cases/{claim_id}/full")
            full_error = _tool_error_message(full)
            if full_error:
                return {"status": "error", "claim_id": claim_id, "steps": steps,
                        "error_message": full_error}
            existing = full.get("results") if isinstance(full.get("results"), dict) else {}
            action_names = []
            if include_growth and not existing.get("growth"):
                action_names.append("run_growth_analysis")
            if include_historical and not existing.get("historical_ndvi"):
                action_names.append("run_historical_ndvi")
            if not existing.get("loss_assessment"):
                action_names.append("run_loss_assessment")
            action_names.append("run_compliance")
        elif state == "COMPLIANCE_DONE":
            full = _curl("GET", f"{API_BASE}/cases/{claim_id}/full")
            full_error = _tool_error_message(full)
            if full_error:
                return {"status": "error", "claim_id": claim_id, "steps": steps,
                        "error_message": full_error}
            existing = full.get("results") if isinstance(full.get("results"), dict) else {}
            action_names = [] if existing.get("payout") else ["run_payout_estimate"]
            action_names.append("run_rule_engine")
        elif state == "RULE_DONE":
            action_names = ["generate_report"]
        else:
            return {"status": "error", "claim_id": claim_id, "final_state": state, "steps": steps,
                    "error_message": f"无法自动处理未知案件状态: {state}"}

        for action_name in action_names:
            result = execute_tool(action_name, {"claim_id": claim_id}, scope=scope)
            steps.append(_compact_step_result(action_name, result))
            action_error = _tool_error_message(result)
            if action_error:
                return {
                    "status": "error",
                    "claim_id": claim_id,
                    "final_state": state,
                    "failed_tool": action_name,
                    "steps": steps,
                    "error_message": action_error,
                    "synthetic_fallback_used": False,
                }
        updated_case = _curl("GET", f"{API_BASE}/cases/{claim_id}")
        updated_error = _tool_error_message(updated_case)
        if updated_error:
            return {
                "status": "error",
                "claim_id": claim_id,
                "final_state": state,
                "steps": steps,
                "error_message": updated_error,
            }
        updated_state = str(updated_case.get("state") or "")
        if updated_state == state:
            return {
                "status": "error",
                "claim_id": claim_id,
                "final_state": state,
                "failed_tool": action_names[-1] if action_names else None,
                "steps": steps,
                "error_message": "当前步骤未通过，案件状态未推进；自动调度已停止，避免重复执行。",
                "synthetic_fallback_used": False,
            }
    return {"status": "error", "claim_id": claim_id, "steps": steps,
            "error_message": "自动调度超过最大步骤数，已停止以避免重复执行。"}


def _safe_material_review(claim_id: str) -> dict:
    review = _curl("GET", f"{API_BASE}/cases/{claim_id}/material-review")
    error = _tool_error_message(review)
    if error:
        return {"status": "error", "error_message": error}
    documents = [
        {
            "document_id": item.get("document_id"),
            "document_type": item.get("document_type"),
            "filename": item.get("original_filename"),
            "parse_status": item.get("parse_status"),
            "sha256": item.get("sha256"),
        }
        for item in review.get("documents", [])
        if isinstance(item, dict)
    ]
    fields = [
        {
            "field_name": item.get("field_name"),
            "normalized_value": item.get("normalized_value"),
            "confidence": item.get("confidence"),
            "source_ref": item.get("source_ref"),
            "document_id": item.get("document_id"),
            "extractor": item.get("extractor"),
        }
        for item in review.get("fields", [])
        if isinstance(item, dict)
    ]
    findings = [
        {
            "finding_id": item.get("finding_id"),
            "code": item.get("code"),
            "severity": item.get("severity"),
            "field_name": item.get("field_name"),
            "expected_value": json.loads(item["expected_value_json"])
            if item.get("expected_value_json")
            else None,
            "actual_value": json.loads(item["actual_value_json"])
            if item.get("actual_value_json")
            else None,
            "source_ref": item.get("source_ref"),
            "message": item.get("message"),
            "status": item.get("status"),
        }
        for item in review.get("findings", [])
        if isinstance(item, dict)
    ]
    return {
        "status": "success",
        "source": "案件私有材料库与确定性一致性核验",
        "claim_id": claim_id,
        "passed": review.get("passed"),
        "required_document_types": review.get("required_document_types", []),
        "missing_document_types": review.get("missing_document_types", []),
        "blocking_count": review.get("blocking_count", 0),
        "documents": documents,
        "fields": fields,
        "findings": findings,
        "privacy": "模型上下文不包含原始文件、完整原文、身份证号、手机号、账户号或地块坐标",
    }


def execute_tool(name: str, args: dict, scope: dict | None = None) -> dict:
    """把工具调用映射到网关接口（自身 localhost）。

    scope（投保人登录态）：{holder_name, username, policy_ids}。给定时，建案只允许其名下保单。
    """
    try:
        claim_id = args.get("claim_id")
        if claim_id and scope:
            scope_error = _scope_claim_error(str(claim_id), scope)
            if scope_error:
                return {"status": "error", "error_message": scope_error}
        if name == "analyze_claims":
            return _analyze_claims(args, scope)
        if name == "get_case_details":
            return _get_case_details(str(args["claim_id"]), scope)
        if name == "inspect_policy_boundary":
            return _inspect_policy_boundary(str(args["policy_id"]), scope)
        if name == "inspect_policy_contract":
            return _inspect_policy_contract(str(args["policy_id"]), scope)
        if name in {
            "list_case_documents",
            "compare_case_materials",
            "explain_material_findings",
        }:
            return _safe_material_review(str(args["claim_id"]))
        if name == "run_claim_workflow":
            return _run_claim_workflow(args, scope)
        if name == "create_claim":
            args = dict(args)
            if scope and scope.get("policy_ids") is not None:
                if args.get("policy_id") not in scope["policy_ids"]:
                    return {"status": "error",
                            "error_message": f"保单 {args.get('policy_id')} 不在您名下，无法办理。"
                                             f"您名下保单：{scope['policy_ids']}"}
            if not args.get("crop_type") and args.get("policy_id"):
                pol = _curl("GET", f"{API_BASE}/policies/{args['policy_id']}")
                if isinstance(pol, dict) and pol.get("crop_type"):
                    args["crop_type"] = pol["crop_type"]
            if not args.get("crop_type"):
                args["crop_type"] = "other"
            return _curl("POST", f"{API_BASE}/tools/create_claim", body=args)
        if name == "validate_materials":
            return _curl("POST", f"{API_BASE}/tools/validate_materials", body={"claim_id": args["claim_id"]})
        if name == "run_satellite_screening":
            roi, ld, err = _resolve_case_roi(args["claim_id"])
            if err:
                return {"status": "error", "error_message": err}
            from datetime import date, timedelta
            d = date.fromisoformat(ld) if ld else date(2025, 8, 15)
            return _curl("POST", f"{API_BASE}/tools/run_satellite_screening", body={
                "claim_id": args["claim_id"], "roi_geojson": roi,
                "start_date": (d - timedelta(days=10)).isoformat(),
                "end_date": (d + timedelta(days=15)).isoformat(),
            }, timeout=180)
        if name == "run_loss_assessment":
            roi, _ld, err = _resolve_case_roi(args["claim_id"])
            if err:
                return {"status": "error", "error_message": err}
            return _curl("POST", f"{API_BASE}/tools/run_loss_assessment",
                         body={"claim_id": args["claim_id"], "roi_geojson": roi}, timeout=280)
        if name == "run_compliance":
            return _curl("POST", f"{API_BASE}/tools/run_compliance_estimate",
                         body={"claim_id": args["claim_id"]})
        if name == "run_payout_estimate":
            return _curl("POST", f"{API_BASE}/tools/run_payout_estimate", body={"claim_id": args["claim_id"]})
        if name == "run_rule_engine":
            return _curl("POST", f"{API_BASE}/tools/run_rule_engine",
                         body={"claim_id": args["claim_id"]})
        if name == "generate_report":
            return _curl("POST", f"{API_BASE}/tools/generate_report", body={"claim_id": args["claim_id"], "data": {}})
        if name == "generate_excel_report":
            return _curl("POST", f"{API_BASE}/tools/generate_excel_report", body={"claim_id": args["claim_id"]})
        if name == "run_growth_analysis":
            body = {"claim_id": args["claim_id"], "ndvi_source": "auto", "method": "fixed"}
            if args.get("start_date") and args.get("end_date"):
                body.update({"start_date": args["start_date"], "end_date": args["end_date"]})
            return _curl("POST", f"{API_BASE}/tools/run_growth_analysis_by_claim", body=body, timeout=280)
        if name == "run_historical_ndvi":
            from datetime import date

            body = {
                "claim_id": args["claim_id"],
                "start_year": int(args.get("start_year") or 2022),
                "end_year": int(args.get("end_year") or date.today().year),
                "max_cloud_pct": float(args.get("max_cloud_pct") or 30),
                "scale_m": 10,
            }
            return _curl("POST", f"{API_BASE}/tools/run_historical_ndvi_by_claim", body=body, timeout=900)
        if name == "run_parcel_growth":
            return _curl(
                "POST",
                f"{API_BASE}/tools/run_parcel_growth_by_claim",
                body={"claim_id": args["claim_id"]},
                timeout=600,
            )
        return {"status": "error", "error_message": f"未知工具: {name}"}
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "error_message": f"{name} 执行异常: {e}"}


def provider_health(probe: bool = False) -> dict:
    configured = bool(AGENT_API_KEY and AGENT_BASE_URL and AGENT_MODEL)
    payload = {
        "provider": AGENT_PROVIDER,
        "base_url": AGENT_BASE_URL,
        "model": AGENT_MODEL,
        "configured": configured,
        "tool_count": len(TOOLS),
        "status": "ready" if configured else "unavailable",
    }
    if not configured:
        payload["message"] = "Agent 模型服务尚未配置"
        return payload
    if not probe:
        return payload
    result = _provider_chat(
        [
            {"role": "system", "content": "只回复 READY。"},
            {"role": "user", "content": "health check"},
        ],
        include_tools=False,
    )
    if result.get("_error") or "choices" not in result:
        payload["status"] = "degraded"
        payload["message"] = str(
            result.get("_error") or result.get("error") or result.get("error_message") or "模型响应异常"
        )[:500]
    else:
        payload["probe_ok"] = True
        payload["resolved_model"] = result.get("_resolved_model", AGENT_MODEL)
    return payload


def _provider_chat(messages: list[dict], include_tools: bool = True) -> dict:
    if not AGENT_API_KEY:
        return {"_error": "Agent 模型 API Key 未配置，请设置 AGENT_API_KEY 后重启后端。"}
    body: dict = {
        "model": AGENT_MODEL,
        "messages": messages,
        "temperature": 0,
        "stream": False,
    }
    if include_tools:
        body["tools"] = TOOLS
        body["tool_choice"] = "auto"
    if "deepseek.com" in AGENT_BASE_URL:
        body["thinking"] = {"type": "disabled"}
    models = [AGENT_MODEL]
    if AGENT_FALLBACK_MODEL and AGENT_FALLBACK_MODEL not in models:
        models.append(AGENT_FALLBACK_MODEL)

    last: dict = {}
    for model_index, model in enumerate(models):
        body["model"] = model
        for attempt in range(3):
            last = _curl(
                "POST",
                f"{AGENT_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {AGENT_API_KEY}"},
                body=body,
                timeout=AGENT_TIMEOUT_SECONDS,
            )
            if "choices" in last:
                last["_resolved_model"] = model
                return last
            error = last.get("error") if isinstance(last, dict) else None
            message = (
                error.get("message")
                if isinstance(error, dict)
                else last.get("error_message") if isinstance(last, dict) else str(last)
            )
            normalized_message = str(message).lower()
            if "overload" in normalized_message and model_index < len(models) - 1:
                break
            retryable = any(
                marker in normalized_message
                for marker in (
                    "429", "rate", "timeout", "temporar", "502", "503", "504",
                    "resource", "overload", "空响应", "无法解析",
                )
            )
            if not retryable:
                break
            import time
            time.sleep(0.75 * (2**attempt))
    return last


def _validate_tool_args(tool_name: str, args: object) -> tuple[dict | None, str | None]:
    if not isinstance(args, dict):
        return None, "工具参数必须是 JSON 对象"
    spec = next(
        (
            item["function"]
            for item in TOOLS
            if item.get("function", {}).get("name") == tool_name
        ),
        None,
    )
    if not spec:
        return None, f"未知工具: {tool_name}"
    schema = spec.get("parameters") or {}
    properties = schema.get("properties") or {}
    required = schema.get("required") or []
    unknown = sorted(set(args) - set(properties))
    if unknown:
        return None, f"工具 {tool_name} 包含未声明参数: {', '.join(unknown)}"
    missing = [
        name for name in required if args.get(name) is None or args.get(name) == ""
    ]
    if missing:
        return None, f"工具 {tool_name} 缺少必需参数: {', '.join(missing)}"
    for name, value in args.items():
        expected = properties.get(name, {}).get("type")
        if expected == "string" and not isinstance(value, str):
            return None, f"工具 {tool_name} 参数 {name} 必须是字符串"
        if expected == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
            return None, f"工具 {tool_name} 参数 {name} 必须是整数"
        if expected == "number" and not isinstance(value, (int, float)):
            return None, f"工具 {tool_name} 参数 {name} 必须是数字"
        if expected == "boolean" and not isinstance(value, bool):
            return None, f"工具 {tool_name} 参数 {name} 必须是布尔值"
        enum = properties.get(name, {}).get("enum")
        if enum and value not in enum:
            return None, f"工具 {tool_name} 参数 {name} 不在允许范围"
    return dict(args), None


def _tool_content_for_model(result: dict, max_chars: int = 20_000) -> str:
    """给模型一个有效 JSON；超长时明确截断，而不是切出无效 JSON。"""
    raw = json.dumps(result, ensure_ascii=False)
    if len(raw) <= max_chars:
        return raw
    return json.dumps(
        {
            "status": "partial",
            "truncated": True,
            "message": "工具结果过长，以下为前缀；请缩小状态或案件范围后重试。",
            "preview": raw[:max_chars],
        },
        ensure_ascii=False,
    )


def run_agent_api(
    messages: list[dict],
    max_iters: int | None = None,
    scope: dict | None = None,
    event_callback: object | None = None,
    cancel_check: object | None = None,
) -> dict:
    """跑一个对话回合（含其中的多次工具调用）。

    入参 messages：不含 system 的对话历史（user/assistant/tool）。
    scope：投保人登录态 {holder_name, username, policy_ids}；给定时智能体仅能办理其名下保单。
    返回 {messages(含本回合追加), trace(工具调用明细), reply(最终助手文本), error}。
    """
    sys_prompt = SYSTEM_PROMPT
    if scope:
        pids = "、".join(scope.get("policy_ids") or []) or "（名下暂无在册保单）"
        sys_prompt += (
            f"\n\n【当前登录投保人】{scope.get('holder_name', '')}；名下保单：{pids}。"
            f"只能为这些保单办理理赔；用户给出的保单号若不在此列表，必须拒绝并提示其只能办理自己名下保单。"
        )
    convo: list[dict] = [{"role": "system", "content": sys_prompt}]

    # 知识库 RAG：按最近用户消息检索领域口径并注入（降幻觉；不作业务数值来源）
    try:
        from agent.knowledge_base import retrieve as _kb_retrieve
    except Exception:  # noqa: BLE001
        _kb_retrieve = None
    if _kb_retrieve:
        last_user = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
        hits = _kb_retrieve(last_user, k=3) if last_user else []
        if hits:
            kb_text = "\n\n".join(f"[{h['source']}] {h['text']}" for h in hits)
            convo.append({"role": "system",
                          "content": f"【知识库参考（仅供研判口径，业务数值仍以工具返回为准）】\n{kb_text}"})

    convo += [m for m in messages if m.get("role") != "system"]
    trace: list[dict] = []

    iteration_limit = max_iters if max_iters is not None else AGENT_MAX_TOOL_ROUNDS
    for _ in range(iteration_limit):
        if callable(cancel_check) and cancel_check():
            return {
                "messages": [m for m in convo if m.get("role") != "system"],
                "trace": trace,
                "reply": "任务已取消。",
                "error": True,
                "cancelled": True,
            }
        result = _provider_chat(convo)
        if result.get("_error"):
            return {"messages": messages, "trace": trace, "reply": result["_error"], "error": True}
        if "choices" not in result:
            err = result.get("error") or result.get("error_message") or "模型返回异常"
            if isinstance(err, dict):
                err = err.get("message") or json.dumps(err, ensure_ascii=False)
            return {"messages": messages, "trace": trace, "reply": f"模型调用失败：{err}", "error": True}

        msg = result["choices"][0]["message"]
        if msg.get("tool_calls"):
            convo.append(msg)
            for tc in msg["tool_calls"]:
                tname = tc["function"]["name"]
                try:
                    targs = json.loads(tc["function"]["arguments"] or "{}")
                except Exception:  # noqa: BLE001
                    targs = {}
                validated_args, validation_error = _validate_tool_args(tname, targs)
                if validation_error:
                    tres = {"status": "error", "error_message": validation_error}
                else:
                    if callable(event_callback):
                        event_callback(
                            "tool_started",
                            {"tool": tname, "args": validated_args},
                        )
                    tres = execute_tool(tname, validated_args or {}, scope=scope)
                trace.append({"tool": tname, "args": targs, "result": tres})
                if callable(event_callback):
                    event_callback(
                        "tool_completed",
                        {
                            "tool": tname,
                            "status": tres.get("status", "success")
                            if isinstance(tres, dict)
                            else "unknown",
                            "error_message": _tool_error_message(tres),
                        },
                    )
                convo.append({"role": "tool", "tool_call_id": tc["id"],
                              "content": _tool_content_for_model(tres)})
            continue

        reply = msg.get("content", "") or ""
        convo.append({"role": "assistant", "content": reply})
        return {"messages": [m for m in convo if m.get("role") != "system"],
                "trace": trace, "reply": reply, "error": False}

    return {"messages": [m for m in convo if m.get("role") != "system"],
            "trace": trace, "reply": "（已达最大工具调用轮数，请继续指示）", "error": False}
