"""Multi-KML archives are inspected, never implicitly registered as one parcel."""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).parent.parent / "api_gateway"))

import parcel_preflight as parcel_preflight_module
from parcel_preflight import canonical_geometry_sha256, preflight_kml_zip


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _legacy_gbk_zip(filename: str, content: bytes) -> bytes:
    """Build the legacy unflagged GBK filename form emitted by older GIS tools."""

    encoded_name = filename.encode("gbk")
    placeholder = (b"x" * (len(encoded_name) - 4)) + b".kml"
    payload = _zip_bytes({placeholder.decode("ascii"): content})
    assert payload.count(placeholder) == 2  # local header + central directory
    return payload.replace(placeholder, encoded_name)


def _placemark(name: str, field_id: str, coordinates: str, declared_area: str = "15") -> str:
    return f"""
    <Placemark>
      <name>{name}</name>
      <ExtendedData>
        <Data name="field_id"><value>{field_id}</value></Data>
        <Data name="real_area"><value>{declared_area}</value></Data>
      </ExtendedData>
      <Polygon><outerBoundaryIs><LinearRing><coordinates>
        {coordinates}
      </coordinates></LinearRing></outerBoundaryIs></Polygon>
    </Placemark>
    """


def _kml(*placemarks: str, document_name: str = "田块数据") -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        f"<name>{document_name}</name>{''.join(placemarks)}"
        "</Document></kml>"
    ).encode("utf-8")


SQUARE_A = "114.0000,34.0000 114.0010,34.0000 114.0010,34.0010 114.0000,34.0010 114.0000,34.0000"
SQUARE_B = "114.0020,34.0000 114.0030,34.0000 114.0030,34.0010 114.0020,34.0010 114.0020,34.0000"


def test_preflight_preserves_files_and_features_with_stable_proposed_ids() -> None:
    payload = _zip_bytes(
        {
            "地块/甲村.kml": _kml(
                _placemark("甲一", "source-1", SQUARE_A),
                _placemark("甲二", "source-2", SQUARE_B),
            ),
            "地块/乙村.kml": _kml(
                _placemark(
                    "乙一",
                    "source-3",
                    "114.0040,34.0000 114.0050,34.0000 114.0050,34.0010 114.0040,34.0010 114.0040,34.0000",
                )
            ),
        }
    )

    first = preflight_kml_zip(payload, "地块.zip")
    second = preflight_kml_zip(payload, "地块.zip")

    assert first["status"] == "success"
    assert first["schema_version"] == "parcel_preflight_v1"
    assert first["algorithm_version"] == "multi_kml_preflight_v1"
    assert first["id_scheme"] == "source_path_geometry_v1"
    assert first["geometry_hash_scheme"] == "canonical_geometry_v1"
    assert first["preflight_status"] == "ready_for_mapping"
    assert first["direct_import_allowed"] is False
    assert first["requires_human_confirmation"] is True
    assert first["summary"]["parcel_file_count"] == 2
    assert first["summary"]["feature_count"] == 3
    assert first["summary"]["placemark_count"] == 3
    assert first["summary"]["valid_feature_count"] == 3
    assert {parcel["source_file"] for parcel in first["parcels"]} == {
        "地块/甲村.kml",
        "地块/乙村.kml",
    }

    first_ids = [
        (parcel["parcel_id"], [feature["feature_id"] for feature in parcel["features"]])
        for parcel in first["parcels"]
    ]
    second_ids = [
        (parcel["parcel_id"], [feature["feature_id"] for feature in parcel["features"]])
        for parcel in second["parcels"]
    ]
    assert first_ids == second_ids
    assert all(parcel["mapping"]["confirmed"] is False for parcel in first["parcels"])
    feature = first["parcels"][0]["features"][0]
    assert feature["geometry"]["type"] == "Polygon"
    assert feature["geometry"]["coordinates"][0][0] == feature["geometry"]["coordinates"][0][-1]
    assert feature["geometry_sha256"] == canonical_geometry_sha256(feature["geometry"])
    assert feature["computed_area_mu"] > 0
    assert feature["area_mu"] == feature["computed_area_mu"]


def test_archive_global_placemark_limit_spans_multiple_kml_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(parcel_preflight_module, "MAX_PLACEMARKS", 3)
    payload = _zip_bytes(
        {
            "first.kml": _kml(
                _placemark("first-1", "first-1", SQUARE_A),
                _placemark("first-2", "first-2", SQUARE_B),
            ),
            "second.kml": _kml(
                _placemark("second-1", "second-1", SQUARE_A),
                _placemark("second-2", "second-2", SQUARE_B),
            ),
        }
    )

    result = preflight_kml_zip(payload, "combined-limit.zip")

    assert result["status"] == "error"
    assert result["error_code"] == "TOO_MANY_PLACEMARKS"
    assert "ZIP 内 Placemark 总数" in result["error_message"]
    assert result["direct_import_allowed"] is False


def test_duplicate_geometries_are_separate_candidates_but_require_resolution() -> None:
    geometry = _placemark("同一地块", "one", SQUARE_A)
    payload = _zip_bytes(
        {
            "一.kml": _kml(geometry),
            "二.kml": _kml(_placemark("同一地块副本", "two", SQUARE_A)),
        }
    )

    result = preflight_kml_zip(payload, "duplicates.zip")

    assert result["status"] == "success"
    assert result["preflight_status"] == "needs_human_resolution"
    assert result["summary"]["duplicate_geometry_group_count"] == 1
    assert len({parcel["parcel_id"] for parcel in result["parcels"]}) == 2
    features = [feature for parcel in result["parcels"] for feature in parcel["features"]]
    assert len({feature["feature_id"] for feature in features}) == 2
    assert all(
        "DUPLICATE_GEOMETRY"
        in {warning["code"] for warning in feature["validation"]["warnings"]}
        for feature in features
    )


def test_source_business_id_conflict_is_reported() -> None:
    payload = _zip_bytes(
        {
            "一.kml": _kml(_placemark("地块一", "same-id", SQUARE_A)),
            "二.kml": _kml(_placemark("地块二", "same-id", SQUARE_B)),
        }
    )

    result = preflight_kml_zip(payload, "id-conflict.zip")

    assert result["preflight_status"] == "needs_human_resolution"
    assert result["summary"]["source_id_conflict_count"] == 1
    assert result["source_id_conflicts"][0]["source_business_id"] == "same-id"


def test_self_intersection_is_preserved_and_flagged_for_human_review() -> None:
    bow_tie = "114.0000,34.0000 114.0010,34.0010 114.0010,34.0000 114.0000,34.0010 114.0000,34.0000"
    result = preflight_kml_zip(
        _zip_bytes({"bow-tie.kml": _kml(_placemark("交叉边界", "bow", bow_tie))}),
        "repair.zip",
    )

    assert result["status"] == "success"
    assert result["preflight_status"] == "needs_human_resolution"
    assert result["summary"]["repaired_feature_count"] == 1
    assert result["summary"]["human_resolution_feature_count"] == 1
    assert result["summary"]["originally_valid_feature_count"] == 0
    feature = result["parcels"][0]["features"][0]
    assert feature["geometry"]["type"] == "Polygon"  # Original coordinates remain auditable.
    assert feature["geometry_sha256"]
    assert feature["computed_area_mu"] > 0
    assert "GEOMETRY_REPAIRED_FOR_CANONICAL_USE" in {
        warning["code"] for warning in feature["validation"]["warnings"]
    }


def test_unclosed_ring_is_explicitly_repaired_and_requires_confirmation() -> None:
    unclosed = "114.0000,34.0000 114.0010,34.0000 114.0010,34.0010 114.0000,34.0010"
    result = preflight_kml_zip(
        _zip_bytes({"unclosed.kml": _kml(_placemark("未闭合", "ring", unclosed))}),
        "unclosed.zip",
    )

    assert result["preflight_status"] == "needs_human_resolution"
    feature = result["parcels"][0]["features"][0]
    assert feature["geometry"]["coordinates"][0][0] == feature["geometry"]["coordinates"][0][-1]
    assert "RING_AUTO_CLOSED" in {
        warning["code"] for warning in feature["validation"]["warnings"]
    }


def test_malformed_kml_stays_in_manifest_and_blocks_mapping() -> None:
    payload = _zip_bytes(
        {
            "valid.kml": _kml(_placemark("有效地块", "ok", SQUARE_A)),
            "broken.kml": b"<kml><Placemark>",
        }
    )

    result = preflight_kml_zip(payload, "mixed.zip")

    assert result["status"] == "success"
    assert result["preflight_status"] == "blocked"
    broken = next(parcel for parcel in result["parcels"] if parcel["source_file"] == "broken.kml")
    assert broken["validation"]["status"] == "error"
    assert {error["code"] for error in broken["validation"]["errors"]} == {"MALFORMED_KML"}


def test_preflight_rejects_unsafe_paths_before_parsing() -> None:
    result = preflight_kml_zip(
        _zip_bytes({"../escape.kml": _kml(_placemark("地块", "one", SQUARE_A))}),
        "unsafe.zip",
    )

    assert result == {
        "status": "error",
        "error_code": "UNSAFE_ZIP_PATH",
        "error_message": "ZIP 包含不安全路径",
        "direct_import_allowed": False,
    }


def test_preflight_does_not_expand_xml_entities() -> None:
    unsafe_kml = b"""<?xml version="1.0"?>
    <!DOCTYPE kml [<!ENTITY parcel "secret">]>
    <kml><Document><Placemark><name>&parcel;</name></Placemark></Document></kml>
    """

    result = preflight_kml_zip(_zip_bytes({"unsafe.kml": unsafe_kml}), "xml.zip")

    assert result["status"] == "success"
    assert result["preflight_status"] == "blocked"
    assert result["parcels"][0]["validation"]["errors"][0]["code"] == "UNSAFE_XML_DECLARATION"


def test_preflight_rejects_zip_without_kml() -> None:
    result = preflight_kml_zip(_zip_bytes({"readme.txt": b"not geometry"}), "empty.zip")

    assert result["status"] == "error"
    assert result["error_code"] == "NO_KML_IN_ZIP"
    assert result["direct_import_allowed"] is False


def test_preflight_decodes_unflagged_legacy_gbk_filenames() -> None:
    payload = _legacy_gbk_zip(
        "地块/甲村.kml",
        _kml(_placemark("甲地块", "legacy", SQUARE_A)),
    )

    result = preflight_kml_zip(payload, "legacy.zip")

    assert result["status"] == "success"
    assert result["parcels"][0]["source_file"] == "地块/甲村.kml"
    assert result["parcels"][0]["source_file_name_encoding"] == "gb18030"
