#!/usr/bin/env bash
set -euo pipefail
CODE="${CODE:-$HOME/hydro_ctgssm_code}"
DATASET="${DATASET:-$HOME/hydro_ctgssm_run/dataset}"
RUNS="${RUNS:-$HOME/hydro_ctgssm_run/runs}"
PYTHON="${PYTHON:-$HOME/venvs/hydroctgssm/bin/python}"
export PYTHONPATH="$CODE"

for seed in 43 44 45 46; do
  out="$RUNS/final_no_groupdro_seed${seed}"
  "$PYTHON" "$CODE/train.py" "$DATASET" --out "$out" --epochs 30 --hidden-dim 128 --graph-layers 3 --grad-accum 4 --patience 6 --seed "$seed" --no-group-dro
  "$PYTHON" "$CODE/evaluate.py" "$DATASET" "$out/best.pt" --out "$out/evaluation"
done

