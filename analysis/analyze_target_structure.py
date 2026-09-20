"""Analyze river-only target structure to decide the model from the data."""

from __future__ import annotations

import argparse
import itertools
import sqlite3
import tempfile
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pandas as pd


TARGETS = {
    "TEMP": "Temperature.csv",
    "pH": "pH.csv",
    "NO3N": "Oxidized_Nitrogen.csv",
    "TP": "Phosphorus.csv",
    "O2-Dis": "Dissolved_Gas.csv",
    "EC": "Electrical_Conductance.csv",
    "TSS": "Water.csv",
}


def interval_bin(days: float) -> str:
    if days <= 1:
        return "<=1d"
    if days <= 7:
        return "2-7d"
    if days <= 31:
        return "8-31d"
    if days <= 90:
        return "32-90d"
    if days <= 365:
        return "91-365d"
    return ">365d"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--since", default="2010-01-01")
    parser.add_argument("--chunksize", type=int, default=250_000)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(args.archive) as zf:
        meta = pd.read_csv(zf.open("GEMStat_station_metadata.csv"), encoding="cp1252", low_memory=False)
        rivers = meta[meta["Water Type"].eq("River station")].copy()
        rivers = rivers.dropna(subset=["GEMS Station Number", "Latitude", "Longitude"])
        rivers = rivers.drop_duplicates(subset=["GEMS Station Number"], keep="first")
        info = rivers.set_index("GEMS Station Number")[["Country Name", "Main Basin"]].to_dict("index")
        river_ids = set(info)

        db_path = Path(tempfile.gettempdir()) / "gemstat_cooccurrence.sqlite"
        if db_path.exists():
            db_path.unlink()
        con = sqlite3.connect(db_path)
        con.execute("CREATE TABLE events (station TEXT, sample_date TEXT, mask INTEGER, PRIMARY KEY(station, sample_date))")

        summary_rows = []
        gap_counts = defaultdict(Counter)
        last_date = {}
        station_sets = defaultdict(set)
        countries = defaultdict(set)
        basins = defaultdict(set)
        obs_counts = Counter()

        for bit, (code, member) in enumerate(TARGETS.items()):
            with zf.open(member) as handle:
                for chunk in pd.read_csv(handle, chunksize=args.chunksize, encoding="cp1252", low_memory=False):
                    chunk = chunk[chunk["Parameter Code"].astype(str).eq(code)].copy()
                    if chunk.empty:
                        continue
                    chunk["Sample Date"] = pd.to_datetime(chunk["Sample Date"], errors="coerce")
                    chunk = chunk[
                        chunk["GEMS Station Number"].isin(river_ids)
                        & chunk["Sample Date"].ge(args.since)
                        & chunk["Sample Date"].notna()
                    ]
                    if chunk.empty:
                        continue
                    chunk = chunk.sort_values(["GEMS Station Number", "Sample Date"])
                    for station, dates in chunk.groupby("GEMS Station Number")["Sample Date"]:
                        unique_dates = dates.drop_duplicates().sort_values()
                        previous = last_date.get((code, station))
                        if previous is not None and len(unique_dates):
                            gap_counts[code][interval_bin((unique_dates.iloc[0] - previous).days)] += 1
                        diffs = unique_dates.diff().dt.days.dropna()
                        gap_counts[code].update(interval_bin(float(x)) for x in diffs if x >= 0)
                        if len(unique_dates):
                            last_date[(code, station)] = unique_dates.iloc[-1]

                    stations_here = chunk["GEMS Station Number"].astype(str)
                    obs_counts[code] += len(chunk)
                    station_sets[code].update(stations_here.unique())
                    for station in stations_here.unique():
                        countries[code].add(info[station]["Country Name"])
                        basin = info[station]["Main Basin"]
                        if pd.notna(basin):
                            basins[code].add(str(basin))

                    rows = [
                        (str(s), d.strftime("%Y-%m-%d"), 1 << bit)
                        for s, d in chunk[["GEMS Station Number", "Sample Date"]].drop_duplicates().itertuples(index=False)
                    ]
                    con.executemany(
                        "INSERT INTO events(station,sample_date,mask) VALUES(?,?,?) "
                        "ON CONFLICT(station,sample_date) DO UPDATE SET mask=mask|excluded.mask",
                        rows,
                    )
                    con.commit()

        for code in TARGETS:
            total_gaps = sum(gap_counts[code].values())
            summary_rows.append(
                {
                    "parameter_code": code,
                    "river_observations_since_cutoff": obs_counts[code],
                    "river_stations": len(station_sets[code]),
                    "countries": len(countries[code]),
                    "named_main_basins": len(basins[code]),
                    **{f"gap_{k}_fraction": v / total_gaps if total_gaps else np.nan for k, v in gap_counts[code].items()},
                }
            )

        pd.DataFrame(summary_rows).to_csv(args.out / "river_target_structure.csv", index=False)

        mask_counts = Counter(dict(con.execute("SELECT mask, COUNT(*) FROM events GROUP BY mask").fetchall()))
        pair_rows = []
        codes = list(TARGETS)
        for a, b in itertools.combinations(range(len(codes)), 2):
            both = sum(n for mask, n in mask_counts.items() if mask & (1 << a) and mask & (1 << b))
            either_a = sum(n for mask, n in mask_counts.items() if mask & (1 << a))
            either_b = sum(n for mask, n in mask_counts.items() if mask & (1 << b))
            pair_rows.append(
                {
                    "parameter_a": codes[a],
                    "parameter_b": codes[b],
                    "same_station_day_events": both,
                    "fraction_of_a_events": both / either_a if either_a else np.nan,
                    "fraction_of_b_events": both / either_b if either_b else np.nan,
                }
            )
        pd.DataFrame(pair_rows).sort_values("same_station_day_events", ascending=False).to_csv(
            args.out / "target_cooccurrence.csv", index=False
        )
        con.close()
        db_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
