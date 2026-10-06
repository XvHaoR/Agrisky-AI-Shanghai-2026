"use client";

import { Bot, ExternalLink, FileText, LogOut, MapPinned, ShieldCheck, UserCircle2, X } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { fetchMe, logoutSession } from "../_lib/auth";
import { LoginScreen } from "../_components/LoginScreen";

type Policy = { policy_id: string; crop_type?: string; address?: string; area_mu?: number | null };
type Case = {
  claim_id: string;
  policy_id?: string;
  state?: string;
  disaster_type?: string;
  loss_date?: string;
  reported_at?: string;
  payout_amount_yuan?: number | null;
  risk_level?: string | null;
};
type Me = { username: string; holder_name: string; role: string; policies: Policy[]; cases: Case[] };

const STATE_LABELS: Record<string, string> = {
  INIT: "新建",
  MATERIAL_CHECK: "材料校验",
  PREPROCESS_READY: "待初筛",
  SCREENING_DONE: "已初筛",
  NDVI_DONE: "已评估",
  COMPLIANCE_DONE: "已合规",
  RULE_DONE: "已评级",
  REPORT_DRAFTED: "报告草稿",
  HUMAN_REVIEW: "待审核",
  ARCHIVED: "已归档"
};

function area(value?: number | null) {
  if (typeof value !== "number" || Number.isNaN(value)) return "-";
  return `${new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(value)} 亩`;
}

function yuan(value?: number | null) {
  if (typeof value !== "number" || Number.isNaN(value)) return "-";
  return `${new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 0 }).format(value)} 元`;
}

export default function PortalPage() {
  const router = useRouter();
  const [me, setMe] = useState<Me | null>(null);
  const [ready, setReady] = useState(false);
  const [selectedPolicy, setSelectedPolicy] = useState<Policy | null>(null);

  const loadMe = useCallback(async () => {
    const payload = await fetchMe();
    if (!payload) {
      setMe(null);
      return;
    }
    if (payload.role === "admin") {
      router.replace("/");
      return;
    }
    setMe(payload as Me);
  }, [router]);

  useEffect(() => {
    loadMe().catch(() => setMe(null)).finally(() => setReady(true));
  }, [loadMe]);

  function logout() {
    void logoutSession().catch(() => undefined).finally(() => setMe(null));
  }

  if (!ready) return null;

  if (!me) {
    return (
      <LoginScreen
        defaultUsername="nonghu"
        title="穹野智保"
        subtitle="Agrisky AI · 农业保险理赔辅助 Agent"
        onAuthenticated={({ me: nextMe }) => {
          if (nextMe.role === "admin") {
            router.replace("/");
            return;
          }
          void loadMe().catch(() => setMe(null));
        }}
      />
    );
  }

  return (
    <main className="shell">
      <header className="topbar">
        <div className="brand-block">
          <div className="brand-mark"><UserCircle2 size={22} /></div>
          <div>
            <p className="eyebrow">Agrisky AI · 投保人门户</p>
            <h1>你好，{me.holder_name}</h1>
          </div>
        </div>
        <div className="top-status">
          <Link
            href="/agent"
            className="primary icon-action"
            style={{ textDecoration: "none", padding: "0 14px", display: "inline-flex", alignItems: "center", gap: 6 }}
          >
            <Bot size={16} />
            去报案
          </Link>
          <button className="secondary icon-action" type="button" onClick={logout} title="退出登录">
            <LogOut size={16} />
          </button>
        </div>
      </header>

      <section className="portal-workspace">
        <section className="claim-main">
          <section className="table-panel">
            <div className="section-head">
              <div>
                <p className="eyebrow">My Policies</p>
                <h2><MapPinned size={15} /> 我的保单（{me.policies.length}）</h2>
              </div>
            </div>
            {me.policies.length === 0 ? (
              <div className="empty-table">名下暂无在册保单。</div>
            ) : (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>保单号</th>
                      <th>作物</th>
                      <th>承保面积</th>
                      <th>地址</th>
                    </tr>
                  </thead>
                  <tbody>
                    {me.policies.map((policy) => (
                      <tr
                        key={policy.policy_id}
                        className="row-clickable"
                        onClick={() => setSelectedPolicy(policy)}
                        title="点击查看详情"
                      >
                        <td>{policy.policy_id}</td>
                        <td>{policy.crop_type || "-"}</td>
                        <td>{area(policy.area_mu)}</td>
                        <td>{policy.address || "-"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <section className="table-panel">
            <div className="section-head">
              <div>
                <p className="eyebrow">My Claims</p>
                <h2><ShieldCheck size={15} /> 我的理赔（{me.cases.length}）</h2>
              </div>
            </div>
            {me.cases.length === 0 ? (
              <div className="empty-table">名下暂无理赔案件。你可以先通过智能体发起一笔报案。</div>
            ) : (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>案件号</th>
                      <th>灾害</th>
                      <th>受灾日期</th>
                      <th>状态</th>
                      <th>风险</th>
                      <th>预计赔款</th>
                    </tr>
                  </thead>
                  <tbody>
                    {me.cases.map((claim) => (
                      <tr
                        key={claim.claim_id}
                        className="row-clickable"
                        onClick={() => router.push(`/claims?claim_id=${claim.claim_id}`)}
                        title="点击查看详情"
                      >
                        <td>{claim.claim_id}</td>
                        <td>{claim.disaster_type || "-"}</td>
                        <td>{claim.loss_date || "-"}</td>
                        <td>{STATE_LABELS[claim.state ?? ""] ?? claim.state}</td>
                        <td>{claim.risk_level ?? "-"}</td>
                        <td>{yuan(claim.payout_amount_yuan)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </section>
      </section>

      {selectedPolicy ? (
        <PolicyDetailModal
          policy={selectedPolicy}
          relatedCases={me.cases.filter((c) => c.policy_id === selectedPolicy.policy_id)}
          onClose={() => setSelectedPolicy(null)}
          onViewClaim={(claimId) => router.push(`/claims?claim_id=${claimId}`)}
        />
      ) : null}
    </main>
  );
}

function PolicyDetailModal({
  policy,
  relatedCases,
  onClose,
  onViewClaim
}: {
  policy: Policy;
  relatedCases: Case[];
  onClose: () => void;
  onViewClaim: (claimId: string) => void;
}) {
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-card" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h2>
            <FileText size={20} />
            保单详情
          </h2>
          <button type="button" onClick={onClose} title="关闭">
            <X size={18} />
          </button>
        </div>

        <div className="modal-detail-grid">
          <div className="detail-item">
            <span className="detail-label">保单号</span>
            <span className="detail-value">{policy.policy_id}</span>
          </div>
          <div className="detail-item">
            <span className="detail-label">作物类型</span>
            <span className="detail-value">{policy.crop_type || "-"}</span>
          </div>
          <div className="detail-item">
            <span className="detail-label">承保面积</span>
            <span className="detail-value">{area(policy.area_mu)}</span>
          </div>
          <div className="detail-item">
            <span className="detail-label">地址</span>
            <span className="detail-value">{policy.address || "-"}</span>
          </div>
        </div>

        <hr className="modal-divider" />

        <p className="modal-related-title">关联理赔案件（{relatedCases.length}）</p>
        {relatedCases.length === 0 ? (
          <div className="modal-related-empty">该保单暂无理赔记录。</div>
        ) : (
          <div className="table-wrap" style={{ marginTop: 0 }}>
            <table>
              <thead>
                <tr>
                  <th>案件号</th>
                  <th>灾害</th>
                  <th>状态</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {relatedCases.map((c) => (
                  <tr key={c.claim_id}>
                    <td>{c.claim_id}</td>
                    <td>{c.disaster_type || "-"}</td>
                    <td>{STATE_LABELS[c.state ?? ""] ?? c.state}</td>
                    <td>
                      <button
                        className="secondary icon-action"
                        type="button"
                        onClick={() => onViewClaim(c.claim_id)}
                        style={{ padding: "4px 10px", fontSize: 12 }}
                      >
                        <ExternalLink size={13} />
                        查看
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
