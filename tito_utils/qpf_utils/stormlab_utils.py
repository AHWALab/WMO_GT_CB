"""
stormlab_utils.py
=================
StormLab-GFS QPF integration for TITO.

Handles:
  1. Running StormLab-GFS-realtime for one or more TITO regions.
  2. Converting ensemble NetCDF (qpf[member,time,lat,lon] mm/h) → EF5 GeoTIFFs.
  3. Organizing GeoTIFFs into per-member folders for nested EF5 jobs.

StormLab output layout::

    StormLab-GFS-realtime/output/<domain>/qpf_ens_<domain>_<YYYYMMDDHH>.nc

TITO GeoTIFF layout::

    precip/stormlab/<tito_region>/ensQ1/stormlab.YYYYMMDDHH00.tif
    precip/stormlab/<tito_region>/ensQ2/...
"""

from __future__ import annotations

import glob
import logging
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
from osgeo import gdal, osr

log = logging.getLogger("stormlab-utils")

TITO_ROOT = Path(__file__).resolve().parent.parent.parent
# StormLab lives next to this module under tito_utils/qpf_utils/
STORMLAB_REPO = Path(__file__).resolve().parent / "StormLab-GFS-realtime"
STORMLAB_REGIONS_SCRIPT = STORMLAB_REPO / "scripts" / "06_run_regions.py"
STORMLAB_CYCLE_SCRIPT = STORMLAB_REPO / "scripts" / "05_run_operational_cycle.py"
DEFAULT_TIF_ROOT = TITO_ROOT / "EF5_conf" / "precip" / "stormlab"
DEFAULT_NC_ROOT = STORMLAB_REPO / "output"

FILL_VALUE = -9999.0

# TITO region name → StormLab domain (config/<domain>.yaml)
TITO_TO_STORMLAB_DOMAIN = {
    "antigua": "lesserantilles",
    "barbados": "barbados",
    "guatemala": "guatemala",
    "haiti": "haiti",
    "comoros": "comoros",
}


def get_stormlab_domain(region: str) -> str:
    r = region.strip().lower()
    if r in TITO_TO_STORMLAB_DOMAIN:
        return TITO_TO_STORMLAB_DOMAIN[r]
    raise ValueError(
        f"No StormLab domain for TITO region '{region}'. "
        f"Known: {sorted(TITO_TO_STORMLAB_DOMAIN)}"
    )


def stormlab_cycle_time(
    cycle_time: Optional[datetime] = None,
    *,
    min_age_h: float = 5.0,
    now: Optional[datetime] = None,
) -> datetime:
    """Pick a GEFS/StormLab cycle that is usually on the NOAA bucket.

    Mirrors ``stormlab_gfs.data.gefs_operational.latest_cycle``:
    floor to 00/06/12/18, then step back while the cycle is younger than
    *min_age_h* hours (GEFS ~5 h latency).  For hindcast, pass the
    hindcast *cycle_time* as both reference and ``now`` so we stay at/before T.
    """
    ref = cycle_time or datetime.utcnow()
    wall = now if now is not None else datetime.utcnow()
    # Naive UTC assumed throughout TITO
    hour = (ref.hour // 6) * 6
    cyc = ref.replace(hour=hour, minute=0, second=0, microsecond=0)
    if cyc > ref:
        cyc -= timedelta(hours=6)
    # GEFS latency gate against wall clock (ops) or cycle_time (hindcast)
    while (wall - cyc) < timedelta(hours=float(min_age_h)):
        cyc -= timedelta(hours=6)
    return cyc


def stormlab_cycle_str(
    cycle_time: Optional[datetime] = None,
    *,
    min_age_h: float = 5.0,
    now: Optional[datetime] = None,
) -> str:
    """``YYYYMMDDHH`` string for :func:`stormlab_cycle_time`."""
    return stormlab_cycle_time(cycle_time, min_age_h=min_age_h, now=now).strftime(
        "%Y%m%d%H"
    )


def probe_gefs_cycle_available(cycle: datetime, timeout: float = 15.0) -> bool:
    """Return True if GEFS control f003 .idx is HTTP 200 on noaa-gefs-pds."""
    import urllib.request

    url = (
        f"https://noaa-gefs-pds.s3.amazonaws.com/"
        f"gefs.{cycle:%Y%m%d}/{cycle:%H}/atmos/pgrb2sp25/"
        f"gec00.t{cycle:%H}z.pgrb2s.0p25.f003.idx"
    )
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= getattr(resp, "status", 200) < 300
    except Exception:
        # Some S3 endpoints reject HEAD — try a tiny GET range via curl-like GET
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                return 200 <= getattr(resp, "status", 200) < 300
        except Exception:
            return False


def hindcast_stormlab_cycle(cycle_time: datetime) -> datetime:
    """GEFS/StormLab cycle for hindcast so valid times cover from T.

    StormLab QPF valid times start at **init+6h**.  Pick the 00/06/12/18
    cycle at or before **T−6h** so the first lead lands at/before T:

      T = 12:00 → T−6h = 06:00 → cycle 06Z  (valid from 12Z)
      T = 18:00 → T−6h = 12:00 → cycle 12Z  (valid from 18Z)
      T = 00:00 → T−6h = previous day 18Z
      T = 15:00 → T−6h = 09:00 → floor → 06Z
    """
    target = cycle_time - timedelta(hours=6)
    hour = (int(target.hour) // 6) * 6
    return target.replace(hour=hour, minute=0, second=0, microsecond=0)


def resolve_stormlab_cycle(
    cycle_time: datetime,
    *,
    hindcast: bool = False,
    min_age_h: float = 5.0,
    max_back_cycles: int = 4,
) -> str:
    """Choose a StormLab cycle string, probing S3 when possible.

    Operational: age-gated latest cycle, step back if idx missing.
    Hindcast: cycle ≤ T−6h (floored to 00/06/12/18) so StormLab +6h
    leads cover EF5 from T; probe S3 and step back if missing.
    """
    if hindcast:
        cyc = hindcast_stormlab_cycle(cycle_time)
    else:
        cyc = stormlab_cycle_time(cycle_time, min_age_h=min_age_h, now=None)

    for _ in range(max(1, int(max_back_cycles))):
        if probe_gefs_cycle_available(cyc):
            return cyc.strftime("%Y%m%d%H")
        print(f"    [StormLab] GEFS {cyc:%Y%m%d%H} not on S3 yet — try −6h")
        cyc -= timedelta(hours=6)
    if hindcast:
        return hindcast_stormlab_cycle(cycle_time).strftime("%Y%m%d%H")
    return stormlab_cycle_time(
        cycle_time, min_age_h=min_age_h, now=None
    ).strftime("%Y%m%d%H")


def find_stormlab_nc(
    domain: str,
    cycle: Optional[str] = None,
    nc_root: Optional[str] = None,
) -> Optional[Path]:
    """Locate qpf_ens_<domain>_<cycle>.nc (or latest for domain)."""
    root = Path(nc_root) if nc_root else DEFAULT_NC_ROOT
    domain_dir = root / domain
    if not domain_dir.is_dir():
        return None
    if cycle and cycle != "latest":
        p = domain_dir / f"qpf_ens_{domain}_{cycle}.nc"
        return p if p.is_file() else None
    matches = sorted(domain_dir.glob(f"qpf_ens_{domain}_*.nc"))
    return matches[-1] if matches else None


def _stormlab_python() -> str:
    """Interpreter for StormLab subprocesses — always the active TITO env.

    Uses ``sys.executable`` (tito_env2 in Docker/Apptainer/native).  Optional
    override: ``STORMLAB_PYTHON=/path/to/python``.
    """
    override = os.environ.get("STORMLAB_PYTHON", "").strip()
    if override and os.path.isfile(override):
        return override
    return sys.executable


def run_stormlab_for_regions(
    regions: Sequence[str],
    *,
    cycle: str = "latest",
    members: Optional[int] = None,
    forcing_members: Optional[int] = None,
    source: str = "gefs",
    timeout_seconds: int = 14400,
    pipeline_log: Any = None,
) -> Dict[str, Any]:
    """Run StormLab via 06_run_regions.py for the mapped domains.

    Always launched with the TITO conda env (tito_env2) — same as STREAM-Sat.

    Returns dict with domains run, rc, and elapsed seconds.
    """
    if not STORMLAB_REGIONS_SCRIPT.is_file():
        raise FileNotFoundError(f"StormLab script not found: {STORMLAB_REGIONS_SCRIPT}")

    domains: List[str] = []
    seen = set()
    for r in regions:
        d = get_stormlab_domain(r)
        if d not in seen:
            seen.add(d)
            domains.append(d)

    py = _stormlab_python()
    cmd = [
        py,
        str(STORMLAB_REGIONS_SCRIPT),
        "--region", *domains,
        "--cycle", str(cycle),
        "--source", source,
    ]
    if members is not None:
        cmd += ["--members", str(int(members))]
    if forcing_members is not None:
        cmd += ["--forcing-members", str(int(forcing_members))]

    from tito_utils.logging_utils import (
        debug_print, filter_stormlab_line, is_debug, user_print,
    )

    env = os.environ.copy()
    src = str(STORMLAB_REPO / "src")
    prev = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{src}{os.pathsep}{prev}" if prev else src
    # Explicit: do not activate a separate stormlab conda env
    env["STORMLAB_USE_TITO_ENV"] = "1"
    if is_debug():
        print(f"    [StormLab] python={py} (tito_env2 / active TITO env)")
    if pipeline_log:
        pipeline_log.info("[StormLab] python=%s", py)

    log.info("StormLab run: %s", " ".join(cmd))
    if is_debug():
        print(f"    [StormLab] cmd: {' '.join(cmd)}")
    else:
        user_print(f"    StormLab: starting ({', '.join(domains)}) …")
    if pipeline_log:
        pipeline_log.info("[StormLab] cmd: %s", " ".join(cmd))

    t0 = time.time()
    combined_chunks: List[str] = []
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(STORMLAB_REPO),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None
        deadline = t0 + float(timeout_seconds)
        while True:
            if time.time() > deadline:
                proc.kill()
                raise subprocess.TimeoutExpired(cmd, timeout_seconds)
            line = proc.stdout.readline()
            if line == "" and proc.poll() is not None:
                break
            if not line:
                time.sleep(0.05)
                continue
            combined_chunks.append(line)
            shown = filter_stormlab_line(line)
            if shown is not None:
                print(shown, flush=True)
            elif is_debug():
                debug_print(line.rstrip("\n"))
        rc = proc.wait(timeout=max(1, int(deadline - time.time())))
    except subprocess.TimeoutExpired:
        raise

    elapsed = time.time() - t0
    combined = "".join(combined_chunks)

    if pipeline_log:
        if combined:
            pipeline_log.info("[StormLab] OUTPUT:\n%s", combined[-8000:])
        pipeline_log.info("[StormLab] elapsed=%.1fs rc=%s", elapsed, rc)

    if rc != 0:
        raise RuntimeError(
            f"StormLab failed (rc={rc}): {combined[-2000:]}"
        )

    user_print(f"    StormLab: completed in {elapsed:.0f}s")
    return {"domains": domains, "elapsed": elapsed, "rc": rc}


def _geo_from_latlon(lats, lons):
    lat_ascending = len(lats) > 1 and float(lats[1]) > float(lats[0])
    if lat_ascending:
        ul_lat = float(lats[-1])
        lat_res = float(lats[1] - lats[0])
    else:
        ul_lat = float(lats[0])
        lat_res = float(lats[1] - lats[0]) if len(lats) > 1 else -0.1
    ul_lon = float(lons[0])
    lon_res = float(lons[1] - lons[0]) if len(lons) > 1 else 0.1
    return lat_ascending, ul_lat, lat_res, ul_lon, lon_res


def _write_geotiff(path: str, data: np.ndarray, ul_lon, lon_res, ul_lat, lat_res) -> None:
    ny, nx = data.shape
    driver = gdal.GetDriverByName("GTiff")
    out_ds = driver.Create(
        path, nx, ny, 1, gdal.GDT_Float32,
        options=["COMPRESS=LZW", "TILED=NO", "BIGTIFF=IF_SAFER"],
    )
    out_ds.SetGeoTransform([ul_lon, lon_res, 0, ul_lat, 0, lat_res])
    srs = osr.SpatialReference()
    srs.SetWellKnownGeogCS("WGS84")
    out_ds.SetProjection(srs.ExportToWkt())
    band = out_ds.GetRasterBand(1)
    band.WriteArray(data.astype(np.float32))
    band.SetNoDataValue(FILL_VALUE)
    band.FlushCache()
    out_ds = None


def _parse_time_units(units: str) -> datetime:
    """Parse CF-like ``hours since …`` time units.

    Accepts:
      - ``hours since 2026-07-29T12:00:00``
      - ``hours since 2026-07-29 12:00:00``
      - ``hours since 2026-07-29 12:00``
      - ``hours since 2026-07-30``          (date only → 00:00:00)
    """
    u = units.strip()
    m = re.match(
        r"hours\s+since\s+(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?)?",
        u,
        re.I,
    )
    if not m:
        raise ValueError(f"Unrecognized time units: {units!r}")
    day = m.group(1)
    hh = m.group(2) or "00"
    mm = m.group(3) or "00"
    ss = m.group(4) or "00"
    return datetime.strptime(f"{day} {hh}:{mm}:{ss}", "%Y-%m-%d %H:%M:%S")


def convert_stormlab_nc_to_geotiffs(
    nc_path: str,
    tif_dest_root: str,
    *,
    n_members: Optional[int] = None,
    tif_naming: str = "stormlab",
    max_workers: Optional[int] = None,
    min_valid_time: Optional[datetime] = None,
    max_valid_time: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Convert one StormLab ensemble NC → per-member hourly GeoTIFFs.

    Filenames: ``{tif_naming}.YYYYMMDDHH00.tif`` (EF5 FREQ=1h).
    """
    import netCDF4 as nc

    os.makedirs(tif_dest_root, exist_ok=True)
    ds = nc.Dataset(nc_path, "r")
    try:
        if "qpf" not in ds.variables:
            raise KeyError(f"No 'qpf' variable in {nc_path}")
        qpf = ds.variables["qpf"]
        n_mem_all = qpf.shape[0]
        n_time = qpf.shape[1]
        use_n = n_members if n_members is not None else n_mem_all
        use_n = max(1, min(int(use_n), n_mem_all))

        lats = np.asarray(ds.variables["lat"][:], dtype=np.float64)
        lons = np.asarray(ds.variables["lon"][:], dtype=np.float64)
        time_var = ds.variables["time"]
        t0 = _parse_time_units(getattr(time_var, "units", ""))
        time_vals = np.asarray(time_var[:], dtype=np.float64)
        valid_times = [t0 + timedelta(hours=float(h)) for h in time_vals]

        lat_ascending, ul_lat, lat_res, ul_lon, lon_res = _geo_from_latlon(lats, lons)
        if lat_ascending:
            lat_res = -abs(lat_res)

        # Preload selected members into memory for worker-less sequential write
        # (NC not picklable easily across processes with open handles).
        data_all = np.asarray(qpf[:use_n, :, :, :], dtype=np.float32)
    finally:
        ds.close()

    ok = fail = 0
    t_start = time.time()
    for m_idx in range(use_n):
        member_dir = os.path.join(tif_dest_root, f"ensQ{m_idx + 1}")
        os.makedirs(member_dir, exist_ok=True)
        for t_idx, vt in enumerate(valid_times):
            if min_valid_time is not None and vt < min_valid_time:
                continue
            if max_valid_time is not None and vt > max_valid_time:
                continue
            out_name = f"{tif_naming}.{vt.strftime('%Y%m%d%H')}00.tif"
            out_path = os.path.join(member_dir, out_name)
            if os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
                ok += 1
                continue
            try:
                arr = data_all[m_idx, t_idx, :, :].astype(np.float64)
                arr = np.where(np.isnan(arr), FILL_VALUE, arr)
                arr = np.where(arr < -9000, FILL_VALUE, arr)
                if lat_ascending:
                    arr = np.flipud(arr)
                _write_geotiff(out_path, arr, ul_lon, lon_res, ul_lat, lat_res)
                ok += 1
            except Exception as exc:
                log.error("StormLab TIF fail m=%s t=%s: %s", m_idx + 1, vt, exc)
                fail += 1

    elapsed = time.time() - t_start
    log.info(
        "StormLab conversion %s: %d OK, %d fail, %d members, %.1fs",
        nc_path, ok, fail, use_n, elapsed,
    )
    if fail > 0:
        raise RuntimeError(f"{fail} StormLab GeoTIFF conversions failed for {nc_path}")

    return {
        "tif_root": tif_dest_root,
        "ensemble_size": use_n,
        "n_times": n_time,
        "n_tifs": ok,
        "nc_path": nc_path,
        "valid_start": valid_times[0] if valid_times else None,
        "valid_end": valid_times[-1] if valid_times else None,
    }


def run_and_convert_stormlab(
    regions: Sequence[str],
    *,
    cycle_time: datetime,
    ensemble_size: Optional[int] = None,
    forcing_members: Optional[int] = None,
    run_pipeline: bool = True,
    cycle: Optional[str] = None,
    tif_root_base: Optional[str] = None,
    nc_root: Optional[str] = None,
    source: str = "auto",
    timeout_seconds: int = 14400,
    tif_naming: str = "stormlab",
    lr_hours: int = 24,
    hindcast: bool = False,
    min_age_h: float = 5.0,
    pipeline_log: Any = None,
) -> Dict[str, dict]:
    """Run StormLab (optional) and convert NC→TIF per TITO region.

    Operational: ``--cycle latest`` (StormLab's own GEFS age gate).
    Hindcast: ``--cycle YYYYMMDDHH`` for the GEFS cycle whose QPF covers
    TITO cycle time T (StormLab valid times start at init+6h, so we pick
    the 00/06/12/18 cycle at or before T−6h, then probe S3 if needed).

    Returns mapping ``region → info dict`` (tif_root, ensemble_size, ...).
    """
    if cycle and str(cycle) != "latest":
        cyc = str(cycle)
    elif hindcast:
        # Floor T to 00/06/12/18 (12–17 → 12Z, 18–23 → 18Z, …)
        cyc = resolve_stormlab_cycle(cycle_time, hindcast=True, min_age_h=min_age_h)
    else:
        cyc = "latest"
    print(f"    [StormLab] using cycle {cyc} (source={source}, hindcast={hindcast})")
    if pipeline_log:
        pipeline_log.info(
            "[StormLab] cycle=%s source=%s hindcast=%s", cyc, source, hindcast)

    pipeline_s = 0.0
    pipeline_ok = False
    if run_pipeline:
        try:
            run_info = run_stormlab_for_regions(
                regions,
                cycle=cyc,
                members=ensemble_size,
                forcing_members=forcing_members,
                source=source,
                timeout_seconds=timeout_seconds,
                pipeline_log=pipeline_log,
            )
            pipeline_s = float(run_info.get("elapsed", 0.0) or 0.0)
            pipeline_ok = True
        except Exception as exc:
            # Fall back to existing NC on disk if pipeline fails
            log.warning("StormLab pipeline error (will try existing NC): %s", exc)
            print(f"    [StormLab] pipeline error (will try existing NC): {exc}")
            if pipeline_log:
                pipeline_log.warning("StormLab pipeline error: %s", exc)

    base = tif_root_base or str(DEFAULT_TIF_ROOT)
    if not os.path.isabs(base):
        base = str(TITO_ROOT / base)

    min_vt = cycle_time
    max_vt = cycle_time + timedelta(hours=int(lr_hours))
    out: Dict[str, dict] = {}

    # Convert once per StormLab domain, then point each TITO region at its domain tree
    domain_info: Dict[str, dict] = {}
    for region in regions:
        try:
            domain = get_stormlab_domain(region)
        except ValueError as exc:
            out[region] = {"error": str(exc)}
            continue

        if domain not in domain_info:
            # Prefer NC for the requested cycle; fall back to newest on disk
            nc_path = find_stormlab_nc(domain, cycle=cyc, nc_root=nc_root)
            if nc_path is None and cyc != "latest":
                nc_path = find_stormlab_nc(domain, cycle="latest", nc_root=nc_root)
            if nc_path is None:
                err = f"No StormLab NC for domain={domain} cycle={cyc}"
                print(f"    [StormLab {domain}] ERROR: {err}")
                if pipeline_log:
                    pipeline_log.error("[StormLab %s] %s", domain, err)
                domain_info[domain] = {"error": err, "domain": domain}
            else:
                # Domain-level TIF root (shared by regions mapped to same domain)
                dom_tif = os.path.join(base, domain)
                try:
                    t_c0 = time.time()
                    info = convert_stormlab_nc_to_geotiffs(
                        str(nc_path),
                        dom_tif,
                        n_members=ensemble_size,
                        tif_naming=tif_naming,
                        min_valid_time=min_vt,
                        max_valid_time=max_vt,
                    )
                    convert_s = time.time() - t_c0
                    if int(info.get("n_tifs", 0) or 0) <= 0:
                        raise RuntimeError(
                            f"0 TIFs after convert of {nc_path} "
                            f"(window {min_vt} → {max_vt}); check time units / filter"
                        )
                    info["domain"] = domain
                    info["cycle"] = cyc
                    info["nc_path"] = str(nc_path)
                    info["pipeline_s"] = pipeline_s
                    info["convert_s"] = convert_s
                    info["elapsed_s"] = pipeline_s + convert_s
                    info["pipeline_ok"] = pipeline_ok
                    domain_info[domain] = info
                    print(
                        f"    [StormLab {domain}]: {info['ensemble_size']} members, "
                        f"{info['n_tifs']} TIFs → {dom_tif} "
                        f"(pipeline={pipeline_s:.1f}s convert={convert_s:.1f}s) "
                        f"nc={os.path.basename(str(nc_path))}"
                    )
                except Exception as exc:
                    print(f"    [StormLab {domain}] convert ERROR: {exc}")
                    if pipeline_log:
                        pipeline_log.error(
                            "[StormLab %s] convert failed nc=%s: %s",
                            domain, nc_path, exc,
                        )
                    domain_info[domain] = {
                        "error": str(exc), "domain": domain,
                        "pipeline_s": pipeline_s, "pipeline_ok": pipeline_ok,
                        "nc_path": str(nc_path) if nc_path else None,
                    }

        info = domain_info[domain]
        if "error" in info:
            out[region] = dict(info)
            print(f"    [StormLab] region {region} skipped: {info['error']}")
        else:
            # Per-region alias path (copy-free: same domain tif root)
            # Antigua shares lesserantilles TIFs.
            region_tif = os.path.join(base, region.strip().lower())
            if os.path.abspath(region_tif) != os.path.abspath(info["tif_root"]):
                os.makedirs(region_tif, exist_ok=True)
                # Symlink ensQ* folders into region path when missing
                for name in os.listdir(info["tif_root"]):
                    src = os.path.join(info["tif_root"], name)
                    dst = os.path.join(region_tif, name)
                    if not os.path.exists(dst) and os.path.isdir(src):
                        try:
                            os.symlink(src, dst)
                        except OSError:
                            # Fallback: point region at domain root
                            region_tif = info["tif_root"]
                            break
            else:
                region_tif = info["tif_root"]

            out[region] = {
                **info,
                "tif_root": region_tif,
                "region": region,
            }

    return out


def get_stormlab_member_folders(tif_root: str, ensemble_size: int) -> List[str]:
    folders = []
    for i in range(1, ensemble_size + 1):
        d = os.path.join(tif_root, f"ensQ{i}")
        if os.path.isdir(d):
            folders.append(os.path.join(d, ""))
    return folders


def stage_stormlab_member_to_qpf_store(
    member_tif_dir: str,
    qpf_store_region: str,
    member_idx: int,
) -> str:
    """Expose member TIFs under qpf_store/<region>/stormlab_data/ensQ{n}/.

    Uses symlink of the directory when possible; otherwise returns the
    original member path for direct LOC use.
    """
    dest_root = os.path.join(qpf_store_region, "stormlab_data", f"ensQ{member_idx}")
    os.makedirs(os.path.dirname(dest_root), exist_ok=True)
    if os.path.lexists(dest_root):
        if os.path.islink(dest_root) or os.path.isdir(dest_root):
            return os.path.join(dest_root, "")
    try:
        os.symlink(os.path.abspath(member_tif_dir.rstrip(os.sep)), dest_root)
    except OSError:
        return os.path.join(member_tif_dir, "")
    return os.path.join(dest_root, "")
