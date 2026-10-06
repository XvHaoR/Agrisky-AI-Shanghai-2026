# Agrisky AI public-data validation

This directory evaluates the frozen Agrisky AI Sentinel-1 water-screening
algorithm against the manually labelled subset of Sen1Floods11.

## Scope

Sen1Floods11 quality-control labels distinguish `water` from `not water`.
They do not distinguish newly flooded water from permanent water. The benchmark
therefore validates post-event water extraction, which is one input to flood
screening. It does not validate parcel loss ratios, crop-yield loss, claim
eligibility, or final indemnity amounts.

## Reproduce

```powershell
python -m pip install -r validation/requirements.txt
python -m validation.sen1floods11_benchmark all
```

The command performs five deterministic steps:

1. Read the official Sen1Floods11 train, validation, and test split files.
2. Select 100 official-training, 20 official-validation, and 30 held-out
   official-test chips with seed `20260829`.
3. Reproduce the production algorithm and train a lightweight SAR feature
   fusion candidate only on the official training split.
4. Select the candidate probability threshold only on validation chips.
5. Produce CSV, JSON, and PNG evidence under `validation/results/` and
   `validation/figures/`.

Downloaded GeoTIFF files are stored in `validation/data/` and ignored by Git.
The tracked manifest records source URLs and SHA-256 hashes.

## Interpretation

- `production_-16db` is the unchanged production threshold and is the primary
  current-version result.
- `calibrated_*db` is selected only from the 20 calibration chips. The 30 test
  chips are not used for threshold selection.
- `sar_feature_fusion_v2` is trained on 100 official-training chips. Its
  decision threshold is selected on the 20 validation chips before the 30
  official-test chips are evaluated.
- Precision, recall, F1, and IoU are reported as both macro averages across
  chips and micro values pooled across valid pixels.
- Overall pixel accuracy is intentionally not a headline metric because class
  imbalance can make it misleading.

## Data source

- Dataset: Sen1Floods11 v1.1
- Paper: Bonafilia et al., CVPR Workshops 2020
- Repository: https://github.com/cloudtostreet/Sen1Floods11
- Public bucket: https://storage.googleapis.com/sen1floods11/
