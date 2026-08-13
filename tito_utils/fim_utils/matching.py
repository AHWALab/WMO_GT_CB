"""Matching rules: from a rainfall total to a catalog storm.

The rules are deliberately loose because ~200 scenarios cannot cover every
event. They widen until they find a match, and always round up:

  1. candidates with magnitude in [band[0]*T, band[1]*T]
     (and direction within tolerance, when enabled and available)
  2. none -> widen to band_wide, keep direction
  3. none -> band_wide without the direction condition
  4. none and T above the largest storm -> two largest storms,
     flag beyond_catalog
  5. none and T below the smallest storm -> smallest storm,
     flag below_catalog
  Selection within candidates: the storm closest to T from above
  (round up). If every candidate is below T, the largest candidate is
  used and flagged rounded_down.

Every decision records which rule fired, for the decision log.
"""

from dataclasses import dataclass, field

from .catalog import Catalog, circular_diff_deg


@dataclass
class MatchRules:
    band: tuple = (0.9, 1.2)
    band_wide: tuple = (0.8, 1.3)
    use_direction: bool = False
    direction_tolerance_deg: float = 45.0
    rounding: str = "up"

    @classmethod
    def from_config(cls, m):
        return cls(band=tuple(m.band), band_wide=tuple(m.band_wide),
                   use_direction=bool(m.use_direction),
                   direction_tolerance_deg=float(m.direction_tolerance_deg),
                   rounding=m.rounding)


@dataclass
class MatchDecision:
    aoc_id: str
    member_id: str
    total_mm: float
    storm_id: str = ""
    storm_magnitude_mm: float = float("nan")
    alt_storm_id: str = ""            # second storm when beyond catalog
    rule_applied: str = ""            # band | band_wide | no_direction | beyond_catalog | below_catalog | no_total
    n_candidates: int = 0
    flags: list = field(default_factory=list)

    def to_dict(self):
        return {
            "aoc_id": self.aoc_id, "member_id": self.member_id,
            "total_mm": None if self.total_mm != self.total_mm else round(self.total_mm, 2),
            "storm_id": self.storm_id,
            "storm_magnitude_mm": None if self.storm_magnitude_mm != self.storm_magnitude_mm
                                  else round(self.storm_magnitude_mm, 2),
            "alt_storm_id": self.alt_storm_id,
            "rule_applied": self.rule_applied,
            "n_candidates": self.n_candidates,
            "flags": ";".join(self.flags),
        }


def _pick_round_up(cands, total):
    above = cands[cands["magnitude_mm"] >= total]
    if len(above):
        row = above.sort_values("magnitude_mm").iloc[0]
        return row, []
    row = cands.sort_values("magnitude_mm").iloc[-1]
    return row, ["rounded_down"]


def match_total(total_mm: float, catalog: Catalog, aoc_id: str = None,
                direction_deg: float = float("nan"),
                rules: MatchRules = None,
                member_id: str = "", ) -> MatchDecision:
    """Apply the matching rules for one rainfall total."""
    rules = rules or MatchRules()
    decision = MatchDecision(aoc_id=str(aoc_id), member_id=member_id, total_mm=total_mm)

    if total_mm != total_mm:
        decision.rule_applied = "no_total"
        decision.flags.append("missing_total")
        return decision

    table = catalog.magnitudes(aoc_id)
    if not len(table):
        decision.rule_applied = "no_total"
        decision.flags.append("empty_catalog")
        return decision

    def band_filter(band, with_direction):
        lo, hi = band[0] * total_mm, band[1] * total_mm
        sub = table[(table["magnitude_mm"] >= lo) & (table["magnitude_mm"] <= hi)]
        if with_direction and rules.use_direction and direction_deg == direction_deg:
            keep = sub["direction_deg"].map(
                lambda d: circular_diff_deg(d, direction_deg) <= rules.direction_tolerance_deg
                if d == d else False)
            sub = sub[keep]
        return sub

    tiers = [
        ("band", rules.band, True),
        ("band_wide", rules.band_wide, True),
        ("no_direction", rules.band_wide, False),
    ]
    # Without direction in play, tier 3 duplicates tier 2; harmless.
    for rule_name, band, with_dir in tiers:
        cands = band_filter(band, with_dir)
        if len(cands):
            row, extra = _pick_round_up(cands, total_mm)
            decision.storm_id = str(row["storm_id"])
            decision.storm_magnitude_mm = float(row["magnitude_mm"])
            decision.rule_applied = rule_name
            decision.n_candidates = int(len(cands))
            decision.flags.extend(extra)
            return decision

    # Nothing in any band: outside the catalog range
    ordered = table.sort_values("magnitude_mm")
    if total_mm > float(ordered["magnitude_mm"].iloc[-1]):
        top = ordered.iloc[-1]
        second = ordered.iloc[-2] if len(ordered) > 1 else ordered.iloc[-1]
        decision.storm_id = str(top["storm_id"])
        decision.storm_magnitude_mm = float(top["magnitude_mm"])
        decision.alt_storm_id = str(second["storm_id"])
        decision.rule_applied = "beyond_catalog"
        decision.flags.append("beyond_catalog")
    else:
        low = ordered.iloc[0]
        decision.storm_id = str(low["storm_id"])
        decision.storm_magnitude_mm = float(low["magnitude_mm"])
        decision.rule_applied = "below_catalog"
        decision.flags.append("below_catalog")
    return decision


def match_members(totals, catalog: Catalog, rules: MatchRules = None,
                  directions: dict = None) -> list:
    """Match every (AOC, member) total. directions: member_id -> deg (optional)."""
    rules = rules or MatchRules()
    directions = directions or {}
    decisions = []
    for t in totals:
        decisions.append(match_total(
            t.total_mm, catalog, aoc_id=t.aoc_id,
            direction_deg=directions.get(t.member_id, float("nan")),
            rules=rules, member_id=t.member_id))
    return decisions


def select_scenarios(decisions, selectors: dict = None) -> dict:
    """Reduce per-member matches to named selections per AOC.

    selectors: name -> percentile of the member totals (default best=50,
    upper=90). Uses nearest-rank on the members that have a valid total.
    Returns {aoc_id: {name: MatchDecision}}.
    """
    selectors = selectors or {"best": 50, "upper": 90}
    by_aoc = {}
    for d in decisions:
        if d.total_mm == d.total_mm and d.storm_id:
            by_aoc.setdefault(d.aoc_id, []).append(d)

    import math
    out = {}
    for aoc_id, ds in by_aoc.items():
        ds_sorted = sorted(ds, key=lambda d: d.total_mm)
        n = len(ds_sorted)
        out[aoc_id] = {}
        for name, pct in selectors.items():
            rank = max(1, min(n, math.ceil(pct / 100.0 * n)))  # nearest-rank
            out[aoc_id][name] = ds_sorted[rank - 1]
    return out
