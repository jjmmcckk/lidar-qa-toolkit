"""Write QA outputs: density GeoTIFF, gap polygons, CSV and JSON summaries, and an HTML report."""
from __future__ import annotations

import base64
import csv
import html
import io
import json
import os
from datetime import datetime, timezone

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import rasterio  # noqa: E402
from matplotlib.colors import ListedColormap  # noqa: E402

from .core import QAResult, class_summary, density_stats  # noqa: E402


def write_density_tif(result: QAResult, path: str):
    profile = dict(driver="GTiff", width=result.grid.width, height=result.grid.height, count=1,
                   dtype="float32", transform=result.grid.transform, compress="deflate",
                   crs=result.crs.to_wkt() if result.crs else None, nodata=-1.0)
    data = np.where(result.analysis_mask | (result.counts > 0), result.density, -1.0).astype("float32")
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data, 1)
        dst.update_tags(1, units="points per square metre", returns=result.density_returns)


def write_gaps(result: QAResult, gaps, path: str):
    """Write gap polygons as GeoJSON in WGS 84 (or in the source CRS if it is unknown)."""
    import geopandas as gpd

    gdf = gpd.GeoDataFrame(
        {"area_m2": [round(a, 1) for _, a, _ in gaps], "type": ["void" if v else "low density" for _, _, v in gaps]},
        geometry=[g for g, _, _ in gaps], crs=result.crs)
    if result.crs is not None and len(gdf):
        gdf = gdf.to_crs(4326)
    gdf.to_file(path, driver="GeoJSON")


def write_class_csv(result: QAResult, path: str):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["class", "name", "points", "percent"])
        for c, name, n, pct in class_summary(result):
            w.writerow([c, name, n, f"{pct:.2f}"])


def _png(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def density_figure(result: QAResult, min_density: float, gaps, png_path: str | None = None) -> str:
    g = result.grid
    extent = (g.transform.c, g.transform.c + g.width * g.cell_units,
              g.transform.f - g.height * g.cell_units, g.transform.f)
    fig, (ax, axh) = plt.subplots(1, 2, figsize=(12, 5), gridspec_kw={"width_ratios": [1.6, 1]})

    data_or_aoi = result.analysis_mask | (result.counts > 0)
    shown = np.ma.masked_where(~data_or_aoi | (result.counts == 0), result.density)
    vmax = max(min_density * 2, float(np.percentile(result.density[result.counts > 0], 98))) \
        if (result.counts > 0).any() else min_density * 2
    im = ax.imshow(shown, extent=extent, cmap="viridis", vmin=0, vmax=vmax, interpolation="nearest")
    low = result.analysis_mask & (result.counts > 0) & (result.density < min_density)
    void = result.analysis_mask & (result.counts == 0)
    ax.imshow(np.ma.masked_where(~low, np.ones_like(result.density)), extent=extent,
              cmap=ListedColormap(["#e4572e"]), interpolation="nearest")
    ax.imshow(np.ma.masked_where(~void, np.ones_like(result.density)), extent=extent,
              cmap=ListedColormap(["#4a4a4a"]), interpolation="nearest")

    # Outline of the analysis area: the AOI polygon, or the data footprint
    if result.aoi_geom is not None:
        geoms = getattr(result.aoi_geom, "geoms", [result.aoi_geom])
        for gm in geoms:
            xs, ys = gm.exterior.xy
            ax.plot(xs, ys, color="#00b4d8", linewidth=1.6)
    else:
        xc = g.transform.c + (np.arange(g.width) + 0.5) * g.cell_units
        yc = g.transform.f - (np.arange(g.height) + 0.5) * g.cell_units
        ax.contour(xc, yc, result.analysis_mask.astype(float), levels=[0.5], colors="#00b4d8", linewidths=1.2)
    ax.set_title(f"Density ({result.density_returns} returns): red below {min_density:g} pts/m², grey void,\n"
                 f"blue line = {'AOI' if result.aoi_geom is not None else 'data footprint'}", fontsize=10)
    ax.set_xlabel(f"Easting ({result.unit_name})"); ax.set_ylabel(f"Northing ({result.unit_name})")
    ax.ticklabel_format(useOffset=False, style="plain")
    plt.setp(ax.get_xticklabels(), rotation=30, ha="right")
    fig.colorbar(im, ax=ax, label="points per m²", shrink=0.85)

    d = result.density[result.analysis_mask]
    axh.hist(d, bins=40, color="#3a6ea5")
    axh.axvline(min_density, color="#e4572e", linestyle="--", label=f"target {min_density:g} pts/m²")
    axh.set_xlabel("points per m² (per cell)"); axh.set_ylabel("cells")
    axh.set_title("Density distribution in the analysis area"); axh.legend()
    fig.tight_layout()
    if png_path:
        fig.savefig(png_path, dpi=150, bbox_inches="tight")
    return _png(fig)


def write_report(result: QAResult, checks, gaps, min_density: float, out_dir: str) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    write_density_tif(result, os.path.join(out_dir, "density.tif"))
    write_gaps(result, gaps, os.path.join(out_dir, "gaps.geojson"))
    write_class_csv(result, os.path.join(out_dir, "class_summary.csv"))
    img = density_figure(result, min_density, gaps, os.path.join(out_dir, "density.png"))
    stats = density_stats(result)

    summary = {
        "file": os.path.basename(result.path),
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "crs": result.crs.name if result.crs else None,
        "horizontal_unit": result.unit_name,
        "point_count": result.point_count,
        "z_range": result.z_range,
        "cell_size_m": result.grid.cell_m,
        "analysis_area": result.aoi_source,
        "density_returns": result.density_returns,
        "density_stats_pts_per_m2": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in stats.items()},
        "checks": checks,
        "gaps": [{"area_m2": round(a, 1), "type": "void" if v else "low density"} for _, a, v in gaps],
        "classes": [{"class": c, "name": n, "points": p, "percent": round(pct, 2)}
                    for c, n, p, pct in class_summary(result)],
        "returns": result.return_counts,
    }
    with open(os.path.join(out_dir, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2, default=str)

    def badge(p):
        if p is None:
            return '<span class="info">INFO</span>'
        return '<span class="pass">PASS</span>' if p else '<span class="fail">FAIL</span>'

    rows = "".join(f"<tr><td>{html.escape(c['check'])}</td><td>{html.escape(str(c['value']))}</td>"
                   f"<td>{html.escape(c['target'])}</td><td>{badge(c['pass'])}</td></tr>" for c in checks)
    cls_rows = "".join(f"<tr><td>{c}</td><td>{html.escape(n)}</td><td>{p:,}</td><td>{pct:.2f}%</td></tr>"
                       for c, n, p, pct in class_summary(result))
    ret_rows = "".join(f"<tr><td>{k}</td><td>{v:,}</td></tr>" for k, v in sorted(result.return_counts.items()))
    gap_rows = "".join(f"<tr><td>{i + 1}</td><td>{a:,.0f}</td><td>{'void (no points)' if nd else 'low density'}</td></tr>"
                       for i, (_, a, nd) in enumerate(gaps[:15]))
    overall = all(c["pass"] for c in checks if c["pass"] is not None)
    st = summary["density_stats_pts_per_m2"]

    page = f"""<!doctype html><html><head><meta charset="utf-8"><title>LiDAR QA report: {html.escape(summary['file'])}</title>
<style>
body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;max-width:1100px;margin:24px auto;padding:0 16px;color:#1d2430}}
h1{{font-size:22px;margin-bottom:4px}} h2{{font-size:17px;margin-top:28px;border-bottom:1px solid #ddd;padding-bottom:4px}}
table{{border-collapse:collapse;width:100%;font-size:14px}} td,th{{border:1px solid #ddd;padding:6px 8px;text-align:left}}
th{{background:#f3f5f8}} .pass{{color:#fff;background:#2e7d32;padding:2px 8px;border-radius:4px}}
.fail{{color:#fff;background:#c62828;padding:2px 8px;border-radius:4px}} .info{{color:#fff;background:#546e7a;padding:2px 8px;border-radius:4px}}
.meta{{color:#555;font-size:14px}} img{{max-width:100%}} .grid{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}
@media (max-width:700px){{.grid{{grid-template-columns:1fr}}}}
</style></head><body>
<h1>LiDAR QA report: {html.escape(summary['file'])} {badge(overall)}</h1>
<p class="meta">Generated {summary['generated_utc']} UTC · {result.point_count:,} points · CRS: {html.escape(str(summary['crs']))}
 ({html.escape(result.unit_name)}) · cell size {result.grid.cell_m:g} m · analysis area: {html.escape(str(result.aoi_source))}</p>
<h2>Checks</h2><table><tr><th>Check</th><th>Result</th><th>Target</th><th>Status</th></tr>{rows}</table>
<h2>Density</h2>
<p>Mean {st.get('mean', 0)} · median {st.get('median', 0)} · 5th percentile {st.get('p5', 0)} · 95th percentile {st.get('p95', 0)} pts/m²
 across {st.get('cells', 0):,} cells ({st.get('empty_cells', 0):,} empty).</p>
<img src="data:image/png;base64,{img}" alt="Density map and histogram">
<div class="grid"><div><h2>Void and low density areas (largest 15)</h2>
<table><tr><th>#</th><th>Area (m²)</th><th>Type</th></tr>{gap_rows or '<tr><td colspan=3>None</td></tr>'}</table></div>
<div><h2>Returns</h2><table><tr><th>Return number</th><th>Points</th></tr>{ret_rows}</table></div></div>
<h2>Classification (ASPRS)</h2><table><tr><th>Class</th><th>Name</th><th>Points</th><th>Share</th></tr>{cls_rows}</table>
<p class="meta">Outputs: density.tif (pts/m² GeoTIFF), gaps.geojson (WGS 84), class_summary.csv, summary.json.</p>
</body></html>"""
    with open(os.path.join(out_dir, "report.html"), "w") as f:
        f.write(page)
    return summary
