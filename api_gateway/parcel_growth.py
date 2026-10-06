"""Per-feature and per-parcel NDVI aggregation.

This module deliberately has no API/database side effects.  The caller first
preflights and maps uploaded parcel files to stable ``parcel_id`` and
``feature_id`` values, then passes the resulting manifest and an NDVI raster to
``analyze_parcel_growth``.  A bulk KML archive must not be fed into this module
directly.

The result has three levels:

* ``features`` preserves every source feature and both stable identifiers;
* ``parcels`` unions features sharing a parcel id before counting pixels;
* ``overall`` unions every feature, so overlaps are not double-counted.

Rasterio and NumPy are imported only by the raster entry point.  Manifest
normalisation, canonical hashing, and statistics assembly remain independently
testable without Rasterio.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

try:  # Reuse the established classifier when the API service layout is active.
    from growth_analysis import (
        FIXED_NDVI_BREAKS_5,
        summarize_classes,
        validate_ndvi_stats,
    )
except ImportError:  # pragma: no cover - standalone/package use without service extras
    FIXED_NDVI_BREAKS_5 = [0.30, 0.45, 0.60, 0.75, 1.00]
    _LEVEL_LABELS_5 = {1: "差", 2: "一般", 3: "中", 4: "良", 5: "优"}
    _LEVEL_COLORS_5 = {
        1: "#d64545",
        2: "#ef8f35",
        3: "#e4c441",
        4: "#8cc152",
        5: "#2f9e44",
    }

    def summarize_classes(
        count_map: dict[int, int],
        total_area_mu: float,
        n_classes: int,
    ) -> list[dict[str, Any]]:
        valid_total = sum(count for value, count in count_map.items() if value > 0)
        rows: list[dict[str, Any]] = []
        for value in range(1, n_classes + 1):
            count = int(count_map.get(value, 0))
            ratio = count / valid_total if valid_total else 0.0
            rows.append(
                {
                    "value": value,
                    "label": (
                        _LEVEL_LABELS_5.get(value, f"等级 {value}")
                        if n_classes == 5
                        else f"等级 {value}"
                    ),
                    "count": count,
                    "ratio": round(ratio, 6),
                    "area_mu": round(ratio * float(total_area_mu), 2),
                    "color": (
                        _LEVEL_COLORS_5.get(value, "#6b7280")
                        if n_classes == 5
                        else "#6b7280"
                    ),
                }
            )
        return rows

    def validate_ndvi_stats(stats: dict[str, Any], *, tolerance: float = 1e-4) -> None:
        minimum = stats.get("min")
        maximum = stats.get("max")
        if minimum is None or maximum is None:
            raise ValueError("NDVI 有效像元为空，无法校验物理范围")
        if float(minimum) < -1.0 - tolerance or float(maximum) > 1.0 + tolerance:
            raise ValueError(
                f"NDVI 栅格值范围为 [{float(minimum):.6g}, {float(maximum):.6g}]，"
                "超出 [-1, 1]"
            )


try:  # Keep intake and downstream validation on one version/scheme contract.
    from parcel_preflight import (
        BOUNDARY_HASH_SCHEME,
        PREFLIGHT_ALGORITHM_VERSION,
        PREFLIGHT_SCHEMA_VERSION,
        PROPOSED_ID_SCHEME,
    )
except ImportError:  # pragma: no cover - package import path
    from .parcel_preflight import (
        BOUNDARY_HASH_SCHEME,
        PREFLIGHT_ALGORITHM_VERSION,
        PREFLIGHT_SCHEMA_VERSION,
        PROPOSED_ID_SCHEME,
    )


PARCEL_GROWTH_SCHEMA_VERSION = "parcel_growth_v1"
PARCEL_GROWTH_ALGORITHM_VERSION = "zonal_ndvi_union_v1"
CLASSIFICATION_SCHEME_VERSION = "ndvi_upper_bound_digitize_v1"
GEOJSON_MANIFEST_SCHEMA_VERSION = "geojson_feature_collection_v1"
SUPPORTED_MANIFEST_SCHEMA_VERSIONS = frozenset({PREFLIGHT_SCHEMA_VERSION})
SUPPORTED_PREFLIGHT_ALGORITHM_VERSIONS = frozenset({PREFLIGHT_ALGORITHM_VERSION})
SUPPORTED_BOUNDARY_HASH_SCHEMES = frozenset({BOUNDARY_HASH_SCHEME})
SUPPORTED_ID_SCHEMES = frozenset({PROPOSED_ID_SCHEME})
DEFAULT_VALID_COVERAGE_THRESHOLD = 0.70
DEFAULT_LOW_MEAN_NDVI_THRESHOLD = 0.30
DEFAULT_POOR_GROWTH_RATIO_THRESHOLD = 0.50
_AREA_SQM_TO_MU = 0.0015
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _read_manifest(manifest: Mapping[str, Any] | str | Path) -> dict[str, Any]:
    if isinstance(manifest, Mapping):
        return dict(manifest)
    path = Path(manifest)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取地块清单 JSON: {path}") from exc
    if not isinstance(loaded, dict):
        raise ValueError("地块清单顶层必须是 JSON 对象")
    return loaded


def _validate_sha256(value: Any, field_name: str, *, required: bool = False) -> str | None:
    if value in (None, ""):
        if required:
            raise ValueError(f"{field_name} 必须是 64 位小写十六进制 SHA-256")
        return None
    digest = str(value)
    if _SHA256_RE.fullmatch(digest) is None:
        raise ValueError(f"{field_name} 必须是 64 位小写十六进制 SHA-256")
    return digest


def _validate_supported_value(
    value: Any,
    field_name: str,
    supported: frozenset[str],
    *,
    required: bool = False,
) -> str | None:
    if value in (None, ""):
        if required:
            raise ValueError(f"地块清单缺少 {field_name}")
        return None
    candidate = str(value)
    if candidate not in supported:
        raise ValueError(
            f"不支持的 {field_name}: {candidate}；支持值为 {', '.join(sorted(supported))}"
        )
    return candidate


def _validate_geometry_metadata(entry: Mapping[str, Any], label: str) -> None:
    digest = _validate_sha256(entry.get("geometry_sha256"), f"{label}.geometry_sha256")
    scheme = _validate_supported_value(
        entry.get("geometry_hash_scheme"),
        f"{label}.geometry_hash_scheme",
        SUPPORTED_BOUNDARY_HASH_SCHEMES,
    )
    if (digest is None) != (scheme is None):
        raise ValueError(f"{label} 的 geometry_sha256 与 geometry_hash_scheme 必须同时提供")
    _validate_sha256(entry.get("source_file_sha256"), f"{label}.source_file_sha256")


def _validate_manifest_contract(data: Mapping[str, Any], *, require_confirmed: bool) -> str:
    """Reject version/scheme confusion before normalising untrusted identifiers."""

    if data.get("type") == "FeatureCollection":
        schema_version = data.get("schema_version")
        if schema_version not in (None, GEOJSON_MANIFEST_SCHEMA_VERSION):
            raise ValueError(
                f"不支持的 GeoJSON schema_version: {schema_version}；"
                f"支持值为 {GEOJSON_MANIFEST_SCHEMA_VERSION} 或不声明"
            )
        _validate_sha256(data.get("manifest_sha256"), "manifest_sha256")
        _validate_supported_value(
            data.get("geometry_hash_scheme"),
            "geometry_hash_scheme",
            SUPPORTED_BOUNDARY_HASH_SCHEMES,
        )
        return "geojson_feature_collection"

    if not isinstance(data.get("parcels"), list):
        return "unknown"

    _validate_supported_value(
        data.get("schema_version"),
        "schema_version",
        SUPPORTED_MANIFEST_SCHEMA_VERSIONS,
        required=True,
    )
    _validate_sha256(data.get("manifest_sha256"), "manifest_sha256", required=True)
    _validate_supported_value(
        data.get("algorithm_version"),
        "algorithm_version",
        SUPPORTED_PREFLIGHT_ALGORITHM_VERSIONS,
        required=require_confirmed,
    )
    _validate_supported_value(
        data.get("id_scheme"),
        "id_scheme",
        SUPPORTED_ID_SCHEMES,
        required=require_confirmed,
    )
    _validate_supported_value(
        data.get("geometry_hash_scheme"),
        "geometry_hash_scheme",
        SUPPORTED_BOUNDARY_HASH_SCHEMES,
        required=require_confirmed,
    )
    source = data.get("source")
    if isinstance(source, Mapping):
        _validate_sha256(source.get("archive_sha256"), "source.archive_sha256")

    if require_confirmed:
        if data.get("preflight_status") != "human_confirmed":
            raise ValueError("分地块长势分析仅接受 preflight_status=human_confirmed 的清单")
        for index, parcel in enumerate(data["parcels"], start=1):
            if not isinstance(parcel, Mapping):
                raise ValueError(f"第 {index} 个 parcel 不是对象")
            mapping = parcel.get("mapping")
            if not isinstance(mapping, Mapping) or mapping.get("confirmed") is not True:
                raise ValueError(f"第 {index} 个 parcel 缺少人工确认映射")
            for field_name in ("policy_id", "policy_version_id"):
                if not str(mapping.get(field_name) or "").strip():
                    raise ValueError(f"第 {index} 个 parcel 映射缺少 {field_name}")
    return "parcel_preflight"


def _clean_id(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _positive_area(value: Any, field_name: str) -> float | None:
    if value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} 必须是正数") from exc
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{field_name} 必须是正数")
    return result


def _geometry_from_entry(entry: Mapping[str, Any]) -> dict[str, Any] | None:
    geometry = entry.get("geometry")
    if isinstance(geometry, Mapping):
        return dict(geometry)
    if entry.get("type") in {"Polygon", "MultiPolygon"}:
        return dict(entry)
    return None


def _manifest_source_crs(data: Mapping[str, Any]) -> str | None:
    explicit = _clean_id(data.get("source_crs"))
    if explicit:
        return explicit
    legacy_crs = data.get("crs")
    if isinstance(legacy_crs, str):
        return _clean_id(legacy_crs)
    if isinstance(legacy_crs, Mapping):
        properties = legacy_crs.get("properties")
        if isinstance(properties, Mapping):
            return _clean_id(properties.get("name"))
    return None


def normalize_parcel_manifest(
    manifest: Mapping[str, Any] | str | Path,
    *,
    require_confirmed: bool = False,
) -> dict[str, Any]:
    """Normalise GeoJSON or a parcel manifest into stable feature records.

    Supported inputs are a GeoJSON ``FeatureCollection`` and a manifest with a
    ``parcels`` list.  A parcel entry may contain one ``geometry`` or a
    ``features`` list.  Parent-level ``area_mu`` applies to the parcel aggregate;
    feature area is only inherited when the parcel has a single feature.  Set
    ``require_confirmed`` for authoritative analysis so a raw preflight manifest
    cannot bypass the policy-version mapping and human-confirmation stage.
    """

    data = _read_manifest(manifest)
    manifest_type = _validate_manifest_contract(data, require_confirmed=require_confirmed)
    source_crs = _manifest_source_crs(data) or "EPSG:4326"
    records: list[dict[str, Any]] = []

    if data.get("type") == "FeatureCollection":
        raw_features = data.get("features")
        if not isinstance(raw_features, list):
            raise ValueError("FeatureCollection.features 必须是数组")
        for index, raw in enumerate(raw_features, start=1):
            if not isinstance(raw, Mapping) or raw.get("type") != "Feature":
                raise ValueError(f"第 {index} 个要素不是有效的 GeoJSON Feature")
            _validate_geometry_metadata(raw, f"features[{index - 1}]")
            properties = raw.get("properties") if isinstance(raw.get("properties"), Mapping) else {}
            geometry = _geometry_from_entry(raw)
            parcel_id = (
                _clean_id(properties.get("parcel_id"))
                or _clean_id(properties.get("plot_id"))
                or _clean_id(properties.get("field_id"))
                or _clean_id(properties.get("地块编号"))
                or _clean_id(raw.get("parcel_id"))
            )
            feature_id = (
                _clean_id(properties.get("feature_id"))
                or _clean_id(raw.get("feature_id"))
                or _clean_id(raw.get("id"))
            )
            if not parcel_id:
                parcel_id = feature_id or f"parcel-{index:04d}"
            if not feature_id:
                feature_id = f"feature-{index:04d}"
            records.append(
                {
                    "parcel_id": parcel_id,
                    "feature_id": feature_id,
                    "geometry": geometry,
                    "declared_area_mu": _positive_area(
                        properties.get("area_mu", properties.get("insured_area_mu")),
                        f"feature {feature_id} area_mu",
                    ),
                    "parcel_declared_area_mu": None,
                    "properties": dict(properties),
                    "source_index": index - 1,
                    "source_feature_index": raw.get("source_feature_index", index),
                    "source_file": raw.get("source_file") or properties.get("source_file"),
                    "mapping": dict(raw.get("mapping")) if isinstance(raw.get("mapping"), Mapping) else {},
                }
            )
    elif isinstance(data.get("parcels"), list):
        source_index = 0
        for parcel_index, raw_parcel in enumerate(data["parcels"], start=1):
            if not isinstance(raw_parcel, Mapping):
                raise ValueError(f"第 {parcel_index} 个 parcel 不是对象")
            _validate_geometry_metadata(raw_parcel, f"parcels[{parcel_index - 1}]")
            parcel_id = _clean_id(raw_parcel.get("parcel_id")) or f"parcel-{parcel_index:04d}"
            parcel_area = _positive_area(
                raw_parcel.get("area_mu", raw_parcel.get("insured_area_mu")),
                f"parcel {parcel_id} area_mu",
            )
            child_features = raw_parcel.get("features")
            if child_features is None:
                child_features = [raw_parcel]
            if not isinstance(child_features, list) or not child_features:
                raise ValueError(f"parcel {parcel_id} 没有可分析的 features")
            for child_index, raw_feature in enumerate(child_features, start=1):
                if not isinstance(raw_feature, Mapping):
                    raise ValueError(f"parcel {parcel_id} 的第 {child_index} 个 feature 不是对象")
                _validate_geometry_metadata(
                    raw_feature,
                    f"parcels[{parcel_index - 1}].features[{child_index - 1}]",
                )
                properties = (
                    raw_feature.get("properties")
                    if isinstance(raw_feature.get("properties"), Mapping)
                    else {}
                )
                feature_id = (
                    _clean_id(raw_feature.get("feature_id"))
                    or _clean_id(raw_feature.get("id"))
                    or _clean_id(properties.get("feature_id"))
                    or f"{parcel_id}-feature-{child_index:04d}"
                )
                feature_area = _positive_area(
                    raw_feature.get(
                        "area_mu",
                        properties.get("area_mu", properties.get("insured_area_mu")),
                    ),
                    f"feature {feature_id} area_mu",
                )
                if feature_area is None and len(child_features) == 1:
                    feature_area = parcel_area
                records.append(
                    {
                        "parcel_id": parcel_id,
                        "feature_id": feature_id,
                        "geometry": _geometry_from_entry(raw_feature),
                        "declared_area_mu": feature_area,
                        "parcel_declared_area_mu": parcel_area,
                        "properties": dict(properties),
                        "source_index": source_index,
                        "source_feature_index": raw_feature.get("source_feature_index", child_index),
                        "source_file": raw_parcel.get("source_file"),
                        "mapping": (
                            dict(raw_parcel.get("mapping"))
                            if isinstance(raw_parcel.get("mapping"), Mapping)
                            else {}
                        ),
                    }
                )
                source_index += 1
    else:
        raise ValueError("仅支持 GeoJSON FeatureCollection 或包含 parcels 数组的地块清单")

    if not records:
        raise ValueError("地块清单没有可分析的要素")
    seen_feature_ids: set[str] = set()
    parcel_area_values: dict[str, set[float]] = defaultdict(set)
    for record in records:
        feature_id = record["feature_id"]
        if feature_id in seen_feature_ids:
            raise ValueError(f"feature_id 重复: {feature_id}")
        seen_feature_ids.add(feature_id)
        geometry = record.get("geometry")
        if not isinstance(geometry, dict) or geometry.get("type") not in {"Polygon", "MultiPolygon"}:
            raise ValueError(f"feature {feature_id} 必须提供 Polygon 或 MultiPolygon geometry")
        if record["parcel_declared_area_mu"] is not None:
            parcel_area_values[record["parcel_id"]].add(record["parcel_declared_area_mu"])
    conflicts = [parcel_id for parcel_id, values in parcel_area_values.items() if len(values) > 1]
    if conflicts:
        raise ValueError(f"同一 parcel_id 存在冲突的声明面积: {', '.join(sorted(conflicts))}")

    return {
        "source_crs": source_crs,
        "total_area_mu": _positive_area(data.get("total_area_mu"), "total_area_mu"),
        "provenance": {
            "manifest_type": manifest_type,
            "schema_version": data.get("schema_version"),
            "algorithm_version": data.get("algorithm_version"),
            "id_scheme": data.get("id_scheme"),
            "geometry_hash_scheme": data.get("geometry_hash_scheme"),
            "manifest_sha256": data.get("manifest_sha256"),
            "preflight_status": data.get("preflight_status"),
            "archive_sha256": (
                data.get("source", {}).get("archive_sha256")
                if isinstance(data.get("source"), Mapping)
                else None
            ),
        },
        "features": records,
    }


def _polygonal_shape(geometry: Mapping[str, Any]):
    from shapely import make_valid, union_all
    from shapely.geometry import GeometryCollection, MultiPolygon, shape
    from shapely.ops import unary_union

    geometry_type = geometry.get("type")
    if geometry_type == "Feature":
        raw_geometry = geometry.get("geometry")
        if not isinstance(raw_geometry, Mapping):
            raise ValueError("GeoJSON Feature 缺少 geometry")
        candidate = _polygonal_shape(raw_geometry)
    elif geometry_type == "FeatureCollection":
        raw_features = geometry.get("features")
        if not isinstance(raw_features, list) or not raw_features:
            raise ValueError("GeoJSON FeatureCollection 没有有效 features")
        candidates = [
            _polygonal_shape(feature)
            for feature in raw_features
            if isinstance(feature, Mapping)
        ]
        if not candidates:
            raise ValueError("GeoJSON FeatureCollection 没有有效面要素")
        candidate = union_all(candidates)
    else:
        candidate = make_valid(shape(geometry))
    if isinstance(candidate, GeometryCollection):
        polygons = [part for part in candidate.geoms if part.geom_type in {"Polygon", "MultiPolygon"}]
        candidate = unary_union(polygons) if polygons else candidate
    if candidate.is_empty or candidate.geom_type not in {"Polygon", "MultiPolygon"}:
        raise ValueError("地块 geometry 修复后没有有效的面要素")
    if isinstance(candidate, MultiPolygon) and len(candidate.geoms) == 1:
        candidate = candidate.geoms[0]
    return candidate


def canonical_geometry_sha256(geometry: Mapping[str, Any]) -> str:
    """Hash a valid geometry with the same scheme used by policy boundaries."""

    from shapely.geometry import mapping

    candidate = _polygonal_shape(geometry)
    if hasattr(candidate, "normalize"):
        candidate = candidate.normalize()
    canonical = json.dumps(
        mapping(candidate),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _quality_status(flags: Sequence[str]) -> str:
    if any(flag in flags for flag in ("no_raster_overlap", "no_roi_pixels", "no_valid_pixels")):
        return "unusable"
    return "warning" if flags else "ok"


def _reconcile_class_areas(
    summary: list[dict[str, Any]],
    total_area_mu: float,
) -> None:
    """Allocate rounded hundredths by largest remainder so class areas add up.

    The shared growth summarizer rounds every class independently.  For ratios
    such as thirds that can leave a 0.01-mu reporting difference.  Work in
    integer hundredths and distribute the residual deterministically, without
    assigning insured area when there are no valid pixels.
    """

    total_count = sum(int(row.get("count") or 0) for row in summary)
    if total_count <= 0:
        return
    target_units = int(round(float(total_area_mu) * 100))
    allocations: list[int] = []
    remainders: list[tuple[int, int]] = []
    for index, row in enumerate(summary):
        numerator = target_units * int(row.get("count") or 0)
        units, remainder = divmod(numerator, total_count)
        allocations.append(units)
        remainders.append((remainder, index))
    residual = target_units - sum(allocations)
    for _, index in sorted(remainders, key=lambda item: (-item[0], item[1]))[:residual]:
        allocations[index] += 1
    for row, units in zip(summary, allocations):
        row["area_mu"] = units / 100


def build_growth_statistics(
    *,
    count_map: Mapping[int, int],
    roi_pixel_count: int,
    area_mu: float,
    ndvi_sum: float = 0.0,
    ndvi_min: float | None = None,
    ndvi_max: float | None = None,
    raster_overlap_ratio: float = 1.0,
    n_classes: int = 5,
    valid_coverage_threshold: float = DEFAULT_VALID_COVERAGE_THRESHOLD,
    low_mean_ndvi_threshold: float = DEFAULT_LOW_MEAN_NDVI_THRESHOLD,
    poor_growth_ratio_threshold: float = DEFAULT_POOR_GROWTH_RATIO_THRESHOLD,
) -> dict[str, Any]:
    """Assemble counts, coverage, class areas, and deterministic quality flags."""

    if n_classes < 2 or n_classes > 9:
        raise ValueError("n_classes 需在 2 到 9 之间")
    for name, threshold in (
        ("valid_coverage_threshold", valid_coverage_threshold),
        ("poor_growth_ratio_threshold", poor_growth_ratio_threshold),
    ):
        if not 0 <= threshold <= 1:
            raise ValueError(f"{name} 必须在 0 到 1 之间")
    if not math.isfinite(low_mean_ndvi_threshold) or not -1 <= low_mean_ndvi_threshold <= 1:
        raise ValueError("low_mean_ndvi_threshold 必须在 -1 到 1 之间")
    if roi_pixel_count < 0:
        raise ValueError("roi_pixel_count 不能为负数")
    if not math.isfinite(area_mu) or area_mu < 0:
        raise ValueError("area_mu 不能为负数")
    if not math.isfinite(raster_overlap_ratio) or not 0 <= raster_overlap_ratio <= 1:
        raise ValueError("raster_overlap_ratio 必须在 0 到 1 之间")

    clean_counts: dict[int, int] = {}
    for class_id in range(1, n_classes + 1):
        count = int(count_map.get(class_id, 0))
        if count < 0:
            raise ValueError("等级像元数不能为负数")
        clean_counts[class_id] = count
    unknown = {int(key) for key, value in count_map.items() if int(value) and int(key) not in clean_counts}
    if unknown:
        raise ValueError(f"存在超出 1..{n_classes} 的等级: {sorted(unknown)}")

    valid_pixel_count = sum(clean_counts.values())
    if valid_pixel_count > roi_pixel_count:
        raise ValueError("有效像元数不能大于 ROI 像元数")
    if not math.isfinite(ndvi_sum):
        raise ValueError("ndvi_sum 必须是有限数值")
    valid_pixel_coverage = valid_pixel_count / roi_pixel_count if roi_pixel_count else 0.0
    effective_coverage = valid_pixel_coverage * raster_overlap_ratio
    mean_ndvi = ndvi_sum / valid_pixel_count if valid_pixel_count else None
    if valid_pixel_count:
        if mean_ndvi is None or mean_ndvi < -1.0001 or mean_ndvi > 1.0001:
            raise ValueError("平均 NDVI 超出 [-1, 1]")
        if ndvi_min is not None and ndvi_max is not None:
            validate_ndvi_stats({"min": ndvi_min, "max": ndvi_max})
    summary = summarize_classes(clean_counts, area_mu, n_classes)
    _reconcile_class_areas(summary, area_mu)
    poor_class_count = max(1, math.floor(n_classes * 0.4))
    poor_growth_ratio = sum(row["ratio"] for row in summary[:poor_class_count])

    flags: list[str] = []
    if raster_overlap_ratio <= 0:
        flags.append("no_raster_overlap")
    elif roi_pixel_count == 0:
        flags.append("no_roi_pixels")
    elif raster_overlap_ratio < 0.999999:
        flags.append("partial_raster_overlap")
    if valid_pixel_count == 0:
        flags.append("no_valid_pixels")
    elif valid_pixel_coverage < valid_coverage_threshold:
        flags.append("low_valid_coverage")
    if mean_ndvi is not None and mean_ndvi < low_mean_ndvi_threshold:
        flags.append("low_mean_ndvi")
    if valid_pixel_count and poor_growth_ratio >= poor_growth_ratio_threshold:
        flags.append("poor_growth_dominant")

    return {
        "area_mu": round(float(area_mu), 2),
        "roi_pixel_count": int(roi_pixel_count),
        "valid_pixel_count": valid_pixel_count,
        "valid_pixel_coverage": round(valid_pixel_coverage, 6),
        "raster_overlap_ratio": round(raster_overlap_ratio, 6),
        "effective_coverage": round(effective_coverage, 6),
        "mean_ndvi": round(mean_ndvi, 6) if mean_ndvi is not None else None,
        "min_ndvi": round(float(ndvi_min), 6) if ndvi_min is not None else None,
        "max_ndvi": round(float(ndvi_max), 6) if ndvi_max is not None else None,
        "poor_growth_ratio": round(poor_growth_ratio, 6),
        "summary": summary,
        "quality_status": _quality_status(flags),
        "anomaly_flags": flags,
    }


def _geometry_area_mu(geometry: Any, source_crs: str) -> float:
    from pyproj import Transformer
    from shapely.ops import transform

    transformer = Transformer.from_crs(source_crs, "EPSG:6933", always_xy=True)
    area_sqm = float(transform(transformer.transform, geometry).area)
    if not math.isfinite(area_sqm) or area_sqm <= 0:
        raise ValueError("地块几何面积无效")
    return area_sqm * _AREA_SQM_TO_MU


def _union_shapes(shapes: Sequence[Any]):
    from shapely import union_all

    candidate = union_all(list(shapes))
    if candidate.is_empty:
        raise ValueError("地块合并后为空")
    return candidate


def _sample_geometry(
    dataset: Any,
    geometry: Any,
    class_breaks: Sequence[float],
) -> dict[str, Any]:
    import numpy as np
    from rasterio.errors import WindowError
    from rasterio.features import geometry_mask, geometry_window
    from rasterio.windows import Window
    from shapely.geometry import box, mapping

    raster_bounds = box(*dataset.bounds)
    geometry_area = float(geometry.area)
    intersection = geometry.intersection(raster_bounds)
    overlap_ratio = (
        max(0.0, min(1.0, float(intersection.area) / geometry_area))
        if geometry_area > 0 and not intersection.is_empty
        else 0.0
    )
    if intersection.is_empty:
        return {
            "count_map": {},
            "roi_pixel_count": 0,
            "ndvi_sum": 0.0,
            "ndvi_min": None,
            "ndvi_max": None,
            "raster_overlap_ratio": 0.0,
        }

    try:
        raw_window = geometry_window(dataset, [mapping(geometry)])
        window = raw_window.intersection(Window(0, 0, dataset.width, dataset.height))
    except WindowError:
        return {
            "count_map": {},
            "roi_pixel_count": 0,
            "ndvi_sum": 0.0,
            "ndvi_min": None,
            "ndvi_max": None,
            "raster_overlap_ratio": overlap_ratio,
        }

    values = dataset.read(1, window=window, masked=False)
    source_valid_mask = dataset.read_masks(1, window=window) > 0
    inside_roi = geometry_mask(
        [mapping(geometry)],
        out_shape=(int(window.height), int(window.width)),
        transform=dataset.window_transform(window),
        invert=True,
    )
    valid = inside_roi & source_valid_mask & np.isfinite(values)
    nodata = dataset.nodata
    if nodata is not None and np.isfinite(nodata):
        valid &= values != nodata
    roi_pixel_count = int(np.count_nonzero(inside_roi))
    valid_values = values[valid].astype("float64", copy=False)
    if valid_values.size:
        stats = {
            "min": float(valid_values.min()),
            "max": float(valid_values.max()),
        }
        validate_ndvi_stats(stats)
        bins = np.asarray([-np.inf, *class_breaks[:-1], np.inf], dtype="float64")
        classes = np.digitize(valid_values, bins=bins, right=False)
        unique, counts = np.unique(classes, return_counts=True)
        count_map = {int(key): int(value) for key, value in zip(unique.tolist(), counts.tolist())}
        ndvi_sum = float(valid_values.sum(dtype="float64"))
        ndvi_min = stats["min"]
        ndvi_max = stats["max"]
    else:
        count_map = {}
        ndvi_sum = 0.0
        ndvi_min = None
        ndvi_max = None
    return {
        "count_map": count_map,
        "roi_pixel_count": roi_pixel_count,
        "ndvi_sum": ndvi_sum,
        "ndvi_min": ndvi_min,
        "ndvi_max": ndvi_max,
        "raster_overlap_ratio": overlap_ratio,
    }


def _analyse_shape(
    *,
    dataset: Any,
    source_shape: Any,
    raster_shape: Any,
    source_crs: str,
    declared_area_mu: float | None,
    class_breaks: Sequence[float],
    n_classes: int,
    valid_coverage_threshold: float,
    low_mean_ndvi_threshold: float,
    poor_growth_ratio_threshold: float,
) -> dict[str, Any]:
    from shapely.geometry import mapping

    sample = _sample_geometry(dataset, raster_shape, class_breaks)
    area_mu = declared_area_mu or _geometry_area_mu(source_shape, source_crs)
    result = build_growth_statistics(
        count_map=sample["count_map"],
        roi_pixel_count=sample["roi_pixel_count"],
        area_mu=area_mu,
        ndvi_sum=sample["ndvi_sum"],
        ndvi_min=sample["ndvi_min"],
        ndvi_max=sample["ndvi_max"],
        raster_overlap_ratio=sample["raster_overlap_ratio"],
        n_classes=n_classes,
        valid_coverage_threshold=valid_coverage_threshold,
        low_mean_ndvi_threshold=low_mean_ndvi_threshold,
        poor_growth_ratio_threshold=poor_growth_ratio_threshold,
    )
    result["boundary_sha256"] = canonical_geometry_sha256(mapping(source_shape))
    result["boundary_hash_scheme"] = BOUNDARY_HASH_SCHEME
    result["area_source"] = "declared" if declared_area_mu is not None else "geometry_epsg6933"
    return result


def _append_flag(result: dict[str, Any], flag: str) -> None:
    flags = result["anomaly_flags"]
    if flag not in flags:
        flags.append(flag)
        result["quality_status"] = _quality_status(flags)


def analyze_parcel_growth(
    *,
    raster_path: str | Path,
    manifest: Mapping[str, Any] | str | Path,
    source_crs: str | None = None,
    class_breaks: Sequence[float] = FIXED_NDVI_BREAKS_5,
    valid_coverage_threshold: float = DEFAULT_VALID_COVERAGE_THRESHOLD,
    low_mean_ndvi_threshold: float = DEFAULT_LOW_MEAN_NDVI_THRESHOLD,
    poor_growth_ratio_threshold: float = DEFAULT_POOR_GROWTH_RATIO_THRESHOLD,
) -> dict[str, Any]:
    """Aggregate one NDVI raster by mapped feature, parcel, and overall union."""

    import rasterio
    from pyproj import Transformer
    from shapely.geometry import mapping
    from shapely.ops import transform

    normalized = normalize_parcel_manifest(manifest, require_confirmed=True)
    manifest_crs = source_crs or normalized["source_crs"]
    breaks = [float(value) for value in class_breaks]
    if len(breaks) < 2 or len(breaks) > 9 or any(not math.isfinite(value) for value in breaks):
        raise ValueError("class_breaks 必须包含 2 到 9 个有限阈值")
    # Constant scenes can legitimately produce repeated Jenks/quantile breaks;
    # match the existing classifier by accepting a non-decreasing sequence.
    if any(left > right for left, right in zip(breaks, breaks[1:])):
        raise ValueError("class_breaks 必须按非递减顺序排列")
    n_classes = len(breaks)

    source_records: list[dict[str, Any]] = []
    for record in normalized["features"]:
        source_records.append({**record, "source_shape": _polygonal_shape(record["geometry"])})

    raster_file = Path(raster_path)
    with rasterio.open(raster_file) as dataset:
        if dataset.count < 1 or dataset.crs is None:
            raise ValueError("NDVI 栅格必须至少包含一个波段并声明 CRS")
        transformer = Transformer.from_crs(manifest_crs, dataset.crs, always_xy=True)
        for record in source_records:
            record["raster_shape"] = _polygonal_shape(
                mapping(transform(transformer.transform, record["source_shape"]))
            )

        feature_results: list[dict[str, Any]] = []
        for record in source_records:
            statistics = _analyse_shape(
                dataset=dataset,
                source_shape=record["source_shape"],
                raster_shape=record["raster_shape"],
                source_crs=manifest_crs,
                declared_area_mu=record["declared_area_mu"],
                class_breaks=breaks,
                n_classes=n_classes,
                valid_coverage_threshold=valid_coverage_threshold,
                low_mean_ndvi_threshold=low_mean_ndvi_threshold,
                poor_growth_ratio_threshold=poor_growth_ratio_threshold,
            )
            feature_results.append(
                {
                    "parcel_id": record["parcel_id"],
                    "feature_id": record["feature_id"],
                    "source_index": record["source_index"],
                    "source_feature_index": record["source_feature_index"],
                    "source_file": record["source_file"],
                    **statistics,
                }
            )

        # Overlap is a manifest-quality issue even though parcel/overall union
        # summaries below correctly avoid double-counting it.
        overlaps: dict[str, list[str]] = defaultdict(list)
        from shapely.strtree import STRtree

        source_shapes = [record["source_shape"] for record in source_records]
        spatial_index = STRtree(source_shapes)
        for left_index, left in enumerate(source_records):
            candidate_indices = spatial_index.query(left["source_shape"], predicate="intersects")
            for candidate_index in candidate_indices:
                right_index = int(candidate_index)
                if right_index <= left_index:
                    continue
                right = source_records[right_index]
                if left["source_shape"].intersection(right["source_shape"]).area > 0:
                    overlaps[left["feature_id"]].append(right["feature_id"])
                    overlaps[right["feature_id"]].append(left["feature_id"])
        for result in feature_results:
            related = sorted(overlaps.get(result["feature_id"], []))
            result["overlapping_feature_ids"] = related
            if related:
                _append_flag(result, "overlaps_other_feature")

        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for record in source_records:
            grouped[record["parcel_id"]].append(record)
        parcel_results: list[dict[str, Any]] = []
        for parcel_id, records in grouped.items():
            source_union = _union_shapes([record["source_shape"] for record in records])
            raster_union = _union_shapes([record["raster_shape"] for record in records])
            declared_values = {
                record["parcel_declared_area_mu"]
                for record in records
                if record["parcel_declared_area_mu"] is not None
            }
            declared_area = next(iter(declared_values)) if declared_values else None
            if declared_area is None and len(records) == 1:
                declared_area = records[0]["declared_area_mu"]
            statistics = _analyse_shape(
                dataset=dataset,
                source_shape=source_union,
                raster_shape=raster_union,
                source_crs=manifest_crs,
                declared_area_mu=declared_area,
                class_breaks=breaks,
                n_classes=n_classes,
                valid_coverage_threshold=valid_coverage_threshold,
                low_mean_ndvi_threshold=low_mean_ndvi_threshold,
                poor_growth_ratio_threshold=poor_growth_ratio_threshold,
            )
            if any(record["feature_id"] in overlaps for record in records):
                _append_flag(statistics, "overlapping_features_present")
            parcel_results.append(
                {
                    "parcel_id": parcel_id,
                    "feature_ids": [record["feature_id"] for record in records],
                    "feature_count": len(records),
                    "source_files": sorted(
                        {
                            str(record["source_file"])
                            for record in records
                            if record["source_file"]
                        }
                    ),
                    "mapping": records[0]["mapping"],
                    **statistics,
                }
            )

        overall_source = _union_shapes([record["source_shape"] for record in source_records])
        overall_raster = _union_shapes([record["raster_shape"] for record in source_records])
        overall = _analyse_shape(
            dataset=dataset,
            source_shape=overall_source,
            raster_shape=overall_raster,
            source_crs=manifest_crs,
            declared_area_mu=normalized["total_area_mu"],
            class_breaks=breaks,
            n_classes=n_classes,
            valid_coverage_threshold=valid_coverage_threshold,
            low_mean_ndvi_threshold=low_mean_ndvi_threshold,
            poor_growth_ratio_threshold=poor_growth_ratio_threshold,
        )
        if overlaps:
            _append_flag(overall, "overlapping_features_present")
        overall.update(
            {
                "parcel_count": len(parcel_results),
                "feature_count": len(feature_results),
                "anomalous_parcel_count": sum(
                    result["quality_status"] != "ok" for result in parcel_results
                ),
                "anomalous_feature_count": sum(
                    result["quality_status"] != "ok" for result in feature_results
                ),
            }
        )

        raster_metadata = {
            # Do not expose an absolute server filesystem path through API/report
            # payloads.  The caller registers the controlled artifact URL; this
            # module records only portable identity and integrity metadata.
            "filename": raster_file.name,
            "sha256": _file_sha256(raster_file),
            "crs": str(dataset.crs),
            "width": int(dataset.width),
            "height": int(dataset.height),
            "nodata": float(dataset.nodata) if dataset.nodata is not None else None,
            "band": 1,
        }
    return {
        "schema_version": PARCEL_GROWTH_SCHEMA_VERSION,
        "algorithm_version": PARCEL_GROWTH_ALGORITHM_VERSION,
        "classification_scheme_version": CLASSIFICATION_SCHEME_VERSION,
        "boundary_hash_scheme": BOUNDARY_HASH_SCHEME,
        "boundary_sha256": overall["boundary_sha256"],
        "source_crs": str(manifest_crs),
        "manifest": normalized["provenance"],
        "class_breaks": [round(value, 6) for value in breaks],
        "n_classes": n_classes,
        "valid_coverage_threshold": valid_coverage_threshold,
        "raster": raster_metadata,
        "features": feature_results,
        "parcels": parcel_results,
        "overall": overall,
    }


__all__ = [
    "BOUNDARY_HASH_SCHEME",
    "CLASSIFICATION_SCHEME_VERSION",
    "DEFAULT_LOW_MEAN_NDVI_THRESHOLD",
    "DEFAULT_POOR_GROWTH_RATIO_THRESHOLD",
    "DEFAULT_VALID_COVERAGE_THRESHOLD",
    "PARCEL_GROWTH_ALGORITHM_VERSION",
    "PARCEL_GROWTH_SCHEMA_VERSION",
    "analyze_parcel_growth",
    "build_growth_statistics",
    "canonical_geometry_sha256",
    "normalize_parcel_manifest",
]
