"""Quarterly Sentinel-2 NDVI planning, collection, and immutable persistence.

This module is intentionally independent from the API routes.  A route or job
runner can call :func:`run_gee_historical_ndvi`, persist its validated JSON, and
then pass the result to ``historical_ndvi_report`` for chart/DOCX generation.

Quarterly semantics follow the supplied reference report:

* each value is the spatial median of a per-pixel quarterly median composite;
* calendar start/end dates are inclusive in persisted data;
* the current quarter may be partial through ``as_of_date``;
* future quarters are explicit ``not_reached`` records and remain blank;
* no monthly averaging or temporal interpolation is performed.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sentinel2_ndvi import (
    S2_CLOUD_MASK_DESCRIPTION,
    S2_CLOUD_MASK_VERSION,
    S2_NDVI_FORMULA,
    S2_SR_COLLECTION,
    build_s2_sr_collection,
)


SCHEMA_VERSION = "agrisky.historical-ndvi/v1"
SOURCE_PROVENANCE_SCHEMA_VERSION = "agrisky.historical-ndvi-source/v1"
SCENE_SET_SCHEMA_VERSION = "agrisky.historical-ndvi-scene-set/v1"
SOURCE_ID = "google-earth-engine"
ALGORITHM_ID = "agrisky.sentinel2.quarterly-ndvi"
ALGORITHM_VERSION = "1"
AGGREGATION_METHOD = "quarterly_image_median_then_parcel_pixel_median"
MAX_HISTORY_YEARS = 20
MAX_SCENE_PROVENANCE_ITEMS = 512
MAX_SCENE_IDENTIFIER_LENGTH = 200
LOW_VALID_COVERAGE = 0.70
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SCENE_IDENTIFIER_RE = re.compile(
    rf"^[A-Za-z0-9][A-Za-z0-9._-]{{0,{MAX_SCENE_IDENTIFIER_LENGTH - 1}}}$"
)

WindowStatus = Literal["complete", "partial", "not_reached"]
ObservationStatus = Literal[
    "complete",
    "partial",
    "no_imagery",
    "no_valid_pixels",
    "not_reached",
    "failed",
]
ObservationOutcomeCode = Literal[
    "HISTORICAL_NDVI_AVAILABLE",
    "HISTORICAL_NDVI_NO_IMAGERY",
    "HISTORICAL_NDVI_NO_VALID_PIXELS",
    "HISTORICAL_NDVI_NOT_REACHED",
    "HISTORICAL_NDVI_PROCESSING_FAILED",
]
CacheDisposition = Literal[
    "observation",
    "negative_no_imagery",
    "negative_no_valid_pixels",
    "not_reached",
    "retryable_failure",
]
PublicQuarterErrorCode = Literal[
    "GEE_QUARTER_FAILED",
    "GEE_SCENE_SET_TOO_LARGE",
    "GEE_SCENE_METADATA_UNAVAILABLE",
    "GEE_SCENE_METADATA_INVALID",
    "GEE_EMPTY_ROI",
]

_PUBLIC_QUARTER_ERROR_CODES = frozenset(
    {
        "GEE_QUARTER_FAILED",
        "GEE_SCENE_SET_TOO_LARGE",
        "GEE_SCENE_METADATA_UNAVAILABLE",
        "GEE_SCENE_METADATA_INVALID",
        "GEE_EMPTY_ROI",
    }
)
_OUTCOME_BY_STATUS: dict[str, ObservationOutcomeCode] = {
    "complete": "HISTORICAL_NDVI_AVAILABLE",
    "partial": "HISTORICAL_NDVI_AVAILABLE",
    "no_imagery": "HISTORICAL_NDVI_NO_IMAGERY",
    "no_valid_pixels": "HISTORICAL_NDVI_NO_VALID_PIXELS",
    "not_reached": "HISTORICAL_NDVI_NOT_REACHED",
    "failed": "HISTORICAL_NDVI_PROCESSING_FAILED",
}
_CACHE_DISPOSITION_BY_STATUS: dict[str, CacheDisposition] = {
    "complete": "observation",
    "partial": "observation",
    "no_imagery": "negative_no_imagery",
    "no_valid_pixels": "negative_no_valid_pixels",
    "not_reached": "not_reached",
    "failed": "retryable_failure",
}


def _scene_set_digest(scene_records: Sequence[dict[str, str]]) -> str:
    """Hash a canonical scene set together with its provenance identities."""
    payload = {
        "schema_version": SCENE_SET_SCHEMA_VERSION,
        "source_id": SOURCE_ID,
        "collection": S2_SR_COLLECTION,
        "algorithm_id": ALGORITHM_ID,
        "algorithm_version": ALGORITHM_VERSION,
        "formula": S2_NDVI_FORMULA,
        "aggregation_method": AGGREGATION_METHOD,
        "cloud_mask_version": S2_CLOUD_MASK_VERSION,
        "scenes": list(scene_records),
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class _SafeQuarterFailure(RuntimeError):
    """Internal exception whose code is safe to persist or return publicly."""

    def __init__(self, code: PublicQuarterErrorCode) -> None:
        super().__init__(code)
        self.code = code


def _quarter_dates(year: int, quarter: int) -> tuple[date, date]:
    if not 1 <= quarter <= 4:
        raise ValueError("季度必须为 1 至 4")
    start_month = (quarter - 1) * 3 + 1
    start = date(year, start_month, 1)
    if quarter == 4:
        end = date(year, 12, 31)
    else:
        end = date(year, start_month + 3, 1) - timedelta(days=1)
    return start, end


class QuarterWindow(BaseModel):
    """One calendar-quarter query window with explicit as-of semantics."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    year: int = Field(ge=2017, le=2100)
    quarter: int = Field(ge=1, le=4)
    start_date: date
    calendar_end_date: date
    observation_end_date: date | None = None
    status: WindowStatus

    @property
    def key(self) -> tuple[int, int]:
        return self.year, self.quarter

    @property
    def label(self) -> str:
        return f"{self.year}-Q{self.quarter}"

    @model_validator(mode="after")
    def validate_calendar_semantics(self) -> "QuarterWindow":
        expected_start, expected_end = _quarter_dates(self.year, self.quarter)
        if self.start_date != expected_start or self.calendar_end_date != expected_end:
            raise ValueError("季度日期与 year/quarter 不一致")
        if self.status == "not_reached":
            if self.observation_end_date is not None:
                raise ValueError("未到达季度不得填写 observation_end_date")
        else:
            if self.observation_end_date is None:
                raise ValueError("已观测季度必须填写 observation_end_date")
            if not self.start_date <= self.observation_end_date <= self.calendar_end_date:
                raise ValueError("observation_end_date 必须位于季度内")
            if self.status == "complete" and self.observation_end_date != self.calendar_end_date:
                raise ValueError("完整季度的观测截止日必须等于季度截止日")
            if self.status == "partial" and self.observation_end_date >= self.calendar_end_date:
                raise ValueError("阶段性季度的观测截止日必须早于季度截止日")
        return self


def plan_quarter_windows(
    start_year: int,
    end_year: int,
    *,
    as_of_date: date | str | None = None,
) -> list[QuarterWindow]:
    """Return all quarter slots, including future slots that must stay blank."""
    if start_year < 2017:
        raise ValueError("Sentinel-2 SR 历史季度监测起始年份不得早于 2017")
    if end_year < start_year:
        raise ValueError("历史监测结束年份不能早于起始年份")
    if end_year - start_year + 1 > MAX_HISTORY_YEARS:
        raise ValueError(f"单次历史监测最多支持 {MAX_HISTORY_YEARS} 年")
    if end_year > 2100:
        raise ValueError("历史监测结束年份不能晚于 2100")

    if as_of_date is None:
        cutoff = date.today()
    elif isinstance(as_of_date, str):
        try:
            cutoff = date.fromisoformat(as_of_date)
        except ValueError as exc:
            raise ValueError("as_of_date 必须为 YYYY-MM-DD") from exc
    elif isinstance(as_of_date, date):
        cutoff = as_of_date
    else:
        raise TypeError("as_of_date 必须是 date、YYYY-MM-DD 字符串或 None")

    windows: list[QuarterWindow] = []
    for year in range(start_year, end_year + 1):
        for quarter in range(1, 5):
            start, calendar_end = _quarter_dates(year, quarter)
            if cutoff < start:
                status: WindowStatus = "not_reached"
                observation_end = None
            elif cutoff >= calendar_end:
                status = "complete"
                observation_end = calendar_end
            else:
                status = "partial"
                observation_end = cutoff
            windows.append(
                QuarterWindow(
                    year=year,
                    quarter=quarter,
                    start_date=start,
                    calendar_end_date=calendar_end,
                    observation_end_date=observation_end,
                    status=status,
                )
            )
    return windows


class Sentinel2SceneReference(BaseModel):
    """Bounded public identifiers for one image in the quarterly collection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scene_id: str = Field(
        min_length=1,
        max_length=MAX_SCENE_IDENTIFIER_LENGTH,
        pattern=_SCENE_IDENTIFIER_RE.pattern,
    )
    product_id: str = Field(
        min_length=1,
        max_length=MAX_SCENE_IDENTIFIER_LENGTH,
        pattern=_SCENE_IDENTIFIER_RE.pattern,
    )


class QuarterlySceneProvenance(BaseModel):
    """Complete, ordered and hash-bound Sentinel-2 scene metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        "agrisky.historical-ndvi-scene-set/v1"
    ] = SCENE_SET_SCHEMA_VERSION
    source_id: Literal["google-earth-engine"] = SOURCE_ID
    collection: Literal["COPERNICUS/S2_SR_HARMONIZED"] = S2_SR_COLLECTION
    algorithm_id: Literal[
        "agrisky.sentinel2.quarterly-ndvi"
    ] = ALGORITHM_ID
    algorithm_version: Literal["1"] = ALGORITHM_VERSION
    formula: Literal[
        "NDVI = (Sentinel-2 B8 - B4) / (B8 + B4)"
    ] = S2_NDVI_FORMULA
    aggregation_method: Literal[
        "quarterly_image_median_then_parcel_pixel_median"
    ] = AGGREGATION_METHOD
    cloud_mask_version: Literal["s2-scl-v2-20m"] = S2_CLOUD_MASK_VERSION
    metadata_complete: Literal[True] = True
    scene_count: int = Field(ge=0, le=MAX_SCENE_PROVENANCE_ITEMS)
    scenes: list[Sentinel2SceneReference] = Field(
        default_factory=list,
        max_length=MAX_SCENE_PROVENANCE_ITEMS,
    )
    scene_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_order_and_digest(self) -> "QuarterlySceneProvenance":
        records = [item.model_dump(mode="json") for item in self.scenes]
        if self.scene_count != len(records):
            raise ValueError("scene_count 必须等于已记录的场景数")
        ordered = sorted(
            records,
            key=lambda item: (item["scene_id"], item["product_id"]),
        )
        if records != ordered:
            raise ValueError("场景元数据必须按 scene_id/product_id 确定性排序")
        if len({item["scene_id"] for item in records}) != len(records):
            raise ValueError("场景元数据中存在重复 scene_id")
        if self.scene_set_sha256 != _scene_set_digest(records):
            raise ValueError("scene_set_sha256 与场景元数据或算法身份不一致")
        return self


class QuarterlyNDVIObservation(BaseModel):
    """Persisted statistics and artifact linkage for one planned quarter."""

    model_config = ConfigDict(extra="forbid")

    window: QuarterWindow
    status: ObservationStatus
    image_count: int = Field(default=0, ge=0)
    median_ndvi: float | None = Field(default=None, ge=-1.0, le=1.0)
    mean_ndvi: float | None = Field(default=None, ge=-1.0, le=1.0)
    stddev_ndvi: float | None = Field(default=None, ge=0.0, le=1.0)
    min_ndvi: float | None = Field(default=None, ge=-1.0, le=1.0)
    max_ndvi: float | None = Field(default=None, ge=-1.0, le=1.0)
    valid_pixel_count: int | None = Field(default=None, ge=0)
    roi_pixel_count: int | None = Field(default=None, ge=0)
    valid_pixel_coverage: float | None = Field(default=None, ge=0.0, le=1.0)
    quarter_map_png: str | None = None
    quarter_map_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    scene_provenance: QuarterlySceneProvenance | None = None
    outcome_code: ObservationOutcomeCode | None = None
    cache_disposition: CacheDisposition | None = None
    error_code: PublicQuarterErrorCode | None = None
    # Accepted only to safely read older v1 files. It is always discarded and
    # never serialized; public failures are represented by stable error codes.
    error_message: str | None = Field(default=None, exclude=True)

    @property
    def usable(self) -> bool:
        return self.status in {"complete", "partial"} and self.median_ndvi is not None

    @model_validator(mode="before")
    @classmethod
    def normalise_public_outcome(cls, payload: Any) -> Any:
        """Derive cache semantics and strip untrusted exception text."""
        if not isinstance(payload, dict):
            return payload
        normalized = dict(payload)
        status = normalized.get("status")
        if isinstance(status, str) and status in _OUTCOME_BY_STATUS:
            normalized["outcome_code"] = _OUTCOME_BY_STATUS[status]
            normalized["cache_disposition"] = _CACHE_DISPOSITION_BY_STATUS[status]
        if status == "failed":
            candidate = normalized.get("error_code")
            normalized["error_code"] = (
                candidate
                if isinstance(candidate, str) and candidate in _PUBLIC_QUARTER_ERROR_CODES
                else "GEE_QUARTER_FAILED"
            )
        else:
            normalized["error_code"] = None
        normalized["error_message"] = None
        return normalized

    @model_validator(mode="after")
    def validate_status_and_statistics(self) -> "QuarterlyNDVIObservation":
        stats = (self.median_ndvi, self.mean_ndvi, self.stddev_ndvi, self.min_ndvi, self.max_ndvi)
        pixel_fields = (
            self.valid_pixel_count,
            self.roi_pixel_count,
            self.valid_pixel_coverage,
        )
        if self.status == "not_reached":
            if self.window.status != "not_reached":
                raise ValueError("not_reached 结果必须对应未到达季度")
            if (
                self.image_count != 0
                or any(value is not None for value in stats)
                or any(value is not None for value in pixel_fields)
            ):
                raise ValueError("未到达季度不得包含影像数、像元数或 NDVI 统计")
        elif self.window.status == "not_reached":
            raise ValueError("未到达季度只能保存 not_reached 结果")

        if self.status in {"complete", "partial"}:
            if self.status != self.window.status:
                raise ValueError("可用季度的结果状态必须与计划窗口状态一致")
            if self.image_count <= 0:
                raise ValueError("可用季度必须至少包含一景影像")
            if any(value is None for value in stats):
                raise ValueError("可用季度必须包含完整 NDVI 统计")
            if not self.valid_pixel_count or not self.roi_pixel_count:
                raise ValueError("可用季度必须包含正数有效像元和 ROI 像元数")
        elif self.status == "no_imagery":
            if (
                self.image_count != 0
                or any(value is not None for value in stats)
                or any(value is not None for value in pixel_fields)
            ):
                raise ValueError("无影像季度不得包含像元数或 NDVI 统计")
        elif self.status == "no_valid_pixels":
            if (
                self.image_count <= 0
                or self.valid_pixel_count != 0
                or not self.roi_pixel_count
                or self.valid_pixel_coverage != 0.0
            ):
                raise ValueError("无有效像元季度应有影像但有效像元数为 0")
            if any(value is not None for value in stats):
                raise ValueError("无有效像元季度不得包含 NDVI 统计")
        elif self.status == "failed":
            if not self.error_code:
                raise ValueError("失败季度必须记录稳定 error_code")
            if any(value is not None for value in stats) or any(
                value is not None for value in pixel_fields
            ):
                raise ValueError("失败季度不得混入未完成的像元或 NDVI 统计")

        if self.error_message is not None:
            raise ValueError("历史 NDVI 公开结果不得包含内部异常文本")
        if self.status != "failed" and self.error_code is not None:
            raise ValueError("非失败季度不得包含 error_code")
        expected_outcome = _OUTCOME_BY_STATUS[self.status]
        expected_cache_disposition = _CACHE_DISPOSITION_BY_STATUS[self.status]
        if self.outcome_code != expected_outcome:
            raise ValueError("outcome_code 与季度状态不一致")
        if self.cache_disposition != expected_cache_disposition:
            raise ValueError("cache_disposition 与季度状态不一致")
        if self.scene_provenance is not None:
            if self.scene_provenance.scene_count != self.image_count:
                raise ValueError("场景元数据数量必须与 image_count 一致")
            if self.status == "not_reached":
                raise ValueError("未到达季度不得绑定场景元数据")
        if self.status in {"complete", "partial", "no_imagery", "no_valid_pixels"} and (
            self.scene_provenance is None
        ):
            raise ValueError("已到达季度必须绑定完整的 Sentinel-2 场景来源")

        if self.status not in {"complete", "partial"} and (
            self.quarter_map_png or self.quarter_map_sha256
        ):
            raise ValueError("不可用季度不得绑定季度图")
        if any(value is not None for value in pixel_fields) and not all(
            value is not None for value in pixel_fields
        ):
            raise ValueError("有效像元数、ROI 像元数和覆盖率必须同时存在")

        if self.valid_pixel_count is not None and self.roi_pixel_count is not None:
            if self.valid_pixel_count > self.roi_pixel_count:
                raise ValueError("有效像元数不能大于 ROI 像元数")
            expected = (
                self.valid_pixel_count / self.roi_pixel_count if self.roi_pixel_count else 0.0
            )
            if self.valid_pixel_coverage is None or not math.isclose(
                self.valid_pixel_coverage, expected, abs_tol=1e-6
            ):
                raise ValueError("有效像元覆盖率与像元计数不一致")

        numeric = [value for value in (self.min_ndvi, self.max_ndvi) if value is not None]
        if len(numeric) == 2 and numeric[0] > numeric[1]:
            raise ValueError("NDVI 最小值不能大于最大值")
        if self.min_ndvi is not None and self.max_ndvi is not None:
            for name, value in (("中位值", self.median_ndvi), ("均值", self.mean_ndvi)):
                if value is not None and not self.min_ndvi - 1e-6 <= value <= self.max_ndvi + 1e-6:
                    raise ValueError(f"NDVI {name}必须位于最小值与最大值之间")
        if bool(self.quarter_map_png) != bool(self.quarter_map_sha256):
            raise ValueError("季度图路径与 SHA-256 必须同时存在或同时为空")
        return self


class HistoricalNDVISource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provenance_schema_version: Literal[
        "agrisky.historical-ndvi-source/v1"
    ] = SOURCE_PROVENANCE_SCHEMA_VERSION
    result_schema_version: Literal["agrisky.historical-ndvi/v1"] = SCHEMA_VERSION
    source: Literal["gee"] = "gee"
    source_id: Literal["google-earth-engine"] = SOURCE_ID
    source_label: str = "GEE Sentinel-2 SR NDVI"
    collection: Literal["COPERNICUS/S2_SR_HARMONIZED"] = S2_SR_COLLECTION
    algorithm_id: Literal[
        "agrisky.sentinel2.quarterly-ndvi"
    ] = ALGORITHM_ID
    algorithm_version: Literal["1"] = ALGORITHM_VERSION
    formula: Literal[
        "NDVI = (Sentinel-2 B8 - B4) / (B8 + B4)"
    ] = S2_NDVI_FORMULA
    aggregation_method: Literal[
        "quarterly_image_median_then_parcel_pixel_median"
    ] = AGGREGATION_METHOD
    date_semantics: str = "季度起止日期均含；GEE filterDate 使用下一日作为排他截止日"
    cloud_mask: str = S2_CLOUD_MASK_DESCRIPTION
    cloud_mask_version: str = S2_CLOUD_MASK_VERSION
    max_cloud_pct: float = Field(ge=0.0, le=100.0)
    scale_m: int = Field(ge=10, le=100)


class HistoricalNDVIOutputs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result_json: str | None = None
    trend_chart_png: str | None = None
    yearly_panel_pngs: dict[str, str] = Field(default_factory=dict)
    report_docx: str | None = None


class HistoricalNDVIResult(BaseModel):
    """Versioned authority model for one aggregate quarterly history run."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["agrisky.historical-ndvi/v1"] = SCHEMA_VERSION
    task_id: str
    scope_label: str = Field(min_length=1, max_length=200)
    parcel_id: str | None = Field(default=None, max_length=128)
    feature_id: str | None = Field(default=None, max_length=128)
    boundary_geometry_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    generated_at: datetime
    as_of_date: date
    start_year: int = Field(ge=2017, le=2100)
    end_year: int = Field(ge=2017, le=2100)
    source: HistoricalNDVISource
    quarters: list[QuarterlyNDVIObservation] = Field(min_length=1)
    warnings: list[str] = Field(default_factory=list)
    outputs: HistoricalNDVIOutputs = Field(default_factory=HistoricalNDVIOutputs)

    @model_validator(mode="after")
    def validate_result_series(self) -> "HistoricalNDVIResult":
        if not _TASK_ID_RE.fullmatch(self.task_id):
            raise ValueError("task_id 只能包含字母、数字、点、下划线和连字符")
        if self.end_year < self.start_year:
            raise ValueError("end_year 不能早于 start_year")
        if self.end_year - self.start_year + 1 > MAX_HISTORY_YEARS:
            raise ValueError(f"历史结果最多包含 {MAX_HISTORY_YEARS} 年")
        if self.generated_at.tzinfo is None or self.generated_at.utcoffset() is None:
            raise ValueError("generated_at 必须包含时区")

        expected = {
            (year, quarter)
            for year in range(self.start_year, self.end_year + 1)
            for quarter in range(1, 5)
        }
        actual = [item.window.key for item in self.quarters]
        if len(actual) != len(set(actual)):
            raise ValueError("季度结果中存在重复 year/quarter")
        if set(actual) != expected:
            missing = sorted(expected - set(actual))
            extra = sorted(set(actual) - expected)
            raise ValueError(f"季度结果必须覆盖完整季度槽位；缺失={missing}，多余={extra}")
        if actual != sorted(actual):
            raise ValueError("季度结果必须按 year/quarter 升序保存")

        planned = plan_quarter_windows(
            self.start_year,
            self.end_year,
            as_of_date=self.as_of_date,
        )
        for observation, window in zip(self.quarters, planned, strict=True):
            if observation.window != window:
                raise ValueError(f"{window.label} 的日期或截至日与 as_of_date 不一致")
        return self


def _exclusive_end(inclusive_end: date) -> str:
    return (inclusive_end + timedelta(days=1)).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _finite_float(payload: dict[str, Any], key: str) -> float | None:
    value = payload.get(key)
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _build_scene_provenance(
    collection: Any,
    image_count: int,
) -> QuarterlySceneProvenance:
    """Fetch a complete bounded scene list and bind it to a canonical digest."""
    if image_count < 0:
        raise _SafeQuarterFailure("GEE_SCENE_METADATA_INVALID")
    if image_count > MAX_SCENE_PROVENANCE_ITEMS:
        raise _SafeQuarterFailure("GEE_SCENE_SET_TOO_LARGE")
    try:
        ordered_collection = collection.sort("system:index")
        scene_ids = ordered_collection.aggregate_array("system:index").getInfo()
        product_ids = ordered_collection.aggregate_array("PRODUCT_ID").getInfo()
    except Exception as exc:
        raise _SafeQuarterFailure("GEE_SCENE_METADATA_UNAVAILABLE") from exc
    if not isinstance(scene_ids, list) or not isinstance(product_ids, list):
        raise _SafeQuarterFailure("GEE_SCENE_METADATA_INVALID")
    if len(scene_ids) != image_count or len(product_ids) != image_count:
        raise _SafeQuarterFailure("GEE_SCENE_METADATA_INVALID")

    records: list[dict[str, str]] = []
    for scene_id, product_id in zip(scene_ids, product_ids, strict=True):
        if not isinstance(scene_id, str) or not isinstance(product_id, str):
            raise _SafeQuarterFailure("GEE_SCENE_METADATA_INVALID")
        if not _SCENE_IDENTIFIER_RE.fullmatch(scene_id) or not _SCENE_IDENTIFIER_RE.fullmatch(
            product_id
        ):
            raise _SafeQuarterFailure("GEE_SCENE_METADATA_INVALID")
        records.append({"scene_id": scene_id, "product_id": product_id})
    records.sort(key=lambda item: (item["scene_id"], item["product_id"]))
    if len({item["scene_id"] for item in records}) != len(records):
        raise _SafeQuarterFailure("GEE_SCENE_METADATA_INVALID")
    return QuarterlySceneProvenance(
        scene_count=image_count,
        scenes=[Sentinel2SceneReference(**item) for item in records],
        scene_set_sha256=_scene_set_digest(records),
    )


def _download_quarter_map(
    *,
    ee_module: Any,
    collection: Any,
    ndvi: Any,
    roi: Any,
    destination: Path,
    request_session: Any,
) -> None:
    """Download a true-colour context image with a five-level NDVI overlay."""
    rgb = collection.median().select(["B4", "B3", "B2"]).visualize(
        min=0,
        max=3000,
        gamma=1.2,
    )
    classified = ndvi.multiply(0).add(5)
    classified = classified.where(ndvi.lt(0.75), 4)
    classified = classified.where(ndvi.lt(0.60), 3)
    classified = classified.where(ndvi.lt(0.45), 2)
    classified = classified.where(ndvi.lt(0.30), 1)
    overlay = classified.visualize(
        min=1,
        max=5,
        palette=["d64545", "ef8f35", "e4c441", "8cc152", "2f9e44"],
        opacity=0.82,
    )
    boundary = ee_module.Image().byte().paint(
        featureCollection=ee_module.FeatureCollection([ee_module.Feature(roi)]),
        color=1,
        width=2,
    ).visualize(palette=["ffffff"])
    preview = rgb.blend(overlay).blend(boundary)
    url = preview.getThumbURL(
        {
            "region": roi.buffer(100).bounds(),
            "dimensions": 1200,
            "format": "png",
        }
    )
    response = request_session.get(url, timeout=180)
    response.raise_for_status()
    if not response.content.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError("GEE 季度图下载结果不是 PNG")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_bytes(response.content)
    os.replace(temporary, destination)


def collect_gee_quarterly_statistics(
    boundary_path: Path,
    work_dir: Path,
    windows: Sequence[QuarterWindow],
    *,
    output_dir: Path | None = None,
    source_crs: str | None = None,
    max_cloud_pct: float = 30.0,
    scale_m: int = 10,
    project_id: str | None = None,
    download_maps: bool = True,
) -> tuple[list[QuarterlyNDVIObservation], list[str]]:
    """Evaluate per-quarter GEE statistics with the shared Sentinel-2 mask."""
    if not 0 <= max_cloud_pct <= 100:
        raise ValueError("max_cloud_pct 必须位于 0 至 100")
    if not 10 <= scale_m <= 100:
        raise ValueError("scale_m 必须位于 10 至 100 米")
    if download_maps and output_dir is None:
        raise ValueError("下载季度图时必须提供 output_dir")

    try:
        import ee
        import requests
        from shapely.geometry import mapping
        from shapely.ops import unary_union

        root = Path(__file__).resolve().parent.parent
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from space_engine.gee_auth import init_gee
        from growth_analysis import _read_boundary_gdf, _same_crs
    except Exception as exc:
        raise RuntimeError("HISTORICAL_NDVI_DEPENDENCIES_UNAVAILABLE") from exc

    try:
        gee_initialized = init_gee(project_id=project_id)
    except Exception as exc:
        raise RuntimeError("GEE_NOT_INITIALIZED") from exc
    if not gee_initialized:
        raise RuntimeError("GEE_NOT_INITIALIZED")

    try:
        gdf = _read_boundary_gdf(Path(boundary_path), Path(work_dir), source_crs=source_crs)
        if not _same_crs(gdf.crs, "EPSG:4326"):
            gdf = gdf.to_crs("EPSG:4326")
        geometry = mapping(unary_union(gdf.geometry))
        roi = ee.Geometry(geometry)
    except Exception as exc:
        raise RuntimeError("HISTORICAL_NDVI_BOUNDARY_PREPARATION_FAILED") from exc
    request_session = requests.Session()
    observations: list[QuarterlyNDVIObservation] = []
    warnings: list[str] = []

    reducer = (
        ee.Reducer.median()
        .combine(reducer2=ee.Reducer.mean(), sharedInputs=True)
        .combine(reducer2=ee.Reducer.stdDev(), sharedInputs=True)
        .combine(reducer2=ee.Reducer.minMax(), sharedInputs=True)
        .combine(reducer2=ee.Reducer.count(), sharedInputs=True)
    )

    for window in windows:
        if window.status == "not_reached":
            observations.append(
                QuarterlyNDVIObservation(window=window, status="not_reached")
            )
            continue

        assert window.observation_end_date is not None
        image_count = 0
        scene_provenance: QuarterlySceneProvenance | None = None
        try:
            collection = build_s2_sr_collection(
                ee,
                roi=roi,
                start_date=window.start_date.isoformat(),
                end_exclusive=_exclusive_end(window.observation_end_date),
                max_cloud_pct=max_cloud_pct,
            )
            image_count = int(collection.size().getInfo())
            scene_provenance = _build_scene_provenance(collection, image_count)
            if image_count == 0:
                observations.append(
                    QuarterlyNDVIObservation(
                        window=window,
                        status="no_imagery",
                        image_count=0,
                        scene_provenance=scene_provenance,
                    )
                )
                warnings.append(f"{window.label} 没有满足云量阈值的 Sentinel-2 影像")
                continue

            ndvi = collection.median().select("ndvi").clip(roi)
            stats = ndvi.reduceRegion(
                reducer=reducer,
                geometry=roi,
                scale=scale_m,
                bestEffort=False,
                maxPixels=100_000_000,
                tileScale=4,
            ).getInfo()
            # Count the ROI on the same projection/grid as the NDVI composite,
            # otherwise slightly different pixel alignment can yield coverage > 1.
            roi_stats = (
                ndvi.unmask(value=0, sameFootprint=False)
                .multiply(0)
                .add(1)
                .rename("roi")
                .clip(roi)
                .reduceRegion(
                    reducer=ee.Reducer.count(),
                    geometry=roi,
                    scale=scale_m,
                    bestEffort=False,
                    maxPixels=100_000_000,
                    tileScale=4,
                )
                .getInfo()
            )
            valid_count = int(stats.get("ndvi_count") or 0)
            roi_count = int(roi_stats.get("roi") or 0)
            if roi_count <= 0:
                raise _SafeQuarterFailure("GEE_EMPTY_ROI")
            if valid_count <= 0:
                observations.append(
                    QuarterlyNDVIObservation(
                        window=window,
                        status="no_valid_pixels",
                        image_count=image_count,
                        valid_pixel_count=0,
                        roi_pixel_count=roi_count,
                        valid_pixel_coverage=0.0,
                        scene_provenance=scene_provenance,
                    )
                )
                warnings.append(f"{window.label} 有影像但没有可用的地块内 NDVI 像元")
                continue

            coverage = valid_count / roi_count
            map_relative: str | None = None
            map_hash: str | None = None
            if download_maps and output_dir is not None:
                map_relative = f"quarter_maps/{window.year}-Q{window.quarter}.png"
                map_path = output_dir / map_relative
                try:
                    _download_quarter_map(
                        ee_module=ee,
                        collection=collection,
                        ndvi=ndvi,
                        roi=roi,
                        destination=map_path,
                        request_session=request_session,
                    )
                    map_hash = _sha256(map_path)
                except Exception:
                    map_relative = None
                    warnings.append(
                        f"{window.label} 季度图生成失败，统计结果仍保留（GEE_MAP_DOWNLOAD_FAILED）"
                    )

            status: Literal["complete", "partial"] = window.status
            observations.append(
                QuarterlyNDVIObservation(
                    window=window,
                    status=status,
                    image_count=image_count,
                    median_ndvi=_finite_float(stats, "ndvi_median"),
                    mean_ndvi=_finite_float(stats, "ndvi_mean"),
                    stddev_ndvi=_finite_float(stats, "ndvi_stdDev"),
                    min_ndvi=_finite_float(stats, "ndvi_min"),
                    max_ndvi=_finite_float(stats, "ndvi_max"),
                    valid_pixel_count=valid_count,
                    roi_pixel_count=roi_count,
                    valid_pixel_coverage=coverage,
                    quarter_map_png=map_relative,
                    quarter_map_sha256=map_hash,
                    scene_provenance=scene_provenance,
                )
            )
            if coverage < LOW_VALID_COVERAGE:
                warnings.append(
                    f"{window.label} 有效像元覆盖率仅 {coverage:.1%}，季度结论需人工复核"
                )
        except Exception as exc:
            error_code: PublicQuarterErrorCode = (
                exc.code if isinstance(exc, _SafeQuarterFailure) else "GEE_QUARTER_FAILED"
            )
            observations.append(
                QuarterlyNDVIObservation(
                    window=window,
                    status="failed",
                    image_count=image_count,
                    scene_provenance=scene_provenance,
                    error_code=error_code,
                )
            )
            warnings.append(f"{window.label} 计算失败（{error_code}）")
    request_session.close()
    return observations, warnings


def save_historical_ndvi_result(
    result: HistoricalNDVIResult,
    output_path: Path,
) -> dict[str, Any]:
    """Atomically persist a validated UTF-8 JSON result and return its digest."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        result.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            dir=output_path.parent,
            delete=False,
        ) as temporary:
            temp_name = temporary.name
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temp_name, output_path)
    finally:
        if temp_name:
            Path(temp_name).unlink(missing_ok=True)
    return {
        "path": str(output_path),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
    }


def load_historical_ndvi_result(input_path: Path) -> HistoricalNDVIResult:
    """Load JSON and re-run every schema and cross-quarter invariant."""
    with Path(input_path).open("r", encoding="utf-8") as source:
        payload = json.load(source)
    return HistoricalNDVIResult.model_validate(payload)


def run_gee_historical_ndvi(
    *,
    task_id: str,
    scope_label: str,
    boundary_path: Path,
    work_dir: Path,
    output_dir: Path,
    start_year: int,
    end_year: int,
    as_of_date: date | str | None = None,
    parcel_id: str | None = None,
    feature_id: str | None = None,
    boundary_geometry_sha256: str | None = None,
    source_crs: str | None = None,
    max_cloud_pct: float = 30.0,
    scale_m: int = 10,
    project_id: str | None = None,
    download_maps: bool = True,
) -> HistoricalNDVIResult:
    """Plan, collect, validate, and atomically persist one history run."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    windows = plan_quarter_windows(start_year, end_year, as_of_date=as_of_date)
    if as_of_date is None:
        cutoff = date.today()
    elif isinstance(as_of_date, str):
        # plan_quarter_windows has already validated the ISO value.
        cutoff = date.fromisoformat(as_of_date)
    else:
        cutoff = as_of_date

    observations, warnings = collect_gee_quarterly_statistics(
        Path(boundary_path),
        Path(work_dir),
        windows,
        output_dir=output_dir,
        source_crs=source_crs,
        max_cloud_pct=max_cloud_pct,
        scale_m=scale_m,
        project_id=project_id,
        download_maps=download_maps,
    )
    if not boundary_geometry_sha256:
        warnings.append("未绑定规范化地块 geometry SHA-256，接入案件流程前不得作为最终证据")
    result = HistoricalNDVIResult(
        task_id=task_id,
        scope_label=scope_label,
        parcel_id=parcel_id,
        feature_id=feature_id,
        boundary_geometry_sha256=boundary_geometry_sha256,
        generated_at=datetime.now(timezone.utc),
        as_of_date=cutoff,
        start_year=start_year,
        end_year=end_year,
        source=HistoricalNDVISource(max_cloud_pct=max_cloud_pct, scale_m=scale_m),
        quarters=observations,
        warnings=warnings,
        outputs=HistoricalNDVIOutputs(result_json="result.json"),
    )
    save_historical_ndvi_result(result, output_dir / "result.json")
    return result
