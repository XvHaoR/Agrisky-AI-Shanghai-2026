"use client";

import {
  AlertTriangle,
  Coins,
  Database,
  ExternalLink,
  Layers,
  Loader2,
  RefreshCcw,
  ShieldAlert,
  Trash2
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
  yield_loss_ratio: number | null;
  payout_amount_yuan: number | null;
};

type CaseList = {
  total: number;
  by_state: Record<string, number>;
  total_payout_yuan: number;
  high_risk_count: number;
  items: CaseItem[];
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

const RISK_LABELS: Record<string, string> = { high: "高", medium: "中", low: "低" };

function yuan(value: number | null) {
  if (typeof value !== "number" || Number.isNaN(value)) return "-";
  return `${new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 0 }).format(value)} 元`;
}

function ratio(value: number | null) {
  if (typeof value !== "number" || Number.isNaN(value)) return "-";
  return `${(value * 100).toFixed(1)}%`;
}

function shortTime(value: string) {
  if (!value) return "-";
  return value.replace("T", " ").slice(0, 16);
}

export default function CasesPage() {
  return (
    <AdminGate>
      <CasesWorkspace />
    </AdminGate>
  );
}

function CasesWorkspace() {
  const [data, setData] = useState<CaseList | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [filter, setFilter] = useState<string>("");
  const [deletingId, setDeletingId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const url = filter ? `${API_BASE}/api/v1/cases?state=${filter}` : `${API_BASE}/api/v1/cases`;
      const res = await fetch(url, { credentials: "include" });
      if (!res.ok) throw new Error(`加载失败: ${res.status}`);
      setData((await res.json()) as CaseList);
    } catch (e) {
      setError(e instanceof Error ? e.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, [filter]);

  useEffect(() => {
    load();
  }, [load]);

  async function deleteCase(item: CaseItem) {
    const evidenceNote = item.state === "ARCHIVED"
      ? "该案件已归档。删除后将从业务页面隐藏，但报告、证据和审计记录仍会保留。"
      : "删除后将从业务页面隐藏，但已有结果、证据和审计记录仍会保留。";
    if (!window.confirm(`确认删除案件 ${item.claim_id}？\n\n${evidenceNote}`)) return;
    setDeletingId(item.claim_id);
    setError("");
    try {
      const response = await fetch(
        `${API_BASE}/api/v1/cases/${encodeURIComponent(item.claim_id)}`,
        { method: "DELETE", credentials: "include" }
      );
      const payload = (await response.json().catch(() => ({}))) as { detail?: string };
      if (!response.ok) throw new Error(payload.detail || `删除失败: ${response.status}`);
      await load();
    } catch (deleteError) {
      setError(deleteError instanceof Error ? deleteError.message : "删除案件失败");
    } finally {
      setDeletingId(null);
    }
  }

  const stateChips = useMemo(() => {
    const entries = Object.entries(data?.by_state ?? {});
    entries.sort((a, b) => b[1] - a[1]);
    return entries;
  }, [data?.by_state]);

  return (
    <main className="shell cases-shell">
      <header className="topbar">
        <div className="brand-block">
          <div className="brand-mark">
            <Layers size={22} />
          </div>
          <div>
            <p className="eyebrow">Agrisky AI · Queue Triage</p>
            <h1>理赔案件队列</h1>
          </div>
        </div>
        <button className="secondary icon-action" type="button" onClick={load} title="刷新">
          {loading ? <Loader2 className="spin" size={16} /> : <RefreshCcw size={16} />}
        </button>
      </header>

      <div className="cases-kpi-grid">
        <div className="metric rich">
          <div className="metric-icon"><Database size={18} /></div>
          <span>案件总数</span>
          <strong>{data?.total ?? "-"}</strong>
        </div>
        <div className="metric rich risk">
          <div className="metric-icon"><ShieldAlert size={18} /></div>
          <span>高风险案件</span>
          <strong>{data?.high_risk_count ?? "-"}</strong>
        </div>
        <div className="metric rich">
          <div className="metric-icon"><Coins size={18} /></div>
          <span>预估赔款合计</span>
          <strong>{yuan(data?.total_payout_yuan ?? null)}</strong>
        </div>
        <div className="metric rich">
          <div className="metric-icon"><Layers size={18} /></div>
          <span>状态分布</span>
          <strong>{stateChips.length} 类</strong>
        </div>
      </div>

      <div className="cases-filters">
        <button className={filter === "" ? "active" : ""} type="button" onClick={() => setFilter("")}>
          全部
        </button>
        {stateChips.map(([state, count]) => (
          <button key={state} className={filter === state ? "active" : ""} type="button" onClick={() => setFilter(state)}>
            {STATE_LABELS[state] ?? state}
            <span>{count}</span>
          </button>
        ))}
      </div>

      <section className="table-panel cases-table">
        {error ? (
          <div className="message-box error">
            <AlertTriangle size={15} />
            <span>{error}</span>
          </div>
        ) : null}
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>案件号</th>
                <th>状态</th>
                <th>灾害</th>
                <th>作物</th>
                <th>减产率</th>
                <th>预估赔款</th>
                <th>风险</th>
                <th>上报时间</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {(data?.items ?? []).map((item) => (
                <tr key={item.claim_id}>
                  <td className="mono">{item.claim_id}</td>
                  <td>
                    <span className={`state-pill ${item.state}`}>{STATE_LABELS[item.state] ?? item.state}</span>
                  </td>
                  <td>{DISASTER_LABELS[item.disaster_type] ?? item.disaster_type}</td>
                  <td>{item.crop_type}</td>
                  <td>{ratio(item.yield_loss_ratio)}</td>
                  <td>{yuan(item.payout_amount_yuan)}</td>
                  <td>
                    {item.risk_level ? (
                      <span className={`risk-tag ${item.risk_level}`}>{RISK_LABELS[item.risk_level] ?? item.risk_level}</span>
                    ) : (
                      "-"
                    )}
                  </td>
                  <td className="mono dim">{shortTime(item.reported_at)}</td>
                  <td>
                    <div className="row-actions">
                      <Link className="row-open" href={`/claims?claim_id=${item.claim_id}`}>
                        <ExternalLink size={13} />
                        打开
                      </Link>
                      <button
                        className="row-delete"
                        type="button"
                        onClick={() => deleteCase(item)}
                        disabled={deletingId === item.claim_id}
                        title="删除案件"
                        aria-label={`删除案件 ${item.claim_id}`}
                      >
                        {deletingId === item.claim_id
                          ? <Loader2 className="spin" size={14} />
                          : <Trash2 size={14} />}
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!loading && (data?.items?.length ?? 0) === 0 ? (
          <div className="empty-table">暂无案件{filter ? `（状态：${STATE_LABELS[filter] ?? filter}）` : ""}。</div>
        ) : null}
      </section>
    </main>
  );
}
