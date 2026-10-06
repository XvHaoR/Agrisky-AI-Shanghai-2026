"use client";

import {
  Activity,
  AlertTriangle,
  Archive,
  ArrowLeft,
  BarChart3,
  CalendarRange,
  CheckCircle2,
  ChevronDown,
  ClipboardCheck,
  Clock3,
  CloudOff,
  Coins,
  Compass,
  Crosshair,
  Database,
  FileJson,
  FileSpreadsheet,
  FileText,
  Globe2,
  Leaf,
  ListChecks,
  Loader2,
  Radar,
  RefreshCcw,
  Satellite,
  Scale,
  Search,
  ShieldCheck,
  UploadCloud,
  X,
  type LucideIcon
} from "lucide-react";
import { FormEvent, ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getApiBase } from "../_lib/api-base";
import { claimsLayout, ClaimsStageStack } from "./components/ClaimsLayout";
import EvidenceViewer from "./components/EvidenceViewer";

const API_BASE = getApiBase();

type WorkflowState =
  | "INIT"
  | "MATERIAL_CHECK"
  | "PREPROCESS_READY"
  | "SCREENING_DONE"
  | "NDVI_DONE"
  | "COMPLIANCE_DONE"
  | "RULE_DONE"
  | "REPORT_DRAFTED"
  | "HUMAN_REVIEW"
  | "ARCHIVED";

type ClaimDraft = {
  policy_id: string;
  disaster_type: string;
  loss_date: string;
  crop_type: string;
  plot_id: string;
};

type PolicyOption = {
  policy_id: string;
  policy_version_id?: string;
  holder_name?: string;
  crop_type?: string;
  address?: string;
  area_mu?: number | null;
  created_at?: string;
};

type GeoJsonLike = {
  type?: string;
  coordinates?: unknown;
  geometries?: GeoJsonLike[];
  geometry?: GeoJsonLike | null;
  features?: GeoJsonLike[];
};

type PolicyDetail = PolicyOption & {
  boundary_geojson?: GeoJsonLike | null;
  roi_geojson?: GeoJsonLike | null;
  boundary_registered?: boolean;
  boundary_sha256?: string | null;
  contract?: PolicyContractSummary | null;
};

type PolicyContractSummary = {
  contract_id?: string;
  contract_number?: string;
  contract_version?: string;
  insurance_period?: {
    start?: string;
    end?: string;
  };
  artifact_url?: string | null;
};

type PolicyListResponse = {
  total: number;
  items: PolicyOption[];
};

type MaterialDocument = {
  document_id: string;
  document_type: string;
  original_filename: string;
  media_type: string;
  size_bytes: number;
  sha256: string;
  parse_status: string;
};

type MaterialField = {
  field_id: string;
  document_id: string;
  field_name: string;
  normalized_value: unknown;
  confidence: number;
  page_number: number;
  source_ref: string;
  extractor: string;
};

type MaterialFinding = {
  finding_id: string;
  code: string;
  severity: "high" | "medium" | "low";
  field_name?: string | null;
  expected_value_json?: string | null;
  actual_value_json?: string | null;
  source_ref?: string | null;
  message: string;
  status: string;
};

type MaterialReview = {
  claim_id: string;
  passed: boolean;
  required: boolean;
  required_document_types: Array<{ code: string; label: string }>;
  missing_document_types: Array<{ code: string; label: string }>;
  documents: MaterialDocument[];
  fields: MaterialField[];
  findings: MaterialFinding[];
  blocking_count: number;
};

type SatelliteResult = {
  status: string;
  suspected_damage_area_mu?: number;
  damage_ratio?: number;
  confidence?: string;
  image_count?: number;
  start_date?: string;
  end_date?: string;
  thumbnail_url?: string | null;
  s2_thumbnail_url?: string | null;
  thumbnail_integrity?: Record<string, { provenance?: string }>;
  error_message?: string;
};

type GrowthSummary = {
  value: number;
  label: string;
  count: number;
  ratio: number;
  area_mu: number;
  color: string;
};

type GrowthResult = {
  status: string;
  task_id: string;
  method: string;
  n_classes: number;
  total_area_mu: number;
  summary: GrowthSummary[];
  outputs: Record<string, string | null | undefined>;
  raster: Record<string, unknown>;
  message?: string;
};

type HistoryForm = {
  startYear: string;
  endYear: string;
  asOfDate: string;
  maxCloudPct: string;
  scaleM: string;
};

type HistoricalQuarter = {
  window: {
    year: number;
    quarter: number;
    start_date: string;
    calendar_end_date: string;
    observation_end_date?: string | null;
    status: "complete" | "partial" | "not_reached" | string;
  };
  status: "complete" | "partial" | "no_imagery" | "no_valid_pixels" | "not_reached" | "failed" | string;
  image_count: number;
  median_ndvi?: number | null;
  mean_ndvi?: number | null;
  valid_pixel_count?: number | null;
  roi_pixel_count?: number | null;
  valid_pixel_coverage?: number | null;
  error_code?: string | null;
  error_message?: string | null;
};

type HistoricalNDVIResult = {
  schema_version: string;
  task_id: string;
  scope_label?: string;
  boundary_geometry_sha256?: string;
  generated_at?: string;
  as_of_date: string;
  start_year: number;
  end_year: number;
  source?: {
    source?: string;
    source_label?: string;
    collection?: string;
    max_cloud_pct?: number;
    scale_m?: number;
  };
  quarters: HistoricalQuarter[];
  warnings?: string[];
  artifacts?: ReportArtifact[];
};

type ParcelGrowthStat = {
  parcel_id?: string;
  feature_id?: string;
  area_mu?: number;
  valid_pixel_count?: number;
  valid_pixel_coverage?: number;
  effective_coverage?: number;
  mean_ndvi?: number | null;
  poor_growth_ratio?: number;
  quality_status?: "ok" | "warning" | "error" | string;
  anomaly_flags?: string[];
};

type ParcelGrowthResult = {
  schema_version: string;
  task_id: string;
  claim_id?: string;
  policy_id?: string;
  policy_version_id?: string;
  boundary_sha256?: string;
  source_growth_task_id?: string;
  generated_at?: string;
  overall?: ParcelGrowthStat & {
    parcel_count?: number;
    feature_count?: number;
    anomalous_parcel_count?: number;
    anomalous_feature_count?: number;
  };
  parcels?: ParcelGrowthStat[];
  features?: ParcelGrowthStat[];
  artifacts?: ReportArtifact[];
};

type ComplianceResult = {
  status: string;
  valid_damage_area_mu?: number;
  damage_ratio?: number;
  excluded_area_mu?: number;
  insured_area_mu?: number;
  clip_log?: string[];
};

type RuleResult = {
  risk_level: "low" | "medium" | "high" | string;
  review_required: boolean;
  rule_trace?: string[];
  rule_version?: string;
};

type ReportArtifact = {
  kind: "claim_report" | "growth_report" | "assessment_excel" | "bundle" | string;
  filename: string;
  url?: string;
  download_url?: string;
  media_type?: string;
  size_bytes?: number;
  sha256?: string;
};

type LegalBasisRow = {
  stage: string;
  conclusion: string;
  basis_ids: string[];
  boundary: string;
};

type ReportResult = {
  status: "success";
  claim_id?: string;
  template_version?: string;
  generation_id?: string;
  generated_at?: string;
  snapshot_sha256?: string;
  bundle_sha256?: string;
  growth_status?: "included" | "summary_rebuilt" | "not_available";
  historical_ndvi_status?: "included" | "not_available" | string;
  historical_ndvi_years?: number[];
  parcel_growth_status?: "included" | "not_available" | string;
  legal_basis?: LegalBasisRow[];
  warnings?: string[];
  artifacts?: ReportArtifact[];
  report_docx_url?: string | null;
  growth_report_docx_url?: string | null;
  excel_report_url?: string | null;
  bundle_zip_url?: string | null;
};

type HumanReviewResult = {
  status: "success";
  claim_id: string;
  state: WorkflowState;
  action: "approved" | "rejected";
  review_id: string;
  receipt_sha256: string;
  receipt_download_url: string;
  receipt?: {
    actor?: string;
    reviewed_at_utc?: string;
    comment?: string;
  };
};

type AuditLogItem = {
  id?: number;
  claim_id: string;
  action: string;
  tool_name?: string | null;
  request_json?: string | null;
  response_json?: string | null;
  notes?: string | null;
  created_at: string;
};

type LossProduct = {
  id: string;
  name_cn: string;
  sensor: string;
  native_res_m: number;
  scale: string;
  decline_score: number;
  weight: number;
  contribution: number;
  data_source: string;
  caveat?: string;
};

type LossAssessment = {
  status: string;
  yield_loss_ratio: number;
  severity?: { value: number; label: string; color: string };
  confidence?: string;
  confidence_score?: number;
  dominant_drivers?: string[];
  product_breakdown?: LossProduct[];
  data_sources?: string[];
  caveats?: string[];
};

type Payout = {
  status: string;
  crop_type?: string;
  sum_insured_per_mu?: number;
  insured_area_mu?: number;
  total_sum_insured_yuan?: number;
  affected_area_mu?: number;
  yield_loss_ratio?: number;
  claimable_loss_ratio?: number;
  payout_factor?: number;
  growth_stage?: string;
  growth_stage_factor?: number;
  tier_label?: string;
  payout_amount_yuan?: number;
  calculation_formula?: string;
  contract_number?: string;
  contract_version?: string;
  contract_sha256?: string;
  contract_clause_refs?: string[];
  trace?: string[];
};

type Message = {
  tone: "info" | "error" | "success";
  text: string;
};

type ManualExtent = {
  lonMin: string;
  latMin: string;
  lonMax: string;
  latMax: string;
};

type TelemetryInfo = {
  center: string;
  lonRange: string;
  latRange: string;
  gridSize: string;
  timeWindow: string;
  acquisition: string;
  layer: string;
  sceneId: string;
  source: string;
};

const STEPS: Array<{
  key: WorkflowState;
  icon: LucideIcon;
  label: string;
  desc: string;
}> = [
  { key: "INIT", icon: FileText, label: "新建案件", desc: "保单、地块和灾害信息" },
  { key: "MATERIAL_CHECK", icon: ClipboardCheck, label: "案件校验", desc: "保单与在册边界确认" },
  { key: "PREPROCESS_READY", icon: Satellite, label: "卫星初筛", desc: "SAR / Sentinel-2 证据" },
  { key: "SCREENING_DONE", icon: Leaf, label: "长势评估", desc: "长势分级 + 多源减产率评估" },
  { key: "NDVI_DONE", icon: Scale, label: "合规核验", desc: "受灾区与承保红线求交" },
  { key: "COMPLIANCE_DONE", icon: BarChart3, label: "规则评级", desc: "阈值、风险和复核要求" },
  { key: "RULE_DONE", icon: FileSpreadsheet, label: "报告草稿", desc: "生成理赔报告" },
  { key: "REPORT_DRAFTED", icon: ShieldCheck, label: "人工审核", desc: "审核意见与归档" },
  { key: "ARCHIVED", icon: Archive, label: "已归档", desc: "案件闭环" }
];

const STATE_STORAGE_KEY = "agrisky.claim.workspace.v3";

const initialCase: ClaimDraft = {
  policy_id: "",
  disaster_type: "flood",
  loss_date: "2025-08-30",
  crop_type: "",
  plot_id: ""
};

const initialExtent: ManualExtent = {
  lonMin: "113.00",
  latMin: "34.00",
  lonMax: "113.20",
  latMax: "34.20"
};

function area(value?: number) {
  if (typeof value !== "number" || Number.isNaN(value)) return "-";
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(value);
}

function ratio(value?: number) {
  if (typeof value !== "number" || Number.isNaN(value)) return "-";
  return `${(value * 100).toFixed(1)}%`;
}

function yuan(value?: number) {
  if (typeof value !== "number" || Number.isNaN(value)) return "-";
  return `${new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 0 }).format(value)} 元`;
}

function historyOutcome(result: HistoricalNDVIResult) {
  const counts = result.quarters.reduce<Record<string, number>>((accumulator, quarter) => {
    accumulator[quarter.status] = (accumulator[quarter.status] ?? 0) + 1;
    return accumulator;
  }, {});
  const usable = (counts.complete ?? 0) + (counts.partial ?? 0);
  const missing = (counts.no_imagery ?? 0) + (counts.no_valid_pixels ?? 0);
  return `可用 ${usable} 季度（阶段性 ${counts.partial ?? 0}）· 缺测 ${missing} · 失败 ${counts.failed ?? 0} · 未到达 ${counts.not_reached ?? 0}`;
}

function numeric(value: string, fallback: number) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function collectGeoCoordinates(value: unknown, coordinates: Array<[number, number]>) {
  if (!value) return;
  if (Array.isArray(value)) {
    if (
      value.length >= 2 &&
      typeof value[0] === "number" &&
      typeof value[1] === "number" &&
      Number.isFinite(value[0]) &&
      Number.isFinite(value[1])
    ) {
      coordinates.push([value[0], value[1]]);
      return;
    }
    value.forEach((item) => collectGeoCoordinates(item, coordinates));
    return;
  }
  if (typeof value !== "object") return;
  const geo = value as GeoJsonLike;
  collectGeoCoordinates(geo.coordinates, coordinates);
  collectGeoCoordinates(geo.geometry, coordinates);
  collectGeoCoordinates(geo.geometries, coordinates);
  collectGeoCoordinates(geo.features, coordinates);
}

function geoJsonExtent(geojson?: GeoJsonLike | null): ManualExtent | null {
  const coordinates: Array<[number, number]> = [];
  collectGeoCoordinates(geojson, coordinates);
  if (!coordinates.length) return null;
  const lonValues = coordinates.map(([lon]) => lon);
  const latValues = coordinates.map(([, lat]) => lat);
  return {
    lonMin: Math.min(...lonValues).toFixed(6),
    latMin: Math.min(...latValues).toFixed(6),
    lonMax: Math.max(...lonValues).toFixed(6),
    latMax: Math.max(...latValues).toFixed(6)
  };
}

function extentPolygon(extent: ManualExtent) {
  const lonMin = Number(extent.lonMin);
  const latMin = Number(extent.latMin);
  const lonMax = Number(extent.lonMax);
  const latMax = Number(extent.latMax);
  return {
    type: "Polygon",
    coordinates: [[[lonMin, latMin], [lonMax, latMin], [lonMax, latMax], [lonMin, latMax], [lonMin, latMin]]]
  };
}

function coordinate(value: number, axis: "lat" | "lon") {
  const suffix = axis === "lon" ? (value >= 0 ? "E" : "W") : value >= 0 ? "N" : "S";
  return `${Math.abs(value).toFixed(3)}°${suffix}`;
}

function dateOffset(value: string, days: number) {
  const source = new Date(`${value}T00:00:00Z`);
  if (Number.isNaN(source.getTime())) return "-";
  const target = new Date(source.getTime() + days * 86400000);
  return target.toISOString().slice(0, 10);
}

async function apiJson<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify(body ?? {})
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload.detail ?? payload);
    throw new Error(detail || `请求失败: ${response.status}`);
  }
  return payload as T;
}

async function apiGet<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, { credentials: "include" });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload.detail ?? payload);
    throw new Error(detail || `请求失败: ${response.status}`);
  }
  return payload as T;
}

function defaultHistoryForm(): HistoryForm {
  return {
    startYear: "2022",
    endYear: String(new Date().getFullYear()),
    asOfDate: "",
    maxCloudPct: "30",
    scaleM: "10"
  };
}

export default function ClaimsPage() {
  const [ready, setReady] = useState(false);
  const [claimId, setClaimId] = useState<string | null>(null);
  const [state, setState] = useState<WorkflowState>("INIT");
  const [caseData, setCaseData] = useState<ClaimDraft>(initialCase);
  const [boundaryFile, setBoundaryFile] = useState<File | null>(null);
  const [damageFile, setDamageFile] = useState<File | null>(null);
  const [manualExtent, setManualExtent] = useState<ManualExtent>(initialExtent);
  const [useManualExtent, setUseManualExtent] = useState(false);
  const [satellite, setSatellite] = useState<SatelliteResult | null>(null);
  const [growth, setGrowth] = useState<GrowthResult | null>(null);
  const [historicalNDVI, setHistoricalNDVI] = useState<HistoricalNDVIResult | null>(null);
  const [parcelGrowth, setParcelGrowth] = useState<ParcelGrowthResult | null>(null);
  const [historyForm, setHistoryForm] = useState<HistoryForm>(defaultHistoryForm);
  const [historyMessage, setHistoryMessage] = useState<Message | null>(null);
  const [parcelGrowthMessage, setParcelGrowthMessage] = useState<Message | null>(null);
  const [compliance, setCompliance] = useState<ComplianceResult | null>(null);
  const [rule, setRule] = useState<RuleResult | null>(null);
  const [report, setReport] = useState<ReportResult | null>(null);
  const [humanReview, setHumanReview] = useState<HumanReviewResult | null>(null);
  const [auditLogs, setAuditLogs] = useState<AuditLogItem[]>([]);
  const [lossAssessment, setLossAssessment] = useState<LossAssessment | null>(null);
  const [payout, setPayout] = useState<Payout | null>(null);
  const [policies, setPolicies] = useState<PolicyOption[]>([]);
  const [policyLoading, setPolicyLoading] = useState(false);
  const [policyError, setPolicyError] = useState("");
  const [policyDetail, setPolicyDetail] = useState<PolicyDetail | null>(null);
  const [policyDetailError, setPolicyDetailError] = useState("");
  const [materialReview, setMaterialReview] = useState<MaterialReview | null>(null);
  const [materialLoading, setMaterialLoading] = useState(false);
  const [message, setMessage] = useState<Message | null>(null);
  const [loading, setLoading] = useState(false);
  const [satelliteLayer, setSatelliteLayer] = useState<"optical" | "sar" | "growth">("optical");
  const [workTab, setWorkTab] = useState<"evidence" | "material">("evidence");
  const lockRef = useRef(false);

  const loadAuditLogs = useCallback(async (targetClaimId: string) => {
    try {
      const payload = await apiGet<AuditLogItem[]>(`/api/v1/audit/${encodeURIComponent(targetClaimId)}`);
      setAuditLogs(payload);
    } catch {
      setAuditLogs([]);
    }
  }, []);

  const loadMaterialReview = useCallback(async (targetClaimId: string) => {
    try {
      const payload = await apiGet<MaterialReview>(
        `/api/v1/cases/${encodeURIComponent(targetClaimId)}/material-review`
      );
      setMaterialReview(payload);
    } catch {
      setMaterialReview(null);
    }
  }, []);

  useEffect(() => {
    if (claimId) void loadMaterialReview(claimId);
    else setMaterialReview(null);
  }, [claimId, loadMaterialReview]);

  useEffect(() => {
    const deepLinkId = new URLSearchParams(window.location.search).get("claim_id");
    if (deepLinkId) {
      (async () => {
        try {
            const res = await fetch(`${API_BASE}/api/v1/cases/${deepLinkId}/full`, {
              credentials: "include"
            });
          if (res.ok) {
            const full = (await res.json()) as {
              claim_id: string;
              state: WorkflowState;
              case: Partial<ClaimDraft>;
              results: {
                satellite?: SatelliteResult | null;
                growth?: GrowthResult | null;
                compliance?: ComplianceResult | null;
                rule?: RuleResult | null;
                loss_assessment?: LossAssessment | null;
                payout?: Payout | null;
                report?: ReportResult | null;
                human_review?: HumanReviewResult | null;
              };
            };
            setClaimId(full.claim_id);
            setState(full.state ?? "INIT");
            setCaseData({
              policy_id: full.case.policy_id ?? "",
              disaster_type: full.case.disaster_type ?? "flood",
              loss_date: full.case.loss_date ?? "",
              crop_type: full.case.crop_type ?? "",
              plot_id: full.case.plot_id ?? ""
            });
            setSatellite(full.results.satellite ?? null);
            setGrowth(full.results.growth ?? null);
            setCompliance(full.results.compliance ?? null);
            setRule(full.results.rule ?? null);
            setLossAssessment(full.results.loss_assessment ?? null);
            setPayout(full.results.payout ?? null);
            setReport(full.results.report ?? null);
            setHumanReview(full.results.human_review ?? null);
            void loadAuditLogs(full.claim_id);
            const [historyResponse, parcelResponse] = await Promise.all([
              fetch(`${API_BASE}/api/v1/cases/${encodeURIComponent(deepLinkId)}/historical-ndvi`, {
                credentials: "include"
              }).catch(() => null),
              fetch(`${API_BASE}/api/v1/cases/${encodeURIComponent(deepLinkId)}/parcel-growth`, {
                credentials: "include"
              }).catch(() => null)
            ]);
            if (historyResponse?.ok) setHistoricalNDVI((await historyResponse.json()) as HistoricalNDVIResult);
            if (parcelResponse?.ok) setParcelGrowth((await parcelResponse.json()) as ParcelGrowthResult);
          }
        } catch {
          // 深链回填失败则按空工作区启动
        } finally {
          setReady(true);
        }
      })();
      return;
    }

    const saved = localStorage.getItem(STATE_STORAGE_KEY);
    if (saved) {
      try {
        const parsed = JSON.parse(saved) as {
          claimId?: string;
          state?: WorkflowState;
          caseData?: ClaimDraft;
          satellite?: SatelliteResult;
          growth?: GrowthResult;
          historicalNDVI?: HistoricalNDVIResult;
          parcelGrowth?: ParcelGrowthResult;
          historyForm?: HistoryForm;
          compliance?: ComplianceResult;
          rule?: RuleResult;
          lossAssessment?: LossAssessment;
          payout?: Payout;
          report?: ReportResult;
          humanReview?: HumanReviewResult;
        };
        setClaimId(parsed.claimId ?? null);
        setState(parsed.state ?? "INIT");
        setCaseData(parsed.caseData ?? initialCase);
        setSatellite(parsed.satellite ?? null);
        setGrowth(parsed.growth ?? null);
        setHistoricalNDVI(parsed.historicalNDVI ?? null);
        setParcelGrowth(parsed.parcelGrowth ?? null);
        setHistoryForm(parsed.historyForm ?? defaultHistoryForm());
        setCompliance(parsed.compliance ?? null);
        setRule(parsed.rule ?? null);
        setLossAssessment(parsed.lossAssessment ?? null);
        setPayout(parsed.payout ?? null);
        setReport(parsed.report ?? null);
        setHumanReview(parsed.humanReview ?? null);
      } catch {
        localStorage.removeItem(STATE_STORAGE_KEY);
      }
    }
    setReady(true);
  }, [loadAuditLogs]);

  useEffect(() => {
    if (!ready) return;
    localStorage.setItem(
      STATE_STORAGE_KEY,
      JSON.stringify({
        claimId,
        state,
        caseData,
        satellite,
        growth,
        historicalNDVI,
        parcelGrowth,
        historyForm,
        compliance,
        rule,
        lossAssessment,
        payout,
        report,
        humanReview
      })
    );
  }, [caseData, claimId, compliance, growth, historicalNDVI, historyForm, humanReview, lossAssessment, parcelGrowth, payout, ready, report, rule, satellite, state]);

  const loadPolicies = useCallback(async () => {
    setPolicyLoading(true);
    setPolicyError("");
    try {
      const payload = await apiGet<PolicyListResponse>("/api/v1/policies");
      setPolicies(payload.items ?? []);
    } catch (error) {
      setPolicyError(error instanceof Error ? error.message : "保单列表加载失败");
      setPolicies([]);
    } finally {
      setPolicyLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadPolicies();
  }, [loadPolicies]);

  useEffect(() => {
    if (!ready || claimId || state !== "INIT" || policies.length === 0) return;
    const current = policies.find((policy) => policy.policy_id === caseData.policy_id);
    if (current) return;
    const first = policies[0];
    setCaseData((value) => ({
      ...value,
      policy_id: first.policy_id,
      crop_type: first.crop_type || value.crop_type,
      plot_id: first.policy_version_id || first.policy_id
    }));
  }, [caseData.policy_id, claimId, policies, ready, state]);

  useEffect(() => {
    if (!caseData.policy_id) {
      setPolicyDetail(null);
      setPolicyDetailError("");
      return;
    }
    let cancelled = false;
    setPolicyDetailError("");
    apiGet<PolicyDetail>(`/api/v1/policies/${encodeURIComponent(caseData.policy_id)}`)
      .then((payload) => {
        if (!cancelled) setPolicyDetail(payload);
      })
      .catch((error) => {
        if (!cancelled) {
          setPolicyDetail(null);
          setPolicyDetailError(error instanceof Error ? error.message : "保单边界加载失败");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [caseData.policy_id]);

  // 长势分级图生成后，默认切到「长势」图层（交互式地图自带图例/控件）。
  useEffect(() => {
    if (growth) setSatelliteLayer("growth");
  }, [growth]);

  // 右列页签随阶段切换：进入材料校验自动切到材料页签，离开自动回到证据页签。
  // 两个面板始终挂载，切换只改可见性，不发生挂载/卸载式布局跳动。
  useEffect(() => {
    setWorkTab(state === "MATERIAL_CHECK" ? "material" : "evidence");
  }, [state]);

  // 操作提示改为右上角 toast：成功/信息自动消失，错误保留待手动关闭，均不推挤布局。
  useEffect(() => {
    if (!message || message.tone === "error") return;
    const timer = setTimeout(() => setMessage(null), 4500);
    return () => clearTimeout(timer);
  }, [message]);

  const activeStepIndex = Math.max(
    0,
    STEPS.findIndex((step) => step.key === state)
  );

  const riskTone = rule?.risk_level === "high" ? "risk" : rule?.risk_level === "medium" ? "warn" : "good";
  const lossSeverity = lossAssessment?.severity?.value ?? 0;
  const lossTone = lossSeverity >= 4 ? "risk" : lossSeverity >= 3 ? "warn" : "good";
  const selectedPolicy = useMemo(
    () => policies.find((policy) => policy.policy_id === caseData.policy_id) ?? null,
    [caseData.policy_id, policies]
  );
  const policyExtent = useMemo(
    () => geoJsonExtent(policyDetail?.roi_geojson ?? policyDetail?.boundary_geojson ?? null),
    [policyDetail]
  );
  const activeExtent = useMemo(
    () => (!useManualExtent && policyExtent ? policyExtent : manualExtent),
    [manualExtent, policyExtent, useManualExtent]
  );

  const telemetry = useMemo<TelemetryInfo>(() => {
    const lonMin = numeric(activeExtent.lonMin, numeric(initialExtent.lonMin, 113));
    const lonMax = numeric(activeExtent.lonMax, numeric(initialExtent.lonMax, 113.2));
    const latMin = numeric(activeExtent.latMin, numeric(initialExtent.latMin, 34));
    const latMax = numeric(activeExtent.latMax, numeric(initialExtent.latMax, 34.2));
    const centerLon = (lonMin + lonMax) / 2;
    const centerLat = (latMin + latMax) / 2;
    const windowStart = satellite?.start_date || dateOffset(caseData.loss_date, -10);
    const windowEnd = satellite?.end_date || dateOffset(caseData.loss_date, 15);
    const sceneDate = caseData.loss_date.replace(/-/g, "");
    return {
      center: `${coordinate(centerLon, "lon")} / ${coordinate(centerLat, "lat")}`,
      lonRange: `${coordinate(lonMin, "lon")} - ${coordinate(lonMax, "lon")}`,
      latRange: `${coordinate(latMin, "lat")} - ${coordinate(latMax, "lat")}`,
      gridSize: `${Math.abs(lonMax - lonMin).toFixed(3)}° x ${Math.abs(latMax - latMin).toFixed(3)}°`,
      timeWindow: `${windowStart} / ${windowEnd}`,
      acquisition: caseData.loss_date || "-",
      layer: growth ? "长势分级图" : satellite ? "SAR / 光学证据" : "等待卫星数据",
      sceneId: `AGRI-${caseData.plot_id || "PLOT"}-${sceneDate || "00000000"}`,
      source: !useManualExtent && policyExtent ? "保单在册边界" : "手动经纬度范围"
    };
  }, [activeExtent, caseData.loss_date, caseData.plot_id, growth, policyExtent, satellite, useManualExtent]);

  const growthInsights = useMemo(() => {
    const rows = growth?.summary ?? [];
    const strong = rows.filter((row) => row.value >= 4).reduce((sum, row) => sum + row.area_mu, 0);
    const weak = rows.filter((row) => row.value <= 2).reduce((sum, row) => sum + row.area_mu, 0);
    const dominant = [...rows].sort((a, b) => b.area_mu - a.area_mu)[0];
    return { strong, weak, dominant };
  }, [growth]);

  useEffect(() => {
    if (!claimId) {
      setAuditLogs([]);
      return;
    }
    void loadAuditLogs(claimId);
  }, [claimId, loadAuditLogs]);

  const run = useCallback(async (task: () => Promise<void>) => {
    if (lockRef.current) return;
    lockRef.current = true;
    setLoading(true);
    setMessage(null);
    try {
      await task();
    } catch (error) {
      setMessage({ tone: "error", text: error instanceof Error ? error.message : "操作失败" });
    } finally {
      if (claimId) await loadAuditLogs(claimId);
      lockRef.current = false;
      setLoading(false);
    }
  }, [claimId, loadAuditLogs]);

  function resetWorkspace() {
    setClaimId(null);
    setState("INIT");
    setCaseData(initialCase);
    setBoundaryFile(null);
    setDamageFile(null);
    setManualExtent(initialExtent);
    setUseManualExtent(false);
    setSatellite(null);
    setGrowth(null);
    setHistoricalNDVI(null);
    setParcelGrowth(null);
    setHistoryForm(defaultHistoryForm());
    setHistoryMessage(null);
    setParcelGrowthMessage(null);
    setCompliance(null);
    setRule(null);
    setLossAssessment(null);
    setPayout(null);
    setReport(null);
    setHumanReview(null);
    setAuditLogs([]);
    setMaterialReview(null);
    setMessage(null);
    setSatelliteLayer("optical");
    localStorage.removeItem(STATE_STORAGE_KEY);
  }

  function back() {
    const previous: Partial<Record<WorkflowState, WorkflowState>> = {
      MATERIAL_CHECK: "INIT",
      PREPROCESS_READY: "MATERIAL_CHECK",
      SCREENING_DONE: "PREPROCESS_READY",
      NDVI_DONE: "SCREENING_DONE",
      COMPLIANCE_DONE: "NDVI_DONE",
      RULE_DONE: "COMPLIANCE_DONE",
      REPORT_DRAFTED: "RULE_DONE",
      HUMAN_REVIEW: "REPORT_DRAFTED"
    };
    const next = previous[state];
    if (next) setState(next);
  }

  async function createClaim(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    await run(async () => {
      if (!caseData.policy_id) throw new Error("请先选择已登记保单。");
      if (policies.length > 0 && !selectedPolicy) throw new Error("当前保单不在保单库中，请先登记或刷新保单列表。");
      if (policyDetail && policyDetail.boundary_registered === false) throw new Error("所选保单没有在册地块边界，请先到“保单与地块”补充边界。");
      const response = await apiJson<{ claim_id: string; state: WorkflowState }>("/api/v1/tools/create_claim", caseData);
      setClaimId(response.claim_id);
      setState(response.state);
      setMessage({ tone: "success", text: "案件已创建，进入案件要素校验。" });
      await loadAuditLogs(response.claim_id);
    });
  }

  async function validateMaterials() {
    await run(async () => {
      const response = await apiJson<{ passed: boolean; missing_fields?: string[] }>("/api/v1/tools/validate_materials", {
        claim_id: claimId
      });
      if (!response.passed) {
        throw new Error(`案件校验未通过: ${(response.missing_fields ?? []).join(", ") || "请检查保单与在册边界"}`);
      }
      setState("PREPROCESS_READY");
      setMessage({ tone: "success", text: "案件要素与在册边界校验通过。" });
    });
  }

  async function uploadMaterial(documentType: string, file: File) {
    if (!claimId) return;
    setMaterialLoading(true);
    setMessage(null);
    try {
      const formData = new FormData();
      formData.append("document_type", documentType);
      formData.append("document", file);
      const response = await fetch(
        `${API_BASE}/api/v1/cases/${encodeURIComponent(claimId)}/documents`,
        {
          method: "POST",
          credentials: "include",
          body: formData
        }
      );
      const payload = (await response.json()) as {
        review?: MaterialReview;
        detail?: string;
      };
      if (!response.ok) throw new Error(payload.detail || `材料上传失败: ${response.status}`);
      if (payload.review) setMaterialReview(payload.review);
      else await loadMaterialReview(claimId);
      await loadAuditLogs(claimId);
      setMessage({ tone: "success", text: "材料已上传并完成结构化解析。" });
    } catch (error) {
      setMessage({
        tone: "error",
        text: error instanceof Error ? error.message : "材料上传失败"
      });
    } finally {
      setMaterialLoading(false);
    }
  }

  async function resolveMaterialFinding(findingId: string) {
    if (!claimId) return;
    setMaterialLoading(true);
    try {
      const response = await apiJson<{ review: MaterialReview }>(
        `/api/v1/cases/${encodeURIComponent(claimId)}/material-findings/${encodeURIComponent(findingId)}/resolve`,
        {
          decision: "accepted",
          note: "演示审核：已对照原始材料与在册信息，由当前审核人确认继续处理。"
        }
      );
      setMaterialReview(response.review);
      await loadAuditLogs(claimId);
    } catch (error) {
      setMessage({
        tone: "error",
        text: error instanceof Error ? error.message : "材料问题处理失败"
      });
    } finally {
      setMaterialLoading(false);
    }
  }

  async function runSatelliteScreening() {
    if (!claimId) return;
    await run(async () => {
      const startDate = dateOffset(caseData.loss_date, -10);
      const endDate = dateOffset(caseData.loss_date, 15);
      if (startDate === "-" || endDate === "-") throw new Error("灾害日期无效，无法计算卫星时窗。");
      let response: SatelliteResult;

      if (boundaryFile && !useManualExtent) {
        const formData = new FormData();
        formData.append("boundary_file", boundaryFile);
        formData.append("claim_id", claimId);
        formData.append("start_date", startDate);
        formData.append("end_date", endDate);
        const res = await fetch(`${API_BASE}/api/v1/tools/run_satellite_screening_upload`, {
          method: "POST",
          credentials: "include",
          body: formData
        });
        response = (await res.json()) as SatelliteResult;
        if (!res.ok) throw new Error(response.error_message || `卫星初筛失败: ${res.status}`);
      } else {
        response = await apiJson<SatelliteResult>("/api/v1/tools/run_satellite_screening", {
          claim_id: claimId,
          roi_geojson: extentPolygon(activeExtent),
          start_date: startDate,
          end_date: endDate
        });
      }

      if (response.status !== "success") throw new Error(response.error_message || "卫星初筛未返回有效结果");
      setSatellite(response);
      setState("SCREENING_DONE");
      setMessage({ tone: "success", text: "卫星初筛完成。" });
    });
  }

  async function runGrowthAnalysis() {
    await run(async () => {
      if (!claimId) throw new Error("缺少案件号，无法执行长势分析。");
      const payload = await apiJson<GrowthResult>("/api/v1/tools/run_growth_analysis_by_claim", {
        claim_id: claimId,
        ndvi_source: "auto",
        method: "fixed"
      });
      if (payload.status !== "success") {
        throw new Error(payload.message || "长势分析失败");
      }
      setGrowth(payload);

      // 多源遥感灾损评估（减产率）—— 超越单期 NDVI，融合多指数 + MODIS 高阶产品
      if (claimId) {
        try {
          const la = await apiJson<LossAssessment>("/api/v1/tools/run_loss_assessment", {
            claim_id: claimId,
            roi_geojson: extentPolygon(activeExtent)
          });
          if (la.status === "success") setLossAssessment(la);
        } catch {
          // 灾损评估失败不阻断主流程
        }
      }

      setState("NDVI_DONE");
      setMessage({ tone: "success", text: "长势分级图 + 多源减产率评估完成。" });
    });
  }

  async function runComplianceCheck() {
    if (!claimId) return;
    await run(async () => {
      // 赔付链只读取服务端已登记的遥感/灾损结果；本地候选受灾文件不得直接决定赔付比例。
      const payload = await apiJson<ComplianceResult>("/api/v1/tools/run_compliance_estimate", {
        claim_id: claimId
      });
      setCompliance(payload);
      setState("COMPLIANCE_DONE");
      setMessage({ tone: "success", text: "合规核验完成。" });
    });
  }

  async function runRuleEngine() {
    if (!claimId) return;
    await run(async () => {
      // 先测算赔款（减产率 → 预估赔款），用于规则引擎的大额预警
      try {
        const p = await apiJson<Payout>("/api/v1/tools/run_payout_estimate", { claim_id: claimId });
        if (p.status === "success") setPayout(p);
      } catch {
        // 赔付测算失败不阻断评级
      }
      const response = await apiJson<RuleResult>("/api/v1/tools/run_rule_engine", { claim_id: claimId });
      setRule(response);
      setState("RULE_DONE");
      setMessage({ tone: "success", text: "赔付测算 + 规则评级完成。" });
    });
  }

  async function generateReport() {
    if (!claimId) return;
    await run(async () => {
      const payload = await apiJson<ReportResult>("/api/v1/tools/generate_report", {
        claim_id: claimId
      });
      setReport(payload);
      setState("REPORT_DRAFTED");
      const warningText = payload.warnings?.length ? `（${payload.warnings.join("；")}）` : "";
      setMessage({ tone: "success", text: `报告草稿及可校验附件包已生成，等待人工审核。${warningText}` });
    });
  }

  function historicalRequestBody() {
    if (!claimId) throw new Error("请先创建或打开案件。");
    const startYear = Number(historyForm.startYear);
    const endYear = Number(historyForm.endYear);
    const maxCloudPct = Number(historyForm.maxCloudPct);
    const scaleM = Number(historyForm.scaleM);
    if (!Number.isInteger(startYear) || !Number.isInteger(endYear) || startYear < 2017 || endYear < startYear) {
      throw new Error("历史年份必须从 2017 年起按升序填写。");
    }
    if (endYear - startYear + 1 > 20) throw new Error("单次历史监测不能超过 20 年。");
    if (!Number.isFinite(maxCloudPct) || maxCloudPct < 0 || maxCloudPct > 100) {
      throw new Error("最大云量必须在 0–100 之间。");
    }
    if (!Number.isInteger(scaleM) || scaleM < 10 || scaleM > 100) {
      throw new Error("计算尺度必须是 10–100 米的整数。");
    }
    return {
      claim_id: claimId,
      start_year: startYear,
      end_year: endYear,
      as_of_date: historyForm.asOfDate || undefined,
      max_cloud_pct: maxCloudPct,
      scale_m: scaleM
    };
  }

  async function runHistoricalNDVI() {
    await run(async () => {
      setHistoryMessage({ tone: "info", text: "正在调用真实 GEE 数据计算季度序列；不会生成模拟结果。" });
      try {
        const payload = await apiJson<HistoricalNDVIResult>(
          "/api/v1/tools/run_historical_ndvi_by_claim",
          historicalRequestBody()
        );
        setHistoricalNDVI(payload);
        const warningSuffix = payload.warnings?.length ? `；另有 ${payload.warnings.length} 条数据质量警告` : "";
        const text = `${historyOutcome(payload)}${warningSuffix}`;
        setHistoryMessage({ tone: payload.warnings?.length ? "info" : "success", text });
        setMessage({ tone: "success", text: `历史季度 NDVI 已保存。${text}` });
      } catch (error) {
        const text = error instanceof Error ? error.message : "历史季度 NDVI 计算失败";
        setHistoryMessage({ tone: "error", text });
        throw error;
      }
    });
  }

  async function queryHistoricalNDVI() {
    if (!claimId) {
      setHistoryMessage({ tone: "error", text: "请先创建或打开案件。" });
      return;
    }
    await run(async () => {
      setHistoryMessage({ tone: "info", text: "正在读取案件已保存的历史季度 NDVI 快照。" });
      try {
        const payload = await apiGet<HistoricalNDVIResult>(
          `/api/v1/cases/${encodeURIComponent(claimId)}/historical-ndvi`
        );
        setHistoricalNDVI(payload);
        setHistoryMessage({
          tone: payload.warnings?.length ? "info" : "success",
          text: `${historyOutcome(payload)}${payload.warnings?.length ? `；${payload.warnings.length} 条警告` : ""}`
        });
      } catch (error) {
        const text = error instanceof Error ? error.message : "历史季度 NDVI 查询失败";
        setHistoryMessage({ tone: "error", text });
        throw error;
      }
    });
  }

  async function runParcelGrowth() {
    if (!claimId) {
      setParcelGrowthMessage({ tone: "error", text: "请先创建或打开案件。" });
      return;
    }
    await run(async () => {
      setParcelGrowthMessage({
        tone: "info",
        text: "正在按已人工确认的 parcel_id / feature_id 聚合当前案件真实 NDVI 栅格。"
      });
      try {
        const payload = await apiJson<ParcelGrowthResult>(
          "/api/v1/tools/run_parcel_growth_by_claim",
          { claim_id: claimId }
        );
        setParcelGrowth(payload);
        const overall = payload.overall;
        const text = `完成 ${overall?.parcel_count ?? payload.parcels?.length ?? 0} 个地块、${overall?.feature_count ?? payload.features?.length ?? 0} 个要素；异常地块 ${overall?.anomalous_parcel_count ?? 0}`;
        setParcelGrowthMessage({
          tone: overall?.quality_status === "ok" ? "success" : "info",
          text
        });
        setMessage({ tone: "success", text: `分地块长势已保存。${text}` });
      } catch (error) {
        const text = error instanceof Error ? error.message : "分地块长势计算失败";
        setParcelGrowthMessage({ tone: "error", text });
        throw error;
      }
    });
  }

  async function queryParcelGrowth() {
    if (!claimId) {
      setParcelGrowthMessage({ tone: "error", text: "请先创建或打开案件。" });
      return;
    }
    await run(async () => {
      setParcelGrowthMessage({ tone: "info", text: "正在读取案件已保存的分地块长势快照。" });
      try {
        const payload = await apiGet<ParcelGrowthResult>(
          `/api/v1/cases/${encodeURIComponent(claimId)}/parcel-growth`
        );
        setParcelGrowth(payload);
        setParcelGrowthMessage({
          tone: payload.overall?.quality_status === "ok" ? "success" : "info",
          text: `已读取 ${payload.overall?.parcel_count ?? payload.parcels?.length ?? 0} 个地块；总体有效覆盖率 ${ratio(payload.overall?.valid_pixel_coverage)}`
        });
      } catch (error) {
        const text = error instanceof Error ? error.message : "分地块长势查询失败";
        setParcelGrowthMessage({ tone: "error", text });
        throw error;
      }
    });
  }

  async function approveClaim() {
    if (!claimId || !report?.generation_id) {
      setMessage({ tone: "error", text: "缺少待审核报告生成号，请刷新案件后重试。" });
      return;
    }
    await run(async () => {
      const response = await fetch(`${API_BASE}/api/v1/cases/${claimId}/human_review`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({
          decision: "approved",
          generation_id: report.generation_id,
          comment: "已核对当前报告版本及附件，同意归档。",
          idempotency_key: `approve:${claimId}:${report.generation_id}`
        })
      });
      const payload = (await response.json().catch(() => ({}))) as HumanReviewResult & { detail?: string };
      if (!response.ok) throw new Error(payload.detail || `审核归档失败: ${response.status}`);
      setHumanReview(payload);
      setState("ARCHIVED");
      setMessage({ tone: "success", text: "案件已归档，并生成了绑定当前报告版本的审核回执。" });
    });
  }

  async function saveArtifact(path: string, filename: string) {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 60_000);
    const response = await fetch(`${API_BASE}${path}`, {
      credentials: "include",
      signal: controller.signal
    }).finally(() => window.clearTimeout(timeout));
    if (!response.ok) {
      const payload = (await response.json().catch(() => ({}))) as { detail?: string };
      throw new Error(payload.detail || `下载失败: ${response.status}`);
    }
    const contentType = (response.headers.get("content-type") || "").toLowerCase();
    const expectsJson = filename.toLowerCase().endsWith(".json");
    if (contentType.includes("text/html") || (contentType.includes("application/json") && !expectsJson)) {
      throw new Error("服务器返回的不是报告文件，请刷新案件或检查登录状态。");
    }
    const blob = await response.blob();
    if (!blob.size) throw new Error("下载文件为空。");
    const disposition = response.headers.get("content-disposition") || "";
    const encodedName = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
    const plainName = disposition.match(/filename="?([^";]+)"?/i)?.[1];
    let resolvedName = filename;
    try {
      resolvedName = encodedName ? decodeURIComponent(encodedName) : plainName || filename;
    } catch {
      resolvedName = filename;
    }
    const objectUrl = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = objectUrl;
    anchor.download = resolvedName;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1_000);
  }

  async function downloadExisting(path: string, filename: string) {
    await run(async () => {
      await saveArtifact(path, filename);
    });
  }

  async function downloadReportArtifact(kind: ReportArtifact["kind"]) {
    const artifact = report?.artifacts?.find(
      (item) => item.kind === kind && typeof item.download_url === "string" && item.download_url.length > 0
    );
    if (!artifact?.download_url) {
      setMessage({ tone: "error", text: "该报告附件尚未迁移到受控下载版本，请联系管理员处理。" });
      return;
    }
    await downloadExisting(artifact.download_url, artifact.filename);
  }

  async function downloadAnalysisArtifact(artifact: ReportArtifact) {
    if (!artifact.download_url) {
      setMessage({ tone: "error", text: "该分析附件没有受控下载地址，已拒绝使用服务端文件路径。" });
      return;
    }
    await downloadExisting(artifact.download_url, artifact.filename);
  }

  if (!ready) return <ClaimsSkeleton />;

  const StageIcon = STEPS[activeStepIndex]?.icon ?? FileText;
  const reportArtifact = (kind: ReportArtifact["kind"]) => report?.artifacts?.find(
    (item) => item.kind === kind && typeof item.download_url === "string" && item.download_url.length > 0
  );
  const hasDownloadableReport = Boolean(report?.artifacts?.some(
    (item) => typeof item.download_url === "string" && item.download_url.length > 0
  ));
  const hasClaimContext = Boolean(claimId);
  // 摘要面板组：固定在左栏操作区下方，任何阶段不再左右搬家。
  const summaryPanels = hasClaimContext ? (
    <div className="claims-summary-slot">
      <ClaimEmbeddedSummary
        growth={growth}
        growthInsights={growthInsights}
        compliance={compliance}
        rule={rule}
        compact
      />
      <div className={`claim-kpi-grid ${claimsLayout.kpis}`}>
        <Metric icon={<Satellite size={18} />} label="卫星初筛" value={ratio(satellite?.damage_ratio)} />
        <Metric icon={<Leaf size={18} />} label="减产率" value={ratio(lossAssessment?.yield_loss_ratio)} tone={lossTone} />
        <Metric icon={<Coins size={18} />} label="预估赔款" value={yuan(payout?.payout_amount_yuan)} />
        <Metric icon={<ShieldCheck size={18} />} label="规则风险" value={rule?.risk_level ?? "-"} tone={riskTone} />
      </div>
    </div>
  ) : null;
  const materialBlocking = materialReview?.blocking_count ?? 0;

  return (
    <main className={`shell claims-shell ${claimsLayout.shell}`}>
      <SpaceBackdrop telemetry={telemetry} />

      <header className={`topbar claims-topbar ${claimsLayout.topbar}`}>
        <div className="brand-block">
          <div className="brand-mark">
            <ShieldCheck size={22} />
          </div>
          <div>
            <p className="eyebrow">Agrisky AI · Claims Command</p>
            <h1>穹野智保理赔驾驶舱</h1>
          </div>
        </div>
        <div className="top-status">
          <StatusPill icon={<Database size={15} />} label={claimId ?? "未建案"} />
          <StatusPill icon={<Activity size={15} />} label={API_BASE.replace(/^https?:\/\//, "")} />
          <button className="secondary icon-action" type="button" onClick={resetWorkspace} title="重置工作区">
            <RefreshCcw size={16} />
          </button>
        </div>
      </header>

      <TelemetryStrip telemetry={telemetry} state={state} />

      <WorkflowBar state={state} activeIndex={activeStepIndex} />

      <MessageToast message={message} onClose={() => setMessage(null)} />

      <section className={`claims-workspace claims-workspace--state-${state.toLowerCase()} ${claimsLayout.workspace}`}>
        <div className={`claims-rail ${claimsLayout.column}`}>
        <aside className={`control-panel claim-controls claims-stage-panel ${claimsLayout.controls}`}>
          <div className="panel-heading">
            <div>
              <p className="eyebrow">当前阶段</p>
              <h2>{STEPS[activeStepIndex]?.label}</h2>
            </div>
            {loading ? <Loader2 className="spin" size={20} /> : <StageIcon size={20} />}
          </div>
          <p className="stage-copy">{STEPS[activeStepIndex]?.desc}</p>

          <ClaimsStageStack>
          {state === "INIT" ? (
            <ClaimForm
              data={caseData}
              loading={loading}
              policies={policies}
              policyError={policyError}
              policyDetailError={policyDetailError}
              policyLoading={policyLoading}
              selectedPolicy={selectedPolicy}
              onChange={setCaseData}
              onRefreshPolicies={loadPolicies}
              onSubmit={createClaim}
            />
          ) : null}

          {state === "MATERIAL_CHECK" ? (
            <StageAction
              icon={<ClipboardCheck size={18} />}
              title="案件要素校验"
              metrics={[
                ["保单匹配", selectedPolicy ? "已匹配" : "待确认"],
                ["在册边界", policyDetail?.boundary_registered ? "已登记" : "待登记"],
                ["阻断问题", `${materialReview?.blocking_count ?? 0} 项`]
              ]}
              actionLabel="确认并进入卫星初筛"
              onAction={validateMaterials}
              loading={loading}
            />
          ) : null}

          {state === "PREPROCESS_READY" ? (
            <BoundaryStage
              boundaryFile={boundaryFile}
              useManualExtent={useManualExtent}
              manualExtent={manualExtent}
              loading={loading}
              onBoundaryFile={setBoundaryFile}
              onManualToggle={setUseManualExtent}
              onExtentChange={setManualExtent}
              onRun={runSatelliteScreening}
            />
          ) : null}

          {state === "SCREENING_DONE" ? (
            <StageAction
              icon={<Leaf size={18} />}
              title="作物长势评估"
              metrics={[
                ["疑似受灾", `${area(satellite?.suspected_damage_area_mu)} 亩`],
                ["初筛比例", ratio(satellite?.damage_ratio)],
                ["置信度", satellite?.confidence ?? "-"]
              ]}
              actionLabel="执行长势评估"
              onAction={runGrowthAnalysis}
              loading={loading}
            />
          ) : null}

          {state === "NDVI_DONE" ? (
            <ComplianceStage
              growthInsights={growthInsights}
              damageFile={damageFile}
              onDamageFile={setDamageFile}
              onRun={runComplianceCheck}
              loading={loading}
            />
          ) : null}

          {state === "COMPLIANCE_DONE" ? (
            <StageAction
              icon={<BarChart3 size={18} />}
              title="规则引擎评级"
              metrics={[
                ["承保面积", `${area(compliance?.insured_area_mu)} 亩`],
                ["合规受灾", `${area(compliance?.valid_damage_area_mu)} 亩`],
                ["合规比例", ratio(compliance?.damage_ratio)]
              ]}
              actionLabel="执行规则评级"
              onAction={runRuleEngine}
              loading={loading}
            />
          ) : null}

          {state === "RULE_DONE" ? (
            <StageAction
              icon={<FileSpreadsheet size={18} />}
              title="报告生成"
              metrics={[
                ["风险等级", rule?.risk_level ?? "-"],
                ["人工复核", rule?.review_required ? "需要" : "不需要"],
                ["规则版本", rule?.rule_version ?? "-"]
              ]}
              actionLabel="生成报告草稿"
              onAction={generateReport}
              loading={loading}
            />
          ) : null}

          {state === "REPORT_DRAFTED" || state === "HUMAN_REVIEW" ? (
            <>
              <StageAction
                icon={<ShieldCheck size={18} />}
                title="人工审核"
                metrics={[
                  ["案件号", claimId ?? "-"],
                  ["风险等级", rule?.risk_level ?? "-"],
                  ["建议动作", rule?.review_required ? "复核后归档" : "确认归档"]
                ]}
                actionLabel="审核通过并归档"
                onAction={approveClaim}
                loading={loading}
              />
              <div className={`archive-downloads ${claimsLayout.downloads}`}>
                {policyDetail?.contract?.artifact_url ? (
                  <button
                    className="secondary contract-download"
                    type="button"
                    onClick={() => downloadExisting(
                      policyDetail.contract?.artifact_url ?? "",
                      `农业保险合同-${policyDetail.contract?.contract_number ?? caseData.policy_id}.docx`
                    )}
                    disabled={loading}
                  >
                    <FileText size={15} /> 保险合同 DOCX
                  </button>
                ) : null}
                {reportArtifact("claim_report") ? (
                  <button className="secondary" type="button" onClick={() => downloadReportArtifact("claim_report")} disabled={loading}>
                    <FileText size={15} /> 理赔报告 DOCX
                  </button>
                ) : null}
                {reportArtifact("growth_report") ? (
                  <button className="secondary" type="button" onClick={() => downloadReportArtifact("growth_report")} disabled={loading}>
                    <Leaf size={15} /> 长势报告 DOCX
                  </button>
                ) : null}
                {reportArtifact("assessment_excel") ? (
                  <button className="secondary" type="button" onClick={() => downloadReportArtifact("assessment_excel")} disabled={loading}>
                    <FileSpreadsheet size={15} /> 评估表 Excel
                  </button>
                ) : null}
                {reportArtifact("legal_basis") ? (
                  <button className="secondary" type="button" onClick={() => downloadReportArtifact("legal_basis")} disabled={loading}>
                    <FileJson size={15} /> 合规依据 JSON
                  </button>
                ) : null}
                {reportArtifact("bundle") ? (
                  <button className="secondary" type="button" onClick={() => downloadReportArtifact("bundle")} disabled={loading}>
                    <Archive size={15} /> 报告草稿包 ZIP
                  </button>
                ) : null}
                {humanReview?.receipt_download_url ? (
                  <button
                    className="secondary"
                    type="button"
                    onClick={() => downloadExisting(
                      humanReview.receipt_download_url,
                      `审核回执-${humanReview.review_id}.json`
                    )}
                    disabled={loading}
                  >
                    <ClipboardCheck size={15} /> 审核回执 JSON
                  </button>
                ) : null}
              </div>
              {report?.generation_id ? (
                <small>
                  报告生成号：{report.generation_id}
                  {report.template_version ? ` · 模板 ${report.template_version}` : ""}
                </small>
              ) : null}
              {humanReview?.receipt_sha256 ? (
                <small>审核回执：{humanReview.review_id} · SHA-256 {humanReview.receipt_sha256.slice(0, 12)}</small>
              ) : null}
              {!hasDownloadableReport ? <small>历史报告尚未迁移到受控下载格式，请联系管理员。</small> : null}
            </>
          ) : null}

          {state === "ARCHIVED" ? (
            <div className="archive-card">
              <Archive size={34} />
              <strong>案件已归档</strong>
              <span>{claimId}</span>
              {policyDetail?.contract?.contract_number ? (
                <div className="archive-contract-ref">
                  <span>合同依据</span>
                  <strong>{policyDetail.contract.contract_number}</strong>
                </div>
              ) : null}
              <div className={`archive-downloads ${claimsLayout.downloads}`}>
                {policyDetail?.contract?.artifact_url ? (
                  <button
                    className="secondary contract-download"
                    type="button"
                    onClick={() => downloadExisting(
                      policyDetail.contract?.artifact_url ?? "",
                      `农业保险合同-${policyDetail.contract?.contract_number ?? caseData.policy_id}.docx`
                    )}
                    disabled={loading}
                  >
                    <FileText size={15} /> 保险合同 DOCX
                  </button>
                ) : null}
                {reportArtifact("claim_report") ? (
                  <button className="secondary" type="button" onClick={() => downloadReportArtifact("claim_report")} disabled={loading}>
                    <FileText size={15} /> 理赔报告 DOCX
                  </button>
                ) : null}
                {reportArtifact("growth_report") ? (
                  <button className="secondary" type="button" onClick={() => downloadReportArtifact("growth_report")} disabled={loading}>
                    <Leaf size={15} /> 长势报告 DOCX
                  </button>
                ) : null}
                {reportArtifact("assessment_excel") ? (
                  <button className="secondary" type="button" onClick={() => downloadReportArtifact("assessment_excel")} disabled={loading}>
                    <FileSpreadsheet size={15} /> 评估表 Excel
                  </button>
                ) : null}
                {reportArtifact("legal_basis") ? (
                  <button className="secondary" type="button" onClick={() => downloadReportArtifact("legal_basis")} disabled={loading}>
                    <FileJson size={15} /> 合规依据 JSON
                  </button>
                ) : null}
                {reportArtifact("bundle") ? (
                  <button className="secondary" type="button" onClick={() => downloadReportArtifact("bundle")} disabled={loading}>
                    <Archive size={15} /> 报告草稿包 ZIP
                  </button>
                ) : null}
                {humanReview?.receipt_download_url ? (
                  <button
                    className="secondary"
                    type="button"
                    onClick={() => downloadExisting(
                      humanReview.receipt_download_url,
                      `审核回执-${humanReview.review_id}.json`
                    )}
                    disabled={loading}
                  >
                    <ClipboardCheck size={15} /> 审核回执 JSON
                  </button>
                ) : null}
              </div>
              {report?.generation_id ? (
                <small>
                  报告生成号：{report.generation_id}
                  {report.snapshot_sha256 ? ` · 快照 ${report.snapshot_sha256.slice(0, 12)}` : ""}
                </small>
              ) : null}
              {humanReview?.receipt_sha256 ? (
                <small>审核回执：{humanReview.review_id} · SHA-256 {humanReview.receipt_sha256.slice(0, 12)}</small>
              ) : null}
              {!hasDownloadableReport ? <small>历史报告尚未迁移到受控下载格式，请联系管理员。</small> : null}
              <button className="primary" type="button" onClick={resetWorkspace}>
                新建下一案件
              </button>
            </div>
          ) : null}

          </ClaimsStageStack>

          {activeStepIndex > 0 && state !== "ARCHIVED" ? (
            <button className="ghost-action" type="button" onClick={back}>
              <ArrowLeft size={15} />
              返回上一阶段
            </button>
          ) : null}

        </aside>
        {summaryPanels}
        </div>

        <section className="preview-panel work-panel">
          <div className="work-tabbar">
            <div className="work-tabs" role="tablist" aria-label="工作区切换">
              <button
                type="button"
                role="tab"
                aria-selected={workTab === "evidence"}
                className={`work-tab ${workTab === "evidence" ? "active" : ""}`}
                onClick={() => setWorkTab("evidence")}
              >
                遥感证据
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={workTab === "material"}
                className={`work-tab ${workTab === "material" ? "active" : ""}`}
                disabled={!hasClaimContext}
                title={hasClaimContext ? undefined : "建案后可用"}
                onClick={() => setWorkTab("material")}
              >
                材料校验
                {state === "MATERIAL_CHECK" || materialBlocking > 0 ? (
                  <span className="work-tab-badge">{materialBlocking > 0 ? materialBlocking : "!"}</span>
                ) : null}
              </button>
            </div>
            {workTab === "evidence" ? (
              <div className="segmented">
                <button
                  className={satelliteLayer === "optical" ? "active" : ""}
                  type="button"
                  onClick={() => setSatelliteLayer("optical")}
                >
                  光学
                </button>
                <button
                  className={satelliteLayer === "sar" ? "active" : ""}
                  type="button"
                  onClick={() => setSatelliteLayer("sar")}
                >
                  SAR
                </button>
                <button
                  className={satelliteLayer === "growth" ? "active" : ""}
                  type="button"
                  disabled={!growth}
                  title={growth ? undefined : "完成长势评估后可用"}
                  onClick={() => setSatelliteLayer("growth")}
                >
                  长势
                </button>
              </div>
            ) : null}
          </div>

          <div className="work-pane" hidden={workTab !== "evidence"}>
            <div className="section-head work-pane-head">
              <div>
                <p className="eyebrow">遥感证据</p>
                <h2>遥感证据与判读结果</h2>
              </div>
            </div>
            <EvidenceViewer
              satellite={satellite}
              growth={growth}
              layer={satelliteLayer}
              telemetry={telemetry}
            />
          </div>
          <div className="work-pane" hidden={workTab !== "material"}>
            {hasClaimContext ? (
              <MaterialReviewWorkspace
                review={materialReview}
                loading={materialLoading}
                onUpload={uploadMaterial}
                onResolve={resolveMaterialFinding}
              />
            ) : (
              <p className="work-pane-empty">建案后可上传并核验理赔材料。</p>
            )}
          </div>
        </section>

        <div className={`${claimsLayout.lower} claims-dock`}>
          <DockSection
            title="扩展分析 · 历史季度 NDVI / 分地块长势"
            ready={Boolean(historicalNDVI || parcelGrowth)}
            status={historicalNDVI || parcelGrowth ? "已有分析结果" : hasClaimContext ? "未运行" : "建案后可用"}
          >
            {hasClaimContext ? (
              <AdvancedAnalysisPanel
                claimId={claimId}
                workflowState={state}
                loading={loading}
                historyForm={historyForm}
                onHistoryFormChange={setHistoryForm}
                historicalNDVI={historicalNDVI}
                historyMessage={historyMessage}
                onRunHistorical={runHistoricalNDVI}
                onQueryHistorical={queryHistoricalNDVI}
                parcelGrowth={parcelGrowth}
                parcelGrowthMessage={parcelGrowthMessage}
                onRunParcelGrowth={runParcelGrowth}
                onQueryParcelGrowth={queryParcelGrowth}
                onDownloadArtifact={downloadAnalysisArtifact}
                report={report}
              />
            ) : (
              <p className="dock-empty">建案后可运行历史 NDVI 与分地块长势分析。</p>
            )}
          </DockSection>
          <DockSection
            title="多源遥感灾损评估"
            ready={Boolean(lossAssessment?.product_breakdown?.length)}
            status={lossAssessment ? `减产率 ${ratio(lossAssessment.yield_loss_ratio)}` : "待长势评估"}
          >
            {lossAssessment?.product_breakdown?.length ? (
              <LossPanel loss={lossAssessment} />
            ) : (
              <p className="dock-empty">长势评估完成后展示多源灾损分量。</p>
            )}
          </DockSection>
          <DockSection
            title="赔付测算"
            ready={Boolean(payout)}
            status={payout ? `预估赔款 ${yuan(payout.payout_amount_yuan)}` : "待规则评级"}
          >
            {payout ? (
              <PayoutPanel payout={payout} />
            ) : (
              <p className="dock-empty">规则评级完成后展示赔付测算。</p>
            )}
          </DockSection>
          <DockSection
            title="本案结论与合规依据"
            ready={Boolean(report?.legal_basis?.length)}
            status={report?.legal_basis?.length ? `${report.legal_basis.length} 个业务环节` : "待报告生成"}
          >
            {report?.legal_basis?.length ? (
              <LegalBasisPanel basis={report.legal_basis} />
            ) : (
              <p className="dock-empty">报告生成后展示合规依据链路。</p>
            )}
          </DockSection>
          <DockSection
            title="审计时间线"
            ready={auditLogs.length > 0}
            status={`${auditLogs.length} 条记录`}
            defaultOpen
          >
            <AuditTimelinePanel auditLogs={auditLogs} />
          </DockSection>
        </div>
      </section>
    </main>
  );
}

const MATERIAL_TYPE_LABELS: Record<string, string> = {
  policy_document: "保单或承保凭证",
  claim_notice: "出险通知或理赔申请",
  damage_certificate: "灾情证明或查勘记录",
  onsite_photo: "现场照片",
  other: "其他材料"
};

const MATERIAL_FIELD_LABELS: Record<string, string> = {
  policy_id: "保单号",
  holder_name: "投保人/经营主体",
  crop_type: "作物",
  insured_area_mu: "承保面积（亩）",
  loss_date: "出险日期",
  disaster_type: "灾害类型",
  plot_id: "地块编号",
  reported_damage_area_mu: "申报受灾面积（亩）",
  reported_loss_ratio: "申报损失比例",
  survey_date: "查勘日期"
};

function materialJson(value?: string | null) {
  if (!value) return "-";
  try {
    const parsed = JSON.parse(value) as unknown;
    return typeof parsed === "string" ? parsed : JSON.stringify(parsed);
  } catch {
    return value;
  }
}

function MaterialReviewWorkspace({
  review,
  loading,
  onUpload,
  onResolve
}: {
  review: MaterialReview | null;
  loading: boolean;
  onUpload: (documentType: string, file: File) => Promise<void>;
  onResolve: (findingId: string) => Promise<void>;
}) {
  const [documentType, setDocumentType] = useState("policy_document");
  const [file, setFile] = useState<File | null>(null);

  return (
    <section className="material-review-workspace">
      <div className="section-head">
        <div>
          <p className="eyebrow">Document Intelligence</p>
          <h2>理赔材料理解与一致性核验</h2>
        </div>
        <span className={`material-review-status ${review?.passed ? "passed" : "pending"}`}>
          {review?.passed ? <CheckCircle2 size={16} /> : <AlertTriangle size={16} />}
          {review?.passed ? "材料门禁已通过" : "等待补齐或人工处理"}
        </span>
      </div>

      <div className="material-upload-bar">
        <label>
          <span>材料类型</span>
          <select value={documentType} onChange={(event) => setDocumentType(event.target.value)}>
            {Object.entries(MATERIAL_TYPE_LABELS).map(([code, label]) => (
              <option key={code} value={code}>{label}</option>
            ))}
          </select>
        </label>
        <label className="material-file-picker">
          <span>选择 PDF / DOCX / PNG / JPEG</span>
          <input
            type="file"
            accept=".pdf,.docx,.png,.jpg,.jpeg"
            onChange={(event) => setFile(event.target.files?.[0] ?? null)}
          />
        </label>
        <button
          className="primary"
          type="button"
          disabled={!file || loading}
          onClick={async () => {
            if (!file) return;
            await onUpload(documentType, file);
            setFile(null);
          }}
        >
          {loading ? <Loader2 className="spin" size={16} /> : <UploadCloud size={16} />}
          上传并解析
        </button>
      </div>

      <div className="material-required-strip">
        {(review?.required_document_types ?? []).map((item) => {
          const missing = review?.missing_document_types.some((missingItem) => missingItem.code === item.code);
          return (
            <span key={item.code} className={missing ? "missing" : "ready"}>
              {missing ? <AlertTriangle size={13} /> : <CheckCircle2 size={13} />}
              {item.label}
            </span>
          );
        })}
      </div>

      <div className="material-review-grid">
        <section className="material-section">
          <div className="section-head">
            <h2>已上传材料</h2>
            <span>{review?.documents.length ?? 0} 份</span>
          </div>
          <div className="material-document-list">
            {(review?.documents ?? []).map((document) => (
              <a
                key={document.document_id}
                href={`${API_BASE}/api/v1/cases/${encodeURIComponent(review?.claim_id ?? "")}/documents/${encodeURIComponent(document.document_id)}/content`}
                target="_blank"
                rel="noreferrer"
              >
                <FileText size={17} />
                <span>
                  <strong>{document.original_filename}</strong>
                  <small>{MATERIAL_TYPE_LABELS[document.document_type] ?? document.document_type}</small>
                </span>
                <em className={document.parse_status}>{document.parse_status}</em>
              </a>
            ))}
            {!review?.documents.length ? <p className="empty-copy">尚未上传案件材料。</p> : null}
          </div>
        </section>

        <section className="material-section material-findings">
          <div className="section-head">
            <h2>一致性问题</h2>
            <span>{review?.findings.length ?? 0} 项</span>
          </div>
          <div className="material-finding-list">
            {(review?.findings ?? []).map((finding) => (
              <article key={finding.finding_id} className={`material-finding ${finding.severity}`}>
                <div>
                  <strong>{finding.message}</strong>
                  <small>
                    {finding.source_ref ? `证据 ${finding.source_ref}` : "系统门禁"}
                    {finding.field_name ? ` · ${MATERIAL_FIELD_LABELS[finding.field_name] ?? finding.field_name}` : ""}
                  </small>
                  <p>
                    系统值：{materialJson(finding.expected_value_json)}
                    {" · "}
                    材料值：{materialJson(finding.actual_value_json)}
                  </p>
                </div>
                <button
                  className="secondary"
                  type="button"
                  disabled={loading}
                  onClick={() => onResolve(finding.finding_id)}
                >
                  人工核验
                </button>
              </article>
            ))}
            {!review?.findings.length ? (
              <p className="empty-copy">未发现需要处理的材料冲突。</p>
            ) : null}
          </div>
        </section>
      </div>

      <section className="material-section material-fields">
        <div className="section-head">
          <div>
            <p className="eyebrow">Grounded Fields</p>
            <h2>结构化字段与证据引用</h2>
          </div>
          <span>{review?.fields.length ?? 0} 个字段</span>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>字段</th>
                <th>提取值</th>
                <th>置信度</th>
                <th>来源</th>
                <th>提取器</th>
              </tr>
            </thead>
            <tbody>
              {(review?.fields ?? []).map((field) => (
                <tr key={field.field_id}>
                  <td>{MATERIAL_FIELD_LABELS[field.field_name] ?? field.field_name}</td>
                  <td>{String(field.normalized_value ?? "-")}</td>
                  <td>{Math.round(field.confidence * 100)}%</td>
                  <td>第 {field.page_number} 页 · {field.source_ref}</td>
                  <td>{field.extractor}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </section>
  );
}

function StatusPill({ icon, label }: { icon: ReactNode; label: string }) {
  return (
    <div className="api-pill">
      {icon}
      <span>{label}</span>
    </div>
  );
}

const AUDIT_ACTION_LABELS: Record<string, string> = {
  create_claim: "建案",
  validate_materials: "材料校验",
  run_satellite_screening: "卫星初筛",
  run_growth_analysis: "长势评估",
  run_historical_ndvi: "历史 NDVI",
  run_parcel_growth: "分地块长势",
  run_loss_assessment: "减产率评估",
  run_compliance_calc: "合规核验",
  run_payout_estimate: "赔付测算",
  run_rule_engine: "规则评级",
  generate_report: "报告草稿",
  generate_excel_report: "评估表生成",
  human_review_approved: "人工审核通过",
  human_review_rejected: "人工审核驳回",
  download_report_artifact: "报告下载",
  download_review_receipt: "审核回执下载"
};

function auditTime(value: string) {
  if (!value) return "-";
  return value.replace("T", " ").slice(0, 19);
}

function AuditTimelinePanel({ auditLogs }: { auditLogs: AuditLogItem[] }) {
  const ordered = [...auditLogs].sort((a, b) => Date.parse(a.created_at) - Date.parse(b.created_at));
  return (
    <section className="preview-panel audit-timeline-panel">
      <div className="section-head">
        <div>
          <p className="eyebrow">Audit Trail</p>
          <h2>审计时间线</h2>
        </div>
        <span className="audit-count">{ordered.length} 条记录</span>
      </div>

      {ordered.length ? (
        <div className="audit-timeline">
          {ordered.map((item, index) => (
            <div className="audit-timeline-item" key={`${item.created_at}-${item.action}-${index}`}>
              <div className="audit-dot">
                <ListChecks size={13} />
              </div>
              <div>
                <strong>{AUDIT_ACTION_LABELS[item.action] ?? item.action}</strong>
                <span>
                  {auditTime(item.created_at)}
                  {item.tool_name ? ` · ${item.tool_name}` : ""}
                </span>
                {item.notes ? <p>{item.notes}</p> : null}
              </div>
            </div>
          ))}
        </div>
      ) : (
        <div className="audit-empty">
          当前案件暂无审计日志。建案后系统会追加记录，已归档案件可用于展示完整审计链路。
        </div>
      )}
    </section>
  );
}

function SpaceBackdrop({ telemetry }: { telemetry: TelemetryInfo }) {
  return (
    <div className="space-backdrop" aria-hidden="true">
      <span className="orbit-line orbit-line-a" />
      <span className="orbit-line orbit-line-b" />
      <span className="space-label space-label-north">{telemetry.latRange}</span>
      <span className="space-label space-label-east">{telemetry.lonRange}</span>
      <span className="space-label space-label-time">{telemetry.timeWindow}</span>
    </div>
  );
}

function TelemetryStrip({ telemetry, state }: { telemetry: TelemetryInfo; state: WorkflowState }) {
  const activeStep = STEPS.find((step) => step.key === state);
  return (
    <section className="telemetry-strip !grid !grid-cols-1 !items-stretch !gap-2 sm:!grid-cols-2 min-[1181px]:!grid-cols-5" aria-label="空天地时空遥测信息">
      <TelemetryCard icon={<Crosshair size={16} />} label="中心坐标" value={telemetry.center} />
      <TelemetryCard icon={<Compass size={16} />} label="格网范围" value={telemetry.gridSize} />
      <TelemetryCard icon={<Clock3 size={16} />} label="轨道时窗" value={telemetry.timeWindow} />
      <TelemetryCard icon={<Globe2 size={16} />} label="范围来源" value={telemetry.source} />
      <TelemetryCard icon={<Radar size={16} />} label="任务状态" value={activeStep?.label ?? state} />
    </section>
  );
}

function TelemetryCard({ icon, label, value }: { icon: ReactNode; label: string; value: string }) {
  return (
    <div className="telemetry-card !m-0 !h-full !min-h-[58px] !p-2.5">
      <div className="telemetry-icon">{icon}</div>
      <div>
        <span>{label}</span>
        <strong>{value}</strong>
      </div>
    </div>
  );
}

function WorkflowBar({ state, activeIndex }: { state: WorkflowState; activeIndex: number }) {
  return (
    <section className="workflow-card !m-0 !overflow-x-auto !p-2">
      <div className="workflow-track !min-w-[860px]">
        {STEPS.map((step, index) => {
          const Icon = step.icon;
          const complete = index < activeIndex || state === "ARCHIVED";
          const active = index === activeIndex && state !== "ARCHIVED";
          return (
            <div className="workflow-step" key={step.key}>
              <div className={`workflow-node ${complete ? "complete" : ""} ${active ? "active" : ""}`}>
                {complete ? <CheckCircle2 size={16} /> : <Icon size={16} />}
                <span>{step.label}</span>
              </div>
              {index < STEPS.length - 1 ? <div className={`workflow-line ${complete ? "complete" : ""}`} /> : null}
            </div>
          );
        })}
      </div>
    </section>
  );
}

function ClaimForm({
  data,
  loading,
  policies,
  policyError,
  policyDetailError,
  policyLoading,
  selectedPolicy,
  onChange,
  onRefreshPolicies,
  onSubmit
}: {
  data: ClaimDraft;
  loading: boolean;
  policies: PolicyOption[];
  policyError: string;
  policyDetailError: string;
  policyLoading: boolean;
  selectedPolicy: PolicyOption | null;
  onChange: (value: ClaimDraft) => void;
  onRefreshPolicies: () => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
}) {
  function selectPolicy(policyId: string) {
    const policy = policies.find((item) => item.policy_id === policyId);
    onChange({
      ...data,
      policy_id: policyId,
      crop_type: policy?.crop_type || data.crop_type,
      plot_id: policy?.policy_version_id || policyId
    });
  }

  return (
    <form className="form-stack" onSubmit={onSubmit}>
      <div className="policy-select-head">
        <div>
          <p className="eyebrow">Policy Registry</p>
          <h3>选择已登记保单</h3>
        </div>
        <button className="secondary icon-action" type="button" onClick={onRefreshPolicies} disabled={policyLoading} title="刷新保单列表">
          {policyLoading ? <Loader2 className="spin" size={15} /> : <RefreshCcw size={15} />}
        </button>
      </div>
      {policyError ? (
        <div className="message-box error">
          <AlertTriangle size={15} />
          <span>{policyError}</span>
        </div>
      ) : null}
      {policyDetailError ? (
        <div className="message-box error">
          <AlertTriangle size={15} />
          <span>{policyDetailError}</span>
        </div>
      ) : null}
      {policies.length > 0 ? (
        <Field label="在册保单">
          <select value={data.policy_id} onChange={(event) => selectPolicy(event.target.value)}>
            {policies.map((policy) => (
              <option key={policy.policy_id} value={policy.policy_id}>
                {policy.policy_id}
                {policy.holder_name ? ` · ${policy.holder_name}` : ""}
                {policy.area_mu ? ` · ${area(policy.area_mu ?? undefined)}亩` : ""}
              </option>
            ))}
          </select>
        </Field>
      ) : (
        <div className="policy-empty-card">
          <AlertTriangle size={16} />
          <div>
            <strong>还没有可选保单</strong>
            <span>请先到“保单与地块”登记承保边界，再回到这里创建理赔案件。</span>
          </div>
        </div>
      )}
      {selectedPolicy ? (
        <div className="policy-context-card">
          <div>
            <span>投保人</span>
            <strong>{selectedPolicy.holder_name || "-"}</strong>
          </div>
          <div>
            <span>保单版本</span>
            <strong>{selectedPolicy.policy_version_id || `${selectedPolicy.policy_id}:v1`}</strong>
          </div>
          <div>
            <span>承保面积</span>
            <strong>{area(selectedPolicy.area_mu ?? undefined)} 亩</strong>
          </div>
          <div>
            <span>地块地址</span>
            <strong>{selectedPolicy.address || "-"}</strong>
          </div>
        </div>
      ) : null}
      <div className="field-grid">
        <Field label="灾害类型">
          <select value={data.disaster_type} onChange={(event) => onChange({ ...data, disaster_type: event.target.value })}>
            <option value="flood">洪涝</option>
            <option value="drought">干旱</option>
            <option value="hail">冰雹</option>
            <option value="typhoon">台风</option>
            <option value="pest">病虫害</option>
            <option value="frost">霜冻</option>
            <option value="other">其他</option>
          </select>
        </Field>
        <Field label="作物">
          <input
            value={data.crop_type}
            onChange={(event) => onChange({ ...data, crop_type: event.target.value })}
            placeholder={selectedPolicy?.crop_type ? "已由保单带出，可按案件修正" : "请输入作物"}
          />
        </Field>
        <Field label="灾害日期">
          <input type="date" value={data.loss_date} onChange={(event) => onChange({ ...data, loss_date: event.target.value })} />
        </Field>
      </div>
      <Field label="地块编号 / 案件地块备注">
        <input value={data.plot_id} onChange={(event) => onChange({ ...data, plot_id: event.target.value })} />
      </Field>
      <button
        className={`primary full-width${loading ? " is-loading" : ""}`}
        type="submit"
        disabled={loading || policyLoading || policies.length === 0 || !data.policy_id || Boolean(policyDetailError)}
      >
        {loading ? <Loader2 className="spin" size={17} /> : <FileText size={17} />}
        基于所选保单创建案件
      </button>
    </form>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="field">
      <span>{label}</span>
      {children}
    </label>
  );
}

function BoundaryStage({
  boundaryFile,
  useManualExtent,
  manualExtent,
  loading,
  onBoundaryFile,
  onManualToggle,
  onExtentChange,
  onRun
}: {
  boundaryFile: File | null;
  useManualExtent: boolean;
  manualExtent: ManualExtent;
  loading: boolean;
  onBoundaryFile: (file: File | null) => void;
  onManualToggle: (value: boolean) => void;
  onExtentChange: (value: ManualExtent) => void;
  onRun: () => void;
}) {
  return (
    <div className="form-stack">
      <label className="file-drop">
        <input
          type="file"
          accept=".shp,.geojson,.json,.gpkg,.kml,.zip"
          onChange={(event) => {
            const file = event.target.files?.[0] ?? null;
            onBoundaryFile(file);
            if (file) onManualToggle(false);
          }}
        />
        <UploadCloud size={20} />
        <span>{boundaryFile ? boundaryFile.name : "上传承保边界 SHP / GeoJSON / GPKG / ZIP"}</span>
      </label>

      <label className="switch-row">
        <input type="checkbox" checked={useManualExtent} onChange={(event) => onManualToggle(event.target.checked)} />
        <span>使用手动经纬度范围</span>
      </label>

      {useManualExtent ? (
        <div className="extent-grid">
          <Field label="左经度">
            <input value={manualExtent.lonMin} onChange={(event) => onExtentChange({ ...manualExtent, lonMin: event.target.value })} />
          </Field>
          <Field label="下纬度">
            <input value={manualExtent.latMin} onChange={(event) => onExtentChange({ ...manualExtent, latMin: event.target.value })} />
          </Field>
          <Field label="右经度">
            <input value={manualExtent.lonMax} onChange={(event) => onExtentChange({ ...manualExtent, lonMax: event.target.value })} />
          </Field>
          <Field label="上纬度">
            <input value={manualExtent.latMax} onChange={(event) => onExtentChange({ ...manualExtent, latMax: event.target.value })} />
          </Field>
        </div>
      ) : null}

      <button className="primary full-width" type="button" onClick={onRun} disabled={loading}>
        {loading ? <Loader2 className="spin" size={17} /> : <Satellite size={17} />}
        执行卫星初筛
      </button>
    </div>
  );
}

function StageAction({
  icon,
  title,
  metrics,
  actionLabel,
  loading,
  onAction
}: {
  icon: ReactNode;
  title: string;
  metrics: Array<[string, string]>;
  actionLabel: string;
  loading: boolean;
  onAction: () => void;
}) {
  return (
    <div className="stage-action !grid !content-start !gap-3">
      <div className="stage-action-title">
        {icon}
        <strong>{title}</strong>
      </div>
      <div className="compact-metric-list !grid !grid-cols-[repeat(auto-fit,minmax(100px,1fr))] !gap-2">
        {metrics.map(([label, value]) => (
          <div key={label}>
            <span>{label}</span>
            <strong>{value}</strong>
          </div>
        ))}
      </div>
      <button className="primary full-width" type="button" onClick={onAction} disabled={loading}>
        {loading ? <Loader2 className="spin" size={17} /> : <CheckCircle2 size={17} />}
        {actionLabel}
      </button>
    </div>
  );
}

function ComplianceStage({
  growthInsights,
  damageFile,
  onDamageFile,
  onRun,
  loading
}: {
  growthInsights: { strong: number; weak: number; dominant?: GrowthSummary };
  damageFile: File | null;
  onDamageFile: (file: File | null) => void;
  onRun: () => void;
  loading: boolean;
}) {
  return (
    <div className="stage-action !grid !content-start !gap-3">
      <div className="stage-action-title">
        <Scale size={18} />
        <strong>服务端权威合规核验</strong>
      </div>
      <div className="compact-metric-list !grid !grid-cols-[repeat(auto-fit,minmax(100px,1fr))] !gap-2">
        <div>
          <span>长势良好</span>
          <strong>{area(growthInsights.strong)} 亩</strong>
        </div>
        <div>
          <span>弱势长势</span>
          <strong>{area(growthInsights.weak)} 亩</strong>
        </div>
        <div>
          <span>主导等级</span>
          <strong>{growthInsights.dominant?.label ?? "-"}</strong>
        </div>
      </div>
      <label className="file-drop">
        <input
          type="file"
          accept=".shp,.geojson,.json,.gpkg,.kml,.zip"
          onChange={(event) => onDamageFile(event.target.files?.[0] ?? null)}
        />
        <UploadCloud size={18} />
        <span>{damageFile ? `${damageFile.name}（仅本地待审，不参与自动赔付）` : "可选：选择待人工审查的受灾范围（不会自动提交）"}</span>
      </label>
      <button className="primary full-width" type="button" onClick={onRun} disabled={loading}>
        {loading ? <Loader2 className="spin" size={17} /> : <CheckCircle2 size={17} />}
        执行合规核验（按减产率）
      </button>
    </div>
  );
}

function Metric({
  icon,
  label,
  value,
  tone
}: {
  icon: ReactNode;
  label: string;
  value: string;
  tone?: "good" | "warn" | "risk";
}) {
  return (
    <div className={`metric rich !m-0 !min-h-[48px] ${tone ?? ""}`}>
      <div className="metric-icon">{icon}</div>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function historyStatusLabel(status: string) {
  const labels: Record<string, string> = {
    complete: "完整",
    partial: "阶段性",
    no_imagery: "无符合条件影像",
    no_valid_pixels: "无有效像元",
    not_reached: "尚未到达",
    failed: "计算失败"
  };
  return labels[status] ?? status;
}

function analysisArtifactLabel(kind: string) {
  if (kind === "historical_ndvi_report") return "历史 NDVI 报告 DOCX";
  if (kind === "historical_ndvi_result") return "历史 NDVI 权威结果 JSON";
  if (kind === "historical_ndvi_trend") return "跨年季度趋势图 PNG";
  if (kind.startsWith("historical_ndvi_year_panel_")) return `${kind.replace("historical_ndvi_year_panel_", "")} 年 2×2 图组 PNG`;
  if (kind === "parcel_growth_result") return "分地块长势权威结果 JSON";
  return kind;
}

function analysisMessageClass(message: Message) {
  if (message.tone === "error") return "error";
  if (message.tone === "success") return "success";
  return "info";
}

function AdvancedAnalysisPanel({
  claimId,
  workflowState,
  loading,
  historyForm,
  onHistoryFormChange,
  historicalNDVI,
  historyMessage,
  onRunHistorical,
  onQueryHistorical,
  parcelGrowth,
  parcelGrowthMessage,
  onRunParcelGrowth,
  onQueryParcelGrowth,
  onDownloadArtifact,
  report
}: {
  claimId: string | null;
  workflowState: WorkflowState;
  loading: boolean;
  historyForm: HistoryForm;
  onHistoryFormChange: (form: HistoryForm) => void;
  historicalNDVI: HistoricalNDVIResult | null;
  historyMessage: Message | null;
  onRunHistorical: () => void;
  onQueryHistorical: () => void;
  parcelGrowth: ParcelGrowthResult | null;
  parcelGrowthMessage: Message | null;
  onRunParcelGrowth: () => void;
  onQueryParcelGrowth: () => void;
  onDownloadArtifact: (artifact: ReportArtifact) => void;
  report: ReportResult | null;
}) {
  const historyCounts = (historicalNDVI?.quarters ?? []).reduce<Record<string, number>>((accumulator, quarter) => {
    accumulator[quarter.status] = (accumulator[quarter.status] ?? 0) + 1;
    return accumulator;
  }, {});
  const frozen = ["REPORT_DRAFTED", "HUMAN_REVIEW", "ARCHIVED"].includes(workflowState);

  return (
    <section className="preview-panel advanced-analysis-panel">
      <div className="section-head">
        <div>
          <p className="eyebrow">Authority analysis extensions</p>
          <h2>历史季度 NDVI · 分地块长势</h2>
        </div>
        <div className="analysis-report-status">
          <span className={report?.historical_ndvi_status === "included" ? "included" : "missing"}>
            历史 NDVI：{report?.historical_ndvi_status === "included" ? "已入报告包" : "未入当前报告包"}
          </span>
          <span className={report?.parcel_growth_status === "included" ? "included" : "missing"}>
            分地块：{report?.parcel_growth_status === "included" ? "已入报告包" : "未入当前报告包"}
          </span>
        </div>
      </div>

      <div className="analysis-freeze-note">
        <ListChecks size={16} />
        <span>
          两类结果均绑定案件当前在册边界和不可变附件。{frozen
            ? " 当前案件报告已冻结：可以查询既有结果；只有参数完全相同的历史任务才可复用，不能替换快照。"
            : " 请在生成报告草稿前完成；之后生成的报告包会自动收录可用结果。"}
        </span>
      </div>

      <div className="advanced-analysis-grid">
        <section className="analysis-card">
          <div className="analysis-card-head">
            <div className="analysis-card-icon"><CalendarRange size={20} /></div>
            <div>
              <h3>跨年度季度 NDVI</h3>
              <p>真实 GEE Sentinel-2 SR；保留完整、阶段性、缺影像、无有效像元、失败和未来季度。</p>
            </div>
          </div>

          <div className="history-parameter-grid">
            <label className="field"><span>起始年</span>
              <input type="number" min="2017" max="2100" value={historyForm.startYear}
                onChange={(event) => onHistoryFormChange({ ...historyForm, startYear: event.target.value })} />
            </label>
            <label className="field"><span>结束年</span>
              <input type="number" min="2017" max="2100" value={historyForm.endYear}
                onChange={(event) => onHistoryFormChange({ ...historyForm, endYear: event.target.value })} />
            </label>
            <label className="field"><span>统计截止日</span>
              <input type="date" value={historyForm.asOfDate}
                onChange={(event) => onHistoryFormChange({ ...historyForm, asOfDate: event.target.value })} />
            </label>
            <label className="field"><span>最大云量 %</span>
              <input type="number" min="0" max="100" step="1" value={historyForm.maxCloudPct}
                onChange={(event) => onHistoryFormChange({ ...historyForm, maxCloudPct: event.target.value })} />
            </label>
            <label className="field"><span>尺度 m</span>
              <input type="number" min="10" max="100" step="10" value={historyForm.scaleM}
                onChange={(event) => onHistoryFormChange({ ...historyForm, scaleM: event.target.value })} />
            </label>
          </div>
          <small className="analysis-prerequisite">截止日留空时使用服务器当日；结束年份不能晚于截止日所在年份，单次最多 20 年。GEE 失败时系统明确报错，不生成模拟序列。</small>
          <div className="analysis-action-row">
            <button className="primary" type="button" onClick={onRunHistorical} disabled={loading || !claimId}>
              {loading ? <Loader2 className="spin" size={16} /> : <Satellite size={16} />} 运行 / 精确复用
            </button>
            <button className="secondary" type="button" onClick={onQueryHistorical} disabled={loading || !claimId}>
              <Search size={16} /> 查询已保存结果
            </button>
          </div>

          {historyMessage ? (
            <div className={`message-box ${analysisMessageClass(historyMessage)}`}>
              {historyMessage.tone === "error" ? <CloudOff size={15} /> : <CheckCircle2 size={15} />}
              <span>{historyMessage.text}</span>
            </div>
          ) : null}

          {historicalNDVI ? (
            <div className="analysis-result-stack">
              <div className="analysis-result-identity">
                <strong>{historicalNDVI.start_year}–{historicalNDVI.end_year}</strong>
                <span>截至 {historicalNDVI.as_of_date}</span>
                <span title={historicalNDVI.task_id}>任务 {historicalNDVI.task_id}</span>
                <span>云量 ≤ {historicalNDVI.source?.max_cloud_pct ?? "-"}% · {historicalNDVI.source?.scale_m ?? "-"} m</span>
              </div>
              <div className="history-status-grid">
                {[
                  ["complete", "完整"],
                  ["partial", "阶段性"],
                  ["no_imagery", "无影像"],
                  ["no_valid_pixels", "无像元"],
                  ["failed", "失败"],
                  ["not_reached", "未到达"]
                ].map(([key, label]) => (
                  <div key={key} className={key}><strong>{historyCounts[key] ?? 0}</strong><span>{label}</span></div>
                ))}
              </div>
              {(historicalNDVI.warnings?.length ?? 0) > 0 ? (
                <div className="analysis-warning-list">
                  {historicalNDVI.warnings?.map((warning, index) => <div key={`${warning}-${index}`}><AlertTriangle size={13} /><span>{warning}</span></div>)}
                </div>
              ) : null}
              <details className="analysis-table-details">
                <summary>查看全部季度槽位（{historicalNDVI.quarters.length}）</summary>
                <div className="table-wrap analysis-result-table">
                  <table>
                    <thead><tr><th>季度</th><th>状态</th><th>观测截止</th><th>影像</th><th>中位 NDVI</th><th>均值</th><th>覆盖率</th><th>错误/说明</th></tr></thead>
                    <tbody>
                      {historicalNDVI.quarters.map((quarter) => (
                        <tr key={`${quarter.window.year}-Q${quarter.window.quarter}`}>
                          <td>{quarter.window.year}-Q{quarter.window.quarter}</td>
                          <td><span className={`observation-status ${quarter.status}`}>{historyStatusLabel(quarter.status)}</span></td>
                          <td>{quarter.window.observation_end_date || "-"}</td>
                          <td>{quarter.image_count}</td>
                          <td>{typeof quarter.median_ndvi === "number" ? quarter.median_ndvi.toFixed(4) : "-"}</td>
                          <td>{typeof quarter.mean_ndvi === "number" ? quarter.mean_ndvi.toFixed(4) : "-"}</td>
                          <td>{ratio(quarter.valid_pixel_coverage ?? undefined)}</td>
                          <td title={quarter.error_message || ""}>{quarter.error_code || quarter.error_message || "-"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </details>
              <div className="analysis-artifacts">
                {(historicalNDVI.artifacts ?? []).filter((artifact) => artifact.download_url).map((artifact) => (
                  <button className="secondary" type="button" key={`${artifact.kind}-${artifact.sha256}`} onClick={() => onDownloadArtifact(artifact)} disabled={loading}>
                    <FileText size={14} /> {analysisArtifactLabel(artifact.kind)}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <div className="analysis-empty">尚无历史季度结果。查询返回 404 表示本案确实还没有保存结果。</div>
          )}
        </section>

        <section className="analysis-card">
          <div className="analysis-card-head">
            <div className="analysis-card-icon"><ListChecks size={20} /></div>
            <div>
              <h3>parcel_id / feature_id 长势</h3>
              <p>在同一份真实 NDVI 栅格上分别统计每个已确认地块与源要素，保留覆盖率和异常标志。</p>
            </div>
          </div>
          <div className="analysis-prerequisite-box">
            <strong>运行前置条件</strong>
            <span>保单页已完成人工映射确认；确认地块并集与在册保单版本边界一致；案件已生成真实 GEE NDVI，模拟 NDVI 不可作为权威分地块统计。</span>
          </div>
          <div className="analysis-action-row">
            <button className="primary" type="button" onClick={onRunParcelGrowth} disabled={loading || !claimId}>
              {loading ? <Loader2 className="spin" size={16} /> : <Leaf size={16} />} 运行 / 精确复用
            </button>
            <button className="secondary" type="button" onClick={onQueryParcelGrowth} disabled={loading || !claimId}>
              <Search size={16} /> 查询已保存结果
            </button>
          </div>

          {parcelGrowthMessage ? (
            <div className={`message-box ${analysisMessageClass(parcelGrowthMessage)}`}>
              {parcelGrowthMessage.tone === "error" ? <AlertTriangle size={15} /> : <CheckCircle2 size={15} />}
              <span>{parcelGrowthMessage.text}</span>
            </div>
          ) : null}

          {parcelGrowth ? (
            <div className="analysis-result-stack">
              <div className="analysis-result-identity">
                <strong>{parcelGrowth.policy_id || "案件保单"}</strong>
                <span>{parcelGrowth.policy_version_id || "-"}</span>
                <span title={parcelGrowth.task_id}>任务 {parcelGrowth.task_id}</span>
                <span title={parcelGrowth.source_growth_task_id}>源长势 {parcelGrowth.source_growth_task_id || "-"}</span>
              </div>
              <div className="parcel-overall-grid">
                <div><strong>{parcelGrowth.overall?.parcel_count ?? parcelGrowth.parcels?.length ?? 0}</strong><span>地块</span></div>
                <div><strong>{parcelGrowth.overall?.feature_count ?? parcelGrowth.features?.length ?? 0}</strong><span>要素</span></div>
                <div><strong>{ratio(parcelGrowth.overall?.valid_pixel_coverage)}</strong><span>有效覆盖</span></div>
                <div><strong>{ratio(parcelGrowth.overall?.effective_coverage)}</strong><span>综合覆盖</span></div>
                <div><strong>{typeof parcelGrowth.overall?.mean_ndvi === "number" ? parcelGrowth.overall.mean_ndvi.toFixed(4) : "-"}</strong><span>平均 NDVI</span></div>
                <div><strong>{parcelGrowth.overall?.anomalous_parcel_count ?? 0}</strong><span>异常地块</span></div>
              </div>
              {(parcelGrowth.overall?.anomaly_flags?.length ?? 0) > 0 ? (
                <div className="analysis-warning-list">
                  {parcelGrowth.overall?.anomaly_flags?.map((flag) => <div key={flag}><AlertTriangle size={13} /><span>{flag}</span></div>)}
                </div>
              ) : null}
              <details className="analysis-table-details" open>
                <summary>地块级结果（{parcelGrowth.parcels?.length ?? 0}）</summary>
                <div className="table-wrap analysis-result-table parcel-growth-table">
                  <table>
                    <thead><tr><th>parcel_id</th><th>面积</th><th>质量</th><th>有效覆盖</th><th>综合覆盖</th><th>平均 NDVI</th><th>弱势比例</th><th>异常</th></tr></thead>
                    <tbody>
                      {(parcelGrowth.parcels ?? []).map((parcel, index) => (
                        <tr key={parcel.parcel_id || index}>
                          <td>{parcel.parcel_id || "-"}</td>
                          <td>{area(parcel.area_mu)}</td>
                          <td><span className={`quality-status ${parcel.quality_status || "unknown"}`}>{parcel.quality_status || "unknown"}</span></td>
                          <td>{ratio(parcel.valid_pixel_coverage)}</td>
                          <td>{ratio(parcel.effective_coverage)}</td>
                          <td>{typeof parcel.mean_ndvi === "number" ? parcel.mean_ndvi.toFixed(4) : "-"}</td>
                          <td>{ratio(parcel.poor_growth_ratio)}</td>
                          <td title={(parcel.anomaly_flags ?? []).join(", ")}>{(parcel.anomaly_flags ?? []).join(", ") || "-"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </details>
              <details className="analysis-table-details">
                <summary>feature_id 级结果（{parcelGrowth.features?.length ?? 0}）</summary>
                <div className="table-wrap analysis-result-table parcel-growth-table">
                  <table>
                    <thead><tr><th>parcel_id</th><th>feature_id</th><th>质量</th><th>有效覆盖</th><th>平均 NDVI</th><th>弱势比例</th><th>异常</th></tr></thead>
                    <tbody>
                      {(parcelGrowth.features ?? []).map((feature, index) => (
                        <tr key={`${feature.parcel_id}-${feature.feature_id || index}`}>
                          <td>{feature.parcel_id || "-"}</td>
                          <td>{feature.feature_id || "-"}</td>
                          <td><span className={`quality-status ${feature.quality_status || "unknown"}`}>{feature.quality_status || "unknown"}</span></td>
                          <td>{ratio(feature.valid_pixel_coverage)}</td>
                          <td>{typeof feature.mean_ndvi === "number" ? feature.mean_ndvi.toFixed(4) : "-"}</td>
                          <td>{ratio(feature.poor_growth_ratio)}</td>
                          <td title={(feature.anomaly_flags ?? []).join(", ")}>{(feature.anomaly_flags ?? []).join(", ") || "-"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </details>
              <div className="analysis-artifacts">
                {(parcelGrowth.artifacts ?? []).filter((artifact) => artifact.download_url).map((artifact) => (
                  <button className="secondary" type="button" key={`${artifact.kind}-${artifact.sha256}`} onClick={() => onDownloadArtifact(artifact)} disabled={loading}>
                    <FileJson size={14} /> {analysisArtifactLabel(artifact.kind)}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <div className="analysis-empty">尚无分地块结果。若后端提示映射缺失、几何不一致、模拟 NDVI 或报告已冻结，应先处理对应前置条件。</div>
          )}
        </section>
      </div>
    </section>
  );
}

function ClaimEmbeddedSummary({
  growth,
  growthInsights,
  compliance,
  rule,
  compact = false
}: {
  growth: GrowthResult | null;
  growthInsights: { strong: number; weak: number; dominant?: GrowthSummary };
  compliance: ComplianceResult | null;
  rule: RuleResult | null;
  compact?: boolean;
}) {
  const traceLines = [
    ...(compliance?.clip_log ?? []),
    ...(rule?.rule_trace ?? [])
  ].slice(0, 3);

  return (
    <div className={`claim-embedded-stack !grid !items-stretch !gap-2 ${compact ? "!grid-cols-1" : "!grid-cols-1 min-[760px]:!grid-cols-2"}`}>
      <section className="embedded-card !grid !min-h-0 !content-start !gap-2 !p-3">
        <div className="embedded-head !m-0">
          <p className="eyebrow">长势速览</p>
          <h3>长势等级面积</h3>
        </div>
        <div className="embedded-metric-grid !grid !grid-cols-2 !gap-2">
          <div>
            <span>良好长势</span>
            <strong>{area(growthInsights.strong)} 亩</strong>
          </div>
          <div>
            <span>弱势长势</span>
            <strong>{area(growthInsights.weak)} 亩</strong>
          </div>
          <div>
            <span>主导等级</span>
            <strong>{growthInsights.dominant?.label ?? "-"}</strong>
          </div>
          <div>
            <span>分类数量</span>
            <strong>{growth?.summary?.length ?? 0}</strong>
          </div>
        </div>
      </section>

      <section className="embedded-card !grid !min-h-0 !content-start !gap-2 !p-3">
        <div className="embedded-head !m-0">
          <p className="eyebrow">决策链路</p>
          <h3>合规与规则链路</h3>
        </div>
        {traceLines.length ? (
          <div className="embedded-trace-list">
            {traceLines.map((line, index) => (
              <div key={`${line}-${index}`} className="embedded-trace-item">
                <span>{index + 1}</span>
                <p>{line}</p>
              </div>
            ))}
          </div>
        ) : (
          <div className="embedded-empty">暂无合规或规则链路。</div>
        )}
      </section>
    </div>
  );
}

function LossPanel({ loss }: { loss: LossAssessment | null }) {
  if (!loss?.product_breakdown?.length) return null;
  return (
    <section className="preview-panel loss-panel">
      <div className="section-head">
        <div>
          <p className="eyebrow">多源灾损</p>
          <h2>多源遥感灾损评估</h2>
        </div>
        <div className="loss-headline">
          <span className="loss-ratio" style={{ color: loss.severity?.color }}>{ratio(loss.yield_loss_ratio)}</span>
          <span className="loss-sub">{loss.severity?.label ?? "-"} · 置信 {loss.confidence ?? "-"}</span>
        </div>
      </div>
      <p className="loss-drivers">
        <Crosshair size={13} />
        主导因子：{(loss.dominant_drivers ?? []).join("、") || "-"}　｜　数据源：{(loss.data_sources ?? []).join(" / ") || "-"}
      </p>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>遥感产品</th>
              <th>传感器</th>
              <th>分辨率</th>
              <th>损失分量</th>
              <th>权重</th>
              <th>贡献</th>
              <th>来源</th>
            </tr>
          </thead>
          <tbody>
            {loss.product_breakdown.map((p) => (
              <tr key={p.id}>
                <td>{p.name_cn}</td>
                <td>{p.sensor}</td>
                <td>
                  {p.native_res_m}m{p.scale === "regional" ? <span className="scale-tag">区域</span> : null}
                </td>
                <td>{ratio(p.decline_score)}</td>
                <td>{ratio(p.weight)}</td>
                <td>{ratio(p.contribution)}</td>
                <td>
                  <span className={`src-tag ${p.data_source}`}>{p.data_source === "gee" ? "GEE" : "模拟"}</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {(loss.caveats ?? []).length ? (
        <ul className="loss-caveats">
          {(loss.caveats ?? []).map((c, index) => (
            <li key={index}>{c}</li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}

function PayoutPanel({ payout }: { payout: Payout | null }) {
  if (!payout) return null;
  return (
    <section className="preview-panel payout-panel">
      <div className="section-head">
        <div>
          <p className="eyebrow">赔付测算</p>
          <h2>赔付测算</h2>
        </div>
        <strong className="payout-amount">{yuan(payout.payout_amount_yuan)}</strong>
      </div>
      <div className="payout-metrics">
        <div><span>保额 / 亩</span><strong>{yuan(payout.sum_insured_per_mu)}</strong></div>
        <div><span>承保面积</span><strong>{area(payout.insured_area_mu)} 亩</strong></div>
        <div><span>核定受灾面积</span><strong>{area(payout.affected_area_mu)} 亩</strong></div>
        <div><span>总保额</span><strong>{yuan(payout.total_sum_insured_yuan)}</strong></div>
        <div><span>减产率</span><strong>{ratio(payout.yield_loss_ratio)}</strong></div>
        <div><span>赔付比例</span><strong>{ratio(payout.payout_factor)}</strong></div>
        <div><span>生育期系数</span><strong>{payout.growth_stage || "-"} / {payout.growth_stage_factor ?? "-"}</strong></div>
      </div>
      {payout.tier_label ? <p className="payout-tier">{payout.tier_label}</p> : null}
      {payout.contract_number ? (
        <p className="payout-tier">合同依据：{payout.contract_number} / {payout.contract_version}（{(payout.contract_clause_refs ?? []).join("、")}）</p>
      ) : null}
      {payout.calculation_formula ? <p className="payout-tier">计算公式：{payout.calculation_formula}</p> : null}
    </section>
  );
}

function LegalBasisPanel({ basis }: { basis: LegalBasisRow[] }) {
  if (!basis.length) return null;
  return (
    <section className="preview-panel !grid !gap-3">
      <div className="section-head">
        <div>
          <p className="eyebrow">Legal basis trace</p>
          <h2>本案结论与合规依据</h2>
        </div>
        <span className="rounded border border-emerald-200 bg-emerald-50 px-2 py-1 text-xs font-semibold text-emerald-800">
          {basis.length} 个业务环节
        </span>
      </div>
      <div className="grid gap-2">
        {basis.map((row) => (
          <div
            key={row.stage}
            className="grid gap-2 border-b border-slate-200 py-3 last:border-b-0 md:grid-cols-[160px_minmax(0,1fr)]"
          >
            <strong className="text-sm text-slate-900">{row.stage}</strong>
            <div className="grid min-w-0 gap-2">
              <p className="m-0 text-sm leading-6 text-slate-700">{row.conclusion}</p>
              <div className="flex flex-wrap gap-1.5">
                {row.basis_ids.map((id) => (
                  <code key={id} className="rounded bg-slate-100 px-1.5 py-0.5 text-xs font-semibold text-teal-800">
                    {id}
                  </code>
                ))}
              </div>
              <small className="text-xs leading-5 text-slate-500">适用边界：{row.boundary}</small>
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

function MessageToast({ message, onClose }: { message: Message | null; onClose: () => void }) {
  if (!message) return null;
  return (
    <div className={`claims-toast ${message.tone}`} role="status">
      {message.tone === "error" ? <AlertTriangle size={15} /> : <CheckCircle2 size={15} />}
      <span>{message.text}</span>
      <button type="button" onClick={onClose} title="关闭提示">
        <X size={14} />
      </button>
    </div>
  );
}

function DockSection({
  title,
  status,
  ready,
  defaultOpen = false,
  children
}: {
  title: string;
  status: string;
  ready: boolean;
  defaultOpen?: boolean;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <section className={`dock-panel ${open ? "open" : ""}`}>
      <button
        type="button"
        className="dock-head"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <span className="dock-title">{title}</span>
        <span className={`dock-status ${ready ? "ready" : ""}`}>{status}</span>
        <ChevronDown size={16} className="dock-chev" />
      </button>
      {open ? <div className="dock-body">{children}</div> : null}
    </section>
  );
}

function ClaimsSkeleton() {
  return (
    <main className={`shell claims-shell ${claimsLayout.shell}`} aria-label="理赔驾驶舱加载中">
      <div className="skl-block" style={{ height: 48 }} />
      <div className="skl-grid-5">
        {Array.from({ length: 5 }).map((_, index) => (
          <div className="skl-block" key={index} style={{ height: 58 }} />
        ))}
      </div>
      <div className="skl-block" style={{ height: 56 }} />
      <div className="skl-workspace">
        <div className="skl-block" />
        <div className="skl-block" />
      </div>
    </main>
  );
}
