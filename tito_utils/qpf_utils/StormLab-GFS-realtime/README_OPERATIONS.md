# StormLab-GFS — operations guide

Runs the StormLab ensemble QPF downscaling operationally: fetches the latest
GEFS ensemble (or deterministic GFS as fallback) from the NOAA open-data
buckets, and produces a 50-member, hourly, 0.1° quantitative precipitation
forecast netCDF for a configured domain in a few minutes of wall-clock on a single core. 

## Forecast coverage 

Cycles are initialized at 00/06/12/18 UTC and appear on the NOAA bucket
~4–5 h later; the system uses the newest cycle at least 5 h old and produces
leads 6–48 h from that initialization. In wall-clock terms the product
therefore always extends **at least +37 h beyond the current time, and
typically +43 h** (immediately after a new cycle is picked up).

## Install (once)

```bash
cd StormLab-GFS-realtime
conda env create -n stormlab -f environment.yml   # pinned environment
conda activate stormlab
export PYTHONPATH=$PWD/src                        # no package install needed
```

### Parameter files (distributed separately, by email or link)

The fitted parameters are **not** included in this folder; they are
distributed by the authors (Yagmur Derin, yagderin@gmail.com) as a
per-domain set:

- `distribution_params_<domain>.nc` 
- `stormlab_spectra_<domain>.tar.gz` 

## Run (one command)

```bash
python scripts/05_run_operational_cycle.py --config config/barbados.yaml --cycle latest
```

That fetches the latest available GEFS cycle (5 forcing members × 10 noise
seeds by default), runs the downscaling, and writes

```
output/barbados/qpf_ens_barbados_<YYYYMMDDHH>.nc
```
