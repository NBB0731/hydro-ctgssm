"""Evaluate point accuracy, failure groups and conformal coverage."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
import torch

from hydroctgssm.calibration import GroupConformalCalibrator
from hydroctgssm.model import HydroCTGSSM
from train import graph_for_mode, mask_inductive_histories, move_snapshot


LOG1P = {"NO3N", "TP", "O2-Dis", "TSS", "EC"}


def inverse(z, idx, meta):
    x = z * float(meta["stds"][idx]) + float(meta["means"][idx])
    return np.expm1(x).clip(min=0) if meta["parameters"][idx] in LOG1P else x


def metrics(y, pred):
    if len(y) == 0: return {"n": 0, "mae": None, "rmse": None, "r2": None, "spearman": None}
    err = pred - y
    ss = np.sum((y - y.mean()) ** 2)
    return {"n": int(len(y)), "mae": float(np.mean(np.abs(err))), "rmse": float(np.sqrt(np.mean(err ** 2))),
            "r2": float(1 - np.sum(err ** 2) / ss) if ss > 0 else None,
            "spearman": float(pd.Series(y).corr(pd.Series(pred), method="spearman")) if len(y) > 2 else None}


@torch.no_grad()
def collect(model, items, dataset, meta, device, graph_mode, node_selector):
    edge_index, edge_attr = graph_for_mode(meta["edge_index"].to(device), meta["edge_attr"].to(device), graph_mode)
    node_split = meta["node_split"].to(device); static = meta["static"].to(device)
    records = []
    for item in items:
        s = move_snapshot(torch.load(dataset / "snapshots" / item["file"], map_location="cpu", weights_only=False), device)
        mask_inductive_histories(s, node_split, meta["primary_indices"])
        out = model(s["values"], s["observed"], s["censor"], s["delta_days"], s["dynamic"], static, s["valid_event"], edge_index, edge_attr)
        selected = node_selector(node_split)
        idx_node, idx_param = torch.where(s["target_observed"].bool() & selected[:, None])
        if len(idx_node) == 0: continue
        group = meta["group_id"][idx_node.cpu()].numpy()
        station = np.asarray(meta["station_id"], dtype=object)[idx_node.cpu().numpy()]
        for n, p, y, mu, scale, cens, g, st in zip(
            idx_node.cpu().numpy(), idx_param.cpu().numpy(), s["target"][idx_node, idx_param].cpu().numpy(),
            out["location"][idx_node, idx_param].cpu().numpy(), out["scale"][idx_node, idx_param].cpu().numpy(),
            s["target_censor"][idx_node, idx_param].cpu().numpy(), group, station,
        ):
            records.append((item["anchor"], int(n), str(st), int(p), float(y), float(mu), float(scale), int(cens), int(g)))
    return pd.DataFrame(records, columns=["anchor", "node_id", "station_id", "pidx", "y_z", "mu_z", "scale_z", "censor", "group_id"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset", type=Path); ap.add_argument("checkpoint", type=Path); ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--graph-mode", choices=["directed", "undirected", "none"], default="directed")
    args = ap.parse_args(); args.out.mkdir(parents=True, exist_ok=True)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    meta = torch.load(args.dataset / "metadata.pt", map_location="cpu", weights_only=False)
    ckpt = torch.load(args.checkpoint, map_location=device, weights_only=False); cfg = ckpt["args"]
    model = HydroCTGSSM(len(meta["parameters"]), 4, meta["static"].shape[1], meta["edge_attr"].shape[1],
                         cfg["hidden_dim"], cfg["graph_layers"] if args.graph_mode != "none" else 0,
                         cfg["dropout"], use_decay=not cfg["no_decay"]).to(device)
    model.load_state_dict(ckpt["model"]); model.eval()
    manifest = json.loads((args.dataset / "manifest.json").read_text())
    val_items = [x for x in manifest if x["time_split"] == 1]
    test_items = [x for x in manifest if x["time_split"] == 2]
    calib = collect(model, val_items, args.dataset, meta, device, args.graph_mode, lambda s: s.eq(1))
    spatial = collect(model, val_items, args.dataset, meta, device, args.graph_mode, lambda s: s.eq(2)); spatial["task"] = "spatial"
    temporal = collect(model, test_items, args.dataset, meta, device, args.graph_mode, lambda s: s.eq(0)); temporal["task"] = "temporal"
    both = collect(model, test_items, args.dataset, meta, device, args.graph_mode, lambda s: s.eq(2)); both["task"] = "spatial_temporal"
    test = pd.concat([spatial, temporal, both], ignore_index=True)
    rows = []
    for pidx, parameter in enumerate(meta["parameters"]):
        c = calib[calib.pidx.eq(pidx) & calib.censor.eq(0)]
        if len(c) < 2: continue
        conformal = GroupConformalCalibrator(alpha=0.10, min_group_size=50).fit(
            c.y_z.to_numpy(), c.mu_z.to_numpy(), c.scale_z.to_numpy(), c.group_id.to_numpy())
        for task, g in test[test.pidx.eq(pidx)].groupby("task"):
            if not len(g): continue
            lower_z, upper_z = conformal.interval(g.mu_z.to_numpy(), g.scale_z.to_numpy(), g.group_id.to_numpy())
            y = inverse(g.y_z.to_numpy(), pidx, meta); pred = inverse(g.mu_z.to_numpy(), pidx, meta)
            lower = inverse(lower_z, pidx, meta); upper = inverse(upper_z, pidx, meta)
            exact = g.censor.to_numpy() == 0
            m = metrics(y[exact], pred[exact])
            censor = g.censor.to_numpy()
            covered = np.where(censor < 0, lower <= y, np.where(censor > 0, upper >= y, (y >= lower) & (y <= upper)))
            m.update({"n_total": int(len(g)), "parameter": parameter, "task": task, "coverage90": float(np.mean(covered)),
                      "mean_interval_width": float(np.mean(upper - lower))})
            rows.append(m)
            ix = g.index
            test.loc[ix, "y"] = y; test.loc[ix, "prediction"] = pred; test.loc[ix, "lower90"] = lower; test.loc[ix, "upper90"] = upper
    pd.DataFrame(rows).to_csv(args.out / "metrics.csv", index=False)
    test.to_parquet(args.out / "predictions.parquet", index=False)
    pd.DataFrame(rows).to_json(args.out / "metrics.json", orient="records", indent=2)
    print(pd.DataFrame(rows).to_string(index=False))


if __name__ == "__main__":
    main()
