"""
quantum/waterfall.py — orchestrate methods -> causation -> legal constraints
into one auditable QuantumResult with a recoverability waterfall.

This is the layer the UI / report renders: a single chain from the headline
claim down to the probability-weighted recoverable, every step labelled.
"""
from __future__ import annotations

from typing import Optional

from .constraints import apply_causation, apply_constraints
from .models import (
    CausationAdjustment,
    ContractClause,
    EnforceabilityView,
    FraudRoute,
    HeadOfLoss,
    QuantumResult,
    WaterfallStep,
)


def compute(
    case: str,
    heads: list[HeadOfLoss],
    causation: list[CausationAdjustment],
    clauses: list[ContractClause],
    enforceability: list[EnforceabilityView],
    fraud: Optional[FraudRoute],
    *,
    wasted_name: str = "Wasted expenditure",
    lop_name: str = "Loss of profit",
) -> QuantumResult:
    caus = apply_causation(heads, causation)
    supportable = caus["supportable"]

    headline = supportable.get(wasted_name, 0.0) + supportable.get(lop_name, 0.0)

    legal = apply_constraints(
        supportable, clauses, enforceability, fraud,
        wasted_name=wasted_name, lop_name=lop_name)

    # Build the human-readable waterfall.
    pleaded = sum(h.value.amount for h in heads
                  if h.value.grounded and h.name in (wasted_name, lop_name))
    steps = [
        WaterfallStep("Pleaded (cost + value)", pleaded,
                      "As claimed in the Particulars."),
        WaterfallStep("Less causation", -caus["total_deduction"],
                      "Loss not attributable to the defendant."),
        WaterfallStep("Supportable (pre-legal)", headline,
                      "Size of the harm, before recoverability."),
        WaterfallStep("If exclusion + cap hold",
                      legal["scenarios"]["cap_and_exclusion_hold"],
                      "Loss of profit excluded; total capped."),
        WaterfallStep("If fraud proven (carve-out)",
                      legal["scenarios"]["fraud_proven"],
                      "Limitation falls away — full supportable loss."),
        WaterfallStep("Expected recoverable (weighted)",
                      legal["expected_recoverable"],
                      "Probability-weighted across the legal scenario tree."),
    ]

    return QuantumResult(
        case=case,
        heads=heads,
        causation=causation,
        clauses=clauses,
        enforceability=enforceability,
        fraud=fraud,
        headline=round(headline, 2),
        scenarios=legal["scenarios"],
        expected_recoverable=legal["expected_recoverable"],
        waterfall=steps,
    )


# --------------------------------------------------------------------------- #
#  Rendering helpers.
# --------------------------------------------------------------------------- #
def _gbp(x: float) -> str:
    sign = "-" if x < 0 else ""
    return f"{sign}£{abs(x):,.0f}"


def render_text(r: QuantumResult) -> str:
    L = [f"QUANTUM ASSESSMENT — {r.case}", "=" * 60, "", "Heads of loss:"]
    for h in r.heads:
        flag = "" if h.value.grounded else "  [ungrounded — excluded]"
        L.append(f"  • {h.name:24s} [{h.basis.value:6s}] "
                 f"{_gbp(h.value.amount):>12s}  ({h.value.ref}){flag}")

    L += ["", "Causation deductions:"]
    for a in r.causation:
        L.append(f"  − {_gbp(a.amount):>12s}  {a.reason}  ({a.source.ref})")

    L += ["", "Legal constraints (3a contract → 3b enforceability):"]
    for c in r.clauses:
        bits = []
        if c.excludes_loss_of_profit:
            bits.append("excludes loss of profit")
        if c.caps_total:
            bits.append(f"caps at {_gbp(c.cap_value or 0)}")
        p = next((v.p_upheld for v in r.enforceability
                  if v.clause_id == c.clause_id), 1.0)
        L.append(f"  • {c.clause_id}: {', '.join(bits)}  "
                 f"— P(upheld)={p:.0%}  ({c.source.ref})")
    if r.fraud:
        L.append(f"  • Fraud carve-out: P(proven)={r.fraud.p_proven:.0%} "
                 f"→ removes exclusion AND cap  ({r.fraud.source.ref})")

    L += ["", "Recoverability waterfall:"]
    for s in r.waterfall:
        L.append(f"  {s.label:34s} {_gbp(s.amount):>14s}   {s.note}")

    L += ["", f"  EXPECTED RECOVERABLE: {_gbp(r.expected_recoverable)}",
          "  (vs ~£6,000,000 pleaded)"]
    return "\n".join(L)
