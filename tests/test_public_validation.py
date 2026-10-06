from __future__ import annotations

import numpy as np

from validation.sen1floods11_benchmark import (
    _balanced_sample,
    _circle_footprint,
    Candidate,
    compute_metrics,
    extract_sar_features,
    predict_water,
)


def test_circle_footprint_has_expected_radius_and_symmetry():
    footprint = _circle_footprint(5)
    assert footprint.shape == (11, 11)
    assert footprint[5, 5]
    assert footprint[0, 5]
    assert not footprint[0, 0]
    assert np.array_equal(footprint, np.flipud(footprint))
    assert np.array_equal(footprint, np.fliplr(footprint))


def test_compute_metrics_ignores_invalid_labels():
    labels = np.array([[1, 1, 0], [0, -1, 1]])
    prediction = np.array([[True, False, True], [False, True, True]])
    metrics = compute_metrics(labels, prediction)
    assert metrics["tp"] == 2
    assert metrics["fp"] == 1
    assert metrics["fn"] == 1
    assert metrics["tn"] == 1
    assert metrics["valid_pixels"] == 5
    assert metrics["f1"] == 2 / 3


def test_prediction_uses_vh_threshold_after_median_filter():
    vh = np.full((21, 21), -10.0, dtype=np.float32)
    vh[5:16, 5:16] = -20.0
    prediction = predict_water(vh, -16.0)
    assert prediction[10, 10]
    assert not prediction[0, 0]


def test_sar_feature_stack_is_finite_and_has_expected_shape():
    vv = np.full((12, 10), -8.0, dtype=np.float32)
    vh = np.full((12, 10), -16.0, dtype=np.float32)
    features = extract_sar_features(vv, vh)
    assert features.shape == (12, 10, 16)
    assert np.isfinite(features).all()


def test_balanced_sample_is_deterministic_and_exact_size():
    candidates = [
        Candidate(
            sample_id=f"{event}_{index}",
            event=event,
            dataset_split="test",
            s1_filename=f"{event}_{index}_S1Hand.tif",
            label_filename=f"{event}_{index}_LabelHand.tif",
            water_ratio=(index % 3) * 0.15,
        )
        for event in ("A", "B", "C")
        for index in range(10)
    ]
    first = _balanced_sample(candidates, 12, 7)
    second = _balanced_sample(candidates, 12, 7)
    assert first == second
    assert len(first) == 12
    assert {candidate.event for candidate in first} == {"A", "B", "C"}
