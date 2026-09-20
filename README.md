# Hydro-CTGSSM

**Continuous-time graph state-space modelling of sparse global river-water-quality monitoring.**

Reference implementation and frozen results for the study:

> Hydrological graph learning under sparse global monitoring reveals cross-basin failure boundaries and chemistry-limited phosphorus control
> Yanan Zhao, Hao Liu (corresponding author)
> *Water Research* (submitted)
---

## What this repository contains

This is the **analysis code and frozen result files** supporting the manuscript. It is archived so that every number reported in the paper can be reproduced or audited without re-downloading third-party archives.

| Directory | Contents |
|---|---|
| `code/` | Hydro-CTGSSM pipeline: event preparation, station matching, graph construction, snapshot generation, training, evaluation, HistGB baseline, ablations |
| `code/hydroctgssm/` | Model package: state-space model, censored losses, conformal calibration |
| `results/` | Frozen metric files cited in the manuscript (`five_seed_summary.csv`, `all_model_metrics.csv`, `worst_country_mae.csv`, …) |
| `supplementary/` | Supplementary Data S1–S6 as submitted |
| `analysis/` | Manuscript-side analysis and QA scripts: GFH bottle statistics, numeric/unit auditing, reference verification |

---

## Citation and archival

| Version | Status |
|---|---|
| `v1.0.0` | This release — archived on Zenodo, DOI to be minted from tag `v1.0.0` |

If you use this code or the frozen metric files, please cite the article. Until the
article is published, cite the GitHub release:

```bibtex
@software{zhao_hydroctgssm_2026,
  author  = {Zhao, Yanan and Liu, Hao},
  title   = {Hydro-CTGSSM: continuous-time graph state-space modelling of
             sparse global river-water-quality monitoring},
  version = {v1.0.0},
  year    = {2026},
  url     = {https://github.com/NBB0731/hydro-ctgssm}
}
```

Machine-readable metadata is provided in `CITATION.cff`. Once Zenodo mints a DOI,
add `doi: 10.5281/zenodo.XXXXXXX` to that file and to the BibTeX entry above.

---

## Study in one paragraph

Sparse, geographically uneven monitoring can make a water-quality model appear transferable when it mainly recovers site-specific structure. Using 2,564,489 daily river-water-quality events from 14,310 stations linked to HydroRIVERS v10, we show that temporal prediction skill does **not** transfer to unseen basins: across five seeds temporal R² reached 0.550 ± 0.009 for NO₃-N, but unseen-basin R² fell to ≤ 0 for every primary endpoint when each station's own target history was withheld. Directed river-network propagation was necessary within the neural model (removing it collapsed temporal NO₃-N R² from 0.546 to −0.276) but was not sufficient for best-in-class point accuracy. A complementary 30-bottle granular ferric hydroxide (GFH) screen showed that rapid phosphorus removal is strongly chemistry-limited, falling from 72.9 to 30.8 percentage points of advantage across a river-relevant pH–EC envelope.

---

## Design features of the model

| Component | Why it is present |
|---|---|
| Event-time decay | Roughly 85% of successive 2010+ river observations are 8–90 days apart |
| Shared multi-target state | NO₃-N/TP and pH/EC have hundreds of thousands of same-station-day co-observations |
| Censored likelihood | 8–9% of NO₃-N and TP values are reported relative to detection limits |
| Directed graph propagation | Edges derive from HydroRIVERS and are upstream-to-downstream only |
| Group-robust training + calibration | Countries and major basins are strongly imbalanced |

**Primary endpoints:** `NO3N`, `TP`, `O2-Dis`, `TSS`.
**Auxiliary/context variables:** `TEMP`, `pH`, `EC`.

Leakage controls are applied **before** learning: basin splits precede scaling; statistics use training basins and dates only; primary-target histories are hidden for validation/test basin nodes; and interval calibration is separated from the final spatial, temporal, and joint tests.

---

## Installation

```bash
python -m pip install -r code/requirements.txt      # CPU: smoke test + HistGB baseline
python -m pip install -r code/requirements-server.txt  # GPU: full pipeline
```

## Run the implementation test

```bash
cd code
python smoke_test.py
```

The model expects one subgraph at an anchor date. Each node holds a padded sequence of irregular events *before* that date — this avoids inventing daily water-quality values.

## Full pipeline

Six stages, run with:

```bash
cd code
bash run_gpu_pipeline.sh /path/to/run_root
```

1. GEMStat event preparation
2. Local-projection HydroRIVERS station matching
3. Downstream station-graph construction
4. Leakage-controlled quarterly snapshot generation
5. Mixed-precision training
6. Spatial / temporal / joint evaluation with group-aware conformal intervals

The empirically selected five-seed configuration uses a 128-dimensional hidden state, three directed graph layers, a censored Gaussian likelihood, 32 historical station-days, and a 90-day target window.

## Ablations

```bash
bash run_ablations.sh /path/to/run_root
```

Exposed directly on the trainer: `--graph-mode none`, `--graph-mode undirected`, `--no-decay`, `--loss-mode substitute`, `--no-group-dro`. GroupDRO remains an explicit robustness ablation because it improved some worst-group errors but reduced average validation performance.

## Strong tabular baseline

```bash
cd code
python baseline_histgb.py DATASET --out RUN_DIR
```

Fits one `HistGradientBoostingRegressor` per endpoint on static catchment attributes, latest available observations, and anchor seasonality, using identical basin/time partitions and the same primary-history masking rule. This baseline is part of the required comparison, not an optional sanity check.

## Reproducing the GFH bottle statistics

```bash
python analysis/analyze_gfh_experiment.py
```

---

## Evaluation convention

Point metrics use **exact quantitative targets only**. Censored targets are retained for interval evaluation: a left-censored target is covered when the lower bound does not exceed its reporting limit; a right-censored target is covered when the upper bound reaches its reporting limit. Validation targets calibrate the 90% intervals; the final spatial, temporal, and joint test sets are never used for calibration.

---

## Data

Third-party archives are **not** redistributed here; they are publicly available from their providers.

| Dataset | Source | DOI / URL |
|---|---|---|
| GEMStat GFQA v3 (snapshot 2 February 2026) | UNEP GEMS/Water | https://doi.org/10.5281/zenodo.18459694 |
| HydroRIVERS v10 | HydroSHEDS | https://www.hydrosheds.org/products/hydrorivers |
| HydroATLAS | HydroSHEDS | https://www.hydrosheds.org/products/hydroatlas |

The `results/` and `supplementary/` directories contain only derived quantities, so the manuscript's statistics can be audited without access to the raw archives.

---

## License

Code released under the MIT License (see `LICENSE`). Supplementary data tables are released under CC BY 4.0.

## Contact

Hao Liu — lhr3282@zua.edu.cn
