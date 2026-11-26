"""Core LiDAR QA computations: density grid, coverage and gaps, classification summary."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import laspy
import numpy as np
import pyproj
from rasterio.features import geometry_mask, shapes
from rasterio.transform import Affine, from_origin
from scipy import ndimage
from shapely.geometry import box, shape
from shapely.ops import unary_union

# ASPRS LAS 1.4 standard point classes
ASPRS_CLASSES = {
    0: "Created, never classified", 1: "Unclassified", 2: "Ground", 3: "Low vegetation",
    4: "Medium vegetation", 5: "High vegetation", 6: "Building", 7: "Low point (noise)",
    8: "Reserved / model key point", 9: "Water", 10: "Rail", 11: "Road surface",
    12: "Reserved / overlap", 13: "Wire guard", 14: "Wire conductor", 15: "Transmission tower",
    16: "Wire connector", 17: "Bridge deck", 18: "High noise",
}
NOISE_CLASSES = {7, 18}


@dataclass
class Grid:
    """Raster grid in the point cloud's CRS units."""
    transform: Affine
    width: int
    height: int
    cell_units: float          # cell size in CRS units
    cell_m: float              # cell size in metres

    @property
    def cell_area_m2(self) -> float:
        return self.cell_m ** 2


@dataclass
class QAResult:
    path: str
    crs: pyproj.CRS | None
    unit_name: str
    metres_per_unit: float
    point_count: int
    bounds: tuple[float, float, float, float]
    z_range: tuple[float, float]
    grid: Grid
    counts: np.ndarray                      # points per cell (density source)
    analysis_mask: np.ndarray               # cells inside the AOI or the data footprint
    density: np.ndarray = field(init=False)  # points per square metre
    class_counts: dict[int, int] = field(default_factory=dict)
    return_counts: dict[int, int] = field(default_factory=dict)
    density_returns: str = "first"
    aoi_source: str = "data footprint"
    aoi_geom: object = None

    def __post_init__(self):
        self.density = self.counts / self.grid.cell_area_m2


def crs_and_units(header: laspy.LasHeader, crs_override: str | None = None):
    """Return (crs, unit name, metres per horizontal unit)."""
    crs = pyproj.CRS.from_user_input(crs_override) if crs_override else header.parse_crs()
    if crs is None:
        return None, "metre (assumed)", 1.0
    axis = crs.axis_info[0]
    return crs, axis.unit_name, float(axis.unit_conversion_factor)


def build_grid(bounds, cell_m: float, metres_per_unit: float) -> Grid:
    minx, miny, maxx, maxy = bounds
    cell = cell_m / metres_per_unit
    x0 = math.floor(minx / cell) * cell
    y1 = math.ceil(maxy / cell) * cell
    width = max(1, math.ceil((maxx - x0) / cell))
    height = max(1, math.ceil((y1 - miny) / cell))
    return Grid(from_origin(x0, y1, cell, cell), width, height, cell, cell_m)


def load_aoi(aoi_path: str, cloud_crs: pyproj.CRS | None):
    """Read an AOI file (any format GeoPandas reads) and return one geometry in the cloud CRS."""
    import geopandas as gpd

    aoi = gpd.read_file(aoi_path)
    if cloud_crs is not None and aoi.crs is not None and aoi.crs != cloud_crs:
        aoi = aoi.to_crs(cloud_crs)
    return unary_union(list(aoi.geometry))


def analyse(path: str, cell_m: float = 2.0, aoi_path: str | None = None, returns: str = "first",
            crs_override: str | None = None, chunk_size: int = 2_000_000) -> QAResult:
    """Stream a LAS/LAZ file in chunks and build the QA grids and summaries."""
    with laspy.open(path) as reader:
        header = reader.header
        crs, unit_name, mpu = crs_and_units(header, crs_override)
        cloud_bounds = (header.mins[0], header.mins[1], header.maxs[0], header.maxs[1])

        aoi_geom = load_aoi(aoi_path, crs) if aoi_path else None
        grid_bounds = cloud_bounds
        if aoi_geom is not None:
            grid_bounds = unary_union([box(*cloud_bounds), aoi_geom]).bounds
        grid = build_grid(grid_bounds, cell_m, mpu)

        counts = np.zeros(grid.height * grid.width, dtype=np.int64)
        class_counts: dict[int, int] = {}
        return_counts: dict[int, int] = {}
        x0, y1 = grid.transform.c, grid.transform.f

        for pts in reader.chunk_iterator(chunk_size):
            x, y = np.asarray(pts.x), np.asarray(pts.y)
            rn = np.asarray(pts.return_number)
            cls = np.asarray(pts.classification)

            for k, v in zip(*np.unique(cls, return_counts=True)):
                class_counts[int(k)] = class_counts.get(int(k), 0) + int(v)
            for k, v in zip(*np.unique(rn, return_counts=True)):
                return_counts[int(k)] = return_counts.get(int(k), 0) + int(v)

            keep = rn == 1 if returns == "first" else np.ones_like(rn, dtype=bool)
            col = np.clip(((x[keep] - x0) / grid.cell_units).astype(np.int64), 0, grid.width - 1)
            row = np.clip(((y1 - y[keep]) / grid.cell_units).astype(np.int64), 0, grid.height - 1)
            counts += np.bincount(row * grid.width + col, minlength=counts.size)

    counts = counts.reshape(grid.height, grid.width)
    if aoi_geom is not None:
        analysis = geometry_mask([aoi_geom], out_shape=(grid.height, grid.width),
                                 transform=grid.transform, invert=True, all_touched=False)
        aoi_source = aoi_path
    else:
        # No AOI: analyse the data footprint, with internal voids filled so they count as gaps
        analysis = ndimage.binary_fill_holes(counts > 0)
        aoi_source = "data footprint (internal voids count as gaps)"

    return QAResult(path=path, crs=crs, unit_name=unit_name, metres_per_unit=mpu,
                    point_count=int(header.point_count), bounds=cloud_bounds,
                    z_range=(float(header.mins[2]), float(header.maxs[2])), grid=grid,
                    counts=counts, analysis_mask=analysis, class_counts=class_counts,
                    return_counts=return_counts, density_returns=returns, aoi_source=aoi_source,
                    aoi_geom=aoi_geom)


def gap_polygons(result: QAResult, min_density: float, min_gap_m2: float):
    """Areas inside the analysis area that miss the density target, as polygons filtered by area.
    Voids (no points at all) and low density areas (some points, below target) are kept separate.
    Returns a list of (shapely geometry in cloud CRS, area in m2, is_void), largest first."""
    inside = result.analysis_mask
    masks = [(inside & (result.counts == 0), True),
             (inside & (result.counts > 0) & (result.density < min_density), False)]
    gaps = []
    for mask, is_void in masks:
        for geom, _ in shapes(mask.astype(np.uint8), mask=mask, transform=result.grid.transform):
            poly = shape(geom)
            area_m2 = poly.area * result.metres_per_unit ** 2
            if area_m2 >= min_gap_m2:
                gaps.append((poly, area_m2, is_void))
    return sorted(gaps, key=lambda g: -g[1])


def class_summary(result: QAResult):
    total = sum(result.class_counts.values()) or 1
    return [(c, ASPRS_CLASSES.get(c, "User defined"), n, 100 * n / total)
            for c, n in sorted(result.class_counts.items())]


def density_stats(result: QAResult):
    d = result.density[result.analysis_mask]
    if d.size == 0:
        return {}
    return {
        "mean": float(d.mean()), "median": float(np.median(d)),
        "p5": float(np.percentile(d, 5)), "p95": float(np.percentile(d, 95)),
        "cells": int(d.size), "empty_cells": int((d == 0).sum()),
    }


def run_checks(result: QAResult, min_density: float, pass_pct: float, max_noise_pct: float,
               gaps) -> list[dict]:
    """Pass or fail checks against a simple acquisition specification."""
    d = result.density[result.analysis_mask]
    meeting = 100 * float((d >= min_density).mean()) if d.size else 0.0
    covered = 100 * float((d > 0).mean()) if d.size else 0.0
    total = sum(result.class_counts.values()) or 1
    noise = 100 * sum(n for c, n in result.class_counts.items() if c in NOISE_CLASSES) / total
    classified = any(c >= 2 for c in result.class_counts)
    gap_area = sum(a for _, a, _ in gaps)
    return [
        {"check": "Coordinate system defined", "value": result.crs.name if result.crs else "missing",
         "target": "present", "pass": result.crs is not None},
        {"check": f"Cells meeting {min_density:g} pts/m² ({result.density_returns} returns)",
         "value": f"{meeting:.1f}%", "target": f"≥ {pass_pct:g}%", "pass": meeting >= pass_pct},
        {"check": "Analysis area with any data", "value": f"{covered:.1f}%", "target": "100%",
         "pass": covered >= 99.999},
        {"check": "Void and low density areas", "value": f"{gap_area:,.0f} m² in {len(gaps)} polygons",
         "target": "reported", "pass": None},
        {"check": "Classified (any class ≥ 2)", "value": "yes" if classified else "no",
         "target": "yes", "pass": classified},
        {"check": "Noise points (classes 7 and 18)", "value": f"{noise:.2f}%",
         "target": f"≤ {max_noise_pct:g}%", "pass": noise <= max_noise_pct},
    ]
