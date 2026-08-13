"""The lookup table: one row per pre-simulated RainyDay storm.

Layout of a catalog folder (one per region, versioned):

    fim_catalog/{Region}/v1/
        index.csv                  storm_id, magnitude_mm[, direction_deg],
                                   depth_path, extent_path
        magnitudes_by_aoc.csv      optional: aoc_id, storm_id, magnitude_mm
                                   (per-AOC magnitudes override index.csv)
        maps/     {storm}_depth.tif    max depth from the hydraulic run
        extents/  {storm}_extent.tif   max extent (0/1), derived from depth
                                       if the hydraulic run has no extent file
        meta.json

Directions can be given in degrees or as compass points (N, NE, ...).
"""

import glob
import json
import os
from dataclasses import dataclass

import numpy as np
import pandas as pd

COMPASS = {"N": 0.0, "NNE": 22.5, "NE": 45.0, "ENE": 67.5, "E": 90.0,
           "ESE": 112.5, "SE": 135.0, "SSE": 157.5, "S": 180.0,
           "SSW": 202.5, "SW": 225.0, "WSW": 247.5, "W": 270.0,
           "WNW": 292.5, "NW": 315.0, "NNW": 337.5}


def direction_to_deg(value):
    if value is None or (isinstance(value, float) and value != value):
        return float("nan")
    if isinstance(value, (int, float)):
        return float(value) % 360.0
    text = str(value).strip().upper()
    if text in COMPASS:
        return COMPASS[text]
    try:
        return float(text) % 360.0
    except ValueError:
        return float("nan")


def circular_diff_deg(a: float, b: float) -> float:
    if a != a or b != b:
        return float("nan")
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


@dataclass
class Catalog:
    folder: str
    index: pd.DataFrame                 # storm_id, magnitude_mm, direction_deg, depth_path, extent_path
    by_aoc: pd.DataFrame = None         # aoc_id, storm_id, magnitude_mm (optional)
    meta: dict = None

    # ---- access ------------------------------------------------------------

    def magnitudes(self, aoc_id: str = None) -> pd.DataFrame:
        """Table of storm_id, magnitude_mm, direction_deg for one AOC.

        Per-AOC magnitudes are used when available, else the domain values.
        """
        base = self.index[["storm_id", "magnitude_mm", "direction_deg"]].copy()
        if aoc_id is not None and self.by_aoc is not None and len(self.by_aoc):
            sub = self.by_aoc[self.by_aoc["aoc_id"].astype(str) == str(aoc_id)]
            if len(sub):
                merged = base.drop(columns=["magnitude_mm"]).merge(
                    sub[["storm_id", "magnitude_mm"]], on="storm_id", how="inner")
                return merged
        return base

    def storm_row(self, storm_id: str) -> dict:
        row = self.index[self.index["storm_id"].astype(str) == str(storm_id)]
        if not len(row):
            raise KeyError(f"Storm {storm_id} not in catalog")
        return row.iloc[0].to_dict()

    def storm_files(self, storm_id: str) -> dict:
        row = self.storm_row(storm_id)
        return {
            "depth": os.path.join(self.folder, row["depth_path"]),
            "extent": os.path.join(self.folder, row["extent_path"]) if row.get("extent_path") else "",
        }

    # ---- IO ------------------------------------------------------------------

    @classmethod
    def load(cls, folder: str) -> "Catalog":
        index_path = os.path.join(folder, "index.csv")
        if not os.path.isfile(index_path):
            raise FileNotFoundError(f"Catalog index not found: {index_path}")
        index = pd.read_csv(index_path)
        required = {"storm_id", "magnitude_mm", "depth_path"}
        missing = required - set(index.columns)
        if missing:
            raise ValueError(f"index.csv missing columns: {sorted(missing)}")
        if "extent_path" not in index.columns:
            index["extent_path"] = ""
        if "direction_deg" not in index.columns:
            source_col = "direction" if "direction" in index.columns else None
            index["direction_deg"] = (index[source_col].map(direction_to_deg)
                                      if source_col else float("nan"))
        index["storm_id"] = index["storm_id"].astype(str)

        by_aoc_path = os.path.join(folder, "magnitudes_by_aoc.csv")
        by_aoc = pd.read_csv(by_aoc_path) if os.path.isfile(by_aoc_path) else None
        if by_aoc is not None:
            by_aoc["storm_id"] = by_aoc["storm_id"].astype(str)

        meta_path = os.path.join(folder, "meta.json")
        meta = json.load(open(meta_path)) if os.path.isfile(meta_path) else {}
        return cls(folder=folder, index=index, by_aoc=by_aoc, meta=meta)


# ---- offline build ----------------------------------------------------------

def build_catalog(storm_meta_csv: str, maps_dir: str, out_dir: str,
                  aocs=None, rain_dir: str = None,
                  depth_pattern: str = "{storm_id}*depth*.tif",
                  rain_pattern: str = "{storm_id}*.tif",
                  extent_pattern: str = "{storm_id}*extent*.tif",
                  extent_from_depth_m: float = 0.05,
                  rain_stat: str = "mean") -> str:
    """Build a catalog folder from RainyDay + hydraulic model outputs.

    storm_meta_csv columns (flexible):
        storm_id                 required
        magnitude_mm             optional if rain_dir given (computed then)
        direction / direction_deg  optional
    maps_dir : folder with the hydraulic max-depth rasters
    rain_dir : optional folder with per-storm 24-h rainfall rasters, used to
               compute domain and per-AOC magnitudes with the same zonal code
               the runtime uses
    Extent rasters are derived from depth >= extent_from_depth_m when no
    extent file matches extent_pattern.

    Returns out_dir.
    """
    import rasterio
    from .io_utils import read_grid

    meta_df = pd.read_csv(storm_meta_csv)
    if "storm_id" not in meta_df.columns:
        raise ValueError("storm_meta_csv must have a storm_id column")
    meta_df["storm_id"] = meta_df["storm_id"].astype(str)

    os.makedirs(out_dir, exist_ok=True)
    maps_out = os.path.join(out_dir, "maps")
    ext_out = os.path.join(out_dir, "extents")
    os.makedirs(maps_out, exist_ok=True)
    os.makedirs(ext_out, exist_ok=True)

    rows, by_aoc_rows, problems = [], [], []

    for _, meta_row in meta_df.iterrows():
        storm = str(meta_row["storm_id"])

        depth_matches = sorted(glob.glob(os.path.join(maps_dir, depth_pattern.format(storm_id=storm))))
        if not depth_matches:
            problems.append(f"{storm}: no depth raster matching "
                            f"{depth_pattern.format(storm_id=storm)}")
            continue
        depth_src = depth_matches[0]
        depth_rel = os.path.join("maps", os.path.basename(depth_src))
        _link_or_copy(depth_src, os.path.join(out_dir, depth_rel))

        # Extent: existing file or derived from depth
        extent_rel = ""
        extent_matches = sorted(glob.glob(os.path.join(maps_dir, extent_pattern.format(storm_id=storm))))
        if extent_matches and extent_matches[0] != depth_src:
            extent_rel = os.path.join("extents", os.path.basename(extent_matches[0]))
            _link_or_copy(extent_matches[0], os.path.join(out_dir, extent_rel))
        else:
            extent_rel = os.path.join("extents", f"{storm}_extent.tif")
            _derive_extent(depth_src, os.path.join(out_dir, extent_rel), extent_from_depth_m)

        # Magnitude: from metadata or from the storm rainfall raster
        magnitude = meta_row.get("magnitude_mm", float("nan"))
        rain_grid = None
        if rain_dir:
            rain_matches = sorted(glob.glob(os.path.join(rain_dir, rain_pattern.format(storm_id=storm))))
            if rain_matches:
                rain_grid = read_grid(rain_matches[0])
                import numpy as _np
                domain_mag = float(_np.nanmean(rain_grid.data))
                if magnitude != magnitude:
                    magnitude = domain_mag
                if aocs:
                    for aoc in aocs:
                        by_aoc_rows.append({
                            "aoc_id": aoc.aoc_id, "storm_id": storm,
                            "magnitude_mm": round(aoc.zonal(rain_grid, rain_stat), 2),
                        })
            else:
                problems.append(f"{storm}: no rainfall raster matching "
                                f"{rain_pattern.format(storm_id=storm)}")
        if magnitude != magnitude:
            problems.append(f"{storm}: magnitude_mm missing and not computable")
            continue

        direction_raw = meta_row.get("direction_deg", meta_row.get("direction", float("nan")))
        rows.append({
            "storm_id": storm,
            "magnitude_mm": round(float(magnitude), 2),
            "direction_deg": direction_to_deg(direction_raw),
            "depth_path": depth_rel.replace(os.sep, "/"),
            "extent_path": extent_rel.replace(os.sep, "/"),
        })

    if not rows:
        raise RuntimeError("Catalog build produced no rows. Problems: " + "; ".join(problems))

    index = pd.DataFrame(rows).sort_values("magnitude_mm").reset_index(drop=True)
    index.to_csv(os.path.join(out_dir, "index.csv"), index=False)
    if by_aoc_rows:
        pd.DataFrame(by_aoc_rows).to_csv(os.path.join(out_dir, "magnitudes_by_aoc.csv"), index=False)

    meta = {
        "n_storms": int(len(index)),
        "magnitude_min_mm": float(index["magnitude_mm"].min()),
        "magnitude_max_mm": float(index["magnitude_mm"].max()),
        "has_direction": bool(index["direction_deg"].notna().any()),
        "per_aoc_magnitudes": bool(by_aoc_rows),
        "extent_from_depth_m": extent_from_depth_m,
        "problems": problems,
    }
    with open(os.path.join(out_dir, "meta.json"), "w") as fh:
        json.dump(meta, fh, indent=2)

    if problems:
        print("  build_catalog warnings:")
        for p in problems:
            print(f"    - {p}")
    print(f"  Catalog built: {len(index)} storms, "
          f"{meta['magnitude_min_mm']:.0f}-{meta['magnitude_max_mm']:.0f} mm -> {out_dir}")
    return out_dir


def _link_or_copy(src: str, dst: str):
    if os.path.abspath(src) == os.path.abspath(dst) or os.path.exists(dst):
        return
    try:
        os.link(src, dst)
    except OSError:
        import shutil
        shutil.copy2(src, dst)


def _derive_extent(depth_path: str, out_path: str, threshold_m: float):
    import rasterio
    with rasterio.open(depth_path) as src:
        data = src.read(1, masked=True)
        profile = src.profile.copy()
    mask = (np.ma.filled(data, -9999.0) >= threshold_m).astype("uint8")
    profile.update(dtype="uint8", count=1, nodata=0, compress="lzw")
    with rasterio.open(out_path, "w", **profile) as dst:
        dst.write(mask, 1)


# ---- CLI ---------------------------------------------------------------------

def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(
        description="Build a FIM lookup catalog from RainyDay + hydraulic outputs")
    parser.add_argument("--storm-meta", required=True, help="CSV with storm_id[, magnitude_mm, direction]")
    parser.add_argument("--maps-dir", required=True, help="Folder with max-depth rasters")
    parser.add_argument("--out", required=True, help="Output catalog folder (e.g. fim_catalog/Barbados/v1)")
    parser.add_argument("--rain-dir", default=None, help="Optional folder with per-storm rainfall rasters")
    parser.add_argument("--aoc", default=None, help="Optional AOC source for per-AOC magnitudes")
    parser.add_argument("--aoc-id-field", default="")
    parser.add_argument("--depth-pattern", default="{storm_id}*depth*.tif")
    parser.add_argument("--rain-pattern", default="{storm_id}*.tif")
    parser.add_argument("--extent-pattern", default="{storm_id}*extent*.tif")
    parser.add_argument("--extent-from-depth-m", type=float, default=0.05)
    args = parser.parse_args(argv)

    aocs = None
    if args.aoc:
        from .aoc import load_aocs
        aocs = load_aocs(args.aoc, id_field=args.aoc_id_field)

    build_catalog(args.storm_meta, args.maps_dir, args.out,
                  aocs=aocs, rain_dir=args.rain_dir,
                  depth_pattern=args.depth_pattern,
                  rain_pattern=args.rain_pattern,
                  extent_pattern=args.extent_pattern,
                  extent_from_depth_m=args.extent_from_depth_m)


if __name__ == "__main__":
    main()
