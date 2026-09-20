"""Leakage-controlled HistGradientBoosting baseline for the snapshot dataset.

The baseline uses static catchment attributes, the most recent observation of
each parameter, observation flags, and anchor seasonality. Primary-pollutant
history is hidden for validation/test basins, matching the deep model's strict
inductive protocol. Censored targets are excluded from point-model fitting and
point metrics; they remain in interval-coverage evaluation.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import torch
from sklearn.ensemble import HistGradientBoostingRegressor


LOG1P = {"NO3N", "TP", "O2-Dis", "TSS", "EC"}


def inverse(z: np.ndarray, pidx: int, meta: dict) -> np.ndarray:
    x = z * float(meta["stds"][pidx]) + float(meta["means"][pidx])
    return np.expm1(x).clip(min=0) if meta["parameters"][pidx] in LOG1P else x


def point_metrics(y: np.ndarray, pred: np.ndarray) -> dict:
    if not len(y):
        return {"n": 0, "mae": np.nan, "rmse": np.nan, "r2": np.nan, "spearman": np.nan}
    err = pred - y
    ss = np.sum((y - y.mean()) ** 2)
    return {
        "n": int(len(y)),
        "mae": float(np.mean(np.abs(err))),
        "rmse": float(np.sqrt(np.mean(err**2))),
        "r2": float(1 - np.sum(err**2) / ss) if ss > 0 else np.nan,
        "spearman": float(pd.Series(y).corr(pd.Series(pred), method="spearman")) if len(y) > 2 else np.nan,
    }


def latest_features(snapshot: dict, meta: dict) -> np.ndarray:
    values = snapshot["values"].numpy().copy()
    observed = snapshot["observed"].numpy().astype(bool)
    node_split = meta["node_split"].numpy()
    held_out = np.flatnonzero(node_split != 0)
    primary = np.asarray(meta["primary_indices"], dtype=int)
    values[np.ix_(held_out, np.arange(values.shape[1]), primary)] = 0
    observed[np.ix_(held_out, np.arange(values.shape[1]), primary)] = False

    # Take each parameter's rightmost observed value without future access.
    rev = observed[:, ::-1, :]
    has = rev.any(axis=1)
    rev_idx = rev.argmax(axis=1)
    time_idx = values.shape[1] - 1 - rev_idx
    node_idx = np.arange(values.shape[0])[:, None]
    param_idx = np.arange(values.shape[2])[None, :]
    last = values[node_idx, time_idx, param_idx]
    last[~has] = 0.0

    anchor = pd.Timestamp(snapshot["anchor"])
    month_angle = 2 * np.pi * (anchor.month - 1) / 12
    doy_angle = 2 * np.pi * (anchor.dayofyear - 1) / 365.25
    season = np.tile(
        [np.sin(month_angle), np.cos(month_angle), np.sin(doy_angle), np.cos(doy_angle)],
        (values.shape[0], 1),
    )
    static = meta["static"].numpy()
    return np.concatenate([static, last, has.astype(np.float32), season], axis=1).astype(np.float32)


def load_rows(dataset: Path, meta: dict, manifest: list[dict]) -> dict[int, list[dict]]:
    rows: dict[int, list[dict]] = {0: [], 1: [], 2: []}
    node_split = meta["node_split"].numpy()
    group_id = meta["group_id"].numpy()
    station_id = np.asarray(meta["station_id"], dtype=object)
    for item in manifest:
        snap = torch.load(dataset / "snapshots" / item["file"], map_location="cpu", weights_only=False)
        x = latest_features(snap, meta)
        y = snap["target"].numpy()
        observed = snap["target_observed"].numpy().astype(bool)
        censor = snap["target_censor"].numpy()
        for pidx in range(y.shape[1]):
            idx = np.flatnonzero(observed[:, pidx])
            if not len(idx):
                continue
            rows[int(item["time_split"])].append({
                "pidx": pidx, "anchor": item["anchor"], "node": idx,
                "node_split": node_split[idx], "group": group_id[idx], "station": station_id[idx],
                "x": x[idx], "y": y[idx, pidx], "censor": censor[idx, pidx],
            })
    return rows


def concat(rows: list[dict], pidx: int, selector) -> tuple[np.ndarray, ...]:
    chosen = []
    for r in rows:
        if r["pidx"] != pidx:
            continue
        keep = selector(r)
        if np.any(keep):
            chosen.append((r, keep))
    if not chosen:
        return tuple(np.array([]) for _ in range(7))
    return (
        np.concatenate([r["x"][k] for r, k in chosen]),
        np.concatenate([r["y"][k] for r, k in chosen]),
        np.concatenate([r["censor"][k] for r, k in chosen]),
        np.concatenate([r["group"][k] for r, k in chosen]),
        np.concatenate([r["node"][k] for r, k in chosen]),
        np.concatenate([r["station"][k] for r, k in chosen]),
        np.concatenate([[r["anchor"]] * int(np.sum(k)) for r, k in chosen]),
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--max-iter", type=int, default=300)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    meta = torch.load(args.dataset / "metadata.pt", map_location="cpu", weights_only=False)
    manifest = json.loads((args.dataset / "manifest.json").read_text())
    rows = load_rows(args.dataset, meta, manifest)
    metrics_rows, prediction_rows = [], []
    models = {}

    for pidx, parameter in enumerate(meta["parameters"]):
        xtr, ytr, ctr, *_ = concat(rows[0], pidx, lambda r: (r["node_split"] == 0) & (r["censor"] == 0))
        model = HistGradientBoostingRegressor(
            loss="squared_error", learning_rate=0.06, max_iter=args.max_iter,
            max_leaf_nodes=31, l2_regularization=1.0, early_stopping=True,
            validation_fraction=0.1, n_iter_no_change=25, random_state=args.seed,
        ).fit(xtr, ytr)
        models[parameter] = model

        # Validation residuals from both held-out basins and held-out time.
        val_parts = [
            concat(rows[0], pidx, lambda r: (r["node_split"] == 1) & (r["censor"] == 0)),
            concat(rows[1], pidx, lambda r: (r["node_split"] == 0) & (r["censor"] == 0)),
        ]
        residuals = []
        for xv, yv, *_ in val_parts:
            if len(yv): residuals.append(np.abs(yv - model.predict(xv)))
        q = float(np.quantile(np.concatenate(residuals), 0.9, method="higher"))

        tasks = {
            "spatial": (rows[1], lambda r: r["node_split"] == 2),
            "temporal": (rows[2], lambda r: r["node_split"] == 0),
            "spatial_temporal": (rows[2], lambda r: r["node_split"] == 2),
        }
        for task, (source, selector) in tasks.items():
            x, yz, censor, groups, nodes, stations, anchors = concat(source, pidx, selector)
            if not len(yz): continue
            predz = model.predict(x)
            lowerz, upperz = predz - q, predz + q
            y, pred = inverse(yz, pidx, meta), inverse(predz, pidx, meta)
            lower, upper = inverse(lowerz, pidx, meta), inverse(upperz, pidx, meta)
            exact = censor == 0
            m = point_metrics(y[exact], pred[exact])
            covered = np.where(censor < 0, lower <= y, np.where(censor > 0, upper >= y, (y >= lower) & (y <= upper)))
            m.update({"n_total": int(len(y)), "parameter": parameter, "task": task,
                      "coverage90": float(np.mean(covered)), "mean_interval_width": float(np.mean(upper - lower))})
            metrics_rows.append(m)
            prediction_rows.append(pd.DataFrame({
                "anchor": anchors, "node_id": nodes, "station_id": stations, "pidx": pidx,
                "parameter": parameter, "task": task, "group_id": groups, "censor": censor,
                "y": y, "prediction": pred, "lower90": lower, "upper90": upper,
            }))
        print(parameter, "train_exact", len(ytr), "iterations", model.n_iter_, "q90_z", q)

    pd.DataFrame(metrics_rows).to_csv(args.out / "metrics.csv", index=False)
    pd.DataFrame(metrics_rows).to_json(args.out / "metrics.json", orient="records", indent=2)
    pd.concat(prediction_rows, ignore_index=True).to_parquet(args.out / "predictions.parquet", index=False)
    joblib.dump(models, args.out / "models.joblib", compress=3)
    print(pd.DataFrame(metrics_rows).to_string(index=False))


if __name__ == "__main__":
    main()
