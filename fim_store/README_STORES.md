# fim_store: scenario flood map stores shipped with the repository

fim_store_SantaInesPetapa_v1.zarr.zip is the Guatemala Santa Ines Petapa
scenario library: 200 pre-simulated flood maps (max depth and extent) on the
5 m EPSG 3857 display grid, one compressed chunk per scenario.

Unzip it ONCE before first use, next to this file:

    cd fim_store
    unzip fim_store_SantaInesPetapa_v1.zarr.zip -d fim_store_SantaInesPetapa_v1.zarr

The store carries both matching indices, real data on both axes:

- pluvial magnitudes: RainyDay storm totals, mean over the area of concern,
  0 to 1051 mm (magnitudes_SantaInesPetapa_real.csv, attached Aug 2026)
- fluvial index: maximum boundary discharges Q1, Q2 per scenario from the
  hydraulic runs (flood_library.mat of the standalone prototype)

magnitudes_Morales_real.csv holds the RainyDay totals for the Morales site
(domain mean, 200 scenarios); the Morales store itself is pending its flood
map library and can be built with fim_dev/build_store_guatemala.py plus
fim_dev/attach_real_magnitudes_santaines.py as the template.

Rebuild from scratch: build the store from the MaximumDepth GeoTIFFs, then
run the attach script. attach_magnitudes re-sorts the store by magnitude and
re-links every per-scenario index, including the fluvial one.

Never commit model outputs or FIM products; the stores here are versioned
INPUTS of the method, which is why they live in git.
