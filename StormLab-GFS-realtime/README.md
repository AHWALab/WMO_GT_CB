# StormLab-GFS-realtime: Operational Ensemble Precipitation Downscaling for Flash-Flood Forecasting

Runtime-only release of StormLab-GFS, an adaptation of the StormLab
stochastic rainfall generator (Liu, Wright & Lorenz 2024) that downscales
the operational GEFS ensemble (0.25°, 3-hourly) to 50-member, hourly, 0.1°
quantitative precipitation forecasts over small flash-flood-prone domains.


## Inputs

- The latest GEFS ensemble cycle (APCP + PWAT + 850-hPa winds), fetched
  automatically by byte-ranged requests; deterministic GFS is a built-in
  fallback (`--source gfs`/`auto`).
- Fitted parameter files per domain (`params/<domain>/`), distributed
  separately by the authors — see README_OPERATIONS.md.

## Outputs

One netCDF per cycle and domain:
`output/<domain>/qpf_ens_<domain>_<YYYYMMDDHH>.nc` with
`qpf(member, time, lat, lon)` in mm/h — 50 members, hourly valid times
+6…+48 h from the cycle, compressed float32 (~10–30 MB).

## Run (one command)

```bash
conda env create -n stormlab -f environment.yml
conda activate stormlab
export PYTHONPATH=$PWD/src
python scripts/05_run_operational_cycle.py --config config/barbados.yaml --cycle latest
```

## Citation

If you use this software, please cite the underlying method:

> Liu, Y., Wright, D. B., & Lorenz, D. J. (2024). A nonstationary
> stochastic rainfall generator conditioned on global climate models for
> design flood analyses in the Mississippi and other large river basins.
> *Water Resources Research*, 60, e2023WR036826.
> https://doi.org/10.1029/2023WR036826

## Contact

Yagmur Derin, The University of Iowa
(operational deployment & parameter files) — yagderin@gmail.com
