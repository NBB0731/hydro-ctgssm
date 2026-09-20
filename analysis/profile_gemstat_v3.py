"""Profile the GEMStat v3 archive before any modeling decisions are made.

The script never assumes parameter names or units. It inventories the archive,
loads metadata tables, and scans measurement CSV files in chunks. Outputs are
small CSV/JSON files that can be reviewed before defining model targets.
"""

from __future__ import annotations

import argparse
import json
import re
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd


def norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(name).strip().lower()).strip("_")


def find_col(columns, candidates):
    normalized = {norm(c): c for c in columns}
    for candidate in candidates:
        if candidate in normalized:
            return normalized[candidate]
    for key, original in normalized.items():
        if any(candidate in key for candidate in candidates):
            return original
    return None


def read_member(zf: zipfile.ZipFile, member: str) -> pd.DataFrame:
    with zf.open(member) as handle:
        return pd.read_csv(handle, low_memory=False, encoding="cp1252")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--chunksize", type=int, default=250_000)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(args.archive) as zf:
        members = [m for m in zf.namelist() if not m.endswith("/")]
        inventory = pd.DataFrame(
            {
                "member": members,
                "compressed_bytes": [zf.getinfo(m).compress_size for m in members],
                "uncompressed_bytes": [zf.getinfo(m).file_size for m in members],
            }
        )
        inventory.to_csv(args.out / "archive_inventory.csv", index=False)

        csv_members = [m for m in members if m.lower().endswith(".csv")]
        metadata_members = [m for m in csv_members if "metadata" in m.lower()]
        for member in metadata_members:
            frame = read_member(zf, member)
            safe_name = Path(member).name
            frame.to_csv(args.out / safe_name, index=False)

        measurement_members = [m for m in csv_members if m not in metadata_members]
        summaries = []
        year_counts = defaultdict(Counter)
        country_counts = defaultdict(Counter)
        unit_counts = defaultdict(Counter)
        station_sets = defaultdict(set)

        for member in measurement_members:
            row_count = 0
            columns_seen = None
            with zf.open(member) as handle:
                for chunk in pd.read_csv(
                    handle,
                    chunksize=args.chunksize,
                    low_memory=False,
                    encoding="cp1252",
                ):
                    row_count += len(chunk)
                    columns_seen = list(chunk.columns)
                    station_col = find_col(chunk.columns, ["station_id", "station", "stationid"])
                    date_col = find_col(chunk.columns, ["sample_date", "date", "datetime", "timestamp"])
                    country_col = find_col(chunk.columns, ["country_name", "country", "country_code"])
                    unit_col = find_col(chunk.columns, ["unit", "measurement_unit"])
                    if station_col:
                        station_sets[member].update(chunk[station_col].dropna().astype(str).unique())
                    if date_col:
                        years = pd.to_datetime(chunk[date_col], errors="coerce").dt.year.dropna().astype(int)
                        year_counts[member].update(years.tolist())
                    if country_col:
                        country_counts[member].update(chunk[country_col].dropna().astype(str).tolist())
                    if unit_col:
                        unit_counts[member].update(chunk[unit_col].dropna().astype(str).tolist())

            summaries.append(
                {
                    "member": member,
                    "rows": row_count,
                    "stations": len(station_sets[member]),
                    "min_year": min(year_counts[member], default=None),
                    "max_year": max(year_counts[member], default=None),
                    "n_years": len(year_counts[member]),
                    "n_countries_in_measurement_file": len(country_counts[member]),
                    "n_units": len(unit_counts[member]),
                    "columns": json.dumps(columns_seen or [], ensure_ascii=False),
                    "top_units": json.dumps(unit_counts[member].most_common(10), ensure_ascii=False),
                }
            )

        summary = pd.DataFrame(summaries).sort_values("rows", ascending=False)
        summary.to_csv(args.out / "measurement_file_summary.csv", index=False)
        with (args.out / "profile_manifest.json").open("w", encoding="utf-8") as handle:
            json.dump(
                {
                    "archive": str(args.archive.resolve()),
                    "files": len(members),
                    "csv_files": len(csv_members),
                    "metadata_files": metadata_members,
                    "measurement_files": len(measurement_members),
                    "total_measurement_rows": int(summary["rows"].sum()) if len(summary) else 0,
                },
                handle,
                ensure_ascii=False,
                indent=2,
            )


if __name__ == "__main__":
    main()
