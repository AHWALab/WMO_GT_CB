#!/usr/bin/env python3
import argparse
import re
import warnings
from pathlib import Path

import numpy as np
import rasterio
from rasterio.profiles import DefaultGTiffProfile

STATS = {
    "mean": lambda a: np.nanmean(a, axis=0),
    "max": lambda a: np.nanmax(a, axis=0),
    "median": lambda a: np.nanmedian(a, axis=0),
}

NODATA = -9999.0


def find_timesteps(member_dirs, region, subdir, ext="tif", only=None):
    ts = set()
    for d in member_dirs:
        ts_dir = d / region / subdir
        if not ts_dir.is_dir():
            continue
        for child in sorted(ts_dir.iterdir()):
            if not child.is_dir():
                continue
            has_file = any(child.glob(f"*.{ext}"))
            if has_file:
                ts.add(child.name)
    ts = sorted(ts)
    if only:
        ts = [t for t in ts if t in only]
    return ts


def read_member(d, region, subdir, timestep, var, ext):
    f = d / region / subdir / timestep / f"{var}.{timestep}.{ext}"
    if not f.is_file():
        return None, None
    with rasterio.open(f) as ds:
        nodata_val = ds.nodata if ds.nodata is not None else NODATA
        a = ds.read(1).astype(np.float32)
        profile = ds.profile
    a = np.where(a <= nodata_val + 1.0, np.nan, a)
    return a, profile


def write_tif(arr, profile, out_path):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    prof = DefaultGTiffProfile()
    prof.update(
        driver="GTiff",
        width=profile["width"],
        height=profile["height"],
        count=1,
        dtype="float32",
        crs=profile["crs"],
        transform=profile["transform"],
        nodata=NODATA,
    )
    out = np.where(np.isnan(arr), NODATA, arr).astype(np.float32)
    with rasterio.open(out_path, "w", **prof) as ds:
        ds.write(out, 1)


def main():
    parser = argparse.ArgumentParser(
        description="Pixel-wise ensemble statistics (mean/max/median) over CREST geotiff outputs."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(
            "/Dedicated/Humberto/WMO_Caribbean_Comoros/TITO/TITO_Stream_Sat_test_env/outputs/stream_sat"
        ),
    )
    parser.add_argument("--region", default="guatemala")
    parser.add_argument("--subdir", default="tmp_output_crest_streamsat")
    parser.add_argument("--ens-glob", default="ensOut*")
    parser.add_argument(
        "--group-regex",
        default=None,
        help="Optional regex with one capture group to reduce sub-ensembles first, "
        "e.g. '^(ensOut\\d+)_sl\\d+$' for stormlab nested ensembles.",
    )
    parser.add_argument("--vars", nargs="+", default=["maxq", "maxunitq", "qpeaccum"])
    parser.add_argument("--stats", nargs="+", choices=list(STATS), default=["mean", "max", "median"])
    parser.add_argument("--ext", default="tif")
    parser.add_argument("--timestep", nargs="+", default=None)
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output directory (default: <outputs>/ensemble_stats/<level>).",
    )
    args = parser.parse_args()

    root = args.root.resolve()
    member_dirs = sorted(d for d in root.glob(args.ens_glob) if d.is_dir())
    if not member_dirs:
        parser.error(f"no ensemble directories matching '{args.ens_glob}' under {root}")

    group_re = re.compile(args.group_regex) if args.group_regex else None
    groups = None
    if group_re:
        groups = {}
        for d in member_dirs:
            m = group_re.match(d.name)
            if m:
                groups.setdefault(m.group(1), []).append(d)
        if not groups:
            parser.error(f"group regex matched no member directories under {root}")
        print(f"grouped {len(member_dirs)} members into {len(groups)} groups")
    else:
        groups = {d.name: [d] for d in member_dirs}

    only = set(args.timestep) if args.timestep else None
    timesteps = find_timesteps(member_dirs, args.region, args.subdir, args.ext, only)
    if not timesteps:
        parser.error(f"no timesteps found under {root}/{args.region}/{args.subdir}")
    print(f"{len(timesteps)} timesteps: {timesteps}")

    out_root = args.out
    if out_root is None:
        level = root.name
        out_root = root.parent / "ensemble_stats" / level
    out_root = out_root.resolve()
    print(f"output dir: {out_root}")

    for timestep in timesteps:
        for var in args.vars:
            fname = f"{var}.{timestep}.{args.ext}"
            member_arrays = []
            profile = None
            for group_name, members in groups.items():
                arrays = []
                for d in members:
                    a, prof = read_member(d, args.region, args.subdir, timestep, var, args.ext)
                    if a is None:
                        print(f"  warning: missing {d.name}/{timestep}/{fname}")
                        continue
                    arrays.append(a)
                    if profile is None:
                        profile = prof
                if not arrays:
                    print(f"  warning: no members for group {group_name} at {timestep}/{var}")
                    continue
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", RuntimeWarning)
                    if len(arrays) == 1:
                        member_arrays.append(arrays[0])
                    else:
                        member_arrays.append(np.nanmean(np.stack(arrays, axis=0), axis=0))
            if not member_arrays or profile is None:
                print(f"  skip {timestep} {var}: no data from any ensemble member")
                continue
            stack = np.stack(member_arrays, axis=0)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                for stat in args.stats:
                    result = STATS[stat](stack).astype(np.float32)
                    out_file = out_root / f"{var}.{timestep}.ens_{stat}.{args.ext}"
                    write_tif(result, profile, out_file)
                    print(f"  wrote {out_file} (n={stack.shape[0]})")
            del stack


if __name__ == "__main__":
    main()
