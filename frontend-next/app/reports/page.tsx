"use client";

import {
  AlertTriangle,
  Archive,
  CheckCircle2,
  ClipboardCheck,
  Download,
  ExternalLink,
  FileCheck2,
  FileSpreadsheet,
  FileText,
  Loader2,
  RefreshCcw,
  ShieldCheck
} from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { AdminGate } from "../_components/AdminGate";
import { getApiBase } from "../_lib/api-base";

const API_BASE = getApiBase();

type CaseItem = {
  claim_id: string;
  policy_id: string;
  state: string;
  disaster_type: string;
  crop_type: string;
  plot_id: string;
  loss_date: string;
  reported_at: string;
  risk_level: string | null;
  payout_amount_yuan: number | null;
};

type CaseList = {
  total: number;
  items: CaseItem[];
};

type ReportArtifact = {
  kind: string;
  filename: string;
  download_url?: string;
  sha256?: string;
  size_bytes?: number;
  media_type?: string;
};

type ReportResult = {
  generation_id?: string;
  template_version?: string;
  snapshot_sha256?: string;
  report_status?: string;
  artifacts?: ReportArtifact[];
};

type HumanReview = {
  action?: string;
  status?: string;
  review_id?: string;
  decision?: string;
  reviewer?: string;
  reviewed_at?: string;
  comment?: string;
  receipt_sha256?: string;
  receipt_download_url?: string;
  receipt?: {
    actor?: string;
    reviewed_at_utc?: string;
    comment?: string;
    decision?: string;
  };
};

type FullCase = {
  claim_id: string;
  state: string;
  case: {
    policy_id: string;
    disaster_type: string;
    loss_date: string;
    crop_type: string;
    plot_id: string;
  };
  results: {
    report?: ReportResult | null;
    human_review?: HumanReview | null;
    rule?: { risk_level?: string; review_required?: boolean } | null;
    payout?: { payout_amount_yuan?: number } | null;
  };
};

type AuditLogItem = {
  created_at: string;
  action: string;
  tool_name?: string | null;
  notes?: string | null;
};

const STATE_LABELS: Record<string, string> = {
  INIT: "新建",
  MATERIAL_CHECK: "材料校验",
  PREPROCESS_READY: "待初筛",
  SCREENING_DONE: "已初筛",
  NDVI_DONE: "已评估",
  COMPLIANCE_DONE: "已合规",
  RULE_DONE: "已评级",
  REPORT_DRAFTED: "待审核",
  HUMAN_REVIEW: "人工审核",
  ARCHIVED: "已归档"
};

const DISASTER_LABELS: Record<string, string> = {
  flood: "洪涝",
  drought: "干旱",
  hail: "冰雹",
  typhoon: "台风",
  pest: "病虫害",
  frost: "霜冻",
  other: "其他"
};

const ARTIFACT_LABELS: Record<string, string> = {
  claim_report: "理赔报告 DOCX",
  growth_report: "长势报告 DOCX",
  assessment_excel: "评估表 Excel",
  bundle: "报告草稿包 ZIP",
  readme: "说明文件",
  historical_ndvi_report: "历史 NDVI 报告",
  historical_ndvi_result: "历史 NDVI 结果",
  parcel_growth_result: "分地块长势结果"
};

const AUDIT_ACTION_LABELS: Record<string, string> = {
  create_claim: "建案",
  validate_materials: "材料校验",
  run_satellite_screening: "遥感初筛",
  run_growth_analysis: "长势分析",
  run_compliance_calc: "合规核验",
  run_payout_estimate: "赔付测算",
  run_rule_engine: "规则评级",
  generate_report: "生成报告",
  human_review_approved: "人工审核通过",
  human_review_rejected: "人工审核退回",
  download_report_artifact: "报告下载",
  download_review_receipt: "回执下载"
};

function yuan(value?: number | null) {
  if (typeof value !== "number" || Number.isNaN(value)) return "-";
  return `${new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 0 }).format(value)} 元`;
}

function shortTime(value?: string | null) {
  if (!value) return "-";
  return value.replace("T", " ").slice(0, 19);
}

function shortHash(value?: string | null) {
  if (!value) return "-";
  return value.slice(0, 12);
}

function fileSize(value?: number) {
  if (typeof value !== "number" || Number.isNaN(value)) return "-";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function isReportCase(item: CaseItem) {
  return item.state === "REPORT_DRAFTED" || item.state === "HUMAN_REVIEW" || item.state === "ARCHIVED";
}

function reviewDecision(review?: HumanReview | null) {
  return review?.decision ?? review?.action ?? review?.receipt?.decision ?? review?.status ?? "";
}

function reviewStatusLabel(review?: HumanReview | null) {
  const decision = reviewDecision(review);
  if (decision === "approved") return "已通过";
  if (decision === "rejected") return "已退回";
  return "未审核";
}

function reviewActor(review?: HumanReview | null) {
  return review?.reviewer ?? review?.receipt?.actor ?? "-";
}

function reviewTime(review?: HumanReview | null) {
  return review?.reviewed_at ?? review?.receipt?.reviewed_at_utc ?? null;
}

function reviewComment(review?: HumanReview | null) {
  return review?.comment || review?.receipt?.comment || "";
}

export default function ReportsPage() {
  return (
    <AdminGate>
      <ReportsWorkspace />
    </AdminGate>
  );
}

function ReportsWorkspace() {
  const [cases, setCases] = useState<CaseItem[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<FullCase | null>(null);
  const [auditLogs, setAuditLogs] = useState<AuditLogItem[]>([]);
  const [loadingCases, setLoadingCases] = useState(false);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [error, setError] = useState("");

  const loadCases = useCallback(async () => {
    setLoadingCases(true);
    setError("");
    try {
      const res = await fetch(`${API_BASE}/api/v1/cases?limit=200`, { credentials: "include" });
      if (!res.ok) throw new Error(`加载案件失败: ${res.status}`);
      const payload = (await res.json()) as CaseList;
      const items = payload.items ?? [];
      setCases(items);
      setSelectedId((current) => current ?? (items.find(isReportCase) ?? items[0])?.claim_id ?? null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "加载案件失败");
    } finally {
      setLoadingCases(false);
    }
  }, []);

  const loadDetail = useCallback(async (claimId: string) => {
    setLoadingDetail(true);
    setError("");
    try {
      const [fullRes, auditRes] = await Promise.all([
        fetch(`${API_BASE}/api/v1/cases/${encodeURIComponent(claimId)}/full`, { credentials: "include" }),
        fetch(`${API_BASE}/api/v1/audit/${encodeURIComponent(claimId)}`, { credentials: "include" })
      ]);
      if (!fullRes.ok) throw new Error(`加载报告详情失败: ${fullRes.status}`);
      if (!auditRes.ok) throw new Error(`加载审计日志失败: ${auditRes.status}`);
      setDetail((await fullRes.json()) as FullCase);
      setAuditLogs((await auditRes.json()) as AuditLogItem[]);
    } catch (e) {
      setError(e instanceof Error ? e.message : "加载详情失败");
      setDetail(null);
      setAuditLogs([]);
    } finally {
      setLoadingDetail(false);
    }
  }, []);

  useEffect(() => {
    void loadCases();
  }, [loadCases]);

  useEffect(() => {
    if (selectedId) void loadDetail(selectedId);
  }, [loadDetail, selectedId]);

  const stats = useMemo(() => {
    const reportReady = cases.filter(isReportCase).length;
    const pendingReview = cases.filter((item) => item.state === "REPORT_DRAFTED" || item.state === "HUMAN_REVIEW").length;
    const archived = cases.filter((item) => item.state === "ARCHIVED").length;
    const payout = cases.reduce((sum, item) => sum + (item.payout_amount_yuan ?? 0), 0);
    return { reportReady, pendingReview, archived, payout };
  }, [cases]);

  const selectedReport = detail?.results.report ?? null;
  const selectedReview = detail?.results.human_review ?? null;
  const artifacts = selectedReport?.artifacts ?? [];
  const orderedAudit = [...auditLogs].sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at));

  function downloadArtifact(artifact: ReportArtifact) {
    if (!artifact.download_url) return;
    window.open(`${API_BASE}${artifact.download_url}`, "_blank", "noopener,noreferrer");
  }

  function downloadReceipt() {
    if (!selectedReview?.receipt_download_url || !selectedId) return;
    window.open(`${API_BASE}${selectedReview.receipt_download_url}`, "_blank", "noopener,noreferrer");
  }

  return (
    <main className="shell reports-shell">
      <header className="topbar">
        <div className="brand-block">
          <div className="brand-mark">
            <FileCheck2 size={22} />
          </div>
          <div>
            <p className="eyebrow">Agrisky AI · Report & Audit</p>
            <h1>报告与审计</h1>
          </div>
        </div>
        <button className="secondary icon-action" type="button" onClick={loadCases} title="刷新">
          {loadingCases ? <Loader2 className="spin" size={16} /> : <RefreshCcw size={16} />}
        </button>
      </header>

      <div className="cases-kpi-grid reports-kpi-grid">
        <div className="metric rich">
          <div className="metric-icon"><FileText size={18} /></div>
          <span>报告案件</span>
          <strong>{stats.reportReady}</strong>
        </div>
        <div className="metric rich risk">
          <div className="metric-icon"><ShieldCheck size={18} /></div>
          <span>待人工审核</span>
          <strong>{stats.pendingReview}</strong>
        </div>
        <div className="metric rich">
          <div className="metric-icon"><Archive size={18} /></div>
          <span>已归档</span>
          <strong>{stats.archived}</strong>
        </div>
        <div className="metric rich">
          <div className="metric-icon"><FileSpreadsheet size={18} /></div>
          <span>赔款合计</span>
          <strong>{yuan(stats.payout)}</strong>
        </div>
      </div>

      {error ? (
        <div className="message-box error">
          <AlertTriangle size={15} />
          <span>{error}</span>
        </div>
      ) : null}

      <section className="reports-workspace">
        <section className="table-panel report-case-list">
          <div className="section-head">
            <div>
              <p className="eyebrow">Case Archive</p>
              <h2>案件报告索引</h2>
            </div>
            <span className="audit-count">{cases.length} 个案件</span>
          </div>

          <div className="report-list">
            {cases.map((item) => (
              <button
                className={`report-list-item${selectedId === item.claim_id ? " active" : ""}`}
                key={item.claim_id}
                type="button"
                onClick={() => setSelectedId(item.claim_id)}
              >
                <span className="mono">{item.claim_id}</span>
                <strong>{STATE_LABELS[item.state] ?? item.state}</strong>
                <small>
                  {DISASTER_LABELS[item.disaster_type] ?? item.disaster_type} · {item.crop_type} · {shortTime(item.reported_at)}
                </small>
              </button>
            ))}
          </div>

          {!loadingCases && cases.length === 0 ? (
            <div className="empty-table">暂无案件。请先在理赔驾驶舱创建或打开演示案件。</div>
          ) : null}
        </section>

        <section className="report-detail-stack">
          <section className="preview-panel report-summary-panel">
            <div className="section-head">
              <div>
                <p className="eyebrow">Report Package</p>
                <h2>{selectedId ?? "未选择案件"}</h2>
              </div>
              {loadingDetail ? <Loader2 className="spin" size={16} /> : null}
            </div>

            {detail ? (
              <div className="report-summary-grid">
                <div>
                  <span>案件状态</span>
                  <strong>{STATE_LABELS[detail.state] ?? detail.state}</strong>
                </div>
                <div>
                  <span>保单号</span>
                  <strong>{detail.case.policy_id}</strong>
                </div>
                <div>
                  <span>报告生成号</span>
                  <strong>{selectedReport?.generation_id ?? "尚未生成"}</strong>
                </div>
                <div>
                  <span>快照哈希</span>
                  <strong>{shortHash(selectedReport?.snapshot_sha256)}</strong>
                </div>
                <div>
                  <span>审核状态</span>
                  <strong>{reviewStatusLabel(selectedReview)}</strong>
                </div>
                <div>
                  <span>预估赔款</span>
                  <strong>{yuan(detail.results.payout?.payout_amount_yuan)}</strong>
                </div>
              </div>
            ) : (
              <div className="audit-empty">请选择左侧案件查看报告与审计详情。</div>
            )}

            {selectedId ? (
              <div className="report-action-row">
                <Link className="secondary" href={`/claims?claim_id=${selectedId}`}>
                  <ExternalLink size={15} />
                  打开理赔驾驶舱
                </Link>
                {selectedReview?.receipt_download_url ? (
                  <button className="secondary" type="button" onClick={downloadReceipt}>
                    <ClipboardCheck size={15} />
                    下载审核回执
                  </button>
                ) : null}
              </div>
            ) : null}
          </section>

          <section className="preview-panel report-artifacts-panel">
            <div className="section-head">
              <div>
                <p className="eyebrow">Controlled Downloads</p>
                <h2>受控附件</h2>
              </div>
              <span className="audit-count">{artifacts.length} 个文件</span>
            </div>

            {artifacts.length ? (
              <div className="artifact-list">
                {artifacts.map((artifact) => (
                  <div className="artifact-row" key={`${artifact.kind}-${artifact.filename}`}>
                    <div>
                      <strong>{ARTIFACT_LABELS[artifact.kind] ?? artifact.kind}</strong>
                      <span>{artifact.filename}</span>
                      <small>
                        {fileSize(artifact.size_bytes)} · SHA-256 {shortHash(artifact.sha256)}
                      </small>
                    </div>
                    <button className="secondary icon-action" type="button" onClick={() => downloadArtifact(artifact)} disabled={!artifact.download_url}>
                      <Download size={15} />
                    </button>
                  </div>
                ))}
              </div>
            ) : (
              <div className="audit-empty">
                当前案件尚未生成报告附件。完成规则评级后，可在理赔驾驶舱生成报告草稿。
              </div>
            )}
          </section>

          <section className="preview-panel review-panel">
            <div className="section-head">
              <div>
                <p className="eyebrow">Human Review</p>
                <h2>人工审核回执</h2>
              </div>
              {reviewDecision(selectedReview) === "approved" ? <CheckCircle2 size={18} /> : null}
            </div>

            {selectedReview ? (
              <div className="review-facts">
                <span>审核号 <strong>{selectedReview.review_id ?? "-"}</strong></span>
                <span>审核人 <strong>{reviewActor(selectedReview)}</strong></span>
                <span>时间 <strong>{shortTime(reviewTime(selectedReview))}</strong></span>
                <span>回执 <strong>{shortHash(selectedReview.receipt_sha256)}</strong></span>
                {reviewComment(selectedReview) ? <p>{reviewComment(selectedReview)}</p> : null}
              </div>
            ) : (
              <div className="audit-empty">当前案件尚未形成可下载审核回执。</div>
            )}
          </section>

          <section className="preview-panel audit-timeline-panel">
            <div className="section-head">
              <div>
                <p className="eyebrow">Audit Trail</p>
                <h2>审计时间线</h2>
              </div>
              <span className="audit-count">{orderedAudit.length} 条记录</span>
            </div>

            {orderedAudit.length ? (
              <div className="audit-timeline">
                {orderedAudit.map((item, index) => (
                  <div className="audit-timeline-item" key={`${item.created_at}-${item.action}-${index}`}>
                    <div className="audit-dot">
                      <FileCheck2 size={13} />
                    </div>
                    <div>
                      <strong>{AUDIT_ACTION_LABELS[item.action] ?? item.action}</strong>
                      <span>
                        {shortTime(item.created_at)}
                        {item.tool_name ? ` · ${item.tool_name}` : ""}
                      </span>
                      {item.notes ? <p>{item.notes}</p> : null}
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <div className="audit-empty">当前案件暂无审计日志。</div>
            )}
          </section>
        </section>
      </section>
    </main>
  );
}
