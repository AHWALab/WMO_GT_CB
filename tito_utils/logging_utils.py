"""
logging_utils.py
================
Rich-based terminal logging + file-log for detailed EF5 control file output.

Console verbosity (user-facing vs developer)::

    # Caribbean_Comoros_config.py
    console_verbosity = "user"   # default — simplified operator view
    console_verbosity = "debug"  # full developer diagnostics

    # or at runtime:
    export TITO_CONSOLE_VERBOSITY=debug

Usage::

    from tito_utils.logging_utils import (
        console, setup_run_log, configure_console_verbosity,
        suppress_third_party_noise, is_debug, user_print, debug_print,
        progress_line, progress_done,
    )

    configure_console_verbosity("user")
    suppress_third_party_noise()
"""

from __future__ import annotations

import logging
import os
import re
import sys
import threading
import warnings
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── Verbosity ───────────────────────────────────────────────────────────
# "user"  = simplified operator console (default)
# "debug" = full developer diagnostics
_VERBOSITY = os.environ.get("TITO_CONSOLE_VERBOSITY", "user").strip().lower()
if _VERBOSITY not in ("user", "debug"):
    _VERBOSITY = "user"

_PROGRESS_LOCK = threading.Lock()
_PROGRESS_ACTIVE = False


def configure_console_verbosity(level: Optional[str] = None) -> str:
    """Set console verbosity. Env ``TITO_CONSOLE_VERBOSITY`` wins if set."""
    global _VERBOSITY
    env = os.environ.get("TITO_CONSOLE_VERBOSITY", "").strip().lower()
    if env in ("user", "debug"):
        _VERBOSITY = env
    elif level is not None:
        lvl = str(level).strip().lower()
        if lvl in ("user", "debug", "verbose", "developer"):
            _VERBOSITY = "debug" if lvl in ("debug", "verbose", "developer") else "user"
    return _VERBOSITY


def get_console_verbosity() -> str:
    return _VERBOSITY


def is_debug() -> bool:
    return _VERBOSITY == "debug"


def is_user() -> bool:
    return _VERBOSITY != "debug"


def suppress_third_party_noise() -> None:
    """Silence common GDAL / TIFF / OpenMP noise on the console.

    Safe to call multiple times.  Full EF5 logs still go to per-run log files.
    """
    # GDAL / CPL
    os.environ.setdefault("CPL_LOG", "/dev/null")
    os.environ.setdefault("CPL_LOG_ERRORS", "OFF")
    os.environ.setdefault("GDAL_PAM_ENABLED", "NO")
    # OpenMP affinity spam (libgomp) when bind is unsupported
    os.environ.setdefault("OMP_DISPLAY_ENV", "FALSE")
    if "OMP_PROC_BIND" not in os.environ:
        os.environ["OMP_PROC_BIND"] = "false"
    if "OMP_PLACES" in os.environ and os.environ.get("OMP_PROC_BIND", "").lower() == "false":
        # places without bind is harmless; leave as-is
        pass

    warnings.filterwarnings("ignore", category=FutureWarning, module=r"osgeo\.gdal")
    warnings.filterwarnings("ignore", message=r".*gdal\.UseExceptions.*")
    warnings.filterwarnings("ignore", message=r".*legacy Deflate codec.*")
    warnings.filterwarnings("ignore", message=r".*COMPRESSION_ADOBE_DEFLATE.*")

    try:
        from osgeo import gdal
        gdal.UseExceptions()
        try:
            gdal.PushErrorHandler("CPLQuietErrorHandler")
        except Exception:
            pass
        # Quiet CPL error stack to console
        try:
            gdal.SetConfigOption("CPL_LOG", "/dev/null")
            gdal.SetConfigOption("CPL_DEBUG", "OFF")
        except Exception:
            pass
    except ImportError:
        pass


def user_print(*args, **kwargs) -> None:
    """Always print (operator-facing)."""
    kwargs.setdefault("flush", True)
    print(*args, **kwargs)


def debug_print(*args, **kwargs) -> None:
    """Print only in debug verbosity."""
    if is_debug():
        kwargs.setdefault("flush", True)
        print(*args, **kwargs)


def progress_line(msg: str) -> None:
    """Single-line in-place progress (TTY). Falls back to plain print if not a TTY."""
    global _PROGRESS_ACTIVE
    text = msg.rstrip("\n")
    with _PROGRESS_LOCK:
        if sys.stdout.isatty() and is_user():
            # pad to clear previous longer lines
            sys.stdout.write("\r" + text + "\033[K")
            sys.stdout.flush()
            _PROGRESS_ACTIVE = True
        else:
            # non-TTY (hindcast log redirect) or debug: one line per update would
            # flood files — throttle is caller's job; still use single line end.
            if is_user() and not sys.stdout.isatty():
                # In redirected logs, overwrite style doesn't work; print sparingly
                # only when caller chooses cadence. Still print the line once.
                print(text, flush=True)
            else:
                print(text, flush=True)
            _PROGRESS_ACTIVE = False


def progress_done(final_msg: Optional[str] = None) -> None:
    """Finish an in-place progress line (newline)."""
    global _PROGRESS_ACTIVE
    with _PROGRESS_LOCK:
        if final_msg is not None:
            if sys.stdout.isatty() and is_user() and _PROGRESS_ACTIVE:
                sys.stdout.write("\r" + final_msg.rstrip("\n") + "\033[K\n")
                sys.stdout.flush()
            else:
                print(final_msg.rstrip("\n"), flush=True)
        elif _PROGRESS_ACTIVE and sys.stdout.isatty():
            sys.stdout.write("\n")
            sys.stdout.flush()
        _PROGRESS_ACTIVE = False


# ── Subprocess line filters (user mode) ─────────────────────────────────

_SS_KEEP = re.compile(
    r"(Output window|Ingest window|Ensemble size|STREAM-Sat dir|Scratch|"
    r"\[IMERG\(PPS\)\]|Downloaded \d|\[IMERG\(PPS\)\] done|"
    r"Downloading GFS|GFS progress:|GFS grid:|\[GFS|Saved .*GFS|Output: .*GFS|"
    r"Running Semi-Lagrangian|Generating \d+-member|"
    r"done in |ALL GFS wind|GFS wind files failed)",
    re.I,
)
# Always hide these noisy lines in user mode (still in pipeline log file)
_SS_HIDE = re.compile(
    r"(HTTPSConnectionPool|Max retries exceeded|NameResolutionError|"
    r"Failed to resolve|Temporary failure in name resolution|"
    r"urllib3\.connection|retry \d+/\d+|Connection aborted|"
    r"ConnectionResetError|Read timed out|Connection refused|"
    r"Trying AWS|trying NOMADS|trying Google|\[cached\]|"
    r"\[AWS\]|\[NOMADS\]|\[GCS\]|nomads\.ncep\.noaa\.gov|"
    r"filter_gfs_0p25|pgrb2\.0p25)",
    re.I,
)
_SS_DROP = re.compile(
    r"^(Pysteps configuration|0\.\d+|^\d+$|^\d{4}-\d{2}-\d{2}$)",
    re.I,
)

_SL_KEEP = re.compile(
    r"(GEFS ensemble cycle|\[ge[pc]\d+\] fetching|combined ensemble|"
    r"total wall-clock|StormLab|ERROR|Error|FAILED|Failed|cycle )",
    re.I,
)


def filter_streamsat_line(line: str) -> Optional[str]:
    """Return a cleaned operator line for STREAM-Sat, or None to hide.

    Special return prefix ``\\r`` means the caller should use an in-place
    progress line (GFS/IMERG download counters).
    """
    s = line.rstrip()
    if not s:
        return None
    # strip leading timestamps like "18:52:57 INFO "
    s2 = re.sub(r"^\d{2}:\d{2}:\d{2}\s+(INFO|WARNING|ERROR|DEBUG)\s+", "", s)
    s2 = re.sub(r"^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\s+\w+\s+", "", s2)
    s2 = s2.strip()

    # Drop network/retry spam always in user mode (still in pipeline log)
    if not is_debug() and _SS_HIDE.search(s2):
        return None
    # Explicit Error: HTTPS… lines
    if not is_debug() and re.match(r"Error:\s*HTTPS", s2, re.I):
        return None
    if not is_debug() and "Max retries exceeded" in s2:
        return None

    if _SS_DROP.search(s2) and "Semi-Lagrangian" not in s2 and "Generating" not in s2:
        # bare floats / dates from Semi-Lagrangian noise
        if re.match(r"^[\d.]+$", s2.strip()):
            return None
        if re.match(r"^\d{4}-\d{2}-\d{2}", s2.strip()) and "window" not in s2.lower():
            return None
    if "Running Semi-Lagrangian Scheme" in s2:
        return "    Running Semi-Lagrangian Scheme …"
    if re.search(r"Generating\s+(\d+)-member", s2, re.I):
        return f"    {s2.strip()}"
    # GFS download progress → in-place single line
    m_gfs = re.search(
        r"GFS progress:\s*(\d+)/(\d+)\s*\(ok=(\d+)\s*fail=(\d+)\)", s2, re.I)
    if m_gfs:
        return (
            f"\r    GFS winds: {m_gfs.group(1)}/{m_gfs.group(2)} "
            f"(ok={m_gfs.group(3)} fail={m_gfs.group(4)})"
        )
    if "Downloading GFS" in s2:
        return f"    {s2.strip()}"
    if re.search(r"Downloaded\s+\d+/\d+", s2):
        # IMERG PPS progress
        m = re.search(r"Downloaded\s+(\d+)/(\d+)", s2)
        if m:
            return f"\r    IMERG: {m.group(1)}/{m.group(2)} files"
        return f"    IMERG: {s2.strip()}"
    if "[IMERG(PPS)] done" in s2:
        return f"    {s2.strip()}"
    if re.search(r"Output window:|Ingest window:|Ensemble size:", s2):
        return f"    {s2.strip()}"
    if re.search(r"\[GFS.*\] done|GFS grid:", s2):
        return f"    {s2.strip()}"
    if re.search(r"ALL GFS wind|GFS wind files failed", s2, re.I):
        return f"    {s2.strip()}"
    if is_debug():
        return s
    if _SS_KEEP.search(s2):
        # skip the long raw fetch_imerg command line in user mode
        if "[IMERG(PPS)]" in s2 and "fetch_imerg" in s2:
            return "    IMERG (PPS): downloading …"
        if "[GFS→MV]" in s2 and "--gfs" in s2:
            return "    GFS → motion vectors …"
        return f"    {s2.strip()}"
    return None


def filter_stormlab_line(line: str) -> Optional[str]:
    """Return a cleaned operator line for StormLab, or None to hide."""
    s = line.rstrip()
    if not s:
        return None
    s2 = re.sub(r"^\d{2}:\d{2}:\d{2}\s+(INFO|WARNING|ERROR|DEBUG)\s+", "", s)
    if is_debug():
        return s
    if re.search(r"\[ge[pc]\d+\]\s+fetching", s2, re.I):
        return f"    GEFS: {s2.strip()}"
    if "GEFS ensemble cycle" in s2:
        return f"    {s2.strip()}"
    if "combined ensemble" in s2:
        return f"    {s2.strip()}"
    if "total wall-clock" in s2:
        return f"    {s2.strip()}"
    if re.search(r"ERROR|Failed|FAILED", s2):
        return f"    {s2.strip()}"
    if _SL_KEEP.search(s2) and "cmd:" not in s2 and "python=" not in s2:
        return f"    {s2.strip()}"
    return None


# ── Rich imports (graceful fallback) ────────────────────────────────────
try:
    from rich.console import Console as _RichConsole
    from rich.logging import RichHandler
    from rich.theme import Theme
    from rich.traceback import install as _install_tb

    _RICH_THEME = Theme({
        "logging.level.info": "dim cyan",
        "logging.level.warning": "yellow",
        "logging.level.error": "bold red",
        "logging.level.critical": "bold white on red",
    })

    _rich = _RichConsole(theme=_RICH_THEME, highlight=False)
    _install_tb(show_locals=False, width=None)

    _rich_handler = RichHandler(
        console=_rich,
        show_time=True,
        show_level=True,
        show_path=False,
        markup=True,
        rich_tracebacks=True,
    )
    _rich_handler.setFormatter(logging.Formatter("%(message)s", datefmt="%H:%M:%S"))

    class _TitoConsole:
        """Wrapper adding .info/.warning/.error helpers to Rich Console."""
        def __init__(self, rc):
            self._rc = rc
            self._raw = rc

        def info(self, *args, **kwargs):
            self._rc.print(*args, **kwargs)

        def warning(self, *args, **kwargs):
            self._rc.print("[yellow][WARN][/]", *args, **kwargs)

        def error(self, *args, **kwargs):
            self._rc.print("[bold red][ERROR][/]", *args, **kwargs)

        def rule(self, title="", **kwargs):
            self._rc.rule(title, **kwargs)

        def print(self, *args, **kwargs):
            self._rc.print(*args, **kwargs)

        def log(self, *args, **kwargs):
            self._rc.log(*args, **kwargs)

        def debug(self, *args, **kwargs):
            if is_debug():
                self._rc.print(*args, **kwargs)

    _console = _TitoConsole(_rich)
    _RICH_RAW = _rich
    _HAS_RICH = True

except ImportError:
    _HAS_RICH = False
    _RICH_RAW = None

    class _TitoConsole:
        """Minimal console mimicking Rich API with plain print."""
        def info(self, *args, **kwargs):
            print(*args, **kwargs)

        def warning(self, *args, **kwargs):
            print("[WARN]", *args, **kwargs)

        def error(self, *args, **kwargs):
            print("[ERROR]", *args, **kwargs)

        def rule(self, title="", **kwargs):
            width = kwargs.get("width", 70)
            if title:
                print(f"\n{'─' * 10} {title} {'─' * max(1, width - len(str(title)) - 12)}")
            else:
                print("─" * width)

        def print(self, *args, **kwargs):
            print(*args, **kwargs)

        def log(self, *args, **kwargs):
            print(*args, **kwargs)

        def debug(self, *args, **kwargs):
            if is_debug():
                print(*args, **kwargs)

    _console = _TitoConsole()


def get_console():
    """Return the shared Rich (or plain) console."""
    return _console


def setup_run_log(output_dir: str, name: str = "ef5_run") -> logging.Logger:
    """Create a file logger that writes to ``<output_dir>/<name>.log``."""
    os.makedirs(output_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(output_dir, f"{name}_{timestamp}.log")

    logger = logging.getLogger(f"tito.{name}")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    for h in list(logger.handlers):
        logger.removeHandler(h)

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger.addHandler(fh)

    # Terminal: only WARNING+ in user mode; INFO+ in debug
    if _HAS_RICH and _RICH_RAW is not None:
        rh = RichHandler(console=_RICH_RAW, show_time=False, show_level=False,
                         show_path=False, markup=False)
        rh.setLevel(logging.DEBUG if is_debug() else logging.WARNING)
        rh.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(rh)

    logger.info("Run log started — %s", log_path)
    return logger


console = _console
