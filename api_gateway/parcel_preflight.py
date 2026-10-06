"""Read-only preflight for archives containing multiple KML parcels.

This module intentionally stops before policy or claim persistence.  It turns a
KML ZIP into a review manifest that keeps every source file and Placemark
separate, proposes stable identifiers, and records geometry/area/duplicate
checks.  A caller must map the proposed parcels to policy versions and collect
human confirmation before passing any geometry to the existing registration
workflow.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
import stat
import unicodedata
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping
from xml.etree import ElementTree


PREFLIGHT_SCHEMA_VERSION = "parcel_preflight_v1"
PREFLIGHT_ALGORITHM_VERSION = "multi_kml_preflight_v1"
BOUNDARY_HASH_SCHEME = "canonical_geometry_v1"
PROPOSED_ID_SCHEME = "source_path_geometry_v1"

MAX_ARCHIVE_BYTES = 50 * 1024 * 1024
MAX_ARCHIVE_FILES = 256
MAX_KML_FILES = 128
MAX_KML_MEMBER_BYTES = 20 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_COMPRESSION_RATIO = 500
MAX_PLACEMARKS = 10_000
MAX_COORDINATE_TUPLES = 2_000_000
MIN_AREA_SQM = 1.0
AREA_REVIEW_THRESHOLD_SQM = 100 * 1_000_000  # 100 km2 is unusual for one parcel.
DECLARED_AREA_WARNING_RATIO = 0.05
RESOLUTION_REQUIRED_WARNING_CODES = {
    "GEOMETRY_REPAIRED_FOR_CANONICAL_USE",
    "RING_AUTO_CLOSED",
    "AREA_UNUSUALLY_LARGE",
    "DECLARED_AREA_MISMATCH",
    "DUPLICATE_GEOMETRY",
    "SOURCE_ID_GEOMETRY_CONFLICT",
}


class ParcelPreflightError(ValueError):
    """Expected intake error with a stable machine-readable code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _issue(code: str, message: str, **details: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"code": code, "message": message}
    result.update(details)
    return result


def _normalise_archive_path(filename: str) -> str:
    raw = unicodedata.normalize("NFC", str(filename).replace("\\", "/"))
    path = PurePosixPath(raw)
    if (
        not raw
        or len(raw) > 512
        or "\x00" in raw
        or path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or re.fullmatch(r"[A-Za-z]:", path.parts[0]) is not None
    ):
        raise ParcelPreflightError("UNSAFE_ZIP_PATH", "ZIP 包含不安全路径")
    return path.as_posix()


def _legacy_filename_penalty(value: str) -> int:
    """Prefer readable legacy names over CP437 mojibake without guessing ASCII names."""

    penalty = 0
    for character in value:
        codepoint = ord(character)
        if 0x2500 <= codepoint <= 0x259F:  # box drawing/block glyphs are typical GBK mojibake
            penalty += 6
        elif 0x0370 <= codepoint <= 0x03FF or 0x2200 <= codepoint <= 0x22FF:
            penalty += 2
        elif not character.isprintable():
            penalty += 8
    return penalty


def _zip_member_name(member: zipfile.ZipInfo) -> tuple[str, str]:
    """Decode UTF-8 ZIP names and common legacy GB18030/GBK names safely.

    Python 3.10 decodes unflagged ZIP names as CP437 and has no
    ``metadata_encoding`` argument.  Many Chinese GIS exports use GBK without
    setting the UTF-8 flag, so retain the ZipInfo for reads while deriving a
    separate logical name for the manifest and all path checks.
    """

    decoded_by_zipfile = member.filename
    if member.flag_bits & 0x800:
        return unicodedata.normalize("NFC", decoded_by_zipfile), "utf-8"
    try:
        raw_name = decoded_by_zipfile.encode("cp437")
    except UnicodeEncodeError:
        return unicodedata.normalize("NFC", decoded_by_zipfile), "zip-default"

    candidates: list[tuple[str, str]] = [(decoded_by_zipfile, "cp437")]
    for encoding in ("utf-8", "gb18030"):
        try:
            candidate = raw_name.decode(encoding)
        except UnicodeDecodeError:
            continue
        candidates.append((candidate, encoding))
    value, encoding = min(
        candidates,
        key=lambda item: (_legacy_filename_penalty(item[0]), candidates.index(item)),
    )
    return unicodedata.normalize("NFC", value), encoding


def _validated_kml_members(archive: zipfile.ZipFile) -> tuple[list[zipfile.ZipInfo], list[str], int]:
    members = archive.infolist()
    if not members:
        raise ParcelPreflightError("EMPTY_ZIP", "ZIP 中没有文件")
    if len(members) > MAX_ARCHIVE_FILES:
        raise ParcelPreflightError(
            "TOO_MANY_ZIP_MEMBERS",
            f"ZIP 文件数量超过上限 {MAX_ARCHIVE_FILES}",
        )

    total_size = 0
    seen_paths: set[str] = set()
    kml_members: list[zipfile.ZipInfo] = []
    ignored_files: list[str] = []
    for member in members:
        logical_name, _ = _zip_member_name(member)
        normalised_path = _normalise_archive_path(logical_name)
        path_key = normalised_path.casefold()
        if path_key in seen_paths:
            raise ParcelPreflightError("DUPLICATE_ZIP_PATH", f"ZIP 路径重复: {normalised_path}")
        seen_paths.add(path_key)

        if member.flag_bits & 0x1:
            raise ParcelPreflightError("ENCRYPTED_ZIP_MEMBER", "ZIP 包含加密文件")
        mode = (member.external_attr >> 16) & 0o170000
        if mode == stat.S_IFLNK:
            raise ParcelPreflightError("ZIP_SYMLINK", "ZIP 包含符号链接")
        if member.file_size < 0 or member.compress_size < 0:
            raise ParcelPreflightError("INVALID_ZIP_SIZE", "ZIP 文件大小信息无效")
        total_size += member.file_size
        if total_size > MAX_UNCOMPRESSED_BYTES:
            raise ParcelPreflightError(
                "ZIP_TOO_LARGE_UNCOMPRESSED",
                f"ZIP 解压总大小超过 {MAX_UNCOMPRESSED_BYTES // (1024 * 1024)} MB 上限",
            )
        if member.file_size and not member.is_dir():
            ratio = member.file_size / max(member.compress_size, 1)
            if ratio > MAX_COMPRESSION_RATIO:
                raise ParcelPreflightError("SUSPICIOUS_COMPRESSION_RATIO", "ZIP 压缩比异常")

        if member.is_dir():
            continue
        if PurePosixPath(normalised_path).suffix.lower() == ".kml":
            if member.file_size > MAX_KML_MEMBER_BYTES:
                raise ParcelPreflightError(
                    "KML_MEMBER_TOO_LARGE",
                    f"KML 单文件超过 {MAX_KML_MEMBER_BYTES // (1024 * 1024)} MB 上限",
                )
            kml_members.append(member)
        else:
            ignored_files.append(normalised_path)

    if not kml_members:
        raise ParcelPreflightError("NO_KML_IN_ZIP", "ZIP 中没有可预检的 .kml 文件")
    if len(kml_members) > MAX_KML_FILES:
        raise ParcelPreflightError("TOO_MANY_KML_FILES", f"KML 数量超过上限 {MAX_KML_FILES}")
    return kml_members, sorted(ignored_files, key=str.casefold), total_size


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _direct_child_text(element: ElementTree.Element, name: str) -> str | None:
    for child in element:
        if _local_name(child.tag) == name:
            text = "".join(child.itertext()).strip()
            return text or None
    return None


def _extended_properties(placemark: ElementTree.Element) -> dict[str, str]:
    properties: dict[str, str] = {}
    for element in placemark.iter():
        local_name = _local_name(element.tag)
        if local_name == "Data":
            key = str(element.attrib.get("name") or "").strip()
            if not key:
                continue
            value = next(
                (
                    "".join(child.itertext()).strip()
                    for child in element
                    if _local_name(child.tag) == "value"
                ),
                "",
            )
            properties[key] = value
        elif local_name == "SimpleData":
            key = str(element.attrib.get("name") or "").strip()
            if key:
                properties[key] = "".join(element.itertext()).strip()
    return properties


def _coordinate_ring(
    text: str | None,
    coordinate_counter: list[int],
    warnings: list[dict[str, Any]],
) -> list[list[float]]:
    if not text:
        raise ValueError("LinearRing 缺少 coordinates")
    coordinates: list[list[float]] = []
    for token in text.split():
        components = token.split(",")
        if len(components) < 2:
            raise ValueError("KML 坐标格式无效")
        try:
            longitude = float(components[0])
            latitude = float(components[1])
        except ValueError as exc:
            raise ValueError("KML 坐标不是有效数字") from exc
        if not math.isfinite(longitude) or not math.isfinite(latitude):
            raise ValueError("KML 坐标包含非有限值")
        if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
            raise ValueError("KML 坐标超出 WGS84 范围")
        coordinates.append([longitude, latitude])
        coordinate_counter[0] += 1
        if coordinate_counter[0] > MAX_COORDINATE_TUPLES:
            raise ParcelPreflightError(
                "TOO_MANY_COORDINATES",
                f"坐标点总数超过上限 {MAX_COORDINATE_TUPLES}",
            )
    if len(coordinates) < 3:
        raise ValueError("LinearRing 至少需要三个不同坐标点")
    if coordinates[0] != coordinates[-1]:
        warnings.append(
            _issue(
                "RING_AUTO_CLOSED",
                "源 LinearRing 未闭合；预检已补齐闭合点，入库前必须人工复核",
            )
        )
        coordinates.append(coordinates[0].copy())
    if len(coordinates) < 4:
        raise ValueError("闭合 LinearRing 至少需要四个坐标")
    return coordinates


def _boundary_ring(
    boundary: ElementTree.Element,
    coordinate_counter: list[int],
    warnings: list[dict[str, Any]],
) -> list[list[float]]:
    for ring in boundary.iter():
        if _local_name(ring.tag) != "LinearRing":
            continue
        coordinates = next(
            (
                "".join(child.itertext()).strip()
                for child in ring.iter()
                if _local_name(child.tag) == "coordinates"
            ),
            None,
        )
        return _coordinate_ring(coordinates, coordinate_counter, warnings)
    raise ValueError("边界缺少 LinearRing")


def _polygon_geometry(
    polygon: ElementTree.Element,
    coordinate_counter: list[int],
    warnings: list[dict[str, Any]],
) -> dict[str, Any]:
    outer_boundaries = [
        child for child in polygon if _local_name(child.tag) == "outerBoundaryIs"
    ]
    if len(outer_boundaries) != 1:
        raise ValueError("Polygon 必须且只能包含一个 outerBoundaryIs")
    rings = [_boundary_ring(outer_boundaries[0], coordinate_counter, warnings)]
    rings.extend(
        _boundary_ring(child, coordinate_counter, warnings)
        for child in polygon
        if _local_name(child.tag) == "innerBoundaryIs"
    )
    return {"type": "Polygon", "coordinates": rings}


def _placemark_geometry(
    placemark: ElementTree.Element,
    coordinate_counter: list[int],
    warnings: list[dict[str, Any]],
) -> dict[str, Any]:
    polygons = [
        _polygon_geometry(element, coordinate_counter, warnings)
        for element in placemark.iter()
        if _local_name(element.tag) == "Polygon"
    ]
    if not polygons:
        raise ValueError("Placemark 不包含 Polygon；点、线不能作为承保地块")
    if len(polygons) == 1:
        return polygons[0]
    return {
        "type": "MultiPolygon",
        "coordinates": [polygon["coordinates"] for polygon in polygons],
    }


def _polygonal_shape(geometry: Mapping[str, Any]):
    from shapely import make_valid
    from shapely.geometry import GeometryCollection, MultiPolygon, shape
    from shapely.ops import unary_union

    original = shape(geometry)
    repaired = make_valid(original)
    if isinstance(repaired, GeometryCollection):
        polygon_parts = [
            part for part in repaired.geoms if part.geom_type in {"Polygon", "MultiPolygon"}
        ]
        repaired = unary_union(polygon_parts) if polygon_parts else repaired
    if repaired.is_empty or repaired.geom_type not in {"Polygon", "MultiPolygon"}:
        raise ValueError("几何修复后没有有效面要素")
    if isinstance(repaired, MultiPolygon) and len(repaired.geoms) == 1:
        repaired = repaired.geoms[0]
    return original, repaired


def canonical_geometry_sha256(geometry: Mapping[str, Any]) -> str:
    """Hash a polygon using the policy boundary ``canonical_geometry_v1`` scheme."""

    from shapely.geometry import mapping

    _, candidate = _polygonal_shape(geometry)
    if hasattr(candidate, "normalize"):
        candidate = candidate.normalize()
    canonical = json.dumps(
        mapping(candidate),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _area_sqm(geometry: Any) -> float:
    from pyproj import Transformer
    from shapely.ops import transform

    transformer = Transformer.from_crs("EPSG:4326", "EPSG:6933", always_xy=True)
    return float(transform(transformer.transform, geometry).area)


def _declared_area_mu(properties: Mapping[str, str]) -> tuple[float | None, str | None]:
    for key in ("real_area", "area_mu", "insured_area_mu", "total_area"):
        raw_value = properties.get(key)
        if raw_value in (None, ""):
            continue
        try:
            value = float(str(raw_value).replace(",", "").strip())
        except ValueError:
            return None, key
        return value if math.isfinite(value) and value > 0 else None, key
    return None, None


def _stable_id(prefix: str, *components: Any) -> str:
    seed = "\x1f".join(str(component) for component in components)
    return f"{prefix}-{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:20]}"


def _validation_status(errors: Iterable[Mapping[str, Any]], warnings: Iterable[Mapping[str, Any]]) -> str:
    if any(True for _ in errors):
        return "error"
    if any(True for _ in warnings):
        return "warning"
    return "ok"


def _parse_feature(
    placemark: ElementTree.Element,
    *,
    source_path: str,
    source_index: int,
    coordinate_counter: list[int],
) -> tuple[dict[str, Any], Any | None]:
    name = _direct_child_text(placemark, "name")
    properties = _extended_properties(placemark)
    business_id = str(
        properties.get("field_id")
        or properties.get("parcel_id")
        or properties.get("plot_id")
        or ""
    ).strip() or None
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    geometry: dict[str, Any] | None = None
    geometry_hash: str | None = None
    computed_area_sqm: float | None = None
    effective_shape = None
    original_valid: bool | None = None
    validity_reason: str | None = None

    try:
        geometry = _placemark_geometry(placemark, coordinate_counter, warnings)
        original_shape, effective_shape = _polygonal_shape(geometry)
        original_valid = bool(original_shape.is_valid)
        if not original_valid:
            from shapely.validation import explain_validity

            validity_reason = explain_validity(original_shape)
            warnings.append(
                _issue(
                    "GEOMETRY_REPAIRED_FOR_CANONICAL_USE",
                    "源几何无效；已用 make_valid 生成规范哈希和面积，入库前必须人工复核",
                    reason=validity_reason,
                )
            )
        geometry_hash = canonical_geometry_sha256(geometry)
        computed_area_sqm = _area_sqm(effective_shape)
        if not math.isfinite(computed_area_sqm) or computed_area_sqm < MIN_AREA_SQM:
            errors.append(_issue("AREA_TOO_SMALL_OR_INVALID", "地块面积无效或小于 1 平方米"))
        elif computed_area_sqm > AREA_REVIEW_THRESHOLD_SQM:
            warnings.append(
                _issue(
                    "AREA_UNUSUALLY_LARGE",
                    "单个地块面积超过 100 平方公里，需确认坐标和边界",
                )
            )
    except ParcelPreflightError:
        raise
    except Exception as exc:  # noqa: BLE001 - each bad Placemark must remain in the manifest
        errors.append(_issue("INVALID_GEOMETRY", str(exc)))

    declared_area_mu, declared_area_field = _declared_area_mu(properties)
    if declared_area_field and declared_area_mu is None:
        warnings.append(
            _issue(
                "INVALID_DECLARED_AREA",
                f"声明面积字段 {declared_area_field} 不是有效正数",
            )
        )
    computed_area_mu = computed_area_sqm * 0.0015 if computed_area_sqm is not None else None
    area_difference_ratio: float | None = None
    if declared_area_mu is not None and computed_area_mu is not None:
        area_difference_ratio = abs(declared_area_mu - computed_area_mu) / max(
            declared_area_mu,
            computed_area_mu,
        )
        if area_difference_ratio > DECLARED_AREA_WARNING_RATIO:
            warnings.append(
                _issue(
                    "DECLARED_AREA_MISMATCH",
                    "声明面积与边界计算面积偏差超过 5%",
                    difference_ratio=round(area_difference_ratio, 6),
                )
            )

    feature_seed = geometry_hash or hashlib.sha256(
        json.dumps(geometry, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if geometry
        else f"invalid:{source_index}".encode("utf-8")
    ).hexdigest()
    proposed_feature_id = _stable_id(
        "feature",
        PROPOSED_ID_SCHEME,
        source_path.casefold(),
        source_index,
        business_id or "",
        feature_seed,
    )
    feature = {
        "feature_id": proposed_feature_id,
        "proposed_feature_id": proposed_feature_id,
        "id_status": "proposed",
        "source_feature_index": source_index,
        "source_business_id": business_id,
        "name": name,
        "properties": properties,
        "geometry": geometry,
        "geometry_sha256": geometry_hash,
        "geometry_hash_scheme": BOUNDARY_HASH_SCHEME if geometry_hash else None,
        "computed_area_sqm": round(computed_area_sqm, 2) if computed_area_sqm is not None else None,
        "computed_area_mu": round(computed_area_mu, 4) if computed_area_mu is not None else None,
        # ``area_mu`` is the downstream parcel-growth manifest compatibility field.
        # It is computed from geometry; the untrusted source value remains separate.
        "area_mu": round(computed_area_mu, 4) if computed_area_mu is not None else None,
        "declared_area_mu": declared_area_mu,
        "declared_area_source_field": declared_area_field,
        "declared_area_difference_ratio": (
            round(area_difference_ratio, 6) if area_difference_ratio is not None else None
        ),
        "validation": {
            "status": _validation_status(errors, warnings),
            "original_geometry_valid": original_valid,
            "original_geometry_validity_reason": validity_reason,
            "errors": errors,
            "warnings": warnings,
        },
    }
    return feature, effective_shape


def _read_member(archive: zipfile.ZipFile, member: zipfile.ZipInfo) -> bytes:
    with archive.open(member) as source:
        payload = source.read(MAX_KML_MEMBER_BYTES + 1)
    if len(payload) > MAX_KML_MEMBER_BYTES:
        raise ParcelPreflightError("KML_MEMBER_TOO_LARGE", "KML 实际内容超过读取上限")
    return payload


def _parse_kml_file(
    source_path: str,
    payload: bytes,
    coordinate_counter: list[int],
    placemark_counter: list[int],
) -> tuple[dict[str, Any], list[Any]]:
    source_sha256 = hashlib.sha256(payload).hexdigest()
    file_errors: list[dict[str, Any]] = []
    file_warnings: list[dict[str, Any]] = []
    features: list[dict[str, Any]] = []
    effective_shapes: list[Any] = []
    document_name: str | None = None

    if re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", payload, flags=re.IGNORECASE):
        file_errors.append(
            _issue("UNSAFE_XML_DECLARATION", "KML 包含 DOCTYPE/ENTITY 声明，拒绝解析")
        )
    else:
        try:
            root = ElementTree.fromstring(payload)
            document = next(
                (element for element in root.iter() if _local_name(element.tag) == "Document"),
                None,
            )
            if document is not None:
                document_name = _direct_child_text(document, "name")
            placemarks = [
                element for element in root.iter() if _local_name(element.tag) == "Placemark"
            ]
            placemark_counter[0] += len(placemarks)
            if placemark_counter[0] > MAX_PLACEMARKS:
                raise ParcelPreflightError(
                    "TOO_MANY_PLACEMARKS",
                    f"ZIP 内 Placemark 总数超过上限 {MAX_PLACEMARKS}",
                )
            if not placemarks:
                file_errors.append(_issue("NO_PLACEMARK", "KML 中没有 Placemark"))
            for feature_index, placemark in enumerate(placemarks, start=1):
                feature, effective_shape = _parse_feature(
                    placemark,
                    source_path=source_path,
                    source_index=feature_index,
                    coordinate_counter=coordinate_counter,
                )
                features.append(feature)
                if effective_shape is not None and not feature["validation"]["errors"]:
                    effective_shapes.append(effective_shape)
        except ParcelPreflightError:
            raise
        except (ElementTree.ParseError, ValueError) as exc:
            file_errors.append(_issue("MALFORMED_KML", f"KML XML 无法解析: {exc}"))

    parcel_geometry_hash: str | None = None
    parcel_area_sqm: float | None = None
    if effective_shapes:
        from shapely.geometry import mapping
        from shapely.ops import unary_union

        union_shape = unary_union(effective_shapes)
        parcel_geometry_hash = canonical_geometry_sha256(mapping(union_shape))
        parcel_area_sqm = _area_sqm(union_shape)
    proposed_parcel_id = _stable_id(
        "parcel",
        PROPOSED_ID_SCHEME,
        source_path.casefold(),
        parcel_geometry_hash or source_sha256,
    )
    for feature in features:
        feature["parcel_id"] = proposed_parcel_id

    if any(feature["validation"]["errors"] for feature in features):
        file_errors.append(
            _issue("INVALID_FEATURES_PRESENT", "文件中至少一个 Placemark 未通过几何校验")
        )
    parcel = {
        "parcel_id": proposed_parcel_id,
        "proposed_parcel_id": proposed_parcel_id,
        "id_status": "proposed",
        "source_file": source_path,
        "source_file_sha256": source_sha256,
        "document_name": document_name,
        "feature_count": len(features),
        "valid_feature_count": sum(
            1 for feature in features if not feature["validation"]["errors"]
        ),
        "geometry_sha256": parcel_geometry_hash,
        "geometry_hash_scheme": BOUNDARY_HASH_SCHEME if parcel_geometry_hash else None,
        "computed_area_sqm": round(parcel_area_sqm, 2) if parcel_area_sqm is not None else None,
        "area_mu": round(parcel_area_sqm * 0.0015, 4) if parcel_area_sqm is not None else None,
        "features": features,
        "validation": {
            "status": _validation_status(file_errors, file_warnings),
            "errors": file_errors,
            "warnings": file_warnings,
        },
        "mapping": {
            "policy_id": None,
            "policy_version_id": None,
            "confirmed": False,
            "confirmed_by": None,
            "confirmed_at": None,
        },
    }
    return parcel, effective_shapes


def _append_feature_warning(feature: dict[str, Any], warning: dict[str, Any]) -> None:
    feature["validation"]["warnings"].append(warning)
    if not feature["validation"]["errors"]:
        feature["validation"]["status"] = "warning"


def _mark_duplicate_groups(parcels: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_geometry: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_business_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for parcel in parcels:
        for feature in parcel["features"]:
            geometry_hash = feature.get("geometry_sha256")
            if geometry_hash:
                by_geometry[geometry_hash].append(feature)
            business_id = feature.get("source_business_id")
            if business_id:
                by_business_id[str(business_id).casefold()].append(feature)

    duplicate_groups: list[dict[str, Any]] = []
    for geometry_hash, features in sorted(by_geometry.items()):
        if len(features) < 2:
            continue
        feature_ids = sorted(feature["feature_id"] for feature in features)
        duplicate_groups.append(
            {
                "geometry_sha256": geometry_hash,
                "feature_ids": feature_ids,
                "resolution_required": True,
            }
        )
        for feature in features:
            _append_feature_warning(
                feature,
                _issue(
                    "DUPLICATE_GEOMETRY",
                    "其他要素具有相同规范几何，映射前必须确认是否重复地块",
                    duplicate_feature_ids=[
                        feature_id for feature_id in feature_ids if feature_id != feature["feature_id"]
                    ],
                ),
            )

    source_id_conflicts: list[dict[str, Any]] = []
    for business_id, features in sorted(by_business_id.items()):
        geometry_hashes = {feature.get("geometry_sha256") for feature in features}
        if len(features) < 2 or len(geometry_hashes) <= 1:
            continue
        feature_ids = sorted(feature["feature_id"] for feature in features)
        source_id_conflicts.append(
            {
                "source_business_id": business_id,
                "feature_ids": feature_ids,
                "resolution_required": True,
            }
        )
        for feature in features:
            _append_feature_warning(
                feature,
                _issue(
                    "SOURCE_ID_GEOMETRY_CONFLICT",
                    "相同源业务编号对应不同几何，映射前必须核实",
                ),
            )
    return duplicate_groups, source_id_conflicts


def _refresh_parcel_validation(parcels: list[dict[str, Any]]) -> None:
    for parcel in parcels:
        feature_errors = any(feature["validation"]["errors"] for feature in parcel["features"])
        feature_warnings = any(feature["validation"]["warnings"] for feature in parcel["features"])
        if parcel["validation"]["errors"] or feature_errors:
            parcel["validation"]["status"] = "error"
        elif parcel["validation"]["warnings"] or feature_warnings:
            parcel["validation"]["status"] = "warning"
        else:
            parcel["validation"]["status"] = "ok"


def preflight_kml_zip(upload_bytes: bytes, filename: str = "parcels.zip") -> dict[str, Any]:
    """Inspect a multi-KML ZIP and return a non-importing review manifest.

    ``status=success`` means the archive was safely inspected, not that it may be
    imported.  ``direct_import_allowed`` is always false.  Callers must check
    ``preflight_status``, complete every parcel's ``mapping`` object, and record
    a separate human confirmation before invoking a policy-version write path.
    """

    try:
        if not isinstance(upload_bytes, (bytes, bytearray, memoryview)):
            raise ParcelPreflightError("INVALID_UPLOAD", "预检输入必须是 ZIP 字节")
        payload = bytes(upload_bytes)
        if not payload:
            raise ParcelPreflightError("EMPTY_UPLOAD", "上传内容为空")
        if len(payload) > MAX_ARCHIVE_BYTES:
            raise ParcelPreflightError(
                "ARCHIVE_TOO_LARGE",
                f"ZIP 压缩文件超过 {MAX_ARCHIVE_BYTES // (1024 * 1024)} MB 上限",
            )
        safe_filename = PurePosixPath(str(filename).replace("\\", "/")).name or "parcels.zip"
        if PurePosixPath(safe_filename).suffix.lower() != ".zip":
            raise ParcelPreflightError("NOT_A_ZIP", "批量地块预检仅接受 .zip 文件")

        archive_sha256 = hashlib.sha256(payload).hexdigest()
        coordinate_counter = [0]
        placemark_counter = [0]
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            kml_members, ignored_files, total_uncompressed_bytes = _validated_kml_members(archive)
            corrupt_member = archive.testzip()
            if corrupt_member is not None:
                raise ParcelPreflightError("CORRUPT_ZIP_MEMBER", f"ZIP 成员 CRC 校验失败: {corrupt_member}")
            parcels: list[dict[str, Any]] = []
            for member in sorted(
                kml_members,
                key=lambda item: _normalise_archive_path(_zip_member_name(item)[0]).casefold(),
            ):
                logical_name, filename_encoding = _zip_member_name(member)
                source_path = _normalise_archive_path(logical_name)
                parcel, _ = _parse_kml_file(
                    source_path,
                    _read_member(archive, member),
                    coordinate_counter,
                    placemark_counter,
                )
                parcel["source_file_name_encoding"] = filename_encoding
                parcels.append(parcel)

        duplicate_groups, source_id_conflicts = _mark_duplicate_groups(parcels)
        _refresh_parcel_validation(parcels)
        all_features = [feature for parcel in parcels for feature in parcel["features"]]
        invalid_feature_count = sum(
            1 for feature in all_features if feature["validation"]["errors"]
        )
        file_error_count = sum(
            1 for parcel in parcels if parcel["validation"]["errors"]
        )
        resolution_required_features = [
            feature
            for feature in all_features
            if any(
                warning["code"] in RESOLUTION_REQUIRED_WARNING_CODES
                for warning in feature["validation"]["warnings"]
            )
        ]
        if file_error_count or invalid_feature_count:
            preflight_status = "blocked"
        elif resolution_required_features:
            preflight_status = "needs_human_resolution"
        else:
            preflight_status = "ready_for_mapping"

        valid_features = [
            feature
            for feature in all_features
            if feature.get("computed_area_mu") is not None and not feature["validation"]["errors"]
        ]
        manifest = {
            "status": "success",
            "schema_version": PREFLIGHT_SCHEMA_VERSION,
            "algorithm_version": PREFLIGHT_ALGORITHM_VERSION,
            "preflight_status": preflight_status,
            "direct_import_allowed": False,
            "requires_policy_version_mapping": True,
            "requires_human_confirmation": True,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source": {
                "archive_filename": safe_filename,
                "archive_sha256": archive_sha256,
                "archive_size_bytes": len(payload),
                "uncompressed_size_bytes": total_uncompressed_bytes,
                "kml_file_count": len(parcels),
                "ignored_files": ignored_files,
            },
            "id_scheme": PROPOSED_ID_SCHEME,
            "geometry_hash_scheme": BOUNDARY_HASH_SCHEME,
            "summary": {
                "parcel_file_count": len(parcels),
                "feature_count": len(all_features),
                "valid_feature_count": len(valid_features),
                "invalid_feature_count": invalid_feature_count,
                "originally_valid_feature_count": sum(
                    1
                    for feature in all_features
                    if feature["validation"]["original_geometry_valid"] is True
                    and not feature["validation"]["errors"]
                ),
                "repaired_feature_count": sum(
                    1
                    for feature in all_features
                    if feature["validation"]["original_geometry_valid"] is False
                    and not feature["validation"]["errors"]
                ),
                "warning_feature_count": sum(
                    1 for feature in all_features if feature["validation"]["warnings"]
                ),
                "human_resolution_feature_count": len(resolution_required_features),
                "duplicate_geometry_group_count": len(duplicate_groups),
                "source_id_conflict_count": len(source_id_conflicts),
                "coordinate_tuple_count": coordinate_counter[0],
                "placemark_count": placemark_counter[0],
                "feature_area_sum_mu": round(
                    sum(feature["computed_area_mu"] for feature in valid_features),
                    4,
                ),
            },
            "duplicate_geometry_groups": duplicate_groups,
            "source_id_conflicts": source_id_conflicts,
            "parcels": parcels,
            "workflow": {
                "current_stage": "preflight_complete",
                "next_stage": "policy_version_mapping",
                "required_per_parcel": [
                    "policy_id",
                    "policy_version_id",
                    "confirmed",
                    "confirmed_by",
                    "confirmed_at",
                ],
                "rules": [
                    "不得把整个 ZIP 合并成一个承保边界",
                    "每个 proposed parcel_id 必须映射到明确的保单版本",
                    "重复几何和源编号冲突必须先解决",
                    "边界变化必须创建新保单版本，不得覆盖已有理赔引用的边界",
                    "人工确认凭证应绑定 archive_sha256、geometry_sha256 和最终映射",
                ],
            },
        }
        manifest["manifest_sha256"] = hashlib.sha256(
            json.dumps(
                manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return manifest
    except ParcelPreflightError as exc:
        return {
            "status": "error",
            "error_code": exc.code,
            "error_message": str(exc),
            "direct_import_allowed": False,
        }
    except zipfile.BadZipFile:
        return {
            "status": "error",
            "error_code": "BAD_ZIP",
            "error_message": "上传内容不是有效 ZIP",
            "direct_import_allowed": False,
        }


__all__ = [
    "BOUNDARY_HASH_SCHEME",
    "PREFLIGHT_ALGORITHM_VERSION",
    "PREFLIGHT_SCHEMA_VERSION",
    "PROPOSED_ID_SCHEME",
    "canonical_geometry_sha256",
    "preflight_kml_zip",
]
