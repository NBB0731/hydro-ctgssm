"""Build the nearest occupied downstream station graph from HydroRIVERS topology."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


EDGE_COLUMNS = ["LENGTH_KM", "CATCH_SKM", "UPLAND_SKM", "DIS_AV_CMS", "ORD_STRA", "ORD_FLOW"]


def read_topology(path: Path) -> pd.DataFrame:
    try:
        import pyogrio
        return pyogrio.read_dataframe(path, columns=["HYRIV_ID", "NEXT_DOWN", *EDGE_COLUMNS], read_geometry=False)
    except Exception:
        import geopandas as gpd
        return pd.DataFrame(gpd.read_file(path, include_fields=["HYRIV_ID", "NEXT_DOWN", *EDGE_COLUMNS], ignore_geometry=True))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("matched_stations", type=Path)
    ap.add_argument("hydrorivers_shp", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--max-hops", type=int, default=5000)
    args = ap.parse_args()

    stations = pd.read_parquet(args.matched_stations)
    stations = stations[stations["HYRIV_ID"].notna()].copy()
    stations["HYRIV_ID"] = stations["HYRIV_ID"].astype("int64")
    stations = stations.sort_values("GEMS Station Number").drop_duplicates("GEMS Station Number").reset_index(drop=True)
    stations["node_id"] = np.arange(len(stations), dtype=np.int64)

    topo = read_topology(args.hydrorivers_shp)
    topo = topo.dropna(subset=["HYRIV_ID"]).drop_duplicates("HYRIV_ID")
    topo["HYRIV_ID"] = topo["HYRIV_ID"].astype("int64")
    topo["NEXT_DOWN"] = pd.to_numeric(topo["NEXT_DOWN"], errors="coerce").fillna(0).astype("int64")
    next_down = dict(zip(topo["HYRIV_ID"], topo["NEXT_DOWN"]))
    attr_map = topo.set_index("HYRIV_ID")[EDGE_COLUMNS].apply(pd.to_numeric, errors="coerce").to_dict("index")

    occupied: dict[int, list[int]] = {}
    for row in stations[["HYRIV_ID", "node_id"]].itertuples(index=False):
        occupied.setdefault(int(row.HYRIV_ID), []).append(int(row.node_id))

    cache: dict[int, tuple[int | None, int]] = {}

    def downstream_occupied(reach: int) -> tuple[int | None, int]:
        if reach in cache:
            return cache[reach]
        seen = []
        cur = int(next_down.get(reach, 0))
        hops = 0
        while cur > 0 and hops < args.max_hops:
            if cur in occupied:
                result = (cur, hops + 1)
                for s in seen:
                    cache[s] = result
                cache[reach] = result
                return result
            if cur in seen:
                break
            seen.append(cur)
            cur = int(next_down.get(cur, 0))
            hops += 1
        cache[reach] = (None, hops)
        return cache[reach]

    src, dst, attrs, hop_counts = [], [], [], []
    for reach, nodes in occupied.items():
        target_reach, hops = downstream_occupied(reach)
        if target_reach is None:
            continue
        raw = attr_map.get(reach, {})
        a = [float(raw.get(c, np.nan)) for c in EDGE_COLUMNS]
        for u in nodes:
            for v in occupied[target_reach]:
                if u != v:
                    src.append(u); dst.append(v); attrs.append(a); hop_counts.append(hops)

    edge_attr = np.asarray(attrs, dtype=np.float32)
    if len(edge_attr):
        edge_attr[:, :4] = np.log1p(np.clip(edge_attr[:, :4], 0, None))
        med = np.nanmedian(edge_attr, axis=0)
        edge_attr = np.where(np.isnan(edge_attr), med, edge_attr)
        mean = edge_attr.mean(axis=0)
        std = edge_attr.std(axis=0); std[std < 1e-6] = 1.0
        edge_attr = (edge_attr - mean) / std
    else:
        mean = np.zeros(len(EDGE_COLUMNS), dtype=np.float32)
        std = np.ones(len(EDGE_COLUMNS), dtype=np.float32)

    args.out.mkdir(parents=True, exist_ok=True)
    stations.to_parquet(args.out / "nodes.parquet", index=False)
    np.savez_compressed(
        args.out / "graph.npz",
        edge_index=np.asarray([src, dst], dtype=np.int64),
        edge_attr=edge_attr.astype(np.float32),
        hop_count=np.asarray(hop_counts, dtype=np.int32),
        edge_mean=mean.astype(np.float32),
        edge_std=std.astype(np.float32),
    )
    report = {
        "nodes": len(stations), "edges": len(src), "occupied_reaches": len(occupied),
        "nodes_with_outgoing": len(set(src)), "median_hops": float(np.median(hop_counts)) if hop_counts else None,
    }
    (args.out / "graph_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
