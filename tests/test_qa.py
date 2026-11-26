"""Tests on synthetic point clouds with known density, holes, classes and units."""
import json

import laspy
import numpy as np
import pyproj
import pytest
from shapely.geometry import box, mapping

from lidarqa import analyse, class_summary, density_stats, gap_polygons, run_checks
from lidarqa.cli import main

UTM17 = "EPSG:2958"        # NAD83(CSRS) / UTM zone 17N, metres
OREGON_FT = "EPSG:2992"    # NAD83 / Oregon GIC Lambert, international feet


def make_cloud(path, crs=UTM17, size_m=100.0, density=4.0, hole=None, classes=(2, 5), seed=0, origin=(500000.0, 4850000.0)):
    """Uniform first returns at `density` pts/m2 over a square, with an optional empty hole (metres, local)."""
    rng = np.random.default_rng(seed)
    n = int(size_m * size_m * density)
    x, y = rng.uniform(0, size_m, n), rng.uniform(0, size_m, n)
    if hole is not None:
        hx0, hy0, hx1, hy1 = hole
        keep = ~((x >= hx0) & (x < hx1) & (y >= hy0) & (y < hy1))
        x, y = x[keep], y[keep]
    factor = pyproj.CRS(crs).axis_info[0].unit_conversion_factor   # metres per unit
    x, y = (origin[0] + x) / factor, (origin[1] + y) / factor
    header = laspy.LasHeader(point_format=3, version="1.2")
    header.scales = [0.001, 0.001, 0.001]
    header.offsets = [x.min(), y.min(), 0]
    header.add_crs(pyproj.CRS(crs))
    las = laspy.LasData(header)
    las.x, las.y, las.z = x, y, rng.uniform(100, 110, x.size)
    las.return_number = np.ones(x.size, dtype=np.uint8)
    las.number_of_returns = np.ones(x.size, dtype=np.uint8)
    las.classification = rng.choice(np.array(classes, dtype=np.uint8), x.size)
    las.write(path)
    return x.size


def test_density_matches_known_value(tmp_path):
    f = tmp_path / "uniform.las"
    make_cloud(f, density=4.0)
    r = analyse(str(f), cell_m=5.0)
    stats = density_stats(r)
    assert stats["median"] == pytest.approx(4.0, rel=0.1)


def test_hole_is_reported_as_no_data_gap(tmp_path):
    f = tmp_path / "hole.las"
    make_cloud(f, density=4.0, hole=(40, 40, 60, 60))      # 20 m x 20 m void = 400 m2
    r = analyse(str(f), cell_m=2.0)
    gaps = gap_polygons(r, min_density=2.0, min_gap_m2=16)
    no_data = [g for g in gaps if g[2]]
    assert len(no_data) == 1
    assert no_data[0][1] == pytest.approx(400, rel=0.25)


def test_feet_crs_gives_density_in_square_metres(tmp_path):
    f = tmp_path / "feet.las"
    make_cloud(f, crs=OREGON_FT, density=4.0, origin=(400000.0, 300000.0))
    r = analyse(str(f), cell_m=5.0)
    assert r.unit_name == "foot"
    assert density_stats(r)["median"] == pytest.approx(4.0, rel=0.1)


def test_aoi_beyond_coverage_fails_coverage_check(tmp_path):
    f = tmp_path / "cloud.las"
    make_cloud(f, density=4.0)
    # AOI extends 50 m east of the flown area, in the cloud CRS, written as GeoPackage
    import geopandas as gpd
    aoi = gpd.GeoDataFrame(geometry=[box(500000, 4850000, 500150, 4850100)], crs=UTM17)
    aoi_path = tmp_path / "aoi.gpkg"
    aoi.to_file(aoi_path)
    r = analyse(str(f), cell_m=5.0, aoi_path=str(aoi_path))
    gaps = gap_polygons(r, 2.0, 100)
    checks = {c["check"]: c for c in run_checks(r, 2.0, 95, 1.0, gaps)}
    assert checks["Analysis area with any data"]["pass"] is False
    assert sum(a for _, a, nd in gaps if nd) == pytest.approx(50 * 100, rel=0.15)


def test_class_summary_and_noise_check(tmp_path):
    f = tmp_path / "noisy.las"
    make_cloud(f, density=2.0, classes=(2, 2, 2, 7))       # about 25% low noise
    r = analyse(str(f), cell_m=5.0)
    rows = class_summary(r)
    assert sum(pct for *_, pct in rows) == pytest.approx(100)
    checks = {c["check"]: c for c in run_checks(r, 1.0, 95, 1.0, [])}
    assert checks["Noise points (classes 7 and 18)"]["pass"] is False


def test_cli_writes_all_outputs(tmp_path):
    f = tmp_path / "cli.las"
    make_cloud(f, density=4.0, hole=(10, 10, 30, 30))
    out = tmp_path / "report"
    code = main([str(f), "--out", str(out), "--cell", "2", "--min-density", "2"])
    for name in ["report.html", "density.tif", "density.png", "gaps.geojson", "class_summary.csv", "summary.json"]:
        assert (out / name).exists(), name
    summary = json.loads((out / "summary.json").read_text())
    assert summary["point_count"] > 0
    assert code == 1    # the hole makes the coverage check fail
