"use client";

import {
  AlertTriangle,
  CheckCircle2,
  ClipboardCheck,
  Download,
  FileArchive,
  FileText,
  FileJson,
  Loader2,
  MapPinned,
  RefreshCcw,
  Search,
  ShieldAlert,
  Trash2,
  UploadCloud
} from "lucide-react";
import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { AdminGate } from "../_components/AdminGate";
import { getApiBase } from "../_lib/api-base";

const API_BASE = getApiBase();

type Policy = {
  policy_id: string;
  policy_version_id?: string;
  holder_name?: string;
  crop_type?: string;
  address?: string;
  area_mu?: number | null;
  created_at?: string;
};

type ValidationIssue = {
  code: string;
  message: string;
};

type ParcelFeature = {
  proposed_feature_id?: string;
  feature_id?: string;
  source_business_id?: string | null;
  validation?: {
    status?: string;
    errors?: ValidationIssue[];
    warnings?: ValidationIssue[];
  };
};

type PreflightParcel = {
  proposed_parcel_id: string;
  parcel_id?: string;
  source_file: string;
  document_name?: string | null;
  feature_count: number;
  valid_feature_count: number;
  area_mu?: number | null;
  validation?: {
    status?: string;
    errors?: ValidationIssue[];
    warnings?: ValidationIssue[];
  };
  features?: ParcelFeature[];
};

type PreflightSummary = {
  parcel_file_count: number;
  feature_count: number;
  valid_feature_count: number;
  invalid_feature_count: number;
  repaired_feature_count: number;
  warning_feature_count: number;
  human_resolution_feature_count: number;
  duplicate_geometry_group_count: number;
  source_id_conflict_count: number;
  feature_area_sum_mu: number;
};

type DuplicateGroup = {
  geometry_sha256: string;
  feature_ids: string[];
  resolution_required: boolean;
};

type SourceIdConflict = {
  source_business_id: string;
  feature_ids: string[];
  resolution_required: boolean;
};

type PreflightManifest = {
  status: "success";
  preflight_id: string;
  schema_version: string;
  preflight_status: "blocked" | "needs_human_resolution" | "ready_for_mapping" | string;
  manifest_sha256: string;
  direct_import_allowed: false;
  requires_policy_version_mapping: boolean;
  requires_human_confirmation: boolean;
  source: {
    archive_filename: string;
    archive_sha256: string;
    archive_size_bytes: number;
    kml_file_count: number;
    ignored_files?: string[];
  };
  summary: PreflightSummary;
  duplicate_geometry_groups?: DuplicateGroup[];
  source_id_conflicts?: SourceIdConflict[];
  parcels: PreflightParcel[];
};

type ParcelMapping = {
  proposed_parcel_id: string;
  policy_id: string;
  policy_version_id: string;
  parcel_id: string;
  resolution_note: string;
};

type ConfirmationReceipt = {
  status: "confirmed";
  preflight_id: string;
  confirmation_batch_id: string;
  manifest_sha256: string;
  confirmed_at: string;
  confirmed_by: string;
  direct_import_performed: false;
  receipt_sha256: string;
  receipt_download_url: string;
  mappings: Array<{
    proposed_parcel_id: string;
    policy_id: string;
    policy_version_id: string;
    parcel_id: string;
    warning_codes?: string[];
  }>;
};

type ConfirmedParcel = {
  confirmation_id: string;
  preflight_id: string;
  policy_id: string;
  policy_version_id: string;
  parcel_id: string;
  parcel_boundary_sha256: string;
  confirmation_batch_id: string;
  receipt_sha256: string;
  receipt_download_url: string;
  confirmed_by?: string;
  confirmed_at?: string;
  parcel?: {
    source_file?: string;
    feature_count?: number;
    area_mu?: number;
  };
};

type ConfirmedParcelResponse = {
  total: number;
  direct_import_performed: false;
  items: ConfirmedParcel[];
};

type PageMessage = { tone: "ok" | "err" | "info"; text: string };

function area(v?: number | null) {
  if (typeof v !== "number" || Number.isNaN(v)) return "-";
  return `${new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(v)} 亩`;
}

function shortHash(value?: string | null) {
  return value ? `${value.slice(0, 12)}…` : "-";
}

function controlledUrl(path?: string | null) {
  if (!path) return "#";
  if (/^https?:\/\//i.test(path)) return path;
  return `${API_BASE}${path}`;
}

async function responsePayload(response: Response): Promise<Record<string, unknown>> {
  return (await response.json().catch(() => ({}))) as Record<string, unknown>;
}

function responseDetail(payload: Record<string, unknown>, fallback: string) {
  if (typeof payload.detail === "string") return payload.detail;
  if (payload.detail) return JSON.stringify(payload.detail);
  if (typeof payload.error_message === "string") return payload.error_message;
  return fallback;
}

function warningCodes(parcel: PreflightParcel) {
  const issues = [
    ...(parcel.validation?.warnings ?? []),
    ...(parcel.features ?? []).flatMap((feature) => feature.validation?.warnings ?? [])
  ];
  return [...new Set(issues.map((issue) => issue.code))];
}

function statusText(status: string) {
  if (status === "ready_for_mapping") return "可进入映射";
  if (status === "needs_human_resolution") return "需人工消歧";
  if (status === "blocked") return "存在阻断错误";
  return status;
}

export default function PoliciesPage() {
  return (
    <AdminGate>
      <PoliciesWorkspace />
    </AdminGate>
  );
}

function PoliciesWorkspace() {
  const [items, setItems] = useState<Policy[]>([]);
  const [loading, setLoading] = useState(false);
  const [msg, setMsg] = useState<PageMessage | null>(null);
  const [form, setForm] = useState({ policy_id: "", holder_name: "", crop_type: "rice", address: "" });
  const [file, setFile] = useState<File | null>(null);
  const [contractFile, setContractFile] = useState<File | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [deletingPolicyId, setDeletingPolicyId] = useState<string | null>(null);

  const [preflightFile, setPreflightFile] = useState<File | null>(null);
  const [preflight, setPreflight] = useState<PreflightManifest | null>(null);
  const [preflightBusy, setPreflightBusy] = useState(false);
  const [preflightMsg, setPreflightMsg] = useState<PageMessage | null>(null);
  const [defaultPolicyId, setDefaultPolicyId] = useState("");
  const [mappingJson, setMappingJson] = useState("[]");
  const [confirmationChecked, setConfirmationChecked] = useState(false);
  const [confirmationComment, setConfirmationComment] = useState("");
  const [idempotencyKey, setIdempotencyKey] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [receipt, setReceipt] = useState<ConfirmationReceipt | null>(null);

  const [queryPolicyId, setQueryPolicyId] = useState("");
  const [queryVersionId, setQueryVersionId] = useState("");
  const [confirmed, setConfirmed] = useState<ConfirmedParcelResponse | null>(null);
  const [querying, setQuerying] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch(`${API_BASE}/api/v1/policies`, { credentials: "include" });
      const data = await responsePayload(res);
      if (!res.ok) throw new Error(responseDetail(data, `保单列表加载失败: ${res.status}`));
      const policies = Array.isArray(data.items) ? (data.items as Policy[]) : [];
      setItems(policies);
      const first = policies[0];
      setDefaultPolicyId((current) =>
        policies.some((policy) => policy.policy_id === current) ? current : first?.policy_id || ""
      );
      setQueryPolicyId((current) =>
        policies.some((policy) => policy.policy_id === current) ? current : first?.policy_id || ""
      );
      setQueryVersionId((current) => {
        const selected = policies.find((policy) =>
          (policy.policy_version_id || `${policy.policy_id}:v1`) === current
        );
        return selected
          ? current
          : first?.policy_version_id || (first ? `${first.policy_id}:v1` : "");
      });
    } catch (error) {
      setMsg({ tone: "err", text: error instanceof Error ? error.message : "保单列表加载失败。" });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const preflightIssues = useMemo(() => {
    if (!preflight) return [] as Array<{ source: string; issue: ValidationIssue }>;
    return preflight.parcels.flatMap((parcel) => [
      ...(parcel.validation?.errors ?? []).map((issue) => ({ source: parcel.source_file, issue })),
      ...(parcel.validation?.warnings ?? []).map((issue) => ({ source: parcel.source_file, issue })),
      ...(parcel.features ?? []).flatMap((feature) => [
        ...(feature.validation?.errors ?? []).map((issue) => ({
          source: `${parcel.source_file} / ${feature.proposed_feature_id || feature.feature_id || "feature"}`,
          issue
        })),
        ...(feature.validation?.warnings ?? []).map((issue) => ({
          source: `${parcel.source_file} / ${feature.proposed_feature_id || feature.feature_id || "feature"}`,
          issue
        }))
      ])
    ]);
  }, [preflight]);

  async function register(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (!form.policy_id.trim()) return setMsg({ tone: "err", text: "请填写保单号。" });
    if (!file) return setMsg({ tone: "err", text: "请上传承保地块边界文件。" });
    if (!contractFile) return setMsg({ tone: "err", text: "请上传已签订的保险合同 PDF 或 DOCX。" });
    setSubmitting(true);
    setMsg(null);
    try {
      const fd = new FormData();
      fd.append("policy_id", form.policy_id.trim());
      fd.append("holder_name", form.holder_name);
      fd.append("crop_type", form.crop_type);
      fd.append("address", form.address);
      fd.append("boundary_file", file);
      fd.append("contract_file", contractFile);
      const res = await fetch(`${API_BASE}/api/v1/policies/upload`, {
        method: "POST",
        credentials: "include",
        body: fd
      });
      const data = await responsePayload(res);
      if (!res.ok || data.status !== "success") throw new Error(responseDetail(data, "登记失败"));
      setMsg({ tone: "ok", text: `保单 ${String(data.policy_id)} 已登记，承保面积约 ${area(data.area_mu as number | null)}。` });
      setForm({ policy_id: "", holder_name: "", crop_type: "rice", address: "" });
      setFile(null);
      setContractFile(null);
      await load();
    } catch (error) {
      setMsg({ tone: "err", text: error instanceof Error ? error.message : "登记失败" });
    } finally {
      setSubmitting(false);
    }
  }

  async function deletePolicy(policy: Policy) {
    if (!window.confirm(
      `确认删除保单 ${policy.policy_id}？\n\n删除后将从在册保单中隐藏，但边界、地块确认和审计记录仍会保留。若存在未删除案件，系统会拒绝本次操作。`
    )) return;
    setDeletingPolicyId(policy.policy_id);
    setMsg(null);
    try {
      const response = await fetch(
        `${API_BASE}/api/v1/policies/${encodeURIComponent(policy.policy_id)}`,
        { method: "DELETE", credentials: "include" }
      );
      const payload = await responsePayload(response);
      if (!response.ok) throw new Error(responseDetail(payload, `删除失败: ${response.status}`));
      setMsg({ tone: "ok", text: `保单 ${policy.policy_id} 已从在册列表移除，审计证据仍保留。` });
      await load();
    } catch (error) {
      setMsg({ tone: "err", text: error instanceof Error ? error.message : "删除保单失败" });
    } finally {
      setDeletingPolicyId(null);
    }
  }

  function createMappingTemplate(manifest: PreflightManifest, policyId = defaultPolicyId) {
    const policy = items.find((item) => item.policy_id === policyId);
    const mappings: ParcelMapping[] = manifest.parcels.map((parcel, index) => ({
      proposed_parcel_id: parcel.proposed_parcel_id,
      policy_id: policy?.policy_id ?? "",
      policy_version_id: policy?.policy_version_id ?? (policy ? `${policy.policy_id}:v1` : ""),
      parcel_id: `PARCEL-${String(index + 1).padStart(4, "0")}`,
      resolution_note: ""
    }));
    setMappingJson(JSON.stringify(mappings, null, 2));
  }

  async function runPreflight(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!preflightFile) {
      setPreflightMsg({ tone: "err", text: "请选择包含多个 KML 的 ZIP。" });
      return;
    }
    if (!preflightFile.name.toLowerCase().endsWith(".zip")) {
      setPreflightMsg({ tone: "err", text: "批量地块预检仅接受 .zip 文件。" });
      return;
    }
    setPreflightBusy(true);
    setPreflightMsg(null);
    setReceipt(null);
    setConfirmed(null);
    try {
      const body = new FormData();
      body.append("archive_file", preflightFile);
      const response = await fetch(`${API_BASE}/api/v1/policies/parcels/preflight`, {
        method: "POST",
        credentials: "include",
        body
      });
      const payload = await responsePayload(response);
      if (!response.ok) throw new Error(responseDetail(payload, `预检失败: ${response.status}`));
      const manifest = payload as unknown as PreflightManifest;
      if (manifest.status !== "success" || manifest.direct_import_allowed !== false) {
        throw new Error("预检响应缺少禁止直接导入标记，已停止后续操作。");
      }
      setPreflight(manifest);
      createMappingTemplate(manifest, defaultPolicyId);
      setConfirmationChecked(false);
      setConfirmationComment("");
      setIdempotencyKey(`parcel-confirm:${manifest.preflight_id}:${Date.now()}`.slice(0, 128));
      setPreflightMsg({
        tone: manifest.preflight_status === "blocked" ? "err" : "ok",
        text: manifest.preflight_status === "blocked"
          ? "只读预检完成，但存在阻断错误；没有导入任何地块。"
          : "只读预检完成；尚未导入地块，请继续完成全量保单版本映射和人工确认。"
      });
    } catch (error) {
      setPreflight(null);
      setMappingJson("[]");
      setPreflightMsg({ tone: "err", text: error instanceof Error ? error.message : "预检失败" });
    } finally {
      setPreflightBusy(false);
    }
  }

  function validateMappings(): ParcelMapping[] {
    if (!preflight) throw new Error("请先完成 ZIP 预检。");
    if (preflight.preflight_status === "blocked") throw new Error("预检存在阻断错误，不能确认映射。");
    if (!confirmationChecked) throw new Error("请勾选人工复核确认声明。");

    let parsed: unknown;
    try {
      parsed = JSON.parse(mappingJson);
    } catch (error) {
      throw new Error(`映射 JSON 无法解析: ${error instanceof Error ? error.message : "格式错误"}`);
    }
    if (!Array.isArray(parsed)) throw new Error("映射 JSON 顶层必须是数组。");
    const mappings = parsed as ParcelMapping[];
    const expectedIds = new Set(preflight.parcels.map((parcel) => parcel.proposed_parcel_id));
    const mappedIds = mappings.map((mapping) => String(mapping.proposed_parcel_id || "").trim());
    if (mappings.length !== expectedIds.size || new Set(mappedIds).size !== expectedIds.size) {
      throw new Error(`必须且只能映射全部 ${expectedIds.size} 个 proposed_parcel_id，不能缺失或重复。`);
    }
    const missing = [...expectedIds].filter((id) => !mappedIds.includes(id));
    const extra = mappedIds.filter((id) => !expectedIds.has(id));
    if (missing.length || extra.length) {
      throw new Error(`映射覆盖不完整：缺失 ${missing.join(", ") || "无"}；多余 ${extra.join(", ") || "无"}。`);
    }

    const policyById = new Map(items.map((policy) => [policy.policy_id, policy]));
    const finalKeys = new Set<string>();
    for (const mapping of mappings) {
      const fields = ["proposed_parcel_id", "policy_id", "policy_version_id", "parcel_id"] as const;
      for (const field of fields) {
        if (typeof mapping[field] !== "string" || !mapping[field].trim()) {
          throw new Error(`${mapping.proposed_parcel_id || "未知地块"} 缺少 ${field}。`);
        }
      }
      const policy = policyById.get(mapping.policy_id);
      if (!policy) throw new Error(`保单 ${mapping.policy_id} 尚未登记，不能用于映射。`);
      const registeredVersion = policy.policy_version_id || `${policy.policy_id}:v1`;
      if (mapping.policy_version_id !== registeredVersion) {
        throw new Error(`保单 ${mapping.policy_id} 当前版本为 ${registeredVersion}，映射中却是 ${mapping.policy_version_id}。`);
      }
      if (!/^[A-Za-z0-9][A-Za-z0-9._:-]*$/.test(mapping.parcel_id)) {
        throw new Error(`正式地块编号 ${mapping.parcel_id} 格式无效。`);
      }
      const finalKey = `${mapping.policy_id}\u0000${mapping.policy_version_id}\u0000${mapping.parcel_id}`;
      if (finalKeys.has(finalKey)) throw new Error(`正式地块编号重复: ${mapping.parcel_id}。`);
      finalKeys.add(finalKey);
      const parcel = preflight.parcels.find((item) => item.proposed_parcel_id === mapping.proposed_parcel_id);
      if (parcel && warningCodes(parcel).length && !String(mapping.resolution_note || "").trim()) {
        throw new Error(`地块 ${mapping.proposed_parcel_id} 有预检警告，必须在 resolution_note 中记录处置结论。`);
      }
    }
    return mappings.map((mapping) => ({
      ...mapping,
      proposed_parcel_id: mapping.proposed_parcel_id.trim(),
      policy_id: mapping.policy_id.trim(),
      policy_version_id: mapping.policy_version_id.trim(),
      parcel_id: mapping.parcel_id.trim(),
      resolution_note: String(mapping.resolution_note || "").trim()
    }));
  }

  async function confirmMappings() {
    let mappings: ParcelMapping[];
    try {
      mappings = validateMappings();
    } catch (error) {
      setPreflightMsg({ tone: "err", text: error instanceof Error ? error.message : "映射校验失败" });
      return;
    }
    if (!preflight) return;
    setConfirming(true);
    setPreflightMsg(null);
    try {
      const response = await fetch(
        `${API_BASE}/api/v1/policies/parcels/preflights/${encodeURIComponent(preflight.preflight_id)}/confirm`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "include",
          body: JSON.stringify({
            manifest_sha256: preflight.manifest_sha256,
            mappings,
            confirmation_statement: "I_CONFIRM_REVIEWED_PARCEL_MAPPINGS",
            comment: confirmationComment.trim(),
            idempotency_key: idempotencyKey.trim() || undefined
          })
        }
      );
      const payload = await responsePayload(response);
      if (!response.ok) throw new Error(responseDetail(payload, `映射确认失败: ${response.status}`));
      const result = payload as unknown as ConfirmationReceipt;
      setReceipt(result);
      setPreflightMsg({
        tone: "ok",
        text: `已确认 ${result.mappings.length} 个地块映射并生成不可变回执；此步骤没有覆盖或新建保单边界。`
      });
      const first = mappings[0];
      if (first) {
        setQueryPolicyId(first.policy_id);
        setQueryVersionId(first.policy_version_id);
      }
    } catch (error) {
      setPreflightMsg({ tone: "err", text: error instanceof Error ? error.message : "映射确认失败" });
    } finally {
      setConfirming(false);
    }
  }

  function selectQueryPolicy(policyId: string) {
    const policy = items.find((item) => item.policy_id === policyId);
    setQueryPolicyId(policyId);
    setQueryVersionId(policy?.policy_version_id || (policyId ? `${policyId}:v1` : ""));
    setConfirmed(null);
  }

  async function queryConfirmedParcels() {
    if (!queryPolicyId || !queryVersionId) {
      setPreflightMsg({ tone: "err", text: "请选择要查询的在册保单及版本。" });
      return;
    }
    setQuerying(true);
    setPreflightMsg(null);
    try {
      const path = `/api/v1/policies/${encodeURIComponent(queryPolicyId)}/parcels?policy_version_id=${encodeURIComponent(queryVersionId)}`;
      const response = await fetch(`${API_BASE}${path}`, { credentials: "include" });
      const payload = await responsePayload(response);
      if (!response.ok) throw new Error(responseDetail(payload, `查询失败: ${response.status}`));
      const result = payload as unknown as ConfirmedParcelResponse;
      setConfirmed(result);
      setPreflightMsg({ tone: "info", text: `查询到 ${result.total} 个已确认地块；结果来自不可变映射记录。` });
    } catch (error) {
      setConfirmed(null);
      setPreflightMsg({ tone: "err", text: error instanceof Error ? error.message : "查询失败" });
    } finally {
      setQuerying(false);
    }
  }

  return (
      <main className="shell policies-shell">
        <header className="topbar">
          <div className="brand-block">
            <div className="brand-mark"><MapPinned size={22} /></div>
            <div>
              <p className="eyebrow">Agrisky AI · Policy Boundary</p>
              <h1>保单与地块底册</h1>
            </div>
          </div>
          <div className="top-status">
            <span className="api-pill">承保边界在册 · 批量 KML 独立预检</span>
            <button className="secondary icon-action" type="button" onClick={load} title="刷新"><RefreshCcw size={16} /></button>
          </div>
        </header>

        <section className="claims-workspace policy-register-workspace">
          <aside className="control-panel">
            <div className="panel-heading">
              <div>
                <p className="eyebrow">Register</p>
                <h2>登记保单、合同与承保边界</h2>
              </div>
            </div>
            {msg ? <div className={`message-box ${msg.tone === "ok" ? "success" : msg.tone === "err" ? "error" : "info"}`}><span>{msg.text}</span></div> : null}
            <form className="form-stack" onSubmit={register}>
              <label className="field"><span>保单号</span>
                <input value={form.policy_id} onChange={(event) => setForm({ ...form, policy_id: event.target.value })} placeholder="POL-2026-001" />
              </label>
              <label className="field"><span>投保人/合作社</span>
                <input value={form.holder_name} onChange={(event) => setForm({ ...form, holder_name: event.target.value })} />
              </label>
              <div className="field-grid">
                <label className="field"><span>作物</span>
                  <select value={form.crop_type} onChange={(event) => setForm({ ...form, crop_type: event.target.value })}>
                    <option value="rice">水稻</option>
                    <option value="corn">玉米</option>
                    <option value="wheat">小麦</option>
                  </select>
                </label>
                <label className="field"><span>地址/区域</span>
                  <input value={form.address} onChange={(event) => setForm({ ...form, address: event.target.value })} />
                </label>
              </div>
              <label className="file-drop">
                <input type="file" accept=".shp,.geojson,.json,.gpkg,.kml,.zip"
                  onChange={(event) => setFile(event.target.files?.[0] ?? null)} />
                <UploadCloud size={20} />
                <span>{file ? file.name : "单一边界 SHP / GeoJSON / GPKG / KML"}</span>
              </label>
              <div className="contract-upload-row">
                <label className="file-drop">
                  <input type="file" accept=".pdf,.docx"
                    onChange={(event) => setContractFile(event.target.files?.[0] ?? null)} />
                  <FileText size={20} />
                  <span>{contractFile ? contractFile.name : "上传已签订保险合同 PDF / DOCX"}</span>
                </label>
                <a
                  className="secondary contract-template-link"
                  href={`${API_BASE}/api/v1/contracts/template/download?policy_id=${encodeURIComponent(form.policy_id || "POL-DEMO-001")}&holder_name=${encodeURIComponent(form.holder_name || "示范种植合作社")}&crop_type=${encodeURIComponent(form.crop_type)}&address=${encodeURIComponent(form.address || "示范承保地块")}&insurance_year=2025`}
                  title="下载当前作物的示范保险合同模板"
                >
                  <Download size={15} />
                  示范合同模板
                </a>
              </div>
              <small className="workflow-warning">ZIP 在这里仅允许包含一个 Shapefile 数据集；含多个 KML 的 ZIP 必须走下方独立预检流程。</small>
              <small className="workflow-warning contract-confirmation">提交即确认合同解析字段与登记信息一致；合同原件、结构化条款和 SHA-256 将按保单版本冻结，案件仅引用该版本。</small>
              <button className="primary full-width" type="submit" disabled={submitting}>
                {submitting ? <Loader2 className="spin" size={17} /> : <MapPinned size={17} />}
                校验合同并登记保单
              </button>
            </form>
          </aside>

          <section className="claim-main">
            <section className="table-panel">
              <div className="section-head">
                <div>
                  <p className="eyebrow">Policies</p>
                  <h2>在册保单（{items.length}）</h2>
                </div>
              </div>
              {loading ? (
                <div className="empty-table"><Loader2 className="spin" size={16} /> 加载中…</div>
              ) : items.length === 0 ? (
                <div className="empty-table">暂无在册保单，请先登记。批量地块映射不能替代保单登记。</div>
              ) : (
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr><th>保单号</th><th>版本</th><th>投保人</th><th>作物</th><th>承保面积</th><th>地址</th><th>操作</th></tr>
                    </thead>
                    <tbody>
                      {items.map((policy) => (
                        <tr key={policy.policy_id}>
                          <td>{policy.policy_id}</td>
                          <td className="mono-cell">{policy.policy_version_id || `${policy.policy_id}:v1`}</td>
                          <td>{policy.holder_name || "-"}</td>
                          <td>{policy.crop_type || "-"}</td>
                          <td>{area(policy.area_mu)}</td>
                          <td>{policy.address || "-"}</td>
                          <td>
                            <a
                              className="row-delete"
                              href={`${API_BASE}/api/v1/policies/${encodeURIComponent(policy.policy_id)}/contract/download`}
                              title="下载登记时冻结的保险合同原件"
                              aria-label={`下载保单 ${policy.policy_id} 的保险合同原件`}
                            >
                              <Download size={14} />
                            </a>
                            <button
                              className="row-delete"
                              type="button"
                              onClick={() => deletePolicy(policy)}
                              disabled={deletingPolicyId === policy.policy_id}
                              title="删除保单"
                              aria-label={`删除保单 ${policy.policy_id}`}
                            >
                              {deletingPolicyId === policy.policy_id
                                ? <Loader2 className="spin" size={14} />
                                : <Trash2 size={14} />}
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>
          </section>
        </section>

        <section className="table-panel parcel-preflight-panel">
          <div className="section-head">
            <div>
              <p className="eyebrow">Multi-KML preflight</p>
              <h2>批量 KML 地块：只读预检 → 映射确认</h2>
            </div>
            <span className="preflight-no-import"><ShieldAlert size={15} /> 永不直接导入 ZIP</span>
          </div>

          <div className="preflight-rule-grid">
            <div><strong>1. 保单先在册</strong><span>映射只能引用上方已经登记的 policy_id 和当前 policy_version_id。</span></div>
            <div><strong>2. 必须全量映射</strong><span>清单中每一个 proposed_parcel_id 都要且只能出现一次。</span></div>
            <div><strong>3. 几何必须一致</strong><span>映射到同一保单版本的地块并集必须与该版本在册边界完全一致。</span></div>
            <div><strong>4. 只保存确认关系</strong><span>确认不会合并 ZIP、不会覆盖保单边界；边界变化应新建保单版本。</span></div>
          </div>

          {preflightMsg ? (
            <div className={`message-box ${preflightMsg.tone === "ok" ? "success" : preflightMsg.tone === "err" ? "error" : "info"}`}>
              {preflightMsg.tone === "err" ? <AlertTriangle size={15} /> : <CheckCircle2 size={15} />}
              <span>{preflightMsg.text}</span>
            </div>
          ) : null}

          <form className="preflight-upload-row" onSubmit={runPreflight}>
            <label className="file-drop">
              <input type="file" accept=".zip,application/zip" onChange={(event) => setPreflightFile(event.target.files?.[0] ?? null)} />
              <FileArchive size={20} />
              <span>{preflightFile ? preflightFile.name : "选择包含多个 KML 的 ZIP（最大 50 MB）"}</span>
            </label>
            <button className="primary" type="submit" disabled={preflightBusy}>
              {preflightBusy ? <Loader2 className="spin" size={17} /> : <Search size={17} />}
              执行只读预检
            </button>
          </form>

          {preflight ? (
            <>
              <div className="preflight-identity">
                <span>预检号 <strong>{preflight.preflight_id}</strong></span>
                <span>状态 <strong className={`preflight-status ${preflight.preflight_status}`}>{statusText(preflight.preflight_status)}</strong></span>
                <span>清单 SHA-256 <strong title={preflight.manifest_sha256}>{shortHash(preflight.manifest_sha256)}</strong></span>
                <span>源 ZIP SHA-256 <strong title={preflight.source.archive_sha256}>{shortHash(preflight.source.archive_sha256)}</strong></span>
              </div>

              <div className="preflight-stats">
                <div><strong>{preflight.summary.parcel_file_count}</strong><span>KML / proposed parcels</span></div>
                <div><strong>{preflight.summary.feature_count}</strong><span>总要素</span></div>
                <div><strong>{preflight.summary.valid_feature_count}</strong><span>有效要素</span></div>
                <div><strong>{preflight.summary.invalid_feature_count}</strong><span>无效要素</span></div>
                <div><strong>{preflight.summary.warning_feature_count}</strong><span>警告要素</span></div>
                <div><strong>{preflight.summary.duplicate_geometry_group_count}</strong><span>重复几何组</span></div>
                <div><strong>{preflight.summary.source_id_conflict_count}</strong><span>源编号冲突</span></div>
                <div><strong>{area(preflight.summary.feature_area_sum_mu)}</strong><span>要素面积合计</span></div>
              </div>

              {(preflightIssues.length > 0 || (preflight.duplicate_geometry_groups?.length ?? 0) > 0) ? (
                <details className="preflight-details" open={preflight.preflight_status !== "ready_for_mapping"}>
                  <summary>预检问题与重复组（{preflightIssues.length} 条问题）</summary>
                  <div className="preflight-issue-list">
                    {preflightIssues.map(({ source, issue }, index) => (
                      <div key={`${source}-${issue.code}-${index}`}>
                        <code>{issue.code}</code>
                        <span><strong>{source}</strong> · {issue.message}</span>
                      </div>
                    ))}
                    {(preflight.duplicate_geometry_groups ?? []).map((group) => (
                      <div key={group.geometry_sha256}>
                        <code>DUPLICATE_GROUP</code>
                        <span>几何 {shortHash(group.geometry_sha256)} · {group.feature_ids.join(", ")}</span>
                      </div>
                    ))}
                    {(preflight.source_id_conflicts ?? []).map((group) => (
                      <div key={group.source_business_id}>
                        <code>SOURCE_ID_CONFLICT</code>
                        <span>源编号 {group.source_business_id} · {group.feature_ids.join(", ")}</span>
                      </div>
                    ))}
                  </div>
                </details>
              ) : null}

              <details className="preflight-details">
                <summary>逐文件预检清单（{preflight.parcels.length}）</summary>
                <div className="table-wrap parcel-manifest-table">
                  <table>
                    <thead><tr><th>源 KML</th><th>proposed parcel</th><th>要素</th><th>有效</th><th>面积</th><th>状态/警告</th></tr></thead>
                    <tbody>
                      {preflight.parcels.map((parcel) => (
                        <tr key={parcel.proposed_parcel_id}>
                          <td>{parcel.source_file}</td>
                          <td className="mono-cell" title={parcel.proposed_parcel_id}>{parcel.proposed_parcel_id}</td>
                          <td>{parcel.feature_count}</td>
                          <td>{parcel.valid_feature_count}</td>
                          <td>{area(parcel.area_mu)}</td>
                          <td>{parcel.validation?.status || "-"}{warningCodes(parcel).length ? ` · ${warningCodes(parcel).join(", ")}` : ""}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </details>

              <div className="mapping-query-grid">
                <section className="mapping-editor-card">
                  <div className="section-head">
                    <div>
                      <p className="eyebrow">Exact mapping</p>
                      <h3>全量映射 JSON</h3>
                    </div>
                    <FileJson size={19} />
                  </div>
                  <div className="mapping-template-row">
                    <label className="field"><span>模板默认保单</span>
                      <select value={defaultPolicyId} onChange={(event) => setDefaultPolicyId(event.target.value)}>
                        <option value="">请选择已登记保单</option>
                        {items.map((policy) => <option key={policy.policy_id} value={policy.policy_id}>{policy.policy_id} · {policy.policy_version_id}</option>)}
                      </select>
                    </label>
                    <button className="secondary" type="button" onClick={() => createMappingTemplate(preflight)} disabled={!defaultPolicyId}>重建全量模板</button>
                  </div>
                  <label className="json-editor-label">
                    <span>映射数组（适合复制到表格/脚本中批量编辑后贴回）</span>
                    <textarea value={mappingJson} onChange={(event) => setMappingJson(event.target.value)} spellCheck={false} />
                  </label>
                  <small className="workflow-warning">每条警告地块必须填写 resolution_note。映射到多个保单时，请分别保证每个保单版本收到的几何集合与其在册边界一致。</small>
                  <div className="field-grid">
                    <label className="field"><span>确认备注</span>
                      <input value={confirmationComment} onChange={(event) => setConfirmationComment(event.target.value)} placeholder="记录映射依据、复核人或工单号" />
                    </label>
                    <label className="field"><span>幂等键</span>
                      <input value={idempotencyKey} onChange={(event) => setIdempotencyKey(event.target.value)} />
                    </label>
                  </div>
                  <label className="confirmation-check">
                    <input type="checkbox" checked={confirmationChecked} onChange={(event) => setConfirmationChecked(event.target.checked)} />
                    <span>我已逐项复核全部地块、保单版本、正式 parcel_id、警告处置和几何集合一致性。</span>
                  </label>
                  <button className="primary full-width" type="button" onClick={confirmMappings} disabled={confirming || preflight.preflight_status === "blocked"}>
                    {confirming ? <Loader2 className="spin" size={17} /> : <ClipboardCheck size={17} />}
                    确认完整映射并生成回执
                  </button>
                  {receipt ? (
                    <div className="receipt-card">
                      <CheckCircle2 size={20} />
                      <div>
                        <strong>确认批次 {receipt.confirmation_batch_id}</strong>
                        <span>{receipt.mappings.length} 个地块 · 回执 {shortHash(receipt.receipt_sha256)} · 未直接导入</span>
                      </div>
                      <a className="secondary" href={controlledUrl(receipt.receipt_download_url)}>
                        <Download size={15} /> 下载回执 JSON
                      </a>
                    </div>
                  ) : null}
                </section>

                <section className="mapping-editor-card">
                  <div className="section-head">
                    <div>
                      <p className="eyebrow">Confirmed parcels</p>
                      <h3>查询已确认地块</h3>
                    </div>
                    <Search size={19} />
                  </div>
                  <label className="field"><span>在册保单</span>
                    <select value={queryPolicyId} onChange={(event) => selectQueryPolicy(event.target.value)}>
                      <option value="">请选择</option>
                      {items.map((policy) => <option key={policy.policy_id} value={policy.policy_id}>{policy.policy_id}</option>)}
                    </select>
                  </label>
                  <label className="field"><span>精确保单版本</span>
                    <input value={queryVersionId} onChange={(event) => setQueryVersionId(event.target.value)} placeholder="POL-2026-001:v1" />
                  </label>
                  <button className="secondary full-width" type="button" onClick={queryConfirmedParcels} disabled={querying}>
                    {querying ? <Loader2 className="spin" size={16} /> : <Search size={16} />}
                    查询不可变确认记录
                  </button>
                  {confirmed ? (
                    confirmed.total ? (
                      <div className="confirmed-parcel-list">
                        {confirmed.items.map((parcel) => (
                          <div key={parcel.confirmation_id}>
                            <div>
                              <strong>{parcel.parcel_id}</strong>
                              <span>{parcel.parcel?.source_file || parcel.preflight_id}</span>
                              <small>{parcel.policy_version_id} · {parcel.parcel?.feature_count ?? "-"} 要素 · {area(parcel.parcel?.area_mu)}</small>
                            </div>
                            <a className="secondary" href={controlledUrl(parcel.receipt_download_url)} title={parcel.receipt_sha256}>
                              <Download size={14} /> 回执
                            </a>
                          </div>
                        ))}
                      </div>
                    ) : <div className="empty-table">该保单版本尚无已确认地块；这不代表保单边界不存在。</div>
                  ) : <div className="empty-table">选择保单版本后查询。确认记录与保单边界分开保存。</div>}
                </section>
              </div>
            </>
          ) : (
            <div className="preflight-empty">
              <FileArchive size={28} />
              <div><strong>尚未预检批量 KML</strong><span>上传只会解析、校验和生成清单，不会写入保单边界或理赔案件。</span></div>
            </div>
          )}
        </section>
      </main>
  );
}
