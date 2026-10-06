"use client";

import { BookOpenCheck, Download, ExternalLink, FileCheck2, Loader2, Scale, Sprout } from "lucide-react";
import { useEffect, useState } from "react";
import { AdminGate } from "../_components/AdminGate";
import { getApiBase } from "../_lib/api-base";

const API_BASE = getApiBase();

type LegalDocument = {
  id: string;
  title: string;
  issuer: string;
  source_url: string;
  effective_date: string;
  sha256: string;
  download_url: string;
};

type Stage = {
  name: string;
  start: string;
  end: string;
  factor: number;
  focus: string;
  refs: string[];
};

type CropScheme = {
  name: string;
  indices: string[];
  assessment_focus: string;
  stages: Stage[];
  pitfalls: string[];
};

type ReportCitation = {
  id: string;
  document_id: string;
  article: string;
  topic: string;
  summary: string;
  applicability: string;
  report_sections: string[];
};

type Library = {
  schema_version: string;
  disclosure: string;
  legal_documents: LegalDocument[];
  report_citations: ReportCitation[];
  citation_disclosure: string;
  crop_schemes: Record<string, CropScheme>;
  technical_references: Array<{ id: string; title: string; publisher: string; url: string }>;
};

const sectionCard = "rounded-lg border border-[var(--line)] bg-[rgba(255,255,255,0.88)] px-5 py-[18px] shadow-[var(--shadow-soft)]";
const legalChip = "rounded-[5px] bg-[#edf7f4] px-2 py-[5px] text-[11px] text-[#176b57]";
const caveatChip = "rounded-[5px] bg-[#fff6e8] px-2 py-[5px] text-[11px] text-[#8a5a00]";

export default function CompliancePage() {
  return <AdminGate><ComplianceWorkspace /></AdminGate>;
}

function ComplianceWorkspace() {
  const [library, setLibrary] = useState<Library | null>(null);
  const [error, setError] = useState("");
  const [activeCrop, setActiveCrop] = useState("rice");

  useEffect(() => {
    fetch(`${API_BASE}/api/v1/compliance/library`, { credentials: "include" })
      .then(async (response) => {
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.detail || "合规依据加载失败");
        setLibrary(payload as Library);
      })
      .catch((reason) => setError(reason instanceof Error ? reason.message : "合规依据加载失败"));
  }, []);

  const scheme = library?.crop_schemes[activeCrop];
  return (
    <main className="shell !max-w-[1480px]">
      <header className="topbar">
        <div className="brand-block">
          <div className="brand-mark"><Scale size={22} /></div>
          <div><p className="eyebrow">Agrisky AI · Compliance Basis</p><h1>理赔依据与作物规则</h1></div>
        </div>
        <div className="top-status"><span className="api-pill">官方法规 · 冻结合同 · 技术规则 · 人工审核</span></div>
      </header>

      {error ? <div className="message-box error">{error}</div> : null}
      {!library ? <div className="flex min-h-[420px] items-center justify-center gap-2 text-[var(--muted)]"><Loader2 className="spin" size={20} /> 正在加载依据库…</div> : (
        <div className="mt-4 grid gap-4 min-[981px]:grid-cols-[minmax(320px,0.72fr)_minmax(620px,1.55fr)]">
          <section className={`${sectionCard} flex flex-col`}>
            <div className="section-head !mb-3.5">
              <div><p className="eyebrow">Legal corpus</p><h2>内置法规文件</h2></div>
              <BookOpenCheck size={20} />
            </div>
            <div className="mb-3.5 grid max-h-[280px] content-start gap-2.5 overflow-y-auto pr-1">
              {library.legal_documents.map((document) => (
                <article className="grid grid-cols-[auto_minmax(0,1fr)_32px_32px] items-center gap-3 rounded-md border border-[var(--line)] bg-white p-3" key={document.id}>
                  <div className="font-mono text-xs font-bold text-[#176b57]">{document.id}</div>
                  <div className="grid gap-[3px]">
                    <h3 className="m-0 text-[15px]">{document.title}</h3>
                    <p className="m-0 text-[11px] text-[var(--muted)]">{document.issuer} · 生效日期 {document.effective_date}</p>
                    <p className="m-0 text-[11px] font-semibold text-[#176b57]">
                      报告引用 {library.report_citations.filter((item) => item.document_id === document.id).length} 条 · {library.report_citations
                        .filter((item) => item.document_id === document.id)
                        .map((item) => item.article)
                        .join("、")}
                    </p>
                    <code className="text-[11px] text-[var(--muted)]">SHA-256 {document.sha256.slice(0, 16)}…</code>
                  </div>
                  <a className="grid h-8 w-8 place-items-center rounded-md border border-[var(--line)] text-[#16745d]" href={document.source_url} target="_blank" rel="noreferrer" title="查看官方来源"><ExternalLink size={16} /></a>
                  <a className="grid h-8 w-8 place-items-center rounded-md border border-[var(--line)] text-[#16745d]" href={`${API_BASE}${document.download_url}`} title="下载项目内置的官网原始文件"><Download size={16} /></a>
                </article>
              ))}
            </div>
            <div className="mt-auto flex items-center justify-between gap-1.5 rounded-md bg-[#eef8f4] p-2.5 text-xs text-[#176b57]">
              <span className="inline-flex items-center gap-[5px]"><FileCheck2 size={16} />报告引用</span><b>→</b><span className="inline-flex items-center gap-[5px]">合同条款</span><b>→</b><span className="inline-flex items-center gap-[5px]">法规与技术索引</span><b>→</b><span className="inline-flex items-center gap-[5px]">人工审核</span>
            </div>
            <p className="m-0 mt-2 text-[11px] leading-5 text-[var(--muted)]">{library.citation_disclosure}</p>
          </section>

          <section className={sectionCard}>
            <div className="section-head !mb-3.5">
              <div><p className="eyebrow">Crop rule packs</p><h2>分作物遥感评估方案</h2></div>
              <Sprout size={20} />
            </div>
            <div className="mb-3.5 inline-flex gap-1 rounded-[7px] border border-[var(--line)] bg-[#f4f7f6] p-[3px]" role="tablist">
              {[['rice', '水稻'], ['corn', '玉米'], ['wheat', '小麦']].map(([key, label]) => (
                <button className={`cursor-pointer rounded-[5px] border-0 px-4 py-[7px] ${activeCrop === key ? "bg-[#0d6b58] text-white" : "bg-transparent"}`} key={key} onClick={() => setActiveCrop(key)} type="button">{label}</button>
              ))}
            </div>
            {scheme ? (
              <div className="grid gap-3">
                <p className="m-0 text-[#445b52]">{scheme.assessment_focus}</p>
                <div className="flex flex-wrap gap-1.5">{scheme.indices.map((item) => <span className={legalChip} key={item}>{item}</span>)}</div>
                <div className="overflow-x-auto [&>table]:w-full [&_td]:p-[9px] [&_td]:text-xs [&_th]:p-[9px] [&_th]:text-xs">
                  <table><thead><tr><th>生育阶段</th><th>时窗</th><th>合同示范系数</th><th>遥感判读重点</th><th>依据</th></tr></thead>
                    <tbody>{scheme.stages.map((stage) => <tr key={stage.name}><td><strong>{stage.name}</strong></td><td>{stage.start} 至 {stage.end}</td><td>{stage.factor.toFixed(2)}</td><td>{stage.focus}</td><td>{stage.refs.join(" / ")}</td></tr>)}</tbody>
                  </table>
                </div>
                <div className="flex flex-wrap gap-1.5">{scheme.pitfalls.map((item) => <span className={caveatChip} key={item}>{item}</span>)}</div>
              </div>
            ) : null}
          </section>

          <section className={`${sectionCard} col-span-full`}>
            <div className="section-head !mb-3.5"><div><p className="eyebrow">Technical index</p><h2>技术依据索引</h2></div></div>
            <div className="grid gap-2.5 sm:grid-cols-2 xl:grid-cols-4">{library.technical_references.map((item) => (
              <a className="grid grid-cols-[auto_minmax(0,1fr)_auto] gap-x-2 gap-y-[5px] rounded-md border border-[var(--line)] bg-white p-3 text-inherit no-underline" href={item.url} target="_blank" rel="noreferrer" key={item.id}><b className="text-[#176b57]">{item.id}</b><span>{item.title}</span><small className="col-start-2 text-[var(--muted)]">{item.publisher}</small><ExternalLink size={14} /></a>
            ))}</div>
            <p className="m-0 mt-3.5 text-xs text-[var(--muted)]">{library.disclosure}</p>
          </section>
        </div>
      )}
    </main>
  );
}
