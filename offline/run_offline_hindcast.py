#!/usr/bin/env python3
"""
Training offline hindcast entrypoint — no network precip downloads.

Usage (from project root, inside container or native env):
  python offline/run_offline_hindcast.py \\
      "2023-06-21 07:00" "2023-06-21 08:00" --regions Guatemala

Or via launcher:
  ./tito-run.sh hindcast "..." "..." --regions Guatemala --offline

Monkey-patches prepare_cycle_precip only for this process; core TITO code
paths are unchanged.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.chdir(ROOT)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="TITO offline training hindcast")
    ap.add_argument("start", help='Hindcast start "YYYY-MM-DD HH:MM"')
    ap.add_argument("end", help='Hindcast end "YYYY-MM-DD HH:MM"')
    ap.add_argument("--regions", default="Guatemala")
    ap.add_argument("--config", default="Caribbean_Comoros_config")
    ap.add_argument(
        "--offline-precip",
        default=os.environ.get("TITO_OFFLINE_PRECIP", str(ROOT / "offline_precips")),
        help="Archive folder (default: offline_precips/)",
    )
    args = ap.parse_args(argv)

    os.environ["TITO_OFFLINE"] = "1"
    os.environ["TITO_OFFLINE_PRECIP"] = str(Path(args.offline_precip).resolve())
    os.environ["TITO_OFFLINE_CONFIG"] = (
        args.config if not args.config.endswith(".py") else args.config[:-3]
    )
    # Ensure sitecustomize in offline/ is loaded by every child Python process
    # /app (or project root) for `import offline`; offline/ for sitecustomize
    root_s = str(ROOT)
    offline_dir = str(ROOT / "offline")
    prev = os.environ.get("PYTHONPATH", "")
    parts = [p for p in prev.split(os.pathsep) if p]
    for p in (root_s, offline_dir):
        if p not in parts:
            parts.insert(0, p)
    os.environ["PYTHONPATH"] = os.pathsep.join(parts)

    print("==== TITO OFFLINE TRAINING HINDCAST ====")
    print(f"  window : {args.start} → {args.end}")
    print(f"  regions: {args.regions}")
    print(f"  archive: {os.environ['TITO_OFFLINE_PRECIP']}")
    print(f"  PYTHONPATH includes offline/sitecustomize (no downloads)")
    print(f"  warmup : forced off via staged states (set warmup_enabled=False in config)")

    cfg_py = args.config if args.config.endswith(".py") else args.config + ".py"
    cmd = [
        sys.executable,
        str(ROOT / "hindcast_manager.py"),
        str(ROOT / cfg_py),
        args.start,
        args.end,
        "--regions",
        args.regions,
    ]
    # Pass env with TITO_OFFLINE to children
    import subprocess
    return int(subprocess.call(cmd, cwd=str(ROOT), env=os.environ.copy()))


if __name__ == "__main__":
    raise SystemExit(main())
