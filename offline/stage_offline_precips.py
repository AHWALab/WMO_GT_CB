#!/usr/bin/env python3
"""
Stage training offline precip into the live EF5_conf paths.

Does NOT download anything. Selects the wettest ensemble members when the
configured ensemble size is smaller than the offline archive.

Layout expected under offline_precips/ (or --source):
  stream_sat/caribbean/ensP1..N/*.tif   (or stream_sat/ensP1..)
  stormlab/guatemala/ensQ1..M/*.tif
  imerg/*.tif
  qpf_store/_shared/<cycle_key>/gfs_data/*.tif
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

try:
    import rasterio
except ImportError:  # pragma: no cover
    rasterio = None

# Guatemala training package — only these cycle times have offline precip.
# Override with env TITO_OFFLINE_ALLOWED_CYCLES=202306210700,202306210800
DEFAULT_OFFLINE_CYCLES = (
    "202306210700",  # 2023-06-21 07:00 UTC
    "202306210800",  # 2023-06-21 08:00 UTC
)


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def allowed_offline_cycles(source: Optional[Path] = None) -> List[str]:
    """Cycle keys (YYYYMMDDHHMM) permitted in --offline mode."""
    env = os.environ.get("TITO_OFFLINE_ALLOWED_CYCLES", "").strip()
    if env:
        return [c.strip() for c in env.split(",") if c.strip()]
    # Training defaults ∪ any GFS cycle folders present in the archive
    root = _project_root()
    src = Path(source) if source else Path(
        os.environ.get("TITO_OFFLINE_PRECIP", str(root / "offline_precips")))
    found = set(DEFAULT_OFFLINE_CYCLES)
    shared = src / "qpf_store" / "_shared"
    if shared.is_dir():
        for d in shared.iterdir():
            if d.is_dir() and len(d.name) == 12 and d.name.isdigit():
                found.add(d.name)
    return sorted(found)


def validate_offline_cycle(cycle_key: str, source: Optional[Path] = None) -> None:
    """
    Refuse offline runs whose cycle is not in the training archive.

    Without this, a wrong hindcast time would still stage 21 Jun precip
    under a different cycle label → silent wrong science.
    """
    allowed = allowed_offline_cycles(source)
    ck = str(cycle_key).strip()
    if ck in allowed:
        return
    # pretty print
    def _fmt(k: str) -> str:
        if len(k) == 12 and k.isdigit():
            return f"{k[0:4]}-{k[4:6]}-{k[6:8]} {k[8:10]}:{k[10:12]} UTC"
        return k
    lines = [
        f"OFFLINE mode refused: cycle {ck} ({_fmt(ck)}) is not in the training archive.",
        "  This package only ships precip for:",
    ]
    for a in allowed:
        lines.append(f"    - {_fmt(a)}  ({a})")
    lines.append(
        '  Use e.g.:  ./tito-run.sh hindcast "2023-06-21 07:00" '
        '"2023-06-21 07:00" --regions Guatemala --offline'
    )
    lines.append(
        "  Or unset --offline to run online downloads for other times."
    )
    raise RuntimeError("\n".join(lines))


def _member_score(folder: Path) -> float:
    """Wetter score for one ensemble member folder (higher = wetter).

    Fast path: sum of compressed GeoTIFF sizes is a good proxy when LZW
    compression is used (wetter grids compress less / store more variance).
    Optional slow path: set TITO_OFFLINE_SCORE=rasterio to mean-sample pixels.
    """
    tifs = list(folder.glob("*.tif"))
    if not tifs:
        return float("-inf")
    mode = os.environ.get("TITO_OFFLINE_SCORE", "size").strip().lower()
    if mode != "rasterio" or rasterio is None:
        return float(sum(f.stat().st_size for f in tifs))
    # Optional accurate (slower) score — import numpy only here
    import numpy as np
    if len(tifs) <= 8:
        sample = tifs
    else:
        idxs = np.linspace(0, len(tifs) - 1, 8).astype(int)
        sample = [tifs[i] for i in idxs]
    vals = []
    for tif in sample:
        try:
            with rasterio.open(tif) as src:
                data = src.read(
                    1,
                    out_shape=(max(1, src.height // 8), max(1, src.width // 8)),
                    masked=True,
                )
                if hasattr(data, "count") and data.count() == 0:
                    continue
                vals.append(float(np.nanmean(data)))
        except Exception:
            continue
    return float(np.mean(vals)) if vals else float("-inf")


def _rank_ens_dirs(parent: Path, prefix: str) -> List[Path]:
    """Return ens dirs sorted wettest-first (ensP* / ensQ*)."""
    dirs = [p for p in parent.iterdir() if p.is_dir() and p.name.startswith(prefix)]
    scored: List[Tuple[float, Path]] = []
    for d in dirs:
        scored.append((_member_score(d), d))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [p for _, p in scored]


def _wipe_and_copy_tree(src: Path, dst: Path) -> int:
    if dst.exists() or dst.is_symlink():
        if dst.is_dir() and not dst.is_symlink():
            shutil.rmtree(dst)
        else:
            dst.unlink()
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst)
    return sum(1 for _ in dst.rglob("*") if _.is_file())


def _link_or_copy_tree(src: Path, dst: Path) -> None:
    """Prefer hardlinks (fast, no extra space); fall back to copytree."""
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True, exist_ok=True)
    for root, dirs, files in os.walk(src):
        rel = Path(root).relative_to(src)
        target_dir = dst / rel
        target_dir.mkdir(parents=True, exist_ok=True)
        for name in files:
            s = Path(root) / name
            d = target_dir / name
            try:
                if d.exists():
                    d.unlink()
                os.link(s, d)
            except OSError:
                shutil.copy2(s, d)


def _copy_selected_members(
    ranked: Sequence[Path],
    dest_parent: Path,
    prefix: str,
    n_keep: int,
) -> List[str]:
    """Copy top n_keep ranked folders → dest_parent/prefix1..n."""
    dest_parent.mkdir(parents=True, exist_ok=True)
    chosen = list(ranked[: max(1, n_keep)])
    # If source is already under dest_parent, stage via temp then swap
    tmp_parent = dest_parent / f".offline_stage_{prefix}"
    if tmp_parent.exists():
        shutil.rmtree(tmp_parent)
    tmp_parent.mkdir(parents=True, exist_ok=True)
    names = []
    for i, src in enumerate(chosen, start=1):
        dst = tmp_parent / f"{prefix}{i}"
        _link_or_copy_tree(src, dst)
        names.append(f"{src.name}→{prefix}{i} (score≈{_member_score(src):.4g})")
    for old in list(dest_parent.glob(f"{prefix}*")):
        if old.is_dir():
            shutil.rmtree(old)
    for p in tmp_parent.glob(f"{prefix}*"):
        shutil.move(str(p), str(dest_parent / p.name))
    shutil.rmtree(tmp_parent, ignore_errors=True)
    return names


def _resolve_ss_root(source: Path) -> Path:
    """Prefer domain folder caribbean/ if present."""
    ss = source / "stream_sat"
    car = ss / "caribbean"
    if car.is_dir() and any(car.glob("ensP*")):
        return car
    if any(ss.glob("ensP*")):
        return ss
    raise FileNotFoundError(f"No STREAM-Sat ensP* under {ss}")


def _resolve_sl_root(source: Path, region_slug: str = "guatemala") -> Path:
    sl = source / "stormlab" / region_slug
    if not sl.is_dir():
        # maybe flat
        sl = source / "stormlab"
    if not any(sl.glob("ensQ*")):
        raise FileNotFoundError(f"No StormLab ensQ* under {sl}")
    return sl


def stage(
    *,
    project_root: Path,
    source: Path,
    ss_ens: int,
    sl_ens: int,
    cycle_key: str = "202306210700",
    region_slug: str = "guatemala",
) -> dict:
    """
    Stage offline precip into live EF5_conf paths. Returns summary dict.
    """
    source = source.resolve()
    if not source.is_dir():
        raise FileNotFoundError(
            f"Offline precip source not found: {source}\n"
            f"Populate offline_precips/ (see offline/materialize_offline_precips.sh)"
        )
    validate_offline_cycle(cycle_key, source)

    precip_root = project_root / "EF5_conf" / "precip"
    qpf_root = project_root / "EF5_conf" / "qpf_store"
    summary = {"source": str(source), "ss": [], "sl": [], "imerg": 0, "gfs": 0}

    # ── STREAM-Sat ────────────────────────────────────────────────────
    ss_src = _resolve_ss_root(source)
    ranked_ss = _rank_ens_dirs(ss_src, "ensP")
    if not ranked_ss:
        raise RuntimeError(f"No STREAM-Sat members in {ss_src}")
    # live path used by pipeline: precip/stream_sat/<domain>/
    domain = "caribbean" if "caribbean" in str(ss_src) else ss_src.name
    ss_dst = precip_root / "stream_sat" / domain
    # also clear flat ensP at stream_sat/ root if present
    flat = precip_root / "stream_sat"
    flat.mkdir(parents=True, exist_ok=True)
    summary["ss"] = _copy_selected_members(ranked_ss, ss_dst, "ensP", ss_ens)
    print(f"  [offline] STREAM-Sat → {ss_dst}  (n={ss_ens})")
    for line in summary["ss"]:
        print(f"            {line}")

    # ── StormLab ──────────────────────────────────────────────────────
    try:
        sl_src = _resolve_sl_root(source, region_slug)
        ranked_sl = _rank_ens_dirs(sl_src, "ensQ")
        sl_dst = precip_root / "stormlab" / region_slug
        summary["sl"] = _copy_selected_members(ranked_sl, sl_dst, "ensQ", sl_ens)
        print(f"  [offline] StormLab → {sl_dst}  (n={sl_ens})")
        for line in summary["sl"]:
            print(f"            {line}")
    except FileNotFoundError as exc:
        print(f"  [offline] StormLab skipped: {exc}")

    # ── IMERG ─────────────────────────────────────────────────────────
    imerg_src = source / "imerg"
    if imerg_src.is_dir():
        imerg_dst = precip_root / "imerg"
        # keep shared cycle folder layout expected by prepare_precip
        if imerg_dst.exists():
            # only replace tifs, keep subdirs structure if any
            for tif in imerg_dst.glob("*.tif"):
                tif.unlink()
        imerg_dst.mkdir(parents=True, exist_ok=True)
        n = 0
        for tif in imerg_src.glob("*.tif"):
            shutil.copy2(tif, imerg_dst / tif.name)
            n += 1
        # also stage into _shared/<cycle> if callers look there
        shared_imerg = precip_root / "imerg" / "_shared" / cycle_key
        shared_imerg.mkdir(parents=True, exist_ok=True)
        for tif in imerg_src.glob("*.tif"):
            shutil.copy2(tif, shared_imerg / tif.name)
        summary["imerg"] = n
        print(f"  [offline] IMERG → {imerg_dst} + _shared/{cycle_key}  ({n} tifs)")
    else:
        print("  [offline] IMERG skipped (no offline_precips/imerg)")

    # ── GFS / qpf_store ───────────────────────────────────────────────
    # Always stage under the *requested* cycle_key so EF5 finds it.
    # Archive may only have 202306210700; 08:00 reuses that GFS pack.
    qpf_src = source / "qpf_store"
    if qpf_src.is_dir():
        shared_src = qpf_src / "_shared" / cycle_key / "gfs_data"
        if not shared_src.is_dir():
            cands = sorted((qpf_src / "_shared").glob("*/gfs_data"))
            shared_src = cands[0] if cands else None
            if shared_src is not None:
                print(
                    f"  [offline] GFS: no pack for {cycle_key}; "
                    f"reusing archive {shared_src.parent.name}"
                )
        if shared_src and shared_src.is_dir():
            dst = qpf_root / "_shared" / cycle_key / "gfs_data"
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(shared_src, dst)
            reg_dst = qpf_root / region_slug / "gfs_data"
            if reg_dst.exists():
                shutil.rmtree(reg_dst)
            shutil.copytree(shared_src, reg_dst)
            summary["gfs"] = len(list(dst.glob("*.tif")))
            print(f"  [offline] GFS → {dst} and {reg_dst}  ({summary['gfs']} tifs)")
        else:
            print("  [offline] GFS skipped (no gfs_data under qpf_store)")
    else:
        print("  [offline] qpf_store skipped")

    return summary


def build_shared_precip(config, regions, region_cycle_times, region_qpe, region_qpf):
    """Return a SharedPrecip pointing at staged live paths (no downloads)."""
    from tito_utils.file_utils.prepare_precip import SharedPrecip

    root = _project_root()
    result = SharedPrecip()
    ss_ens = int(getattr(config, "stream_sat_ensemble_size", 10))
    sl_ens = int(getattr(config, "stormlab_ensemble_size", 5))

    for region in regions:
        ct = region_cycle_times[region]
        ck = ct.strftime("%Y%m%d%H%M")
        qpe = str(region_qpe.get(region, "")).upper()
        qpf_list = [str(s).upper() for s in region_qpf.get(region, [])]

        if qpe == "STREAM_SAT":
            tif_root = root / "EF5_conf" / "precip" / "stream_sat" / "caribbean"
            if not tif_root.is_dir():
                tif_root = root / "EF5_conf" / "precip" / "stream_sat"
            result.streamsat_info[region] = {
                "tif_root": str(tif_root) + os.sep,
                "ensemble_size": ss_ens,
                "domain": "caribbean",
                "offline": True,
            }
        if qpe == "IMERG":
            # Prefer cycle shared folder; fall back to flat imerg/
            shared = root / "EF5_conf" / "precip" / "imerg" / "_shared" / ck
            flat = root / "EF5_conf" / "precip" / "imerg"
            folder = shared if shared.is_dir() and any(shared.glob("*.tif")) else flat
            result.imerg_folders[ck] = str(folder) + os.sep
            result.imerg_eff_starts[region] = ct
        if "STORMLAB" in qpf_list:
            slug = region.lower().replace(" ", "_")
            tif_root = root / "EF5_conf" / "precip" / "stormlab" / slug
            result.stormlab_info[region] = {
                "tif_root": str(tif_root) + os.sep,
                "ensemble_size": sl_ens,
                "domain": slug,
                "offline": True,
            }
        if "GFS" in qpf_list:
            gfs = root / "EF5_conf" / "qpf_store" / "_shared" / ck / "gfs_data"
            if not gfs.is_dir():
                cands = list((root / "EF5_conf" / "qpf_store" / "_shared").glob("*/gfs_data"))
                gfs = cands[0] if cands else gfs
            if gfs.is_dir():
                # Register under every cycle key this region might use
                gpath = str(gfs) + os.sep
                result.gfs_cache[ck] = gpath
                result.gfs_cache[ct.strftime("%Y%m%d%H%M")] = gpath
                # parent folder name may differ from region cycle key
                result.gfs_cache[gfs.parent.name] = gpath

    return result


def install_offline_hook(config) -> None:
    """Monkey-patch prepare_cycle_precip so the pipeline never downloads."""
    import tito_utils.precip.manager as mgr

    root = _project_root()
    source = Path(os.environ.get(
        "TITO_OFFLINE_PRECIP",
        str(root / "offline_precips"),
    ))
    # If offline_precips empty, use live EF5_conf precip as archive source
    if not source.is_dir() or not any(source.iterdir()):
        source = root / "EF5_conf" / "precip"
        # build a virtual source layout for stage()
        # stage() expects stream_sat/stormlab/imerg/qpf_store under source
        # Live layout already matches for precip/*; qpf is sibling
        print("  [offline] offline_precips/ empty — using EF5_conf/precip + qpf_store as archive")

    ss_ens = int(getattr(config, "stream_sat_ensemble_size", 10))
    sl_ens = int(getattr(config, "stormlab_ensemble_size", 5))

    def _prepare(regions_to_run, region_cycle_times, region_qpe_sources,
                 region_qpf_requested, config, *, master_log=None):
        print("==== TITO OFFLINE precip (no downloads) ====")
        # Build a staging source root:
        # Prefer offline_precips; else synthesize from EF5_conf
        src = Path(os.environ.get("TITO_OFFLINE_PRECIP", str(root / "offline_precips")))
        if not src.is_dir() or not any(Path(src).iterdir()):
            # synthetic: use precip/ as stream_sat parent and point qpf
            src = root / "EF5_conf"
            # stage expects offline_precips/{stream_sat,stormlab,imerg,qpf_store}
            # map: EF5_conf/precip/* and EF5_conf/qpf_store
            class _Src:
                def __init__(self, precip, qpf):
                    self.precip = precip
                    self.qpf = qpf

            # rewrite stage to accept EF5_conf layout via env
            os.environ["TITO_OFFLINE_USE_EF5_LAYOUT"] = "1"
            src_path = root / "EF5_conf"
        else:
            src_path = src
            os.environ.pop("TITO_OFFLINE_USE_EF5_LAYOUT", None)

        for _ct in region_cycle_times.values():
            validate_offline_cycle(
                _ct.strftime("%Y%m%d%H%M"),
                Path(os.environ.get(
                    "TITO_OFFLINE_PRECIP", str(root / "offline_precips"))),
            )
        ct0 = next(iter(region_cycle_times.values()))
        cycle_key = ct0.strftime("%Y%m%d%H%M")

        if os.environ.get("TITO_OFFLINE_USE_EF5_LAYOUT") == "1":
            # Source is EF5_conf — stage from precip/* and qpf_store
            _stage_from_ef5_layout(
                root, ss_ens=ss_ens, sl_ens=sl_ens, cycle_key=cycle_key)
        else:
            stage(
                project_root=root,
                source=src_path,
                ss_ens=ss_ens,
                sl_ens=sl_ens,
                cycle_key=cycle_key,
            )

        shared = build_shared_precip(
            config, list(regions_to_run), dict(region_cycle_times),
            dict(region_qpe_sources),
            {k: list(v) for k, v in region_qpf_requested.items()},
        )
        if master_log:
            master_log.info("OFFLINE precip staged ss=%s sl=%s", ss_ens, sl_ens)
        print("==== OFFLINE precip ready ====")
        return shared

    mgr.prepare_cycle_precip = _prepare  # type: ignore[assignment]


def _reorder_ens_in_place(parent: Path, prefix: str, n_keep: int) -> List[str]:
    """
    Rename ensemble folders so wettest become prefix1..n_keep.
    Does not delete unused members (keeps disk I/O minimal on network FS).
    """
    ranked = _rank_ens_dirs(parent, prefix)
    if not ranked:
        return []
    # Phase 1: move all to temp names
    tmp_map = []
    for i, src in enumerate(ranked):
        tmp = parent / f".tmp_{prefix}{i:03d}"
        if tmp.exists():
            shutil.rmtree(tmp)
        src.rename(tmp)
        tmp_map.append(tmp)
    # Phase 2: wettest → prefix1..n_keep; rest → prefix{n_keep+1}..
    names = []
    for i, tmp in enumerate(tmp_map, start=1):
        dst = parent / f"{prefix}{i}"
        tmp.rename(dst)
        if i <= n_keep:
            names.append(f"rank{i} → {dst.name}")
    return names


def _stage_from_ef5_layout(root: Path, *, ss_ens: int, sl_ens: int, cycle_key: str):
    """When offline_precips is empty, re-rank within live EF5_conf precip."""
    validate_offline_cycle(cycle_key, root / "offline_precips")
    precip = root / "EF5_conf" / "precip"
    ss_src = precip / "stream_sat" / "caribbean"
    if not ss_src.is_dir():
        ss_src = precip / "stream_sat"
    if ss_src.is_dir():
        names = _reorder_ens_in_place(ss_src, "ensP", ss_ens)
        print(f"  [offline] STREAM-Sat re-ranked in place (use first {ss_ens})")
        for n in names[:ss_ens]:
            print(f"            {n}")

    sl_src = precip / "stormlab" / "guatemala"
    if sl_src.is_dir():
        names = _reorder_ens_in_place(sl_src, "ensQ", sl_ens)
        print(f"  [offline] StormLab re-ranked in place (use first {sl_ens})")
        for n in names[:sl_ens]:
            print(f"            {n}")

    # IMERG shared cycle folder — hardlink tifs when possible
    imerg = precip / "imerg"
    if imerg.is_dir() and any(imerg.glob("*.tif")):
        for ck in {cycle_key, "202306210700", "202306210800"}:
            shared = imerg / "_shared" / ck
            shared.mkdir(parents=True, exist_ok=True)
            n = 0
            for tif in imerg.glob("*.tif"):
                dest = shared / tif.name
                if dest.exists():
                    continue
                try:
                    os.link(tif, dest)
                except OSError:
                    shutil.copy2(tif, dest)
                n += 1
            print(f"  [offline] IMERG → _shared/{ck} ({n} new links/copies)")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Stage offline training precip")
    ap.add_argument("--source", default=None, help="offline_precips dir")
    ap.add_argument("--ss-ens", type=int, default=2)
    ap.add_argument("--sl-ens", type=int, default=2)
    ap.add_argument("--cycle-key", default="202306210700")
    args = ap.parse_args(argv)
    root = _project_root()
    src = Path(args.source) if args.source else root / "offline_precips"
    if not src.is_dir() or not any(src.iterdir()):
        print(f"Source {src} empty — staging from live EF5_conf precip")
        _stage_from_ef5_layout(
            root, ss_ens=args.ss_ens, sl_ens=args.sl_ens, cycle_key=args.cycle_key)
    else:
        stage(
            project_root=root, source=src,
            ss_ens=args.ss_ens, sl_ens=args.sl_ens, cycle_key=args.cycle_key)
    return 0


if __name__ == "__main__":
    sys.exit(main())
