#!/usr/bin/env python3
"""Run one operational cycle: fetch forcing, generate ensemble QPF netCDF.

Default source is the operational GEFS ensemble (N forcing members x M noise
seeds, combined into one output file). The deterministic GFS path is kept as
a fallback: --source gfs, or --source auto to try GEFS and fall back to GFS
if the ensemble fetch fails (e.g. cycle not yet on the bucket).

Author:  Dr. Yagmur Derin, UI, 07/2026
"""
import argparse
import time

import pandas as pd

from stormlab_gfs import load_config
from stormlab_gfs.data import gefs_operational, gfs
from stormlab_gfs.operational import run_cycle

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True)
    p.add_argument("--cycle", default="latest", help='"latest" or e.g. 2026071300')
    p.add_argument("--source", default="gefs", choices=["gefs", "gfs", "auto"])
    p.add_argument("--members", type=int, default=None,
                   help="total ensemble size (default: forecast.n_members)")
    p.add_argument("--forcing-members", type=int, default=None,
                   help="GEFS forcing members (default: "
                        "forecast.operational_forcing_members, 5)")
    args = p.parse_args()

    cfg = load_config(args.config)
    t0 = time.time()
    cyc = (gefs_operational.latest_cycle() if args.cycle == "latest"
           else pd.Timestamp(f"{args.cycle[:8]}T{args.cycle[8:]}"))
    n_total = args.members or cfg.forecast["n_members"]
    n_forcing = args.forcing_members or int(
        cfg.forecast.get("operational_forcing_members", 5))

    src = args.source
    if src in ("gefs", "auto"):
        try:
            print(f"GEFS ensemble cycle {cyc:%Y-%m-%d %HZ} "
                  f"({n_forcing} members x seeds -> {n_total} total) ...")
            gefs_operational.run_ensemble_cycle(cfg, cyc, n_forcing, n_total)
            print(f"total wall-clock (fetch + simulate + write): "
                  f"{time.time() - t0:.0f} s")
            raise SystemExit(0)
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001
            if src == "gefs":
                raise
            print(f"GEFS path failed ({exc}); falling back to GFS")

    cyc = gfs.latest_cycle() if args.cycle == "latest" else cyc
    print(f"fetching GFS cycle {cyc:%Y-%m-%d %HZ} ...")
    ds = gfs.fetch_cycle(cfg, cyc)
    run_cycle(cfg, ds, n_members=n_total)
    print(f"total wall-clock (fetch + simulate + write): {time.time() - t0:.0f} s")
