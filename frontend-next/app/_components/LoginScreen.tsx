"use client";

import { ArrowRight, Bot, FileCheck2, KeyRound, LayoutDashboard, LayoutList, LogIn, MapPinned, Radar, UserRound } from "lucide-react";
import { FormEvent, useState } from "react";
import { fetchMe, loginWithPassword, logoutSession, notifyAuthChanged, type SessionMe } from "../_lib/auth";

type AuthSession = {
  me: SessionMe;
};

type LoginScreenProps = {
  defaultUsername?: string;
  title: string;
  subtitle: string;
  onAuthenticated: (session: AuthSession) => void | Promise<void>;
};

const ENTRY_POINTS = [
  { icon: LayoutDashboard, label: "理赔驾驶舱", note: "风险队列与任务闭环" },
  { icon: LayoutList, label: "案件队列", note: "风险优先级与分派" },
  { icon: MapPinned, label: "保单与地块", note: "边界与承保底册" },
  { icon: Radar, label: "遥感证据", note: "SAR 与 NDVI 证据链" },
  { icon: Bot, label: "Agent 助理", note: "受控调度理赔流程" },
  { icon: FileCheck2, label: "报告与审计", note: "人工审核与哈希追溯" }
];

export function LoginScreen({ defaultUsername = "admin", title, subtitle, onAuthenticated }: LoginScreenProps) {
  const [form, setForm] = useState({ username: defaultUsername, password: "" });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (loading) return;

    setError("");
    setLoading(true);
    let cookieCreated = false;
    try {
      await loginWithPassword(form.username.trim(), form.password);
      cookieCreated = true;
      const me = await fetchMe();
      if (!me) {
        throw new Error("会话已失效，请重新登录");
      }
      notifyAuthChanged();
      await onAuthenticated({ me });
    } catch (err) {
      if (cookieCreated) await logoutSession().catch(() => undefined);
      setError(err instanceof Error ? err.message : "登录失败");
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="auth-shell">
      <section className="auth-stage">
        <div className="auth-hero">
          <div className="auth-badge">Agrisky AI · 穹野智保</div>
          <h1>{title}</h1>
          <p className="auth-subtitle">{subtitle}</p>
          <div className="auth-feature-grid">
            {ENTRY_POINTS.map(({ icon: Icon, label, note }) => (
              <div className="auth-feature-card" key={label}>
                <Icon size={18} />
                <strong>{label}</strong>
                <span>{note}</span>
              </div>
            ))}
          </div>
          <p className="auth-note">
            农业保险灾后理赔以可追溯证据为基础：Agent 负责分析与流程协同，归档和最终理赔仍需具名人工审核。
          </p>
        </div>

        <aside className="auth-card">
          <div className="auth-card-head">
            <p className="eyebrow">Sign In</p>
            <h2>穹野智保登录</h2>
            <span>管理员进入运营工作台，投保人账号进入名下保单门户</span>
          </div>

          {error ? <div className="error-box">{error}</div> : null}

          <form className="form-stack auth-form" onSubmit={submit}>
            <label className="field">
              <span>账号</span>
              <div className="auth-input-wrap">
                <UserRound size={16} />
                <input
                  value={form.username}
                  onChange={(event) => setForm((current) => ({ ...current, username: event.target.value }))}
                  placeholder="请输入账号"
                />
              </div>
            </label>

            <label className="field">
              <span>密码</span>
              <div className="auth-input-wrap">
                <KeyRound size={16} />
                <input
                  type="password"
                  value={form.password}
                  onChange={(event) => setForm((current) => ({ ...current, password: event.target.value }))}
                  placeholder="请输入密码"
                />
              </div>
            </label>

            <button className="primary auth-submit" type="submit" disabled={loading}>
              <LogIn size={17} />
              <span>{loading ? "登录中" : "进入系统"}</span>
              <ArrowRight size={16} />
            </button>
          </form>

        </aside>
      </section>
    </main>
  );
}
