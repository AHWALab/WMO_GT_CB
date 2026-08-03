"""
Warmup decision + window helpers.

Extracted from orchestrator so warmup policy is unit-testable without
running EF5 or downloading precip.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from tito_utils.cycle.timeline import (
    STATE_LOOKBACK,
    WARMUP_STATE_OFFSET,
    DEFAULT_WARMUP_DAYS,
)


@dataclass(frozen=True)
class WarmupDecision:
    region: str
    needed: bool
    reason: str
    warmup_start: datetime
    warmup_end: datetime
    precip_source: str          # IMERG | HSAF
    is_stream_sat: bool


def warmup_window(
    cycle_time: datetime,
    warmup_days: int = DEFAULT_WARMUP_DAYS,
) -> tuple:
    """Return ``(warmup_start, warmup_end)`` for *cycle_time*."""
    end = cycle_time - WARMUP_STATE_OFFSET
    start = cycle_time - timedelta(days=int(warmup_days))
    return start, end


def decide_warmup(
    region: str,
    cycle_time: datetime,
    *,
    qpe_source: str,
    warmup_enabled: bool = True,
    warmup_days: int = DEFAULT_WARMUP_DAYS,
    warmup_precip_source: str = "IMERG",
    states_found: bool = False,
    states_time: Optional[datetime] = None,
) -> WarmupDecision:
    """
    Decide whether *region* needs a warmup for this cycle.

    ``states_found`` / ``states_time`` come from ``find_available_states``
    (or a test double).  STREAM_SAT regions are flagged so the runner can
    fan states out to ensemble member folders after warmup.
    """
    start, end = warmup_window(cycle_time, warmup_days)
    qpe = qpe_source.strip().upper()
    precip = (warmup_precip_source or "IMERG").strip().upper()
    if precip not in ("IMERG", "HSAF"):
        precip = "IMERG"

    if not warmup_enabled:
        return WarmupDecision(
            region=region, needed=False, reason="warmup_disabled",
            warmup_start=start, warmup_end=end, precip_source=precip,
            is_stream_sat=(qpe == "STREAM_SAT"),
        )

    if states_found:
        reason = (
            f"states_ok_at_{states_time.strftime('%Y%m%d_%H%M')}"
            if states_time is not None
            else "states_ok"
        )
        return WarmupDecision(
            region=region, needed=False, reason=reason,
            warmup_start=start, warmup_end=end, precip_source=precip,
            is_stream_sat=(qpe == "STREAM_SAT"),
        )

    return WarmupDecision(
        region=region, needed=True, reason="no_states_within_48h",
        warmup_start=start, warmup_end=end, precip_source=precip,
        is_stream_sat=(qpe == "STREAM_SAT"),
    )
