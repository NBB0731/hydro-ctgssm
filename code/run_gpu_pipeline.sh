#!/usr/bin/env bash
set -euo pipefail

ROOT="${1:-$HOME/hydro_ctgssm_run}"
ARCHIVE="${ARCHIVE:-$ROOT/data/GFQA_v3.zip}"
RIVER_SHP="${RIVER_SHP:-$ROOT/data/HydroRIVERS_v10_shp/HydroRIVERS_v10.shp}"
PYTHON="${PYTHON:-python}"

mkdir -p "$ROOT"/{data,prepared,matched,graph,dataset,runs}

$PYTHON prepare_gemstat_events.py "$ARCHIVE" --out "$ROOT/prepared"
$PYTHON match_stations_to_hydrorivers.py "$ROOT/prepared/stations.parquet" "$RIVER_SHP" --out "$ROOT/matched/stations_hydrorivers.parquet"
$PYTHON build_station_graph.py "$ROOT/matched/stations_hydrorivers.parquet" "$RIVER_SHP" --out "$ROOT/graph"
$PYTHON build_snapshots.py "$ROOT/prepared" "$ROOT/graph" --out "$ROOT/dataset"
$PYTHON train.py "$ROOT/dataset" --out "$ROOT/runs/main_erm_seed42" --seed 42 --epochs 30 --no-group-dro --resume
$PYTHON evaluate.py "$ROOT/dataset" "$ROOT/runs/main_erm_seed42/best.pt" --out "$ROOT/runs/main_erm_seed42/evaluation"
$PYTHON baseline_histgb.py "$ROOT/dataset" --out "$ROOT/runs/baseline_histgb" --seed 42
