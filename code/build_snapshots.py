"""Create leakage-controlled event snapshots for Hydro-CTGSSM training."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import torch


PARAMETERS = ["NO3N", "TP", "O2-Dis", "TSS", "TEMP", "pH", "EC"]
PRIMARY = [0, 1, 2, 3]
LOG1P = {"NO3N", "TP", "O2-Dis", "TSS", "EC"}


def aggregate_daily(frame: pd.DataFrame) -> pd.DataFrame:
    keys = ["station_id", "sample_date", "parameter"]
    exact = frame[frame.censor.eq(0)].groupby(keys, sort=False, observed=True).value.median().reset_index()
    exact["censor"] = np.int8(0)
    exact_keys = pd.MultiIndex.from_frame(exact[keys])
    all_keys = pd.MultiIndex.from_frame(frame[keys])
    remaining = frame[~all_keys.isin(exact_keys)]
    if len(remaining):
        grouped = remaining.groupby(keys, sort=False, observed=True)
        censored = grouped.value.median().rename("value").reset_index()
        cmin = grouped.censor.min().to_numpy(); cmax = grouped.censor.max().to_numpy()
        censored["censor"] = np.where(cmin == cmax, cmin, 0).astype(np.int8)
        return pd.concat([exact, censored], ignore_index=True)
    return exact


def balanced_basin_split(nodes: pd.DataFrame, seed: int) -> np.ndarray:
    basin = nodes["Main Basin"].fillna(nodes["Country Name"].fillna("UNKNOWN")).astype(str)
    counts = basin.value_counts().to_dict()
    order = sorted(counts, key=lambda x: (-counts[x], hashlib.sha1(f"{seed}:{x}".encode()).hexdigest()))
    target = np.array([0.70, 0.15, 0.15]) * len(nodes)
    totals = np.zeros(3)
    assignment = {}
    for b in order:
        score = totals / np.maximum(target, 1)
        split = int(np.argmin(score))
        assignment[b] = split
        totals[split] += counts[b]
    return basin.map(assignment).to_numpy(np.int8)


def transform(x: np.ndarray, parameter: str) -> np.ndarray:
    if parameter in LOG1P:
        return np.log1p(np.clip(x, 0, None))
    return x


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("events_dir", type=Path)
    ap.add_argument("graph_dir", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--anchor-start", default="2015-01-01")
    ap.add_argument("--anchor-end", default="2024-10-01")
    ap.add_argument("--anchor-freq", default="QS")
    ap.add_argument("--history-events", type=int, default=32)
    ap.add_argument("--horizon-days", type=int, default=90)
    ap.add_argument("--train-time-end", default="2021-12-31")
    ap.add_argument("--val-time-end", default="2022-12-31")
    ap.add_argument("--seed", type=int, default=20260715)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    snap_dir = args.out / "snapshots"; snap_dir.mkdir(exist_ok=True)

    nodes = pd.read_parquet(args.graph_dir / "nodes.parquet").reset_index(drop=True)
    nodes["station_id"] = nodes["GEMS Station Number"].astype(str)
    station_to_node = dict(zip(nodes.station_id, nodes.node_id.astype(int)))

    parts = pd.read_csv(args.events_dir / "event_parts.csv").file.tolist()
    frames = []
    unit_rows = []
    for name in parts:
        x = pd.read_parquet(args.events_dir / name)
        x = x[x.station_id.astype(str).isin(station_to_node)]
        if len(x):
            unit_rows.append(x.groupby(["parameter", "unit"], dropna=False).size().rename("n").reset_index())
            frames.append(x[["station_id", "sample_date", "parameter", "value", "censor"]])
    events = pd.concat(frames, ignore_index=True)
    events["station_id"] = events.station_id.astype(str)
    events["sample_date"] = pd.to_datetime(events.sample_date).dt.normalize()
    events = events[events.parameter.isin(PARAMETERS)]
    pd.concat(unit_rows).groupby(["parameter", "unit"], dropna=False).n.sum().reset_index().to_csv(args.out / "unit_audit.csv", index=False)
    events = aggregate_daily(events)
    events["node_id"] = events.station_id.map(station_to_node).astype(np.int64)

    train_end = pd.Timestamp(args.train_time_end)
    node_split = balanced_basin_split(nodes, args.seed)
    train_nodes = set(nodes.loc[node_split == 0, "node_id"].astype(int))

    means, stds = [], []
    for p in PARAMETERS:
        raw = events[(events.parameter == p) & events.node_id.isin(train_nodes) & events.sample_date.le(train_end)].value.to_numpy(float)
        z = transform(raw, p)
        means.append(float(np.nanmean(z)))
        s = float(np.nanstd(z)); stds.append(s if s > 1e-6 else 1.0)
    means = np.asarray(means, np.float32); stds = np.asarray(stds, np.float32)
    p_index = {p: i for i, p in enumerate(PARAMETERS)}
    events["pidx"] = events.parameter.map(p_index)
    raw_values = events.value.to_numpy(np.float64)
    for p, idx in p_index.items():
        mask = events.pidx.eq(idx).to_numpy()
        raw_values[mask] = (transform(raw_values[mask], p) - means[idx]) / stds[idx]
    events["zvalue"] = raw_values.astype(np.float32)

    # Static features, imputed and standardized on training nodes only.
    static_cols = ["Upstream Basin Area", "Elevation", "Latitude", "Longitude", "River Width", "Discharge", "snap_distance_m", "CATCH_SKM", "UPLAND_SKM", "DIS_AV_CMS", "ORD_STRA", "ORD_FLOW"]
    available = [c for c in static_cols if c in nodes.columns]
    static = nodes[available].apply(pd.to_numeric, errors="coerce").to_numpy(np.float64)
    for j, col in enumerate(available):
        if col in {"Upstream Basin Area", "River Width", "Discharge", "snap_distance_m", "CATCH_SKM", "UPLAND_SKM", "DIS_AV_CMS"}:
            static[:, j] = np.log1p(np.clip(static[:, j], 0, None))
    train_mask = node_split == 0
    med = np.nanmedian(static[train_mask], axis=0)
    static = np.where(np.isnan(static), med, static)
    sm = static[train_mask].mean(axis=0); ss = static[train_mask].std(axis=0); ss[ss < 1e-6] = 1
    static = ((static - sm) / ss).astype(np.float32)

    group_labels = nodes["Country Name"].fillna("UNKNOWN").astype(str)
    unique_groups = sorted(group_labels.unique())
    group_map = {g: i for i, g in enumerate(unique_groups)}
    group_id = group_labels.map(group_map).to_numpy(np.int64)

    graph = np.load(args.graph_dir / "graph.npz")
    metadata = {
        "parameters": PARAMETERS, "primary_indices": PRIMARY, "means": means, "stds": stds,
        "static": torch.from_numpy(static), "static_columns": available,
        "node_split": torch.from_numpy(node_split.astype(np.int64)), "group_id": torch.from_numpy(group_id),
        "group_labels": unique_groups, "station_id": nodes.station_id.tolist(),
        "edge_index": torch.from_numpy(graph["edge_index"]), "edge_attr": torch.from_numpy(graph["edge_attr"]),
        "train_time_end": args.train_time_end, "val_time_end": args.val_time_end,
    }
    torch.save(metadata, args.out / "metadata.pt")

    by_node = {}
    for node, g in events.sort_values("sample_date").groupby("node_id", sort=False):
        dates = g.sample_date.to_numpy("datetime64[D]")
        by_node[int(node)] = (dates, g.pidx.to_numpy(np.int16), g.zvalue.to_numpy(np.float32), g.censor.to_numpy(np.int8))

    anchors = pd.date_range(args.anchor_start, args.anchor_end, freq=args.anchor_freq)
    manifest = []
    n_nodes, length, k = len(nodes), args.history_events, len(PARAMETERS)
    for anchor in anchors:
        values = np.zeros((n_nodes, length, k), np.float32)
        observed = np.zeros((n_nodes, length, k), np.uint8)
        censor = np.zeros((n_nodes, length, k), np.int8)
        delta = np.zeros((n_nodes, length), np.float32)
        dynamic = np.zeros((n_nodes, length, 4), np.float32)
        valid = np.zeros((n_nodes, length), np.uint8)
        target = np.zeros((n_nodes, k), np.float32)
        target_observed = np.zeros((n_nodes, k), np.uint8)
        target_censor = np.zeros((n_nodes, k), np.int8)
        a = np.datetime64(anchor.date(), "D")
        end = a + np.timedelta64(args.horizon_days, "D")
        for node, (dates, pidxs, zvals, cens) in by_node.items():
            cut = int(np.searchsorted(dates, a, side="left"))
            hist_start = max(0, cut - length * 8)
            daily = {}
            for pos in range(hist_start, cut):
                daily.setdefault(dates[pos], []).append(pos)
            selected_dates = sorted(daily)[-length:]
            offset = length - len(selected_dates)
            previous = None
            for j, dt in enumerate(selected_dates, start=offset):
                valid[node, j] = 1
                delta[node, j] = 0 if previous is None else float((dt - previous) / np.timedelta64(1, "D"))
                previous = dt
                pydate = pd.Timestamp(dt)
                angle_m = 2 * np.pi * (pydate.month - 1) / 12
                angle_d = 2 * np.pi * (pydate.dayofyear - 1) / 365.25
                dynamic[node, j] = [np.sin(angle_m), np.cos(angle_m), np.sin(angle_d), np.cos(angle_d)]
                for pos in daily[dt]:
                    pidx = int(pidxs[pos]); values[node, j, pidx] = zvals[pos]
                    observed[node, j, pidx] = 1; censor[node, j, pidx] = cens[pos]
            stop = int(np.searchsorted(dates, end, side="right"))
            found = set()
            for pos in range(cut, stop):
                pidx = int(pidxs[pos])
                if pidx not in found:
                    target[node, pidx] = zvals[pos]; target_observed[node, pidx] = 1
                    target_censor[node, pidx] = cens[pos]; found.add(pidx)
                if len(found) == k:
                    break
        if anchor <= pd.Timestamp(args.train_time_end): time_split = 0
        elif anchor <= pd.Timestamp(args.val_time_end): time_split = 1
        else: time_split = 2
        filename = f"snapshot_{anchor:%Y%m%d}.pt"
        torch.save({
            "anchor": str(anchor.date()), "time_split": time_split,
            "values": torch.from_numpy(values), "observed": torch.from_numpy(observed),
            "censor": torch.from_numpy(censor), "delta_days": torch.from_numpy(delta),
            "dynamic": torch.from_numpy(dynamic), "valid_event": torch.from_numpy(valid),
            "target": torch.from_numpy(target), "target_observed": torch.from_numpy(target_observed),
            "target_censor": torch.from_numpy(target_censor),
        }, snap_dir / filename)
        n_target = int(target_observed.sum())
        manifest.append({"file": filename, "anchor": str(anchor.date()), "time_split": time_split, "target_entries": n_target})
        print(anchor.date(), "targets", n_target)

    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    summary = {
        "nodes": n_nodes, "events_daily": len(events), "snapshots": len(manifest),
        "node_split_counts": np.bincount(node_split, minlength=3).tolist(),
        "parameters": PARAMETERS, "static_columns": available,
    }
    (args.out / "dataset_report.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
