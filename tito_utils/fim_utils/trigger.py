"""Trigger: EF5 maximum unit streamflow inside the Areas of Concern.

Rule (as agreed): if ANY pixel of maxunitq inside an AOC exceeds the
threshold (default 1.0 m3/s/km2), the lookup procedure starts for that
AOC. Reads are windowed to the AOC bounding box so the check stays fast
even on the Guatemala and Haiti domains.
"""

from dataclasses import dataclass, field

from .io_utils import read_grid


@dataclass
class TriggerReport:
    threshold: float
    max_uq: dict = field(default_factory=dict)      # (aoc_id, member_id) -> value
    triggered_aocs: list = field(default_factory=list)
    triggered: bool = False

    def aoc_max(self, aoc_id: str) -> float:
        vals = [v for (a, _m), v in self.max_uq.items()
                if a == aoc_id and v == v]  # v == v filters NaN
        return max(vals) if vals else float("nan")

    def to_dict(self) -> dict:
        return {
            "threshold": self.threshold,
            "triggered": self.triggered,
            "triggered_aocs": self.triggered_aocs,
            "max_uq": {f"{a}|{m}": (None if v != v else round(v, 4))
                       for (a, m), v in self.max_uq.items()},
        }


def evaluate_trigger(runs, aocs, threshold: float = 1.0,
                     scope: str = "any_member", fast: bool = False) -> TriggerReport:
    """Check maxunitq against the threshold inside each AOC.

    runs  : list of EF5Run (each one ensemble member)
    aocs  : list of AreaOfConcern
    scope : "any_member" (one member above threshold is enough, default)
            or "all_members"
    fast  : stop at the first triggering AOC (skips filling the full report)
    """
    report = TriggerReport(threshold=threshold)
    per_aoc_hits = {}

    for run in runs:
        uq_path = run.path("uq")
        if not uq_path:
            continue
        for aoc in aocs:
            grid = read_grid(uq_path, bounds=aoc.bounds)
            value = aoc.zonal(grid, "max")
            report.max_uq[(aoc.aoc_id, run.member_id)] = value
            hit = (value == value) and value >= threshold
            per_aoc_hits.setdefault(aoc.aoc_id, []).append(bool(hit))
            if fast and hit and scope == "any_member":
                report.triggered_aocs = [aoc.aoc_id]
                report.triggered = True
                return report

    for aoc_id, hits in per_aoc_hits.items():
        ok = any(hits) if scope == "any_member" else (len(hits) > 0 and all(hits))
        if ok:
            report.triggered_aocs.append(aoc_id)

    report.triggered = len(report.triggered_aocs) > 0
    return report
