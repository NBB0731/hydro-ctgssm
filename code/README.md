# Hydro-CTGSSM

Reference implementation for sparse, irregular GEMStat river monitoring.

## Why these components are present

- Event-time decay: 2010+ river observations show that roughly 85% of successive samples are 8–90 days apart.
- Shared multi-target state: NO3-N/TP and pH/EC have hundreds of thousands of same-station-day co-observations.
- Censored likelihood: 8–9% of NO3-N and TP values are reported relative to detection limits.
- Directed graph propagation: edges must come from HydroRIVERS and are upstream-to-downstream only.
- Group robust training and calibration: countries and major basins are strongly imbalanced.

## Target design

Primary endpoints: `NO3N`, `TP`, `O2-Dis`, `TSS`.

Auxiliary/context variables: `TEMP`, `pH`, `EC`. They can be auxiliary prediction tasks or observed event channels, depending on the final masking experiment.

## Run the implementation test

```bash
python -m pip install -r requirements.txt
python smoke_test.py
```

The model expects one subgraph at an anchor date. Each node contains a padded sequence of irregular events before that date. This avoids inventing daily water-quality values.

## End-to-end GPU pipeline

The server pipeline contains six stages: GEMStat event preparation, local-projection HydroRIVERS matching, downstream station-graph construction, leakage-controlled quarterly snapshot generation, mixed-precision training, and spatial/temporal evaluation with group-aware conformal intervals.

Run all stages with `bash run_gpu_pipeline.sh /path/to/run_root`, after setting `ARCHIVE` and `RIVER_SHP` if needed. The empirically selected five-seed configuration uses a 128-dimensional hidden state, three directed graph layers, censored Gaussian likelihood, 32 historical station-days and a 90-day target window. GroupDRO remains an explicit robustness ablation because it improved some worst-group errors but reduced average validation performance.

Leakage controls are applied before learning: basin splits precede scaling; statistics use training basins and dates only; primary-target histories are hidden for validation/test basin nodes; and calibration is separated from final spatial, temporal and joint tests.

Required ablations are exposed through `--graph-mode none`, `--graph-mode undirected`, `--no-decay`, `--loss-mode substitute`, and `--no-group-dro`.

## Strong tabular baseline

Run `baseline_histgb.py DATASET --out RUN_DIR` to fit one
`HistGradientBoostingRegressor` per endpoint using static catchment attributes,
latest available observations and anchor seasonality. It uses the same basin and
time partitions as Hydro-CTGSSM and masks primary histories for unseen basins.
This baseline is part of the required comparison, not an optional sanity check.

## Evaluation convention

Point metrics use exact quantitative targets only. Censored targets are retained
for interval evaluation: a left-censored target is covered when the lower bound
does not exceed its reporting limit, and a right-censored target is covered when
the upper bound reaches its reporting limit. Validation targets calibrate the
90% intervals; final spatial, temporal and joint test sets are never used for
calibration.
