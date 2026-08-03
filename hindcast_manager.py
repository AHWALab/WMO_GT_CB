#!/usr/bin/env python
"""
Hindcast Manager for TITO
=========================
Runs the TITO orchestrator in hindcast mode for consecutive hourly steps,
automatically chaining EF5 states between runs to simulate a continuous
historical event.

How it works
------------
TITO works in hourly cycles.  For a hindcast event the manager:
  1.  Steps through every hour from *start_date* to *end_date*.
  2.  Calls ``orchestrator.py <config> --hindcast-date <step>`` for each hour.
  3.  The orchestrator saves EF5 states at the end of each step; the next
      step automatically locates those states via ``find_available_states()``.
  4.  Per-step stdout / stderr is written to a log file so the console shows
      only progress.

State chaining by QPE experiment
---------------------------------
The timestamp of saved states depends on ``hindcast_qpe_experiment``:

  IMERG_ONLY / IMERG_NOWCAST
      States saved at  T − 4 h  (IMERG latency offset).
      For a run at T = 09:00, states are written at 05:00.
      The *next* run (T = 10:00) sets warm_end = 06:00 and searches back
      6 h from there → finds 05:00 ✓

  IMERG_SCAMPR / IMERG_HSAF
      States saved at  T  (gap fully covered by IR QPE).
      For a run at T = 09:00, states are written at 09:00.
      The *next* run (T = 10:00) sets warm_end = 10:00 and searches back
      6 h → finds 09:00 ✓

In both cases the state chain is maintained automatically.

Usage
-----
    python hindcast_manager.py <config_file> <start_date> <end_date> [options]

    start_date / end_date : "YYYY-MM-DD HH:MM" UTC (both inclusive)

Options
-------
    --step-hours N      Simulation step in hours (default: 1).
    --regions R1,R2     Comma-separated region names to run (overrides config).
    --log-dir PATH      Directory for per-step log files
                        (default: outputs/logs — writable under Docker/Apptainer).
    --quiet             Log to file only (no live terminal stream).
    --dry-run           Print commands without executing them.
    --stop-on-error     Abort the sequence on the first failed step.

Examples
--------
    # 24-hour event — all regions, default 1-hour step
    python hindcast_manager.py Caribbean_Comoros_config.py \\
        "2024-07-04 00:00" "2024-07-05 00:00"

    # Comoros-only IMERG+HSAF experiment
    python hindcast_manager.py Caribbean_Comoros_config.py \\
        "2024-07-04 00:00" "2024-07-05 00:00" --regions Comoros

    # Caribbean only, dry-run preview
    python hindcast_manager.py Caribbean_Comoros_config.py \\
        "2024-07-04 00:00" "2024-07-04 06:00" \\
        --regions Antigua,Barbados,Guatemala,Haiti --dry-run
"""

import argparse
import os
import sys
from datetime import datetime, timedelta
import subprocess


def _parse_dt(text: str, label: str) -> datetime:
    try:
        return datetime.strptime(text.strip(), "%Y-%m-%d %H:%M")
    except ValueError:
        raise ValueError(
            f"Invalid datetime for {label}: '{text}'. Expected format YYYY-MM-DD HH:MM"
        )


def _round_to_hour(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run TITO orchestrator in sequential hindcast mode over a date range.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "config",
        help="Path to TITO config file (e.g. Caribbean_Comoros_config.py)",
    )
    parser.add_argument(
        "start_date",
        help='Hindcast start datetime UTC, e.g. "2024-07-04 00:00"',
    )
    parser.add_argument(
        "end_date",
        help='Hindcast end datetime UTC, e.g. "2024-07-05 00:00"',
    )
    parser.add_argument(
        "--step-hours",
        type=float,
        default=1.0,
        metavar="N",
        help="Step size in hours between hindcast runs (default: 1)",
    )
    parser.add_argument(
        "--regions",
        default=None,
        metavar="R1,R2,...",
        help="Comma-separated list of regions to run (overrides config)",
    )
    parser.add_argument(
        "--log-dir",
        default=None,
        metavar="PATH",
        help="Directory for per-step logs (default: outputs/logs)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the commands that would be executed without running them",
    )
    parser.add_argument(
        "--stop-on-error",
        action="store_true",
        help="Abort the hindcast sequence on the first failed orchestrator step",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Do not stream orchestrator output to the terminal (log file only)",
    )
    args = parser.parse_args()

    start_dt = _round_to_hour(_parse_dt(args.start_date, "start_date"))
    end_dt   = _round_to_hour(_parse_dt(args.end_date,   "end_date"))
    if end_dt < start_dt:
        print(
            f"ERROR: end_date ({end_dt.strftime('%Y-%m-%d %H:%M')}) must be >= "
            f"start_date ({start_dt.strftime('%Y-%m-%d %H:%M')})",
            file=sys.stderr,
        )
        sys.exit(1)

    config_path = os.path.abspath(args.config)
    if not os.path.isfile(config_path):
        print(f"ERROR: Config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    # Default under outputs/ — /app itself is read-only inside Apptainer SIFs;
    # outputs/ is bind-mounted writable by tito-run.sh (Docker and Apptainer).
    log_dir = os.path.abspath(args.log_dir) if args.log_dir else os.path.abspath(
        os.path.join("outputs", "logs")
    )
    if not args.dry_run:
        os.makedirs(log_dir, exist_ok=True)

    step = timedelta(hours=args.step_hours)
    python_exe = sys.executable

    # Build the full list of simulation steps.
    steps = []
    current = start_dt
    while current <= end_dt:
        steps.append(current)
        current += step
    total = len(steps)

    # Print run summary.
    print("=" * 60)
    print("TITO Hindcast Manager")
    print("=" * 60)
    print(f"Config     : {config_path}")
    print(f"Start      : {start_dt.strftime('%Y-%m-%d %H:%M')} UTC")
    print(f"End        : {end_dt.strftime('%Y-%m-%d %H:%M')} UTC")
    print(f"Step       : {args.step_hours} h  ({total} step(s))")
    if args.regions:
        print(f"Regions    : {args.regions}")
    if args.dry_run:
        print("Mode       : DRY-RUN (no commands will be executed)")
    print("=" * 60)

    errors: list[tuple[str, int]] = []  # (date_str, exit_code)

    for idx, step_dt in enumerate(steps, 1):
        date_str = step_dt.strftime("%Y-%m-%d %H:%M")
        step_tag = step_dt.strftime("%Y%m%d_%H%M")
        log_file = os.path.join(log_dir, f"hindcast_{step_tag}.log")

        # Build the orchestrator command for this step.
        cmd = [python_exe, "orchestrator.py", config_path, "--hindcast-date", date_str]
        if args.regions:
            cmd += ["--regions", args.regions]

        print(f"\n[{idx:>3}/{total}] {date_str} UTC", end="")

        if args.dry_run:
            print(f"  [DRY-RUN]")
            print(f"    CMD : {' '.join(cmd)}")
            print(f"    LOG : {log_file}")
            continue

        print(f"  → {log_file}")
        if not args.quiet:
            print("-" * 60)

        with open(log_file, "w", encoding="utf-8") as log_fh:
            log_fh.write(f"=== TITO Hindcast Step {date_str} UTC ===\n")
            log_fh.flush()
            # Stream orchestrator stdout/stderr live to terminal AND the log file.
            # (Previously stdout went only to the log — terminal showed OK/FAILED only.)
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                log_fh.write(line)
                log_fh.flush()
                if not args.quiet:
                    print(line, end="", flush=True)
            returncode = proc.wait()

        if not args.quiet:
            print("-" * 60)

        if returncode != 0:
            print(
                f"    FAILED (exit code {returncode}). "
                f"See log for details: {log_file}"
            )
            errors.append((date_str, returncode))
            if args.stop_on_error:
                print("    --stop-on-error is set. Aborting.")
                break
        else:
            print(f"    OK  (step {idx}/{total})")

    # Final summary.
    print("\n" + "=" * 60)
    if args.dry_run:
        print(f"DRY-RUN complete.  {total} step(s) would be executed.")
        return

    if errors:
        print(f"Hindcast COMPLETED WITH ERRORS  ({len(errors)}/{total} steps failed)")
        print("Failed steps:")
        for date_str, code in errors:
            print(f"  - {date_str}  (exit code {code})")
        sys.exit(1)
    else:
        print(f"Hindcast COMPLETE.  All {total} steps succeeded.")


if __name__ == "__main__":
    main()
