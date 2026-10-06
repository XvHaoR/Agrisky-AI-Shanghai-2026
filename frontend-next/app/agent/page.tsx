"use client";

import { Activity, Bot, Loader2, RotateCcw, SendHorizontal, Square, User, Wrench } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { fetchMe } from "../_lib/auth";
import { getApiBase } from "../_lib/api-base";

const API_BASE = getApiBase();

type ToolCall = { id?: string; function?: { name?: string; arguments?: string } };
type ChatMsg = {
  role: "user" | "assistant" | "tool" | "system";
  content?: string | null;
  tool_calls?: ToolCall[];
  tool_call_id?: string;
};
type AgentResponse = { messages: ChatMsg[]; trace?: unknown[]; reply?: string; error?: boolean };
type AgentHealth = {
  status: "ready" | "degraded" | "unavailable";
  provider?: string;
  model?: string;
  configured?: boolean;
  message?: string;
  tool_count?: number;
};
type AgentRunEvent = {
  sequence_no: number;
  event_type: string;
  payload?: { tool?: string; status?: string; error_message?: string };
};
type AgentRun = {
  run_id: string;
  status: "queued" | "running" | "completed" | "failed" | "cancelled";
  response?: AgentResponse | null;
  error_message?: string | null;
  events?: AgentRunEvent[];
};

const CHAT_STORAGE_PREFIX = "agrisky_agent_chat_v1";

function persistedMessages(source: ChatMsg[]): ChatMsg[] {
  // Tool payloads can be large and may contain operational details. Persist the
  // visible conversation only; a follow-up can safely re-read authoritative data.
  return source
    .filter(
      (message) =>
        (message.role === "user" || message.role === "assistant") &&
        typeof message.content === "string" &&
        message.content.trim().length > 0
    )
    .map((message) => ({ role: message.role, content: message.content }));
}

const TOOL_LABELS: Record<string, string> = {
  analyze_claims: "全局案件分析",
  get_case_details: "案件详情",
  inspect_policy_boundary: "读取在册边界",
  inspect_policy_contract: "读取保单合同",
  run_claim_workflow: "自动理赔调度",
  create_claim: "建案",
  validate_materials: "案件要素校验",
  run_satellite_screening: "卫星初筛",
  run_loss_assessment: "多源减产率评估",
  run_compliance: "合规核验",
  run_payout_estimate: "赔付测算",
  run_rule_engine: "规则评级",
  generate_report: "生成报告草稿",
  generate_excel_report: "生成评估表",
  run_growth_analysis: "当前长势分析",
  run_historical_ndvi: "历史季度长势",
  run_parcel_growth: "分地块长势",
  list_case_documents: "读取案件材料",
  compare_case_materials: "核对材料一致性",
  explain_material_findings: "解释材料问题"
};

const ADMIN_SUGGESTIONS = [
  "分析所有案件情况，按阻断项和调度优先级排序",
  "列出缺少在册边界、等待人工审核和高风险的案件",
  "读取保单 POL-2026-001 的本地在册边界摘要",
  "对指定案件自动执行长势分析和理赔全流程，直到报告草稿",
  "检视归档案件 CLAIM-20260722-9532AC 的完整链路：SAR 初筛、NDVI 长势、合规核验、赔付测算、报告包与哈希可追溯性",
  "说明 Agrisky AI 的决策边界、人工审核节点与结果追溯机制"
];

const POLICYHOLDER_SUGGESTIONS = [
  "分析我名下所有案件并告诉我下一步",
  "读取我名下保单的在册边界摘要",
  "查看指定案件的长势、减产率、赔付和报告状态",
  "对指定案件自动执行长势分析和理赔流程，直到报告草稿"
];

function MarkdownReply({ content }: { content: string }) {
  return (
    <div className="agent-markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        skipHtml
        components={{
          a: ({ children, href }) => (
            <a href={href} target="_blank" rel="noreferrer noopener">
              {children}
            </a>
          ),
          table: ({ children }) => (
            <div className="agent-markdown-table">
              <table>{children}</table>
            </div>
          )
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}

export default function AgentPage() {
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [scoped, setScoped] = useState(false);
  const [role, setRole] = useState<string | null>(null);
  const [health, setHealth] = useState<AgentHealth | null>(null);
  const [runId, setRunId] = useState("");
  const [runEvents, setRunEvents] = useState<AgentRunEvent[]>([]);
  const scrollRef = useRef<HTMLDivElement>(null);
  const storageKeyRef = useRef<string | null>(null);
  const hydratedRef = useRef(false);

  useEffect(() => {
    let cancelled = false;

    async function syncScope() {
      try {
        const me = await fetchMe();
        if (cancelled) return;
        if (me) {
          setRole(me.role);
          setScoped(me.role !== "admin");
          storageKeyRef.current = `${CHAT_STORAGE_PREFIX}:${me.username}`;
          try {
            const raw = window.localStorage.getItem(storageKeyRef.current);
            const restored = raw ? (JSON.parse(raw) as { messages?: ChatMsg[] }) : null;
            if (restored?.messages) setMessages(persistedMessages(restored.messages));
          } catch {
            window.localStorage.removeItem(storageKeyRef.current);
          } finally {
            hydratedRef.current = true;
          }
          return;
        }
      } catch {
        // clear scope below
      }

      if (!cancelled) {
        setScoped(false);
        setRole(null);
        storageKeyRef.current = `${CHAT_STORAGE_PREFIX}:anonymous`;
        hydratedRef.current = true;
      }
    }

    syncScope();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const storageKey = storageKeyRef.current;
    if (!hydratedRef.current || !storageKey) return;
    try {
      window.localStorage.setItem(
        storageKey,
        JSON.stringify({ messages: persistedMessages(messages), saved_at: new Date().toISOString() })
      );
    } catch {
      // Storage quota or privacy mode must not interrupt the operational chat.
    }
  }, [messages]);

  useEffect(() => {
    let cancelled = false;
    async function checkHealth() {
      try {
        const response = await fetch(`${API_BASE}/api/v1/agent/health?probe=true`, {
          credentials: "include"
        });
        const payload = (await response.json()) as AgentHealth;
        if (!cancelled) setHealth(payload);
      } catch {
        if (!cancelled) {
          setHealth({ status: "unavailable", message: "无法连接 Agent 服务" });
        }
      }
    }
    checkHealth();
    return () => {
      cancelled = true;
    };
  }, []);

  const suggestions = scoped ? POLICYHOLDER_SUGGESTIONS : ADMIN_SUGGESTIONS;

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, loading]);

  async function send(text: string) {
    const content = text.trim();
    if (!content || loading) return;

    setError("");
    setInput("");
    const next = [...messages, { role: "user" as const, content }];
    setMessages(next);
    setLoading(true);

    try {
      setRunEvents([]);
      const response = await fetch(`${API_BASE}/api/v1/agent/runs`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ messages: next })
      });
      const created = (await response.json()) as { run_id?: string; detail?: string };
      if (!response.ok || !created.run_id) {
        throw new Error(created.detail || `请求失败: ${response.status}`);
      }
      setRunId(created.run_id);
      let finished: AgentRun | null = null;
      for (let attempt = 0; attempt < 900; attempt += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, 1000));
        const runResponse = await fetch(
          `${API_BASE}/api/v1/agent/runs/${encodeURIComponent(created.run_id)}`,
          { credentials: "include" }
        );
        const run = (await runResponse.json()) as AgentRun & { detail?: string };
        if (!runResponse.ok) throw new Error(run.detail || `任务查询失败: ${runResponse.status}`);
        setRunEvents(run.events ?? []);
        if (["completed", "failed", "cancelled"].includes(run.status)) {
          finished = run;
          break;
        }
      }
      if (!finished) throw new Error("Agent 任务等待超时，请检查任务状态。");
      if (finished.response?.messages?.length) {
        setMessages(finished.response.messages);
      }
      if (finished.status !== "completed" || finished.response?.error) {
        setError(
          finished.response?.reply ||
          finished.error_message ||
          (finished.status === "cancelled" ? "任务已取消。" : "智能体任务执行失败。")
        );
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "请求失败");
    } finally {
      setLoading(false);
      setRunId("");
    }
  }

  async function cancelRun() {
    if (!runId) return;
    await fetch(`${API_BASE}/api/v1/agent/runs/${encodeURIComponent(runId)}/cancel`, {
      method: "POST",
      credentials: "include"
    });
  }

  function resetConversation() {
    if (loading) return;
    setMessages([]);
    setError("");
    setRunEvents([]);
    const storageKey = storageKeyRef.current;
    if (storageKey) window.localStorage.removeItem(storageKey);
  }

  return (
      <main className="shell agent-shell">
        <header className="topbar">
          <div className="brand-block">
            <div className="brand-mark">
              <Bot size={22} />
            </div>
            <div>
              <p className="eyebrow">Agrisky AI · Controlled Claims Agent</p>
              <h1>穹野智保 Agent 助理</h1>
            </div>
          </div>
          <div className="top-status">
            <span className={`api-pill agent-health ${health?.status ?? "checking"}`}>
              <Activity size={14} />
              {health
                ? `${health.status === "ready" ? "Agent 就绪" : health.status === "degraded" ? "Agent 降级" : "Agent 不可用"} · ${health.model ?? "检测中"}`
                : "检测 Agent"}
            </span>
            {scoped ? <span className="api-pill">已登录投保人 · 仅可办理名下保单</span> : null}
            {role === "admin" ? <span className="api-pill">管理员 · 可分析全部案件</span> : null}
            <span className="api-pill">受控调度 · 到报告草稿止 · 归档需人工审核</span>
            <button className="icon-button" type="button" onClick={resetConversation} disabled={loading} title="新建对话">
              <RotateCcw size={15} />
            </button>
          </div>
        </header>

        <section className="agent-chat">
          <div className="chat-scroll" ref={scrollRef}>
            {messages.length === 0 ? (
              <div className="chat-empty">
                <Bot size={34} />
                <strong>读取在册边界，分析案件并驱动理赔辅助流水线</strong>
                <p>Agrisky AI 可调用案件库、保单边界、遥感证据、合规核验、赔付测算和报告工具；所有高风险判断保留来源字段，归档前必须人工审核。</p>
                <div className="chat-suggestions">
                  {suggestions.map((suggestion) => (
                    <button key={suggestion} type="button" onClick={() => send(suggestion)}>
                      {suggestion}
                    </button>
                  ))}
                </div>
              </div>
            ) : null}

            {messages.map((message, index) => {
              if (message.role === "tool") return null;
              if (message.role === "user") {
                return (
                  <div key={index} className="chat-row user">
                    <div className="chat-bubble user">{message.content}</div>
                    <div className="chat-avatar user"><User size={16} /></div>
                  </div>
                );
              }

              const calls = message.tool_calls ?? [];
              return (
                <div key={index} className="chat-row bot">
                  <div className="chat-avatar bot"><Bot size={16} /></div>
                  <div className="chat-bubble-group">
                    {calls.length ? (
                      <div className="tool-chips">
                        {calls.map((call, callIndex) => (
                          <span key={callIndex} className="tool-chip">
                            <Wrench size={12} />
                            {TOOL_LABELS[call.function?.name ?? ""] ?? call.function?.name ?? "工具调用"}
                          </span>
                        ))}
                      </div>
                    ) : null}
                    {message.content ? (
                      <div className="chat-bubble bot markdown">
                        <MarkdownReply content={message.content} />
                      </div>
                    ) : null}
                  </div>
                </div>
              );
            })}

            {loading ? (
              <div className="chat-row bot">
                <div className="chat-avatar bot"><Bot size={16} /></div>
                <div className="chat-bubble bot loading">
                  <Loader2 className="spin" size={15} />
                  <span>
                    {runEvents.length
                      ? TOOL_LABELS[
                          [...runEvents].reverse().find((event) => event.payload?.tool)?.payload?.tool ?? ""
                        ] ?? "智能体正在执行受控工具"
                      : "智能体正在理解任务"}
                  </span>
                  {runId ? (
                    <button className="icon-button" type="button" onClick={cancelRun} title="取消任务">
                      <Square size={13} />
                    </button>
                  ) : null}
                </div>
              </div>
            ) : null}
          </div>

          {error ? <div className="chat-error">{error}</div> : null}

          <form
            className="chat-input"
            onSubmit={(event) => {
              event.preventDefault();
              send(input);
            }}
          >
            <textarea
              value={input}
              placeholder="描述案件或直接下达指令，例如：保单 POL-2026-001，洪涝，2025-08-15，水稻，地块 PLOT-008"
              rows={2}
              disabled={loading}
              onChange={(event) => setInput(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  send(input);
                }
              }}
            />
            <button
              className="primary"
              type="submit"
              disabled={loading || !input.trim() || health?.status === "unavailable"}
            >
              {loading ? <Loader2 className="spin" size={17} /> : <SendHorizontal size={17} />}
              发送
            </button>
          </form>
        </section>
      </main>
  );
}
