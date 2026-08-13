"""Event rainfall totals per Area of Concern, per ensemble member.

The totals come from the EF5 PRECIPACCUM outputs that sit next to the UQ
grid in every run folder:
    qpeaccum.{cycle}.tif  observed part (e.g. SCaMPR warm-up window)
    qpfaccum.{cycle}.tif  forecast part (e.g. GFS 24 h)

total = stat(qpeaccum over AOC) + stat(qpfaccum over AOC)

Using the EF5 accumulations guarantees the totals are exactly the rain the
model saw when it produced the trigger, whatever QPE/QPF combination,
warm-up length or cycle count TITO is running that day.
"""

from dataclasses import dataclass, field

from .io_utils import read_grid


@dataclass
class MemberTotal:
    aoc_id: str
    member_id: str
    qpe_mm: float
    qpf_mm: float
    total_mm: float
    flags: list = field(default_factory=list)

    def to_dict(self):
        return {
            "aoc_id": self.aoc_id, "member_id": self.member_id,
            "qpe_mm": _r(self.qpe_mm), "qpf_mm": _r(self.qpf_mm),
            "total_mm": _r(self.total_mm), "flags": ";".join(self.flags),
        }


def _r(v):
    return None if v != v else round(v, 2)


def member_totals(runs, aocs, stat: str = "mean",
                  qpe_scale: float = 1.0, qpf_scale: float = 1.0,
                  missing_qpf: str = "skip_member") -> list:
    """Compute per (AOC, member) rainfall totals.

    missing_qpf:
      "skip_member" : a run without qpfaccum contributes nothing (default)
      "qpe_only"    : use the QPE part alone, flagged qpe_only
    Returns a list of MemberTotal.
    """
    out = []
    for run in runs:
        qpe_path = run.path("qpe_accum")
        qpf_path = run.path("qpf_accum")

        if not qpf_path and missing_qpf == "skip_member":
            continue

        for aoc in aocs:
            flags = []
            qpe_val = float("nan")
            qpf_val = float("nan")

            if qpe_path:
                qpe_val = aoc.zonal(read_grid(qpe_path, bounds=aoc.bounds), stat) * qpe_scale
            else:
                flags.append("missing_qpe")

            if qpf_path:
                qpf_val = aoc.zonal(read_grid(qpf_path, bounds=aoc.bounds), stat) * qpf_scale
            else:
                flags.append("qpe_only")

            parts = [v for v in (qpe_val, qpf_val) if v == v]
            total = sum(parts) if parts else float("nan")
            out.append(MemberTotal(aoc.aoc_id, run.member_id,
                                   qpe_val, qpf_val, total, flags))
    return out
