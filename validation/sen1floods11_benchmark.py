from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import random
import subprocess
import time
import urllib.error
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import rasterio  # noqa: E402
from scipy import ndimage  # noqa: E402
from sklearn.ensemble import ExtraTreesClassifier  # noqa: E402


ROOT = Path(__file__).resolve().parent.parent
VALIDATION_ROOT = ROOT / "validation"
DATA_ROOT = VALIDATION_ROOT / "data" / "sen1floods11"
RESULTS_ROOT = VALIDATION_ROOT / "results"
FIGURES_ROOT = VALIDATION_ROOT / "figures"
MANIFEST_PATH = VALIDATION_ROOT / "manifest.csv"

BUCKET_BASE = "https://storage.googleapis.com/sen1floods11/v1.1"
SPLIT_BASE = f"{BUCKET_BASE}/splits/flood_handlabeled"
S1_BASE = f"{BUCKET_BASE}/data/flood_events/HandLabeled/S1Hand"
LABEL_BASE = f"{BUCKET_BASE}/data/flood_events/HandLabeled/LabelHand"

SEED = 20260829
TRAINING_COUNT = 100
CALIBRATION_COUNT = 20
TEST_COUNT = 30
PRODUCTION_THRESHOLD_DB = -16.0
THRESHOLD_CANDIDATES = (-18.0, -17.0, -16.0, -15.0, -14.0)
FOCAL_RADIUS_PX = 5
ALGORITHM_VERSION = "agrisky-s1-vh-threshold-v1"
ML_ALGORITHM_VERSION = "agrisky-sar-feature-fusion-v2"
ML_PROBABILITY_CANDIDATES = tuple(float(value) for value in np.arange(0.30, 0.91, 0.05))


@dataclass(frozen=True)
class Candidate:
    sample_id: str
    event: str
    dataset_split: str
    s1_filename: str
    label_filename: str
    water_ratio: float


@dataclass(frozen=True)
class MetricRow:
    sample_id: str
    event: str
    benchmark_split: str
    dataset_split: str
    variant: str
    threshold_db: float
    valid_pixels: int
    gt_water_pixels: int
    pred_water_pixels: int
    tp: int
    fp: int
    fn: int
    tn: int
    precision: float | None
    recall: float | None
    f1: float | None
    iou: float | None
    gt_water_ratio: float
    pred_water_ratio: float
    area_error_pp: float
    runtime_ms: float
    algorithm_version: str


def _download(url: str, destination: Path, retries: int = 4) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.stat().st_size > 0:
        return destination
    temporary = destination.with_suffix(destination.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "AgriskyAI-Validation/1.0"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=90) as response, temporary.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
            temporary.replace(destination)
            return destination
        except (OSError, urllib.error.URLError) as exc:
            temporary.unlink(missing_ok=True)
            if attempt == retries - 1:
                raise RuntimeError(f"Download failed after {retries} attempts: {url}") from exc
            time.sleep(2**attempt)
    raise AssertionError("unreachable")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_split(dataset_split: str) -> list[tuple[str, str]]:
    filename = {
        "train": "flood_train_data.csv",
        "valid": "flood_valid_data.csv",
        "test": "flood_test_data.csv",
    }[dataset_split]
    path = _download(f"{SPLIT_BASE}/{filename}", DATA_ROOT / "splits" / filename)
    with path.open("r", encoding="utf-8", newline="") as stream:
        return [(row[0], row[1]) for row in csv.reader(stream) if len(row) >= 2]


def _sample_id(filename: str) -> str:
    return filename.removesuffix("_S1Hand.tif")


def _label_ratio(path: Path) -> float:
    with rasterio.open(path) as dataset:
        labels = dataset.read(1)
    valid = labels != -1
    return float(np.count_nonzero((labels == 1) & valid) / max(np.count_nonzero(valid), 1))


def _load_candidates(dataset_split: str) -> list[Candidate]:
    candidates: list[Candidate] = []
    for s1_filename, label_filename in _read_split(dataset_split):
        sample_id = _sample_id(s1_filename)
        event = sample_id.split("_", 1)[0]
        label_path = _download(f"{LABEL_BASE}/{label_filename}", DATA_ROOT / "labels" / label_filename)
        candidates.append(
            Candidate(
                sample_id=sample_id,
                event=event,
                dataset_split=dataset_split,
                s1_filename=s1_filename,
                label_filename=label_filename,
                water_ratio=_label_ratio(label_path),
            )
        )
    return candidates


def _water_bin(ratio: float) -> int:
    if ratio < 0.02:
        return 0
    if ratio < 0.20:
        return 1
    return 2


def _balanced_sample(candidates: Iterable[Candidate], count: int, seed: int) -> list[Candidate]:
    rng = random.Random(seed)
    groups: dict[tuple[str, int], list[Candidate]] = defaultdict(list)
    for candidate in candidates:
        groups[(candidate.event, _water_bin(candidate.water_ratio))].append(candidate)
    for values in groups.values():
        rng.shuffle(values)

    keys = sorted(groups)
    rng.shuffle(keys)
    selected: list[Candidate] = []
    while len(selected) < count:
        progress = False
        for key in keys:
            if groups[key] and len(selected) < count:
                selected.append(groups[key].pop())
                progress = True
        if not progress:
            break
    if len(selected) != count:
        raise RuntimeError(f"Requested {count} samples, selected {len(selected)}")
    return sorted(selected, key=lambda item: (item.event, item.sample_id))


def prepare_data() -> list[dict[str, Any]]:
    training_candidates = _load_candidates("train")
    calibration_candidates = _load_candidates("valid")
    test_candidates = _load_candidates("test")
    training = _balanced_sample(training_candidates, TRAINING_COUNT, SEED)
    calibration = _balanced_sample(calibration_candidates, CALIBRATION_COUNT, SEED)
    test = _balanced_sample(test_candidates, TEST_COUNT, SEED + 1)

    rows: list[dict[str, Any]] = []
    selections = (("training", training), ("calibration", calibration), ("test", test))
    selected = [(benchmark_split, candidate) for benchmark_split, values in selections for candidate in values]

    def download_s1(item: tuple[str, Candidate]) -> tuple[str, Candidate, Path]:
        benchmark_split, candidate = item
        path = _download(
            f"{S1_BASE}/{candidate.s1_filename}",
            DATA_ROOT / "s1" / candidate.s1_filename,
        )
        return benchmark_split, candidate, path

    with ThreadPoolExecutor(max_workers=8) as executor:
        downloaded = list(executor.map(download_s1, selected))

    for benchmark_split, candidate, s1_path in downloaded:
        label_path = DATA_ROOT / "labels" / candidate.label_filename
        rows.append(
            {
                **asdict(candidate),
                "benchmark_split": benchmark_split,
                "s1_url": f"{S1_BASE}/{candidate.s1_filename}",
                "label_url": f"{LABEL_BASE}/{candidate.label_filename}",
                "s1_sha256": _sha256(s1_path),
                "label_sha256": _sha256(label_path),
            }
        )

    fieldnames = list(rows[0])
    with MANIFEST_PATH.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return rows


def _circle_footprint(radius_px: int) -> np.ndarray:
    coordinates = np.arange(-radius_px, radius_px + 1)
    yy, xx = np.meshgrid(coordinates, coordinates, indexing="ij")
    return (xx * xx + yy * yy) <= radius_px * radius_px


def predict_water(vh_db: np.ndarray, threshold_db: float) -> np.ndarray:
    smoothed = ndimage.median_filter(
        vh_db,
        footprint=_circle_footprint(FOCAL_RADIUS_PX),
        mode="nearest",
    )
    return smoothed < threshold_db


def extract_sar_features(vv_db: np.ndarray, vh_db: np.ndarray) -> np.ndarray:
    features = [vv_db, vh_db, vv_db - vh_db, (vv_db + vh_db) / 2.0]
    for size in (3, 7, 11):
        for band in (vv_db, vh_db):
            local_mean = ndimage.uniform_filter(band, size=size, mode="nearest")
            local_square_mean = ndimage.uniform_filter(band * band, size=size, mode="nearest")
            local_std = np.sqrt(np.maximum(local_square_mean - local_mean * local_mean, 0.0))
            features.extend((local_mean, local_std))
    return np.stack(features, axis=-1).astype(np.float32)


def _read_chip(entry: dict[str, Any]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    with rasterio.open(DATA_ROOT / "s1" / entry["s1_filename"]) as dataset:
        vv_db, vh_db = dataset.read().astype(np.float32)
    with rasterio.open(DATA_ROOT / "labels" / entry["label_filename"]) as dataset:
        labels = dataset.read(1)
    return vv_db, vh_db, labels


def train_feature_fusion_model(entries: list[dict[str, Any]]) -> ExtraTreesClassifier:
    rng = np.random.default_rng(SEED)
    feature_batches: list[np.ndarray] = []
    label_batches: list[np.ndarray] = []
    for entry in entries:
        vv_db, vh_db, labels = _read_chip(entry)
        features = extract_sar_features(vv_db, vh_db)
        selected_indices: list[int] = []
        for label_value in (0, 1):
            candidates = np.flatnonzero(labels.ravel() == label_value)
            selected_indices.extend(
                rng.choice(candidates, min(len(candidates), 1500), replace=False).tolist()
            )
        selected = np.asarray(selected_indices)
        feature_batches.append(features.reshape(-1, features.shape[-1])[selected])
        label_batches.append(labels.ravel()[selected])
    model = ExtraTreesClassifier(
        n_estimators=200,
        min_samples_leaf=3,
        max_features=0.7,
        class_weight="balanced",
        n_jobs=-1,
        random_state=SEED,
    )
    model.fit(np.concatenate(feature_batches), np.concatenate(label_batches))
    return model


def _predict_feature_fusion(
    model: ExtraTreesClassifier,
    vv_db: np.ndarray,
    vh_db: np.ndarray,
    probability_threshold: float,
) -> np.ndarray:
    features = extract_sar_features(vv_db, vh_db)
    probabilities = model.predict_proba(features.reshape(-1, features.shape[-1]))[:, 1]
    return (probabilities >= probability_threshold).reshape(vh_db.shape)


def compute_metrics(
    labels: np.ndarray,
    prediction: np.ndarray,
) -> dict[str, int | float | None]:
    valid = (labels != -1) & np.isfinite(labels)
    truth = labels == 1
    tp = int(np.count_nonzero(valid & truth & prediction))
    fp = int(np.count_nonzero(valid & ~truth & prediction))
    fn = int(np.count_nonzero(valid & truth & ~prediction))
    tn = int(np.count_nonzero(valid & ~truth & ~prediction))
    valid_pixels = tp + fp + fn + tn

    def ratio(numerator: float, denominator: float) -> float | None:
        return float(numerator / denominator) if denominator else None

    precision = ratio(tp, tp + fp)
    recall = ratio(tp, tp + fn)
    f1 = ratio(2 * tp, 2 * tp + fp + fn)
    iou = ratio(tp, tp + fp + fn)
    gt_ratio = ratio(tp + fn, valid_pixels) or 0.0
    pred_ratio = ratio(tp + fp, valid_pixels) or 0.0
    return {
        "valid_pixels": valid_pixels,
        "gt_water_pixels": tp + fn,
        "pred_water_pixels": tp + fp,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "iou": iou,
        "gt_water_ratio": gt_ratio,
        "pred_water_ratio": pred_ratio,
        "area_error_pp": (pred_ratio - gt_ratio) * 100.0,
    }


def _evaluate_entry(entry: dict[str, Any], threshold_db: float, variant: str) -> MetricRow:
    _, vh_db, labels = _read_chip(entry)
    started = time.perf_counter()
    prediction = predict_water(vh_db, threshold_db)
    runtime_ms = (time.perf_counter() - started) * 1000.0
    metrics = compute_metrics(labels, prediction)
    return MetricRow(
        sample_id=entry["sample_id"],
        event=entry["event"],
        benchmark_split=entry["benchmark_split"],
        dataset_split=entry["dataset_split"],
        variant=variant,
        threshold_db=threshold_db,
        runtime_ms=runtime_ms,
        algorithm_version=ALGORITHM_VERSION,
        **metrics,
    )


def _evaluate_ml_entry(
    entry: dict[str, Any],
    model: ExtraTreesClassifier,
    probability_threshold: float,
) -> MetricRow:
    vv_db, vh_db, labels = _read_chip(entry)
    started = time.perf_counter()
    prediction = _predict_feature_fusion(model, vv_db, vh_db, probability_threshold)
    runtime_ms = (time.perf_counter() - started) * 1000.0
    metrics = compute_metrics(labels, prediction)
    return MetricRow(
        sample_id=entry["sample_id"],
        event=entry["event"],
        benchmark_split=entry["benchmark_split"],
        dataset_split=entry["dataset_split"],
        variant="sar_feature_fusion_v2",
        threshold_db=probability_threshold,
        runtime_ms=runtime_ms,
        algorithm_version=ML_ALGORITHM_VERSION,
        **metrics,
    )


def _mean_defined(rows: Iterable[MetricRow], field: str) -> float | None:
    values = [float(value) for row in rows if (value := getattr(row, field)) is not None]
    return float(np.mean(values)) if values else None


def _aggregate(rows: list[MetricRow]) -> dict[str, Any]:
    counts = {name: sum(getattr(row, name) for row in rows) for name in ("tp", "fp", "fn", "tn")}
    pooled = compute_metrics_from_counts(**counts)
    return {
        "samples": len(rows),
        "macro": {name: _mean_defined(rows, name) for name in ("precision", "recall", "f1", "iou")},
        "micro": pooled,
        "median_abs_area_error_pp": float(np.median([abs(row.area_error_pp) for row in rows])),
        "mean_runtime_ms": float(np.mean([row.runtime_ms for row in rows])),
        "counts": counts,
    }


def compute_metrics_from_counts(tp: int, fp: int, fn: int, tn: int) -> dict[str, float | None]:
    def ratio(numerator: float, denominator: float) -> float | None:
        return float(numerator / denominator) if denominator else None

    return {
        "precision": ratio(tp, tp + fp),
        "recall": ratio(tp, tp + fn),
        "f1": ratio(2 * tp, 2 * tp + fp + fn),
        "iou": ratio(tp, tp + fp + fn),
    }


def _read_manifest() -> list[dict[str, str]]:
    if not MANIFEST_PATH.exists():
        raise RuntimeError("Manifest missing. Run the prepare command first.")
    with MANIFEST_PATH.open("r", encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def _write_metric_rows(path: Path, rows: list[MetricRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(asdict(rows[0])))
        writer.writeheader()
        writer.writerows(asdict(row) for row in rows)


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def run_benchmark() -> tuple[list[MetricRow], dict[str, Any]]:
    entries = _read_manifest()
    training_entries = [entry for entry in entries if entry["benchmark_split"] == "training"]
    calibration_entries = [entry for entry in entries if entry["benchmark_split"] == "calibration"]
    test_entries = [entry for entry in entries if entry["benchmark_split"] == "test"]

    calibration_rows: list[MetricRow] = []
    for threshold in THRESHOLD_CANDIDATES:
        variant = f"candidate_{threshold:g}db"
        calibration_rows.extend(_evaluate_entry(entry, threshold, variant) for entry in calibration_entries)

    calibration_summary: dict[str, Any] = {}
    for threshold in THRESHOLD_CANDIDATES:
        subset = [row for row in calibration_rows if row.threshold_db == threshold]
        calibration_summary[str(threshold)] = _aggregate(subset)
    selected_threshold = max(
        THRESHOLD_CANDIDATES,
        key=lambda threshold: calibration_summary[str(threshold)]["macro"]["f1"] or -1.0,
    )

    ml_model = train_feature_fusion_model(training_entries)
    ml_calibration_rows: list[MetricRow] = []
    ml_calibration_summary: dict[str, Any] = {}
    for probability_threshold in ML_PROBABILITY_CANDIDATES:
        subset = [
            _evaluate_ml_entry(entry, ml_model, probability_threshold)
            for entry in calibration_entries
        ]
        ml_calibration_rows.extend(subset)
        ml_calibration_summary[f"{probability_threshold:.2f}"] = _aggregate(subset)
    selected_ml_probability = max(
        ML_PROBABILITY_CANDIDATES,
        key=lambda threshold: ml_calibration_summary[f"{threshold:.2f}"]["macro"]["f1"] or -1.0,
    )

    test_rows: list[MetricRow] = []
    test_rows.extend(
        _evaluate_entry(entry, PRODUCTION_THRESHOLD_DB, "production_-16db") for entry in test_entries
    )
    calibrated_variant = f"calibrated_{selected_threshold:g}db"
    test_rows.extend(_evaluate_entry(entry, selected_threshold, calibrated_variant) for entry in test_entries)
    test_rows.extend(
        _evaluate_ml_entry(entry, ml_model, selected_ml_probability) for entry in test_entries
    )

    RESULTS_ROOT.mkdir(parents=True, exist_ok=True)
    _write_metric_rows(RESULTS_ROOT / "calibration_metrics.csv", calibration_rows)
    _write_metric_rows(RESULTS_ROOT / "ml_calibration_metrics.csv", ml_calibration_rows)
    _write_metric_rows(RESULTS_ROOT / "sample_metrics.csv", test_rows)

    production_rows = [row for row in test_rows if row.variant == "production_-16db"]
    calibrated_rows = [row for row in test_rows if row.variant == calibrated_variant]
    ml_rows = [row for row in test_rows if row.variant == "sar_feature_fusion_v2"]
    summary = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": {
            "dataset": "Sen1Floods11 v1.1 manually labelled flood-event subset",
            "repository": "https://github.com/cloudtostreet/Sen1Floods11",
            "paper": "https://openaccess.thecvf.com/content_CVPRW_2020/html/w11/Bonafilia_Sen1Floods11_A_Georeferenced_Dataset_to_Train_and_Test_Deep_Learning_CVPRW_2020_paper.html",
        },
        "scope": "Post-event water/non-water segmentation; not newly flooded water or indemnity validation.",
        "seed": SEED,
        "algorithm_version": ALGORITHM_VERSION,
        "ml_algorithm_version": ML_ALGORITHM_VERSION,
        "repository_commit": _git_commit(),
        "focal_radius_px": FOCAL_RADIUS_PX,
        "production_threshold_db": PRODUCTION_THRESHOLD_DB,
        "selected_calibration_threshold_db": selected_threshold,
        "selected_ml_probability_threshold": selected_ml_probability,
        "calibration": calibration_summary,
        "ml_calibration": ml_calibration_summary,
        "test": {
            "production": _aggregate(production_rows),
            "calibrated": _aggregate(calibrated_rows),
            "sar_feature_fusion_v2": _aggregate(ml_rows),
        },
    }
    (RESULTS_ROOT / "summary_metrics.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    create_figures(
        calibration_rows,
        test_rows,
        selected_threshold,
        ml_model,
        selected_ml_probability,
    )
    return test_rows, summary


def _metric_or_zero(value: float | None) -> float:
    return float(value) if value is not None and math.isfinite(value) else 0.0


def create_figures(
    calibration_rows: list[MetricRow],
    test_rows: list[MetricRow],
    selected_threshold: float,
    ml_model: ExtraTreesClassifier,
    selected_ml_probability: float,
) -> None:
    FIGURES_ROOT.mkdir(parents=True, exist_ok=True)
    variants = [
        "production_-16db",
        f"calibrated_{selected_threshold:g}db",
        "sar_feature_fusion_v2",
    ]
    labels = ["Production -16 dB", f"Calibrated {selected_threshold:g} dB", "SAR fusion v2"]
    colors = ["#17315f", "#64748b", "#14937b"]

    fig, ax = plt.subplots(figsize=(9, 5.2))
    metrics = ["precision", "recall", "f1", "iou"]
    x = np.arange(len(metrics))
    width = 0.25
    for index, (variant, label, color) in enumerate(zip(variants, labels, colors, strict=True)):
        rows = [row for row in test_rows if row.variant == variant]
        values = [_metric_or_zero(_mean_defined(rows, metric)) for metric in metrics]
        ax.bar(x + (index - 1) * width, values, width, label=label, color=color)
    ax.set_xticks(x, [name.upper() if name != "precision" else "Precision" for name in metrics])
    ax.set_ylim(0, 1)
    ax.set_ylabel("Macro score")
    ax.set_title("Sen1Floods11 held-out test performance")
    ax.grid(axis="y", alpha=0.25)
    ax.legend()
    fig.tight_layout()
    fig.savefig(FIGURES_ROOT / "01_test_metrics.png", dpi=180)
    plt.close(fig)

    thresholds = list(THRESHOLD_CANDIDATES)
    f1_values = []
    iou_values = []
    for threshold in thresholds:
        rows = [row for row in calibration_rows if row.threshold_db == threshold]
        f1_values.append(_metric_or_zero(_mean_defined(rows, "f1")))
        iou_values.append(_metric_or_zero(_mean_defined(rows, "iou")))
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.plot(thresholds, f1_values, marker="o", label="Macro F1", color="#17315f")
    ax.plot(thresholds, iou_values, marker="s", label="Macro IoU", color="#14937b")
    ax.axvline(PRODUCTION_THRESHOLD_DB, color="#d97706", linestyle="--", label="Production")
    ax.axvline(selected_threshold, color="#be123c", linestyle=":", label="Selected")
    ax.set_xlabel("VH threshold (dB)")
    ax.set_ylabel("Calibration score")
    ax.set_ylim(0, 1)
    ax.set_title("Threshold sensitivity on calibration chips")
    ax.grid(alpha=0.25)
    ax.legend()
    fig.tight_layout()
    fig.savefig(FIGURES_ROOT / "02_threshold_sensitivity.png", dpi=180)
    plt.close(fig)

    production_rows = [row for row in test_rows if row.variant == "production_-16db"]
    ml_rows = [row for row in test_rows if row.variant == "sar_feature_fusion_v2"]
    fig, ax = plt.subplots(figsize=(6.4, 6.4))
    x_values = [row.gt_water_ratio * 100 for row in production_rows]
    production_values = [row.pred_water_ratio * 100 for row in production_rows]
    ml_values = [row.pred_water_ratio * 100 for row in ml_rows]
    ax.scatter(x_values, production_values, color="#d97706", alpha=0.7, label="Production baseline")
    ax.scatter(x_values, ml_values, color="#14937b", alpha=0.8, label="SAR feature fusion v2")
    limit = max([1.0, *x_values, *production_values, *ml_values])
    ax.plot([0, limit], [0, limit], color="#64748b", linestyle="--")
    ax.set_xlabel("Ground-truth water area (%)")
    ax.set_ylabel("Predicted water area (%)")
    ax.set_title("Held-out area agreement")
    ax.grid(alpha=0.2)
    ax.legend()
    fig.tight_layout()
    fig.savefig(FIGURES_ROOT / "03_area_agreement.png", dpi=180)
    plt.close(fig)

    by_event: dict[str, list[float]] = defaultdict(list)
    ml_by_event: dict[str, list[float]] = defaultdict(list)
    for row in production_rows:
        by_event[row.event].append(row.area_error_pp)
    for row in ml_rows:
        ml_by_event[row.event].append(row.area_error_pp)
    fig, ax = plt.subplots(figsize=(10, 5.5))
    events = sorted(by_event)
    positions = np.arange(len(events), dtype=float)
    baseline_plot = ax.boxplot(
        [by_event[event] for event in events],
        positions=positions - 0.18,
        widths=0.3,
        patch_artist=True,
        showfliers=True,
    )
    ml_plot = ax.boxplot(
        [ml_by_event[event] for event in events],
        positions=positions + 0.18,
        widths=0.3,
        patch_artist=True,
        showfliers=True,
    )
    for patch in baseline_plot["boxes"]:
        patch.set_facecolor("#f59e0b")
        patch.set_alpha(0.65)
    for patch in ml_plot["boxes"]:
        patch.set_facecolor("#14937b")
        patch.set_alpha(0.75)
    ax.axhline(0, color="#64748b", linewidth=1)
    ax.set_xticks(positions, events)
    ax.set_ylabel("Area error (percentage points)")
    ax.set_title("Area error by flood event")
    ax.tick_params(axis="x", rotation=35)
    ax.grid(axis="y", alpha=0.2)
    ax.legend([baseline_plot["boxes"][0], ml_plot["boxes"][0]], ["Production baseline", "SAR feature fusion v2"])
    fig.tight_layout()
    fig.savefig(FIGURES_ROOT / "04_event_area_error.png", dpi=180)
    plt.close(fig)

    fig, axes = plt.subplots(1, 2, figsize=(10.5, 4.8), constrained_layout=True)
    for ax, rows, title in (
        (axes[0], production_rows, "Production baseline"),
        (axes[1], ml_rows, "SAR feature fusion v2"),
    ):
        counts = {name: sum(getattr(row, name) for row in rows) for name in ("tp", "fp", "fn", "tn")}
        matrix = np.array([[counts["tn"], counts["fp"]], [counts["fn"], counts["tp"]]], dtype=float)
        normalized = matrix / np.maximum(matrix.sum(axis=1, keepdims=True), 1)
        ax.imshow(normalized, cmap="Blues", vmin=0, vmax=1)
        for row_index in range(2):
            for column_index in range(2):
                ax.text(column_index, row_index, f"{normalized[row_index, column_index]:.1%}\n({int(matrix[row_index, column_index]):,})", ha="center", va="center")
        ax.set_xticks([0, 1], ["Predicted land", "Predicted water"])
        ax.set_yticks([0, 1], ["True land", "True water"])
        ax.set_title(title)
    fig.suptitle("Held-out normalized confusion matrices")
    fig.savefig(FIGURES_ROOT / "05_confusion_matrix.png", dpi=180)
    plt.close(fig)

    ranked = sorted(ml_rows, key=lambda row: _metric_or_zero(row.iou))
    examples = [ranked[-1], ranked[len(ranked) // 2], ranked[0]]
    fig, axes = plt.subplots(3, 4, figsize=(13, 10))
    for row_index, row in enumerate(examples):
        entry = next(entry for entry in _read_manifest() if entry["sample_id"] == row.sample_id)
        vv, vh, labels_array = _read_chip(entry)
        prediction = _predict_feature_fusion(ml_model, vv, vh, selected_ml_probability)
        valid = labels_array != -1
        error = np.zeros((*labels_array.shape, 3), dtype=float)
        error[valid & (labels_array == 1) & prediction] = (0.08, 0.60, 0.46)
        error[valid & (labels_array == 0) & prediction] = (0.90, 0.38, 0.18)
        error[valid & (labels_array == 1) & ~prediction] = (0.10, 0.45, 0.82)
        panels = [vh, np.ma.masked_where(labels_array != 1, labels_array), np.ma.masked_where(~prediction, prediction), error]
        titles = ["VH (dB)", "Ground truth", "Prediction", "TP / FP / FN"]
        for column_index, (panel, title) in enumerate(zip(panels, titles, strict=True)):
            axis = axes[row_index, column_index]
            if column_index == 0:
                axis.imshow(panel, cmap="gray", vmin=-25, vmax=-5)
            elif column_index in (1, 2):
                axis.imshow(panel, cmap="Blues", vmin=0, vmax=1)
            else:
                axis.imshow(panel)
            axis.set_title(title if row_index == 0 else "")
            axis.axis("off")
        axes[row_index, 0].set_ylabel(f"{row.sample_id}\nIoU={_metric_or_zero(row.iou):.3f}")
    fig.suptitle("SAR feature fusion v2: best, median, and worst held-out examples", y=0.995)
    fig.tight_layout()
    fig.savefig(FIGURES_ROOT / "06_example_overlays.png", dpi=180)
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run", "all"), nargs="?", default="all")
    args = parser.parse_args()
    if args.command in {"prepare", "all"}:
        rows = prepare_data()
        print(f"Prepared {len(rows)} samples at {MANIFEST_PATH}")
    if args.command in {"run", "all"}:
        _, summary = run_benchmark()
        production = summary["test"]["production"]
        print(json.dumps({
            "selected_threshold_db": summary["selected_calibration_threshold_db"],
            "selected_ml_probability_threshold": summary["selected_ml_probability_threshold"],
            "production_macro": production["macro"],
            "production_micro": production["micro"],
            "median_abs_area_error_pp": production["median_abs_area_error_pp"],
            "sar_feature_fusion_v2": summary["test"]["sar_feature_fusion_v2"],
        }, indent=2))


if __name__ == "__main__":
    main()
