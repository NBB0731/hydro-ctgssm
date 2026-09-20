"""Train Hydro-CTGSSM on full-graph quarterly event snapshots."""

from __future__ import annotations

import argparse
import json
import math
import random
from pathlib import Path

import numpy as np
import torch

from hydroctgssm.losses import censored_gaussian_nll_elementwise
from hydroctgssm.model import HydroCTGSSM


def seed_all(seed: int) -> None:
    random.seed(seed); np.random.seed(seed); torch.manual_seed(seed); torch.cuda.manual_seed_all(seed)


def move_snapshot(s, device):
    return {k: (v.to(device, non_blocking=True) if torch.is_tensor(v) else v) for k, v in s.items()}


def mask_inductive_histories(s, node_split, primary):
    held = node_split.ne(0)
    for p in primary:
        s["values"][held, :, p] = 0
        s["observed"][held, :, p] = 0
        s["censor"][held, :, p] = 0


def graph_for_mode(edge_index, edge_attr, mode):
    if mode == "none":
        return edge_index[:, :0], edge_attr[:0]
    if mode == "undirected":
        return torch.cat([edge_index, edge_index.flip(0)], 1), torch.cat([edge_attr, edge_attr], 0)
    return edge_index, edge_attr


def group_dro_loss(nll, weight, node_mask, group_id, q, step_size, update):
    entry_weight = weight * node_mask[:, None].to(weight.dtype)
    node_weight = entry_weight.sum(1)
    node_loss = (nll * entry_weight).sum(1) / node_weight.clamp_min(1)
    valid = node_weight.gt(0)
    g = group_id[valid]; loss = node_loss[valid]; w = node_weight[valid]
    n_groups = len(q)
    sums = torch.zeros(n_groups, device=loss.device).index_add_(0, g, loss * w)
    counts = torch.zeros(n_groups, device=loss.device).index_add_(0, g, w)
    present = counts.gt(0)
    gl = sums / counts.clamp_min(1)
    if update:
        with torch.no_grad():
            q[present] *= torch.exp(step_size * gl[present].detach().clamp(max=20))
            q /= q.sum().clamp_min(1e-12)
    qp = q * present
    qp = qp / qp.sum().clamp_min(1e-12)
    return (qp * gl).sum(), int(valid.sum()), gl.detach()


@torch.no_grad()
def validate(model, files, root, metadata, device, graph_mode, loss_mode):
    model.eval(); total, denom = 0.0, 0
    edge_index, edge_attr = graph_for_mode(metadata["edge_index"].to(device), metadata["edge_attr"].to(device), graph_mode)
    node_split = metadata["node_split"].to(device); primary = metadata["primary_indices"]
    static = metadata["static"].to(device)
    for item in files:
        s = move_snapshot(torch.load(root / "snapshots" / item["file"], map_location="cpu", weights_only=False), device)
        mask_inductive_histories(s, node_split, primary)
        out = model(s["values"], s["observed"], s["censor"], s["delta_days"], s["dynamic"], static, s["valid_event"], edge_index, edge_attr)
        censor = torch.zeros_like(s["target_censor"]) if loss_mode == "substitute" else s["target_censor"]
        nll, weight = censored_gaussian_nll_elementwise(out["location"], out["scale"], s["target"], s["target_observed"], censor)
        node_mask = node_split.eq(1)
        w = weight * node_mask[:, None]
        total += float((nll * w).sum()); denom += int(w.sum())
    return total / max(denom, 1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--epochs", type=int, default=30)
    ap.add_argument("--hidden-dim", type=int, default=128)
    ap.add_argument("--graph-layers", type=int, default=3)
    ap.add_argument("--dropout", type=float, default=0.1)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--weight-decay", type=float, default=1e-4)
    ap.add_argument("--grad-accum", type=int, default=2)
    ap.add_argument("--clip-grad", type=float, default=1.0)
    ap.add_argument("--patience", type=int, default=6)
    ap.add_argument("--group-dro-step", type=float, default=0.05)
    ap.add_argument("--no-group-dro", action="store_true")
    ap.add_argument("--no-decay", action="store_true")
    ap.add_argument("--graph-mode", choices=["directed", "undirected", "none"], default="directed")
    ap.add_argument("--loss-mode", choices=["censored", "substitute"], default="censored")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    seed_all(args.seed)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    meta = torch.load(args.dataset / "metadata.pt", map_location="cpu", weights_only=False)
    manifest = json.loads((args.dataset / "manifest.json").read_text())
    train_files = [x for x in manifest if x["time_split"] == 0]
    val_files = [x for x in manifest if x["time_split"] == 1]
    model = HydroCTGSSM(
        n_targets=len(meta["parameters"]), dynamic_dim=4, static_dim=meta["static"].shape[1],
        edge_dim=meta["edge_attr"].shape[1], hidden_dim=args.hidden_dim,
        graph_layers=args.graph_layers if args.graph_mode != "none" else 0,
        dropout=args.dropout, use_decay=not args.no_decay,
    ).to(device)
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=args.weight_decay)
    scaler = torch.amp.GradScaler("cuda", enabled=device.type == "cuda")
    q = torch.ones(len(meta["group_labels"]), device=device) / len(meta["group_labels"])
    start_epoch, best, bad = 1, math.inf, 0
    ckpt_path = args.out / "last.pt"
    if args.resume and ckpt_path.exists():
        c = torch.load(ckpt_path, map_location=device, weights_only=False)
        model.load_state_dict(c["model"]); opt.load_state_dict(c["optimizer"]); q.copy_(c["group_weights"])
        start_epoch = c["epoch"] + 1; best = c["best_val"]

    edge_index, edge_attr = graph_for_mode(meta["edge_index"].to(device), meta["edge_attr"].to(device), args.graph_mode)
    node_split = meta["node_split"].to(device); group_id = meta["group_id"].to(device); static = meta["static"].to(device)
    history = []
    for epoch in range(start_epoch, args.epochs + 1):
        model.train(); order = train_files.copy(); random.shuffle(order)
        opt.zero_grad(set_to_none=True); running, entries = 0.0, 0
        for step, item in enumerate(order, start=1):
            s = move_snapshot(torch.load(args.dataset / "snapshots" / item["file"], map_location="cpu", weights_only=False), device)
            mask_inductive_histories(s, node_split, meta["primary_indices"])
            with torch.autocast(device_type=device.type, dtype=torch.float16, enabled=device.type == "cuda"):
                out = model(s["values"], s["observed"], s["censor"], s["delta_days"], s["dynamic"], static, s["valid_event"], edge_index, edge_attr)
                target_censor = torch.zeros_like(s["target_censor"]) if args.loss_mode == "substitute" else s["target_censor"]
                nll, weight = censored_gaussian_nll_elementwise(out["location"], out["scale"], s["target"], s["target_observed"], target_censor)
                if args.no_group_dro:
                    w = weight * node_split.eq(0)[:, None]
                    loss = (nll * w).sum() / w.sum().clamp_min(1)
                    n_entries = int(w.sum())
                else:
                    loss, n_entries, _ = group_dro_loss(nll, weight, node_split.eq(0), group_id, q, args.group_dro_step, update=True)
                scaled_loss = loss / args.grad_accum
            scaler.scale(scaled_loss).backward()
            if step % args.grad_accum == 0 or step == len(order):
                scaler.unscale_(opt); torch.nn.utils.clip_grad_norm_(model.parameters(), args.clip_grad)
                scaler.step(opt); scaler.update(); opt.zero_grad(set_to_none=True)
            running += float(loss.detach()) * n_entries; entries += n_entries
            del s, out, nll, weight, loss
        val = validate(model, val_files, args.dataset, meta, device, args.graph_mode, args.loss_mode)
        train_loss = running / max(entries, 1)
        row = {"epoch": epoch, "train_loss": train_loss, "val_nll": val, "lr": opt.param_groups[0]["lr"]}
        history.append(row); print(json.dumps(row), flush=True)
        improved = val < best - 1e-5
        if improved: best = val; bad = 0
        else: bad += 1
        state = {"epoch": epoch, "model": model.state_dict(), "optimizer": opt.state_dict(), "group_weights": q.detach().cpu(), "best_val": best, "args": vars(args), "metadata": {"parameters": meta["parameters"], "static_columns": meta["static_columns"]}}
        torch.save(state, ckpt_path)
        if improved: torch.save(state, args.out / "best.pt")
        (args.out / "history.json").write_text(json.dumps(history, indent=2), encoding="utf-8")
        if bad >= args.patience: break
    print(json.dumps({"best_val_nll": best, "device": str(device), "epochs_completed": history[-1]["epoch"]}, indent=2))


if __name__ == "__main__":
    main()
