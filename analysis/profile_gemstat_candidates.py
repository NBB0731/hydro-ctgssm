"""Compute modeling-relevant coverage statistics for candidate water variables."""

from __future__ import annotations

import argparse
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd


DEFAULT_FILES = [
    "Dissolved_Gas.csv",
    "Oxidized_Nitrogen.csv",
    "Other_Nitrogen.csv",
    "Phosphorus.csv",
    "Optical.csv",
    "Temperature.csv",
    "pH.csv",
    "Electrical_Conductance.csv",
    "Oxygen_Demand.csv",
    "Water.csv",
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--chunksize", type=int, default=250_000)
    parser.add_argument("--files", nargs="*", default=DEFAULT_FILES)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    counts = Counter()
    censored = Counter()
    quality = defaultdict(Counter)
    units = defaultdict(Counter)
    stations = defaultdict(set)
    station_observations = defaultdict(Counter)
    years = defaultdict(Counter)
    recent = Counter()
    surface = Counter()

    with zipfile.ZipFile(args.archive) as zf:
        available = set(zf.namelist())
        for member in args.files:
            if member not in available:
                continue
            with zf.open(member) as handle:
                chunks = pd.read_csv(
                    handle,
                    chunksize=args.chunksize,
                    encoding="cp1252",
                    low_memory=False,
                )
                for chunk in chunks:
                    chunk["Sample Date"] = pd.to_datetime(chunk["Sample Date"], errors="coerce")
                    chunk["Parameter Code"] = chunk["Parameter Code"].astype(str)
                    for code, part in chunk.groupby("Parameter Code", sort=False):
                        n = len(part)
                        counts[code] += n
                        station_values = part["GEMS Station Number"].dropna().astype(str)
                        stations[code].update(station_values.unique())
                        station_observations[code].update(station_values.tolist())
                        years[code].update(part["Sample Date"].dt.year.dropna().astype(int).tolist())
                        recent[code] += int((part["Sample Date"] >= "2010-01-01").sum())
                        surface[code] += int((pd.to_numeric(part["Depth"], errors="coerce") <= 1.0).sum())
                        flags = part["Value Flags"].fillna("").astype(str).str.strip()
                        censored[code] += int(flags.isin(["<", ">"]).sum())
                        quality[code].update(part["Data Quality"].fillna("MISSING").astype(str).tolist())
                        units[code].update(part["Unit"].fillna("MISSING").astype(str).tolist())

    rows = []
    for code, n in counts.most_common():
        per_station = pd.Series(list(station_observations[code].values()), dtype=float)
        year_keys = list(years[code])
        rows.append(
            {
                "parameter_code": code,
                "observations": n,
                "stations": len(stations[code]),
                "median_obs_per_station": float(per_station.median()) if len(per_station) else 0,
                "p25_obs_per_station": float(per_station.quantile(0.25)) if len(per_station) else 0,
                "stations_ge_24_obs": int((per_station >= 24).sum()),
                "stations_ge_60_obs": int((per_station >= 60).sum()),
                "min_year": min(year_keys, default=None),
                "max_year": max(year_keys, default=None),
                "observations_since_2010": recent[code],
                "recent_fraction": recent[code] / n,
                "surface_depth_fraction": surface[code] / n,
                "censored_fraction": censored[code] / n,
                "dominant_unit": units[code].most_common(1)[0][0],
                "dominant_unit_fraction": units[code].most_common(1)[0][1] / n,
                "n_units": len(units[code]),
                "top_units": repr(units[code].most_common(8)),
                "top_quality": repr(quality[code].most_common(8)),
            }
        )

    pd.DataFrame(rows).to_csv(args.out / "candidate_parameter_coverage.csv", index=False)

    station_meta = pd.read_csv(
        zipfile.ZipFile(args.archive).open("GEMStat_station_metadata.csv"),
        encoding="cp1252",
        low_memory=False,
    )
    station_meta.groupby(["Water Type", "Country Name"], dropna=False).size().rename("stations").reset_index().to_csv(
        args.out / "station_coverage_by_water_type_country.csv", index=False
    )


if __name__ == "__main__":
    main()
