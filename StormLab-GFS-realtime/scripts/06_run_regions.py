#!/usr/bin/env python3
"""Run operational cycles for one or more domains in parallel.

Wraps scripts/05_run_operational_cycle.py. Each selected region is a separate
subprocess so GEFS fetch + StormLab simulation for different domains overlap.

Examples:
  python scripts/06_run_regions.py --region all --cycle latest
  python scripts/06_run_regions.py --region barbados
  python scripts/06_run_regions.py --region barbados haiti comoros --cycle 2026072906
  python scripts/06_run_regions.py --region all --members 10 --forcing-members 1
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
CYCLE_SCRIPT = ROOT / "scripts" / "05_run_operational_cycle.py"

ALL_REGIONS = (
    "barbados",
    "comoros",
    "guatemala",
    "haiti",
    "lesserantilles",
)


def _parse_regions(values: list[str]) -> list[str]:
    raw: list[str] = []
    for v in values:
        raw.extend(p.strip().lower() for p in v.split(",") if p.strip())
    if not raw or raw == ["all"]:
        return list(ALL_REGIONS)
    if "all" in raw and len(raw) > 1:
        raise SystemExit("--region: use 'all' alone, or list specific regions")
    unknown = sorted(set(raw) - set(ALL_REGIONS))
    if unknown:
        raise SystemExit(
            f"unknown region(s): {', '.join(unknown)}; "
            f"choose from: {', '.join(ALL_REGIONS)}, all"
        )
    seen: set[str] = set()
    out: list[str] = []
    for r in raw:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def _run_one(region: str, cycle: str, source: str,
             members: int | None, forcing_members: int | None,
             ) -> tuple[str, int, float, str]:
    cfg = CONFIG_DIR / f"{region}.yaml"
    if not cfg.is_file():
        return region, 2, 0.0, f"missing config: {cfg}"
    cmd = [
        sys.executable,
        str(CYCLE_SCRIPT),
        "--config", str(cfg),
        "--cycle", cycle,
        "--source", source,
    ]
    if members is not None:
        cmd += ["--members", str(members)]
    if forcing_members is not None:
        cmd += ["--forcing-members", str(forcing_members)]
    env = os.environ.copy()
    src = str(ROOT / "src")
    prev = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{src}{os.pathsep}{prev}" if prev else src
    t0 = time.time()
    proc = subprocess.run(
        cmd,
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    elapsed = time.time() - t0
    out = (proc.stdout or "") + (proc.stderr or "")
    return region, proc.returncode, elapsed, out


def _fmt_secs(s: float) -> str:
    s = max(0.0, float(s))
    if s < 60:
        return f"{s:.0f} s"
    m, sec = divmod(int(round(s)), 60)
    if m < 60:
        return f"{m} m {sec:02d} s"
    h, m = divmod(m, 60)
    return f"{h} h {m:02d} m {sec:02d} s"


def main() -> int:
    p = argparse.ArgumentParser(
        description="Run StormLab-GFS operational cycle for one/many regions in parallel")
    p.add_argument(
        "--region", nargs="+", default=["all"], metavar="REGION",
        help="one or more of: " + ", ".join(ALL_REGIONS) + ", or 'all' "
             "(default: all). Comma-separated values also accepted.",
    )
    p.add_argument("--cycle", default="latest", help='"latest" or e.g. 2026072906')
    p.add_argument("--source", default="gefs", choices=["gefs", "gfs", "auto"])
    p.add_argument("--members", type=int, default=None,
                   help="total ensemble size (default: per-config n_members)")
    p.add_argument("--forcing-members", type=int, default=None,
                   help="GEFS forcing members (default: per-config)")
    p.add_argument(
        "--jobs", type=int, default=None,
        help="max parallel regions (default: number of selected regions)",
    )
    args = p.parse_args()

    regions = _parse_regions(args.region)
    jobs = args.jobs or len(regions)
    jobs = max(1, min(jobs, len(regions)))

    print(
        f"regions={','.join(regions)}  cycle={args.cycle}  source={args.source}  "
        f"parallel_jobs={jobs}",
        flush=True,
    )
    t0 = time.time()
    results: list[tuple[str, int, float, str]] = []

    if jobs == 1 or len(regions) == 1:
        for r in regions:
            print(f">>> starting {r}", flush=True)
            results.append(
                _run_one(r, args.cycle, args.source, args.members, args.forcing_members)
            )
            _, rc, elapsed, _ = results[-1]
            status = "OK" if rc == 0 else f"FAIL({rc})"
            print(f">>> finished {r}: {status}  ({_fmt_secs(elapsed)})", flush=True)
    else:
        with ProcessPoolExecutor(max_workers=jobs) as ex:
            futs = {
                ex.submit(
                    _run_one, r, args.cycle, args.source,
                    args.members, args.forcing_members,
                ): r
                for r in regions
            }
            for fut in as_completed(futs):
                region, rc, elapsed, out = fut.result()
                results.append((region, rc, elapsed, out))
                status = "OK" if rc == 0 else f"FAIL({rc})"
                print(
                    f">>> finished {region}: {status}  ({_fmt_secs(elapsed)})",
                    flush=True,
                )

    order = {r: i for i, r in enumerate(regions)}
    results.sort(key=lambda t: order.get(t[0], 999))

    failed = 0
    for region, rc, elapsed, out in results:
        print(f"\n========== {region} (exit {rc}, {_fmt_secs(elapsed)}) ==========")
        if out.strip():
            print(out.rstrip())
        if rc != 0:
            failed += 1

    wall = time.time() - t0
    cpu_sum = sum(e for _, _, e, _ in results)
    print("\n========== SUMMARY ==========")
    for region, rc, elapsed, _ in results:
        status = "OK" if rc == 0 else f"FAIL({rc})"
        print(f"  {region:16s}  {status:8s}  {_fmt_secs(elapsed)}")
    print(
        f"\nTOTAL wall-clock: {_fmt_secs(wall)}  "
        f"({len(regions) - failed} ok, {failed} failed; "
        f"sum of region times {_fmt_secs(cpu_sum)})",
        flush=True,
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
