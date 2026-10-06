"use client";

import { Clock3, Globe2, LocateFixed, Map, Orbit } from "lucide-react";
import { useState } from "react";
import { assetUrl } from "../../_lib/api-base";
import styles from "./EvidenceViewer.module.css";

type SatelliteEvidence = {
  thumbnail_url?: string | null;
  s2_thumbnail_url?: string | null;
  thumbnail_integrity?: Record<string, { provenance?: string }>;
};

type GrowthEvidence = {
  outputs: Record<string, string | null | undefined>;
};

export type EvidenceTelemetry = {
  center: string;
  lonRange: string;
  latRange: string;
  acquisition: string;
  sceneId: string;
};

export type EvidenceLayer = "optical" | "sar" | "growth";

type EvidenceViewerProps = {
  satellite: SatelliteEvidence | null;
  growth: GrowthEvidence | null;
  layer: EvidenceLayer;
  telemetry: EvidenceTelemetry;
};

function runtimeMapUrl(path?: string | null) {
  const source = assetUrl(path);
  if (!source) return null;

  const caseArtifact = source.match(
    /^(.*\/api\/v1\/cases\/[^/]+\/evidence\/growth\/[^/]+)\/[^/?#]+\.html([?#].*)?$/i
  );
  if (caseArtifact) return `${caseArtifact[1]}/map-runtime.html${caseArtifact[2] ?? ""}`;

  const standaloneArtifact = source.match(
    /^(.*\/api\/v1\/tools\/growth_analysis\/[^/]+)\/artifacts\/[^/?#]+\.html([?#].*)?$/i
  );
  if (standaloneArtifact) return `${standaloneArtifact[1]}/map-runtime.html${standaloneArtifact[2] ?? ""}`;

  return source;
}

export default function EvidenceViewer({ satellite, growth, layer, telemetry }: EvidenceViewerProps) {
  const [imageAspect, setImageAspect] = useState<number | null>(null);
  const opticalSrc = assetUrl(satellite?.s2_thumbnail_url ?? satellite?.thumbnail_url);
  const sarSrc = assetUrl(satellite?.thumbnail_url ?? satellite?.s2_thumbnail_url);
  const mapSrc = runtimeMapUrl(growth?.outputs.map_html);
  const previewSrc = assetUrl(growth?.outputs.class_preview_png ?? growth?.outputs.ndvi_preview_png);

  const showGrowth = layer === "growth";
  const iframeSrc = showGrowth ? mapSrc : null;
  const imageSrc = showGrowth ? (mapSrc ? null : previewSrc) : layer === "sar" ? sarSrc : opticalSrc;
  const hasContent = Boolean(iframeSrc || imageSrc);
  // 影像模式：画布高度贴合影像宽高比，contain 完整呈现承保区域；
  // 地图（iframe）模式：画布纵向填满列高。
  const imageMode = Boolean(imageSrc) && !iframeSrc;
  const showDecor = !iframeSrc;
  const selectedThumbnailField = layer === "sar"
    ? (satellite?.thumbnail_url ? "thumbnail_url" : "s2_thumbnail_url")
    : (satellite?.s2_thumbnail_url ? "s2_thumbnail_url" : "thumbnail_url");
  const contextOnly = satellite?.thumbnail_integrity?.[selectedThumbnailField]?.provenance
    === "context_only_fallback_not_observation_evidence";
  const layerLabel = showGrowth
    ? "长势分级图"
    : contextOnly
      ? "上下文底图（非观测证据）"
      : layer === "sar"
        ? "SAR 微波证据"
        : "光学影像证据";
  const emptyTitle = showGrowth ? "等待证据图层" : layer === "sar" ? "等待 SAR 证据" : "等待光学证据";
  const emptyHint = showGrowth
    ? "卫星初筛或长势评估完成后显示本案图层。"
    : layer === "sar"
      ? "SAR 微波证据将在卫星初筛完成后显示。"
      : "光学影像证据将在卫星初筛完成后显示。";

  return (
    <div
      className={`${styles.canvas} ${imageMode ? styles.imageMode : ""}`}
      data-evidence-layer={layer}
      style={imageMode && imageAspect ? { aspectRatio: `${imageAspect}` } : undefined}
    >
      {iframeSrc ? (
        <iframe
          className={styles.frame}
          src={iframeSrc}
          title="长势分级图"
          sandbox="allow-scripts"
          referrerPolicy="no-referrer"
          loading="lazy"
        />
      ) : null}
      {!iframeSrc && imageSrc ? (
        // The evidence URLs are authenticated runtime assets and cannot use Next image optimization.
        // key 随图源变化重挂载，onLoad 读取真实宽高比驱动画布贴合。
        <img
          key={imageSrc}
          className={styles.image}
          src={imageSrc}
          alt="遥感证据预览"
          onLoad={(event) => {
            const target = event.currentTarget;
            if (target.naturalWidth > 0 && target.naturalHeight > 0) {
              setImageAspect(target.naturalWidth / target.naturalHeight);
            }
          }}
        />
      ) : null}
      {showDecor ? (
        <>
          <div className={styles.hud} aria-label="证据图层时空信息">
            <span><LocateFixed size={13} />{telemetry.center}</span>
            <span><Clock3 size={13} />{telemetry.acquisition}</span>
            <span><Globe2 size={13} />{layerLabel}</span>
            <span><Orbit size={13} />{telemetry.sceneId}</span>
          </div>
          <div className={`${styles.edge} ${styles.edgeTop}`}>{telemetry.lonRange}</div>
          <div className={`${styles.edge} ${styles.edgeLeft}`}>{telemetry.latRange}</div>
          <div className={styles.orbit} aria-hidden="true"><span /></div>
        </>
      ) : null}
      {!hasContent ? (
        <div className={styles.empty}>
          <div className={styles.fieldOutline}><div /><span /></div>
          <Map size={30} />
          <strong>{emptyTitle}</strong>
          <span>{emptyHint}</span>
        </div>
      ) : null}
    </div>
  );
}
