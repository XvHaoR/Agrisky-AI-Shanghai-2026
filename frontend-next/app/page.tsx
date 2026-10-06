"use client";

import {
  Activity,
  BarChart3,
  CalendarDays,
  Database,
  Download,
  FileSpreadsheet,
  FileText,
  Layers3,
  Loader2,
  Map,
  Ruler,
  Satellite,
  SlidersHorizontal,
  Sparkles,
  UploadCloud
} from "lucide-react";
import { FormEvent, useEffect, useMemo, useState } from "react";
import { AdminGate } from "./_components/AdminGate";
import { getApiBase } from "./_lib/api-base";

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
  valid_pixel_count: number;
  class_breaks: number[];
  raster: Record<string, unknown>;
  summary: GrowthSummary[];
  outputs: Record<string, string | null | undefined>;
  message: string;
};

const API_BASE = getApiBase();

function assetUrl(path?: string | null) {
  if (!path) return null;
  if (path.startsWith("http://") || path.startsWith("https://")) return path;
  return `${API_BASE}${path}`;
}

function pct(value: number) {
  return `${(value * 100).toFixed(2)}%`;
}

function area(value: number) {
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(value);
}

const GROWTH_STORAGE_KEY = "agrisky.growth.workspace.v3";

export default function Page() {
  return (
    <AdminGate>
      <GrowthWorkspace />
    </AdminGate>
  );
}

function GrowthWorkspace() {
  const [boundaryFile, setBoundaryFile] = useState<File | null>(null);
  const [totalAreaMu, setTotalAreaMu] = useState("");
  const [cropLabel, setCropLabel] = useState("玉米");
  const [insurer, setInsurer] = useState("示例农业合作社（合成演示）");
  const [boundaryCrs, setBoundaryCrs] = useState("");
  const [method, setMethod] = useState("jenks");
  const [nClasses, setNClasses] = useState("5");
  const [startDate, setStartDate] = useState("2025-08-30");
  const [endDate, setEndDate] = useState("2025-09-15");
  const [ndviSource, setNdviSource] = useState("auto");
  const [result, setResult] = useState<GrowthResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [activePreview, setActivePreview] = useState<"class" | "ndvi">("class");
  const [ready, setReady] = useState(false);

  // 从 localStorage 恢复结果
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const taskId = params.get("task_id") || params.get("task");
    if (taskId) {
      setReady(true);
      return;
    }

    const saved = localStorage.getItem(GROWTH_STORAGE_KEY);
    if (saved) {
      try {
        const parsed = JSON.parse(saved) as { result?: GrowthResult; activePreview?: "class" | "ndvi" };
        if (parsed.result) setResult(parsed.result);
        if (parsed.activePreview) setActivePreview(parsed.activePreview);
      } catch {
        localStorage.removeItem(GROWTH_STORAGE_KEY);
      }
    }
    setReady(true);
  }, []);

  // 保存结果到 localStorage
  useEffect(() => {
    if (!ready) return;
    if (result) {
      localStorage.setItem(GROWTH_STORAGE_KEY, JSON.stringify({ result, activePreview }));
    } else {
      localStorage.removeItem(GROWTH_STORAGE_KEY);
    }
  }, [result, activePreview, ready]);

  const summary = useMemo(() => result?.summary ?? [], [result?.summary]);
  const metric = useMemo(() => {
    const good = summary.filter((row) => row.value >= 4).reduce((sum, row) => sum + row.area_mu, 0);
    const weak = summary.filter((row) => row.value <= 2).reduce((sum, row) => sum + row.area_mu, 0);
    const dominant = [...summary].sort((a, b) => b.area_mu - a.area_mu)[0];
    return { good, weak, dominant };
  }, [summary]);

  const previewUrl = assetUrl(
    activePreview === "class" ? result?.outputs.class_preview_png : result?.outputs.ndvi_preview_png
  );
  const mapUrl = assetUrl(result?.outputs.map_html);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const taskId = params.get("task_id") || params.get("task");
    if (!taskId) return;

    let cancelled = false;
    setLoading(true);
    setError("");
    fetch(`${API_BASE}/api/v1/tools/growth_analysis/${taskId}`, { credentials: "include" })
      .then(async (response) => {
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) {
          const detail = typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload.detail ?? payload);
          throw new Error(detail || `请求失败: ${response.status}`);
        }
        if (!cancelled) {
          setResult(payload as GrowthResult);
          setActivePreview("class");
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "任务结果加载失败");
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, []);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");

    if (!boundaryFile) {
      setError("请上传地块边界文件，支持单独 .shp、GeoJSON、GPKG、KML 或 SHP zip。");
      return;
    }

    const formData = new FormData();
    formData.append("boundary_file", boundaryFile);
    if (totalAreaMu.trim()) formData.append("total_area_mu", totalAreaMu);
    formData.append("crop_label", cropLabel);
    formData.append("insurer", insurer);
    formData.append("boundary_crs", boundaryCrs);
    formData.append("method", method);
    formData.append("n_classes", nClasses);
    formData.append("start_date", startDate);
    formData.append("end_date", endDate);
    formData.append("ndvi_source", ndviSource);

    setLoading(true);
    try {
      const response = await fetch(`${API_BASE}/api/v1/tools/run_growth_analysis_boundary_upload`, {
        method: "POST",
        credentials: "include",
        body: formData
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        const detail = typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload.detail ?? payload);
        throw new Error(detail || `请求失败: ${response.status}`);
      }
      const nextResult = payload as GrowthResult;
      setResult(nextResult);
      setActivePreview("class");
      window.history.replaceState(null, "", `?task_id=${nextResult.task_id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "长势分析失败");
    } finally {
      setLoading(false);
    }
  }

  if (!ready) return null;

  return (
    <main className="shell growth-shell">
      <header className="topbar">
        <div className="brand-block">
          <div className="brand-mark">
            <Satellite size={22} />
          </div>
          <div>
              <p className="eyebrow">Agrisky AI · Evidence Console</p>
              <h1>农业保险遥感证据工作台</h1>
          </div>
        </div>
        <div className="top-status">
          <div className="api-pill">
            <Activity size={16} />
            <span>{API_BASE.replace(/^https?:\/\//, "")}</span>
          </div>
          <div className="api-pill muted-pill">
            <Database size={16} />
            <span>{result?.raster.crs ? String(result.raster.crs) : "坐标系待定"}</span>
          </div>
        </div>
      </header>

      <section className="workspace">
        <aside className="control-panel growth-control-panel">
          <form onSubmit={submit} className="form-stack">
            <div className="panel-heading">
              <div>
                <p className="eyebrow">Evidence Intake</p>
                <h2>地块与影像任务输入</h2>
              </div>
              <UploadCloud size={20} />
            </div>

            <label className="file-drop">
              <input
                type="file"
                accept=".shp,.geojson,.json,.gpkg,.kml,.zip"
                onChange={(event) => setBoundaryFile(event.target.files?.[0] ?? null)}
              />
              <Layers3 size={18} />
              <span>{boundaryFile ? boundaryFile.name : "上传地块边界 SHP / GeoJSON / GPKG / ZIP"}</span>
            </label>

            <div className="field">
              <label>投保人</label>
              <input value={insurer} onChange={(event) => setInsurer(event.target.value)} />
            </div>

            <div className="field-grid">
              <div className="field">
                <label>作物</label>
                <input value={cropLabel} onChange={(event) => setCropLabel(event.target.value)} />
              </div>
              <div className="field">
                <label>总面积（亩，可自动）</label>
                <input
                  type="number"
                  min="0"
                  step="0.01"
                  placeholder="留空按边界计算"
                  value={totalAreaMu}
                  onChange={(event) => setTotalAreaMu(event.target.value)}
                />
              </div>
            </div>

            <div className="field-grid">
              <div className="field">
                <label>开始日期</label>
                <input type="date" value={startDate} onChange={(event) => setStartDate(event.target.value)} />
              </div>
              <div className="field">
                <label>结束日期</label>
                <input type="date" value={endDate} onChange={(event) => setEndDate(event.target.value)} />
              </div>
            </div>

            <div className="field">
              <label>边界 CRS（无 .prj 时）</label>
              <input
                placeholder="可留空，例：EPSG:4526"
                value={boundaryCrs}
                onChange={(event) => setBoundaryCrs(event.target.value)}
              />
            </div>

            <div className="field-grid">
              <div className="field">
                <label>影像来源</label>
                <select value={ndviSource} onChange={(event) => setNdviSource(event.target.value)}>
                  <option value="auto">自动获取</option>
                  <option value="gee">GEE Sentinel-2</option>
                  <option value="synthetic">本地演示</option>
                </select>
              </div>
              <div className="field">
                <label>分级方法</label>
                <select value={method} onChange={(event) => setMethod(event.target.value)}>
                  <option value="jenks">Jenks 自然断点</option>
                  <option value="quantile">分位数</option>
                  <option value="equalinterval">等距</option>
                  <option value="std">标准差</option>
                </select>
              </div>
            </div>

            <div className="field-grid">
              <div className="field">
                <label>等级数</label>
                <input
                  type="number"
                  min="2"
                  max="9"
                  value={nClasses}
                  onChange={(event) => setNClasses(event.target.value)}
                />
              </div>
              <div className="field">
                <label>云量阈值</label>
                <input value="30%" disabled readOnly />
              </div>
            </div>

            {error ? <div className="error-box">{error}</div> : null}

            <div className="action-row">
              <button className="primary" type="submit" disabled={loading}>
                {loading ? <Loader2 className="spin" size={18} /> : <Sparkles size={18} />}
                <span>{loading ? "自动解译中" : "一键自动解译"}</span>
              </button>
              <button
                className="secondary"
                type="button"
                onClick={() => {
                  setResult(null);
                  setError("");
                  localStorage.removeItem(GROWTH_STORAGE_KEY);
                  window.history.replaceState(null, "", window.location.pathname);
                }}
              >
                <span>清空</span>
              </button>
            </div>
          </form>

          <div className="sidebar-section">
            <div className="panel-heading compact-heading">
              <div>
                <p className="eyebrow">Traceable Evidence</p>
                <h2>证据判读指标</h2>
              </div>
              <BarChart3 size={18} />
            </div>
            <div className="metrics-grid">
              <Metric label="优良面积" value={`${area(metric.good)} 亩`} tone="good" />
              <Metric label="弱势面积" value={`${area(metric.weak)} 亩`} tone="risk" />
              <Metric label="主导等级" value={metric.dominant ? `${metric.dominant.label} ${pct(metric.dominant.ratio)}` : "-"} />
              <Metric label="总面积" value={result ? `${area(result.total_area_mu)} 亩` : "-"} />
            </div>
          </div>

          <div className="sidebar-section">
            <div className="download-row">
              <DownloadLink href={assetUrl(result?.outputs.auto_ndvi_tif)} icon={<Download size={16} />} label="NDVI" />
              <DownloadLink href={assetUrl(result?.outputs.summary_csv)} icon={<FileSpreadsheet size={16} />} label="CSV" />
              <DownloadLink href={assetUrl(result?.outputs.report_docx)} icon={<FileText size={16} />} label="DOCX" />
              <DownloadLink href={assetUrl(result?.outputs.classified_tif)} icon={<Download size={16} />} label="TIF" />
            </div>
          </div>
        </aside>

        <section className="result-panel growth-result-panel">
          <div className="scene-strip">
            <SceneItem icon={<Map size={16} />} label="任务ID" value={result?.task_id ?? "-"} />
            <SceneItem icon={<SlidersHorizontal size={16} />} label="分类器" value={`${result?.method ?? "-"} / ${result?.n_classes ?? "-"} 类`} />
            <SceneItem icon={<Ruler size={16} />} label="栅格" value={result?.raster.width ? `${result.raster.width} × ${result.raster.height}` : "待处理"} />
            <SceneItem icon={<CalendarDays size={16} />} label="NDVI来源" value={result?.raster.ndvi_source_label ? String(result.raster.ndvi_source_label) : "-"} />
          </div>

          <section className="preview-panel growth-map-panel">
              <div className="section-head">
                <div>
                  <p className="eyebrow">Geemap Display</p>
                  <h2>交互式遥感解译地图</h2>
                </div>
                <div className="segmented">
                  <button
                    className={activePreview === "class" ? "active" : ""}
                    type="button"
                    onClick={() => setActivePreview("class")}
                  >
                    地图
                  </button>
                  <button
                    className={activePreview === "ndvi" ? "active" : ""}
                    type="button"
                    onClick={() => setActivePreview("ndvi")}
                  >
                    预览
                  </button>
                </div>
              </div>
              <div className="raster-frame">
                {mapUrl && activePreview === "class" ? (
                  <iframe
                    className="map-iframe"
                    src={mapUrl}
                    title="geemap style map"
                    sandbox="allow-scripts"
                    referrerPolicy="no-referrer"
                    loading="lazy"
                  />
                ) : previewUrl ? (
                  <img src={previewUrl} alt="NDVI preview" />
                ) : (
                  <EmptyMapState />
                )}
              </div>
            </section>

          <section className="table-panel growth-stats-panel">
            <div className="section-head">
              <div>
                <p className="eyebrow">Analysis Results</p>
                <h2>解译统计结果</h2>
              </div>
            </div>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>等级</th>
                    <th>标签</th>
                    <th>面积（亩）</th>
                    <th>比例</th>
                    <th>像元数</th>
                    <th>阈值上界</th>
                  </tr>
                </thead>
                <tbody>
                  {summary
                    .slice()
                    .sort((a, b) => b.value - a.value)
                    .map((row) => (
                      <tr key={row.value}>
                        <td>
                          <span className="legend-dot" style={{ background: row.color }} />
                          {row.value}
                        </td>
                        <td>{row.label}</td>
                        <td>{area(row.area_mu)}</td>
                        <td>{pct(row.ratio)}</td>
                        <td>{area(row.count)}</td>
                        <td>{result?.class_breaks[row.value - 1]?.toFixed(4) ?? "-"}</td>
                      </tr>
                    ))}
                </tbody>
              </table>
            </div>
          </section>
        </section>
      </section>
    </main>
  );
}

function Metric({ label, value, tone }: { label: string; value: string; tone?: "good" | "risk" }) {
  return (
    <div className={`metric ${tone ?? ""}`}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function SceneItem({ icon, label, value }: { icon: React.ReactNode; label: string; value: string }) {
  return (
    <div className="scene-item">
      {icon}
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function DownloadLink({ href, icon, label }: { href: string | null; icon: React.ReactNode; label: string }) {
  if (!href) {
    return (
      <span className="download disabled">
        {icon}
        {label}
      </span>
    );
  }
  return (
    <a className="download" href={href} target="_blank" rel="noreferrer">
      {icon}
      {label}
    </a>
  );
}

function EmptyMapState() {
  return (
    <div className="empty-map-state">
      <Map size={28} />
      <strong>等待真实解译结果</strong>
      <span>上传边界并执行计算后，这里只显示本次任务生成的 NDVI 和分级图层。</span>
    </div>
  );
}
