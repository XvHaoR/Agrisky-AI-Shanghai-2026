"""Pure manifest, hashing, and statistics tests for parcel-level NDVI."""

from __future__ import annotations

import re

import pytest

from parcel_growth import (
    BOUNDARY_HASH_SCHEME,
    CLASSIFICATION_SCHEME_VERSION,
    PARCEL_GROWTH_ALGORITHM_VERSION,
    PARCEL_GROWTH_SCHEMA_VERSION,
    build_growth_statistics,
    canonical_geometry_sha256,
    normalize_parcel_manifest,
)


def _polygon(x0: float, y0: float, x1: float, y1: float) -> dict:
    return {
        "type": "Polygon",
        "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]],
    }


def _versioned_parcel_manifest(**overrides: object) -> dict:
    manifest = {
        "schema_version": "parcel_preflight_v1",
        "algorithm_version": "multi_kml_preflight_v1",
        "id_scheme": "source_path_geometry_v1",
        "geometry_hash_scheme": "canonical_geometry_v1",
        "manifest_sha256": "a" * 64,
        "preflight_status": "human_confirmed",
        "parcels": [
            {
                "parcel_id": "P-1",
                "mapping": {
                    "confirmed": True,
                    "policy_id": "POL-1",
                    "policy_version_id": "POL-1:v1",
                },
                "features": [
                    {
                        "feature_id": "F-1",
                        "geometry": _polygon(0, 0, 1, 1),
                    }
                ],
            }
        ],
    }
    manifest.update(overrides)
    return manifest


@pytest.mark.parametrize("field_name", ["algorithm_version", "id_scheme", "geometry_hash_scheme"])
def test_confirmed_manifest_requires_all_identity_schemes(field_name: str) -> None:
    manifest = _versioned_parcel_manifest()
    manifest.pop(field_name)
    with pytest.raises(ValueError, match=field_name):
        normalize_parcel_manifest(manifest, require_confirmed=True)


def test_normalizes_feature_collection_without_merging_stable_ids() -> None:
    normalized = normalize_parcel_manifest(
        {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "id": "source-one",
                    "properties": {"parcel_id": "P-001", "feature_id": "F-001", "area_mu": 12},
                    "geometry": _polygon(0, 0, 1, 1),
                },
                {
                    "type": "Feature",
                    "id": "F-002",
                    "properties": {"plot_id": "P-001"},
                    "geometry": _polygon(1, 0, 2, 1),
                },
            ],
        }
    )

    assert normalized["source_crs"] == "EPSG:4326"
    assert [item["parcel_id"] for item in normalized["features"]] == ["P-001", "P-001"]
    assert [item["feature_id"] for item in normalized["features"]] == ["F-001", "F-002"]
    assert normalized["features"][0]["declared_area_mu"] == 12
    assert normalized["features"][1]["source_index"] == 1


def test_normalizes_multi_feature_parcel_and_keeps_parent_area_at_parcel_level() -> None:
    normalized = normalize_parcel_manifest(
        {
            "source_crs": "EPSG:4490",
            "schema_version": "parcel_preflight_v1",
            "manifest_sha256": "a" * 64,
            "source": {"archive_sha256": "b" * 64},
            "total_area_mu": 20,
            "parcels": [
                {
                    "parcel_id": "P-100",
                    "area_mu": 20,
                    "source_file": "甲村.kml",
                    "mapping": {"policy_id": "POL-1", "confirmed": True},
                    "features": [
                        {"feature_id": "F-A", "geometry": _polygon(0, 0, 1, 1)},
                        {"feature_id": "F-B", "geometry": _polygon(1, 0, 2, 1)},
                    ],
                }
            ],
        }
    )

    assert normalized["source_crs"] == "EPSG:4490"
    assert normalized["total_area_mu"] == 20
    assert normalized["provenance"]["schema_version"] == "parcel_preflight_v1"
    assert normalized["provenance"]["archive_sha256"] == "b" * 64
    assert [item["declared_area_mu"] for item in normalized["features"]] == [None, None]
    assert [item["parcel_declared_area_mu"] for item in normalized["features"]] == [20, 20]
    assert normalized["features"][0]["source_file"] == "甲村.kml"
    assert normalized["features"][0]["mapping"]["policy_id"] == "POL-1"


@pytest.mark.parametrize(
    ("field_name", "unsupported_value"),
    [
        ("schema_version", "parcel_preflight_v2"),
        ("algorithm_version", "multi_kml_preflight_v2"),
        ("id_scheme", "source_path_geometry_v2"),
        ("geometry_hash_scheme", "canonical_geometry_v2"),
    ],
)
def test_rejects_unsupported_manifest_versions_and_schemes(
    field_name: str,
    unsupported_value: str,
) -> None:
    with pytest.raises(ValueError, match=field_name):
        normalize_parcel_manifest(_versioned_parcel_manifest(**{field_name: unsupported_value}))


def test_rejects_invalid_manifest_digest_and_unconfirmed_analysis_input() -> None:
    with pytest.raises(ValueError, match="manifest_sha256"):
        normalize_parcel_manifest(_versioned_parcel_manifest(manifest_sha256="not-a-digest"))

    with pytest.raises(ValueError, match="human_confirmed"):
        normalize_parcel_manifest(
            _versioned_parcel_manifest(preflight_status="ready_for_mapping"),
            require_confirmed=True,
        )


def test_growth_version_constants_are_explicit_and_stable() -> None:
    assert PARCEL_GROWTH_SCHEMA_VERSION == "parcel_growth_v1"
    assert PARCEL_GROWTH_ALGORITHM_VERSION == "zonal_ndvi_union_v1"
    assert CLASSIFICATION_SCHEME_VERSION == "ndvi_upper_bound_digitize_v1"


def test_rejects_duplicate_feature_id_before_raster_analysis() -> None:
    manifest = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"parcel_id": "P-1", "feature_id": "duplicate"},
                "geometry": _polygon(0, 0, 1, 1),
            },
            {
                "type": "Feature",
                "properties": {"parcel_id": "P-2", "feature_id": "duplicate"},
                "geometry": _polygon(2, 0, 3, 1),
            },
        ],
    }

    with pytest.raises(ValueError, match="feature_id 重复"):
        normalize_parcel_manifest(manifest)


def test_canonical_geometry_hash_ignores_ring_direction_and_start_vertex() -> None:
    clockwise = _polygon(0, 0, 2, 1)
    reordered = {
        "type": "Polygon",
        "coordinates": [[[2, 1], [2, 0], [0, 0], [0, 1], [2, 1]]],
    }

    first = canonical_geometry_sha256(clockwise)
    second = canonical_geometry_sha256(reordered)

    assert first == second
    assert re.fullmatch(r"[0-9a-f]{64}", first)
    assert BOUNDARY_HASH_SCHEME == "canonical_geometry_v1"


def test_canonical_feature_collection_hash_is_feature_order_independent() -> None:
    features = [
        {"type": "Feature", "properties": {}, "geometry": _polygon(0, 0, 1, 1)},
        {"type": "Feature", "properties": {}, "geometry": _polygon(2, 0, 3, 1)},
    ]

    first = canonical_geometry_sha256({"type": "FeatureCollection", "features": features})
    second = canonical_geometry_sha256(
        {"type": "FeatureCollection", "features": list(reversed(features))}
    )

    assert first == second


def test_builds_coverage_class_areas_and_deterministic_anomaly_flags() -> None:
    result = build_growth_statistics(
        count_map={1: 2, 2: 2, 5: 4},
        roi_pixel_count=10,
        area_mu=20,
        ndvi_sum=3.2,
        ndvi_min=0.1,
        ndvi_max=0.9,
        raster_overlap_ratio=0.9,
    )

    assert result["valid_pixel_count"] == 8
    assert result["valid_pixel_coverage"] == 0.8
    assert result["effective_coverage"] == 0.72
    assert result["mean_ndvi"] == 0.4
    assert result["poor_growth_ratio"] == 0.5
    assert result["summary"][0]["area_mu"] == 5
    assert result["summary"][4]["area_mu"] == 10
    assert result["anomaly_flags"] == ["partial_raster_overlap", "poor_growth_dominant"]
    assert result["quality_status"] == "warning"


def test_empty_pixels_are_unusable_and_do_not_fabricate_ndvi() -> None:
    result = build_growth_statistics(
        count_map={},
        roi_pixel_count=0,
        area_mu=5,
        raster_overlap_ratio=0,
    )

    assert result["valid_pixel_count"] == 0
    assert result["mean_ndvi"] is None
    assert result["summary"][0]["ratio"] == 0
    assert result["anomaly_flags"] == ["no_raster_overlap", "no_valid_pixels"]
    assert result["quality_status"] == "unusable"


def test_class_area_rounding_preserves_total_area_by_largest_remainder() -> None:
    result = build_growth_statistics(
        count_map={1: 3, 2: 4, 3: 0, 4: 4, 5: 4},
        roi_pixel_count=15,
        area_mu=20,
        ndvi_sum=7.8,
        ndvi_min=0.2,
        ndvi_max=0.8,
    )

    class_areas = [row["area_mu"] for row in result["summary"]]
    assert class_areas == [4.0, 5.34, 0.0, 5.33, 5.33]
    assert sum(class_areas) == result["area_mu"] == 20
