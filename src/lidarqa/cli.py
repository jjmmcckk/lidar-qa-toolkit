"""Command line interface: lidar-qa input.laz --aoi aoi.geojson --out qa_report/"""
from __future__ import annotations

import argparse
import sys

from .core import analyse, gap_polygons, run_checks
from .report import write_report


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="lidar-qa", description="QA a LAS/LAZ point cloud: density, coverage, gaps and classification.")
    p.add_argument("input", help="LAS or LAZ file")
    p.add_argument("--aoi", help="Area of interest (GeoJSON, GeoPackage, Shapefile). Default: the data footprint")
    p.add_argument("--out", default="qa_report", help="Output folder (default: qa_report)")
    p.add_argument("--cell", type=float, default=2.0, help="Grid cell size in metres (default: 2)")
    p.add_argument("--min-density", type=float, default=2.0, help="Target density in points per m² (default: 2)")
    p.add_argument("--pass-pct", type=float, default=95.0, help="Percent of cells that must meet the target (default: 95)")
    p.add_argument("--returns", choices=["first", "all"], default="first", help="Returns counted for density (default: first)")
    p.add_argument("--min-gap", type=float, default=None, help="Smallest gap reported, in m² (default: 4 cells)")
    p.add_argument("--max-noise", type=float, default=1.0, help="Maximum percent of noise points (default: 1)")
    p.add_argument("--crs", help="Override the coordinate system, for files without one (e.g. EPSG:2958)")
    a = p.parse_args(argv)

    result = analyse(a.input, cell_m=a.cell, aoi_path=a.aoi, returns=a.returns, crs_override=a.crs)
    min_gap = a.min_gap if a.min_gap is not None else 4 * a.cell ** 2
    gaps = gap_polygons(result, a.min_density, min_gap)
    checks = run_checks(result, a.min_density, a.pass_pct, a.max_noise, gaps)
    write_report(result, checks, gaps, a.min_density, a.out)

    for c in checks:
        status = "INFO" if c["pass"] is None else ("PASS" if c["pass"] else "FAIL")
        print(f"[{status}] {c['check']}: {c['value']} (target {c['target']})")
    print(f"Report written to {a.out}/report.html")
    failed = any(c["pass"] is False for c in checks)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
