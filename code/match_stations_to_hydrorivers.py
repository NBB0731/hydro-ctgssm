"""Snap GEMStat river stations to HydroRIVERS using local metric projections.

The global shapefile is queried in 5-degree tiles with an expanded bounding box,
so only nearby river reaches are loaded. Each tile uses a local azimuthal
equidistant CRS, avoiding the high-latitude distance distortion of Web Mercator.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import Point


ATTRS = [
    "HYRIV_ID",
    "NEXT_DOWN",
    "MAIN_RIV",
    "LENGTH_KM",
    "DIST_DN_KM",
    "DIST_UP_KM",
    "CATCH_SKM",
    "UPLAND_SKM",
    "DIS_AV_CMS",
    "ORD_STRA",
    "ORD_FLOW",
    "HYBAS_L12",
]


def local_aeqd(lon: float, lat: float) -> str:
    return f"+proj=aeqd +lat_0={lat:.6f} +lon_0={lon:.6f} +datum=WGS84 +units=m +no_defs"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("stations", type=Path)
    parser.add_argument("hydrorivers_shp", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--tile-degrees", type=float, default=5.0)
    parser.add_argument("--bbox-padding-degrees", type=float, default=0.30)
    parser.add_argument("--max-distance-m", type=float, default=20_000)
    args = parser.parse_args()

    stations = (
        pd.read_parquet(args.stations)
        if args.stations.suffix.lower() in {".parquet", ".pq"}
        else pd.read_csv(args.stations, encoding="utf-8")
    )
    stations = stations.dropna(subset=["Latitude", "Longitude"]).copy()
    stations["tile_x"] = np.floor(stations["Longitude"] / args.tile_degrees).astype(int)
    stations["tile_y"] = np.floor(stations["Latitude"] / args.tile_degrees).astype(int)
    outputs = []

    for (_, _), part in stations.groupby(["tile_x", "tile_y"], sort=True):
        xmin, xmax = part["Longitude"].min(), part["Longitude"].max()
        ymin, ymax = part["Latitude"].min(), part["Latitude"].max()
        pad = args.bbox_padding_degrees
        rivers = gpd.read_file(
            args.hydrorivers_shp,
            bbox=(xmin - pad, ymin - pad, xmax + pad, ymax + pad),
            columns=ATTRS,
            engine="pyogrio",
        )
        if rivers.empty:
            continue
        points = gpd.GeoDataFrame(
            part.drop(columns=["tile_x", "tile_y"]),
            geometry=[Point(x, y) for x, y in zip(part["Longitude"], part["Latitude"])],
            crs="EPSG:4326",
        )
        lon0 = float((xmin + xmax) / 2)
        lat0 = float((ymin + ymax) / 2)
        metric = local_aeqd(lon0, lat0)
        joined = gpd.sjoin_nearest(
            points.to_crs(metric),
            rivers.to_crs(metric),
            how="left",
            max_distance=args.max_distance_m,
            distance_col="snap_distance_m",
        )
        joined = joined.sort_values("snap_distance_m").drop_duplicates("GEMS Station Number")
        outputs.append(pd.DataFrame(joined.drop(columns=["geometry", "index_right"], errors="ignore")))

    result = pd.concat(outputs, ignore_index=True) if outputs else pd.DataFrame()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    result.to_parquet(args.out, index=False)
    if len(result):
        report = {
            "input_stations": int(len(stations)),
            "matched_stations": int(result["HYRIV_ID"].notna().sum()),
            "matched_fraction": float(result["HYRIV_ID"].notna().sum() / max(len(stations), 1)),
            "median_snap_distance_m": float(result["snap_distance_m"].median()),
            "p95_snap_distance_m": float(result["snap_distance_m"].quantile(0.95)),
        }
        pd.Series(report).to_json(args.out.with_suffix(".report.json"), indent=2)


if __name__ == "__main__":
    main()
