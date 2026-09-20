#!/usr/bin/env bash
set -euo pipefail

CODE="${CODE:-$HOME/hydro_ctgssm_code}"
DATASET="${DATASET:-$HOME/hydro_ctgssm_run/dataset}"
RUNS="${RUNS:-$HOME/hydro_ctgssm_run/runs}"
PYTHON="${PYTHON:-$HOME/venvs/hydroctgssm/bin/python}"
export PYTHONPATH="$CODE"

run_one() {
  local name="$1"; shift
  "$PYTHON" "$CODE/train.py" "$DATASET" --out "$RUNS/$name" --epochs 30 --hidden-dim 128 --graph-layers 3 --grad-accum 4 --patience 6 --seed 42 "$@"
  local graph_mode="directed"
  if [[ "$name" == "ablation_no_graph" ]]; then graph_mode="none"; fi
  if [[ "$name" == "ablation_undirected" ]]; then graph_mode="undirected"; fi
  "$PYTHON" "$CODE/evaluate.py" "$DATASET" "$RUNS/$name/best.pt" --out "$RUNS/$name/evaluation" --graph-mode "$graph_mode"
}

run_one ablation_no_graph --graph-mode none
run_one ablation_undirected --graph-mode undirected
run_one ablation_no_decay --no-decay
run_one ablation_substitute --loss-mode substitute
run_one ablation_no_groupdro --no-group-dro

