"""Convert GEMStat v3 into an analysis-ready event table.

Run on the GPU server before building graph examples. The primary cohort keeps
river stations, surface samples (<=1 m), 2010 onward, and non-suspect records.
Detection-limit flags are retained for the censored likelihood.
"""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd


PARAMETER_FILES = {
    "NO3N": "Oxidized_Nitrogen.csv",
    "TP": "Phosphorus.csv",
    "O2-Dis": "Dissolved_Gas.csv",
    "TSS": "Water.csv",
    "TEMP": "Temperature.csv",
    "pH": "pH.csv",
    "EC": "Electrical_Conductance.csv",
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--since", default="2010-01-01")
    parser.add_argument("--max-depth-m", type=float, default=1.0)
    parser.add_argument("--chunksize", type=int, default=250_000)
    parser.add_argument("--primary-quality", nargs="+", default=["Fair", "Good"])
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(args.archive) as zf:
        stations = pd.read_csv(zf.open("GEMStat_station_metadata.csv"), encoding="cp1252", low_memory=False)
        stations = stations[stations["Water Type"].eq("River station")].copy()
        stations = stations.drop_duplicates("GEMS Station Number")
        stations = stations.dropna(subset=["Latitude", "Longitude"])
        keep_stations = set(stations["GEMS Station Number"].astype(str))
        station_columns = [
            "GEMS Station Number",
            "Country Name",
            "Main Basin",
            "Upstream Basin Area",
            "Elevation",
            "Latitude",
            "Longitude",
            "River Width",
            "Discharge",
            "Monitoring Type",
        ]
        stations[station_columns].to_parquet(args.out / "stations.parquet", index=False)

        output_parts = []
        part_number = 0
        for parameter, member in PARAMETER_FILES.items():
            with zf.open(member) as handle:
                for chunk in pd.read_csv(handle, chunksize=args.chunksize, encoding="cp1252", low_memory=False):
                    chunk["GEMS Station Number"] = chunk["GEMS Station Number"].astype(str)
                    chunk = chunk[
                        chunk["Parameter Code"].astype(str).eq(parameter)
                        & chunk["GEMS Station Number"].isin(keep_stations)
                    ].copy()
                    if chunk.empty:
                        continue
                    chunk["sample_date"] = pd.to_datetime(chunk["Sample Date"], errors="coerce")
                    chunk["depth_m"] = pd.to_numeric(chunk["Depth"], errors="coerce")
                    chunk["value"] = pd.to_numeric(chunk["Value"], errors="coerce")
                    chunk = chunk[
                        chunk["sample_date"].ge(args.since)
                        & chunk["value"].notna()
                        & (chunk["depth_m"].isna() | chunk["depth_m"].le(args.max_depth_m))
                        & chunk["Data Quality"].isin(args.primary_quality)
                    ]
                    if chunk.empty:
                        continue
                    flag = chunk["Value Flags"].fillna("").astype(str).str.strip()
                    chunk["censor"] = np.select([flag.eq("<"), flag.eq(">")], [-1, 1], default=0).astype("int8")
                    chunk["parameter"] = parameter
                    chunk["station_id"] = chunk["GEMS Station Number"]
                    clean = chunk[
                        [
                            "station_id",
                            "sample_date",
                            "parameter",
                            "value",
                            "censor",
                            "depth_m",
                            "Unit",
                            "Data Quality",
                            "Analysis Method Code",
                        ]
                    ].rename(columns={"Unit": "unit", "Data Quality": "data_quality", "Analysis Method Code": "method_code"})
                    path = args.out / f"events_part_{part_number:05d}.parquet"
                    clean.to_parquet(path, index=False)
                    output_parts.append(path.name)
                    part_number += 1

    pd.Series(output_parts, name="file").to_csv(args.out / "event_parts.csv", index=False)


if __name__ == "__main__":
    main()

