# fim_utils : scenario-library flood inundation mapping for TITO

Pre-simulated flood maps (RainyDay storms run through the hydraulic model)
become an operational lookup table. In real time:

1. **Trigger.** EF5 maximum unit streamflow (`maxunitq`) is checked inside
   each Area of Concern (pilot basin or administrative unit). Any pixel at or
   above the threshold (default 1.0 m3/s/km2) starts the procedure.
2. **Rainfall total.** Per ensemble member, the event magnitude is
   `mean(qpeaccum) + mean(qpfaccum)` over the Area of Concern. Both grids sit
   next to the UQ grid in every EF5 run folder, so the totals are exactly the
   rain the model saw, whatever QPE/QPF combination is running.
3. **Match.** The total is matched to the closest pre-simulated storm in the
   catalog, with loose bands that widen until they find a match, always
   rounding up. Direction can be enabled as a secondary condition.
4. **Display.** The matched storm's **max depth** and **max extent** are
   exported per Area of Concern for the best-estimate (median member) and
   upper (90th percentile member) scenarios, together with a full decision
   log (`fim_summary.json`).

This follows the pre-simulated-scenarios approach used operationally for
surface water flood forecasting in Scotland (Speight et al., 2018, J Flood
Risk Management, doi:10.1111/jfr3.12281), with EF5 in the role of
Grid-to-Grid and the RainyDay catalog in the role of the pluvial hazard map
library, and the offline forecast databases of Bhola et al. (2018,
Geosciences 8:346).

## Layout

```
tito_utils/fim_utils/
    config.py      YAML config (one file per region)
    aoc.py         Areas of Concern: geojson/shp/gpkg polygons or mask tifs
    ef5_runs.py    discovers EF5 run folders; every run = one member
    trigger.py     UQ threshold check, windowed reads, any-pixel rule
    rainfall.py    per-AOC per-member totals from qpeaccum + qpfaccum
    catalog.py     lookup table build + load (index.csv, maps/, extents/)
    matching.py    the widening-band, round-up matching rules
    products.py    export depth + extent, tables, decision log
    pipeline.py    one call per cycle; also a CLI
```

## Run it

```bash
# offline, once per region: build the lookup table
python -m tito_utils.fim_utils.catalog \
    --storm-meta rainday_meta.csv --maps-dir hydraulic_maps/ \
    --rain-dir storm_rain24h/ --aoc aoc/Barbados_parishes.geojson \
    --aoc-id-field shapeName --out fim_catalog/Barbados/v1

# each cycle (TITO task or manual):
python -m tito_utils.fim_utils.pipeline --config fim_config/Barbados.yaml
python -m tito_utils.fim_utils.pipeline --config ... --cycle 20240704.090000
```

From the orchestrator, after the EF5 runs of a cycle finish:

```python
from tito_utils.fim_utils import load_config, run_fim_cycle
summary = run_fim_cycle(load_config(f"fim_config/{region}.yaml"),
                        cycle=output_timestamp_str)
```

Exit codes for the CLI: 0 ok (triggered or quiet), 2 no EF5 runs found.

## Agnostic by construction

- Any number of members: every `tmp_output_*` run folder with outputs for
  the cycle is a member (QPE-only state runs are skipped via `require_qpf`).
- Any cycle frequency: cycles are discovered from folder names.
- File naming, thresholds, bands, selectors: all in the YAML config.
- Areas of Concern: pilot basins (Haiti, Guatemala) and administrative
  units (islands) go through the identical code path.

## Catalog format

`index.csv`: `storm_id, magnitude_mm, direction_deg, depth_path, extent_path`
(one row per simulated storm; direction accepts degrees or compass points).
Optional `magnitudes_by_aoc.csv` (`aoc_id, storm_id, magnitude_mm`) provides
per-AOC magnitudes computed from the storm rainfall rasters; when present it
overrides the domain magnitude during matching. Extent rasters are derived
from depth >= 0.05 m when the hydraulic run has no extent product.

## Tests

`fim_dev/make_synthetic.py` builds a complete synthetic setup and
`fim_dev/run_e2e_test.py` runs the full chain with 21 checks (normal event,
quiet cycle, beyond-catalog event). No real data needed.

## v0.3: location flexibility (this version)

- All internal imports are relative: the package runs unchanged from any
  parent package or folder (verified as `my_tools.fim_utils`).
- Config path resolution order: `root=` argument > `root:` YAML key >
  `TITO_FIM_ROOT` environment variable > current working directory.
- Orchestrator integration: `orchestrator.py` Phase 3 runs
  `fim_config/<Region>.yaml` for every region that has one, non-fatally.
  See `README_FIM.md` at the repo root for manual setup steps and cautions.

## v0.2: ensembles, zarr store, probabilistic product (Guatemala pilot)

Added for the operational ensemble chain (10 QPE members x 5 StormLab
storms and any future layout):

- `ensemble.py`  layout-driven member discovery with `{placeholders}`
  (member template + component templates + trigger source templates),
  plus sampling zones that widen automatically when EF5 outputs are
  masked (nodata) over the Area of Concern, with flags.
- `store.py`  zarr FIM store: one chunk per storm, magnitude-sorted, so
  one rainfall total reads exactly one depth map. depth + extent only.
  `attach_magnitudes()` refreshes magnitudes (e.g. real RainyDay totals)
  without rebuilding chunks.
- `probability.py`  P(max depth >= threshold) across members, IBF
  likelihood classes (Very Low / Low / Medium / High), GeoTIFF + PNG.
- `pipeline_ensemble.py`  the cycle runner + CLI:
  `python -m tito_utils.fim_utils.pipeline_ensemble --config fim_config/Guatemala_SantaInes.yaml`

Requires `zarr` in addition to the base dependencies (pip install zarr).
Verified on real TITO outputs (3 cycles, 70 runs each) with independent
recomputation checks: see `fim_dev/test_guatemala_real.py`.
