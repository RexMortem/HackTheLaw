"""
quantum/constraints.py — turn "how big was the loss" into "how much is collectible".

Two-step legal layer, exactly as discussed:

  3a  CONTRACT — read the clauses the parties actually wrote (exclusion + cap).
  3b  LAW      — decide, probabilistically, whether each clause is enforceable
                 (UCTA 1977 reasonableness), and whether a fraud carve-out
                 (clause 14.3) blows the whole limitation away.

Output is not a single number but a probability-weighted recoverable, plus the
two point scenarios a litigator actually argues: "cap holds" (defendant's best)
and "fraud proven" (claimant's best).
"""
from __future__ import annotations

from typing import Optional

from .models import (
    CausationAdjustment,
    ContractClause,
    EnforceabilityView,
    FraudRoute,
    HeadOfLoss,
)


# --------------------------------------------------------------------------- #
#  Causation: strip out loss not caused by the defendant (pre-legal).
# --------------------------------------------------------------------------- #
def apply_causation(heads: list[HeadOfLoss],
                    adjustments: list[CausationAdjustment]) -> dict:
    """Reduce loss-of-profit heads by the causation deductions. Returns a dict of
    {basis_name: supportable_amount} plus a per-head breakdown. Deductions hit the
    loss-of-profit heads (the value/market/dcf side); cost basis is untouched."""
    total_deduction = sum(a.amount for a in adjustments)
    supportable, breakdown = {}, []
    for h in heads:
        raw = h.value.amount
        if h.is_loss_of_profit:
            adj = max(0.0, raw - total_deduction)
        else:
            adj = raw
        supportable[h.name] = adj
        breakdown.append({"head": h.name, "basis": h.basis.value,
                          "raw": raw, "supportable": adj,
                          "grounded": h.value.grounded})
    return {"supportable": supportable, "breakdown": breakdown,
            "total_deduction": total_deduction}


# --------------------------------------------------------------------------- #
#  3b: enforceability lookups (probabilistic legal judgement).
# --------------------------------------------------------------------------- #
def _p_upheld(clause_id: str, views: list[EnforceabilityView]) -> float:
    for v in views:
        if v.clause_id == clause_id:
            return v.p_upheld
    return 1.0  # no view supplied => treat the clause as written


# --------------------------------------------------------------------------- #
#  The scenario tree.
# --------------------------------------------------------------------------- #
def recoverable_under(
    wasted: float,
    loss_of_profit: float,
    cap: Optional[float],
    excl_upheld: bool,
    cap_upheld: bool,
    fraud: bool,
) -> float:
    """Recoverable in ONE fully-specified legal world.

    fraud  -> clause 14.3 removes BOTH the exclusion and the cap.
    else   -> exclusion (14.1) gates loss of profit; cap (14.2) bounds the total.
    Wasted expenditure is never loss of profit, so the exclusion never touches it.
    """
    if fraud:
        return wasted + loss_of_profit  # limitation falls away entirely

    lp_in = 0.0 if excl_upheld else loss_of_profit
    base = wasted + lp_in
    if cap_upheld and cap is not None:
        return min(base, cap)
    return base


def apply_constraints(
    supportable: dict,
    clauses: list[ContractClause],
    enforceability: list[EnforceabilityView],
    fraud: Optional[FraudRoute],
    *,
    wasted_name: str = "Wasted expenditure",
    lop_name: str = "Loss of profit",
) -> dict:
    """Run the full 3a+3b layer. Returns named point scenarios and the
    probability-weighted expected recoverable across the scenario tree."""
    wasted = supportable.get(wasted_name, 0.0)
    lop = supportable.get(lop_name, 0.0)

    excl = next((c for c in clauses if c.excludes_loss_of_profit), None)
    capc = next((c for c in clauses if c.caps_total), None)
    cap = capc.cap_value if capc else None

    p_excl = _p_upheld(excl.clause_id, enforceability) if excl else 0.0
    p_cap = _p_upheld(capc.clause_id, enforceability) if capc else 0.0
    pf = fraud.p_proven if fraud else 0.0

    # Point scenarios a litigator names out loud.
    scenarios = {
        "headline_uncapped": recoverable_under(
            wasted, lop, cap, excl_upheld=False, cap_upheld=False, fraud=False),
        "cap_and_exclusion_hold": recoverable_under(
            wasted, lop, cap, excl_upheld=True, cap_upheld=True, fraud=False),
        "exclusion_struck_cap_holds": recoverable_under(
            wasted, lop, cap, excl_upheld=False, cap_upheld=True, fraud=False),
        "fraud_proven": recoverable_under(
            wasted, lop, cap, excl_upheld=True, cap_upheld=True, fraud=True),
    }

    # Probability-weighted expectation over the independent legal questions.
    # Branch 1 (prob pf): fraud proven -> limitation gone.
    # Branch 2 (prob 1-pf): enumerate exclusion-upheld? x cap-upheld?.
    expected = pf * recoverable_under(
        wasted, lop, cap, excl_upheld=True, cap_upheld=True, fraud=True)
    for excl_up, p_e in ((True, p_excl), (False, 1 - p_excl)):
        for cap_up, p_c in ((True, p_cap), (False, 1 - p_cap)):
            prob = (1 - pf) * p_e * p_c
            expected += prob * recoverable_under(
                wasted, lop, cap,
                excl_upheld=excl_up, cap_upheld=cap_up, fraud=False)

    return {
        "scenarios": scenarios,
        "expected_recoverable": round(expected, 2),
        "probabilities": {"p_exclusion_upheld": p_excl,
                          "p_cap_upheld": p_cap, "p_fraud_proven": pf},
        "cap": cap,
    }
