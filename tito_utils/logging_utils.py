"""
logging_utils.py
================
Rich-based terminal logging + file-log for detailed EF5 control file output.

Replaces bare ``print()`` calls with a styled Rich Console that separates
INFO / WARN / ERROR visually.  Detailed EF5 diagnostic messages (state
searches, control-file writing) are written to a per-run log file instead
of cluttering the terminal.

Usage::

    from tito_utils.logging_utils import console, setup_run_log

    console.rule("[bold]TITO Orchestrator[/]")
    console.info("Starting real-time cycle …")

    run_log = setup_run_log("outputs/stream_sat/ensOut1/antigua/20260513.210000")
    run_log.info("Writing control file …")
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

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

    # Redirect Python logging through Rich (needs raw Console, not wrapper)
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
            self._raw = rc  # exposed for RichHandler consumers

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
                print(f"\n{'─' * 10} {title} {'─' * (width - len(title) - 12)}")
            else:
                print("─" * width)

        def print(self, *args, **kwargs):
            print(*args, **kwargs)

        def log(self, *args, **kwargs):
            print(*args, **kwargs)

    _console = _TitoConsole()


# ── Public API ───────────────────────────────────────────────────────────

def get_console():
    """Return the shared Rich (or plain) console."""
    return _console


def setup_run_log(output_dir: str, name: str = "ef5_run") -> logging.Logger:
    """Create a file logger that writes to ``<output_dir>/<name>.log``.

    Parameters
    ----------
    output_dir : str
        Directory where the log file will be written.
    name : str
        Logger name (also used as the base filename).

    Returns
    -------
    logging.Logger
    """
    os.makedirs(output_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(output_dir, f"{name}_{timestamp}.log")

    logger = logging.getLogger(f"tito.{name}")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    # Remove existing handlers
    for h in list(logger.handlers):
        logger.removeHandler(h)

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger.addHandler(fh)

    # Also add Rich handler if available (for ERROR/CRITICAL to terminal)
    if _HAS_RICH and _RICH_RAW is not None:
        rh = RichHandler(console=_RICH_RAW, show_time=False, show_level=False,
                         show_path=False, markup=False)
        rh.setLevel(logging.WARNING)
        rh.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(rh)

    logger.info("Run log started — %s", log_path)
    return logger


# Alias for convenience
console = _console
