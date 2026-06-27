"""
quantum/case_meridian.py — Meridian Retail Group plc v TechFlow Solutions Ltd.

The bundle-grounded inputs for the quantum calculation. Every monetary figure
cites a document; every probability is flagged as a LEGAL-JUDGEMENT input, not a
fact from the bundle. Swap these out (or feed them from engine.py's extracted
figures) to run the calculator on a different case.

Sources:
  PoC        = 02_Particulars_of_Claim.txt
  MSA        = 03_Master_Services_Agreement.txt
  GREENHALGH = 20_Expert_Report_Greenhalgh_Quantum.txt
"""
from __future__ import annotations

from . import methods
from .models import (
    CausationAdjustment,
    Cited,
    ContractClause,
    EnforceabilityView,
    FraudRoute,
)
from .waterfall import compute, render_text

CASE = "Meridian Retail Group plc v TechFlow Solutions Ltd [HT-2025-000231]"

# ---- Heads of loss (grounded in the bundle) ------------------------------- #
HEADS = [
    methods.cost_basis(Cited(
        amount=1_800_000, doc_id="GREENHALGH", para="2",
        quote="pleaded wasted expenditure of £1,800,000 is consistent with the "
              "Claimant's accounting records")),
    methods.value_basis(Cited(
        amount=4_200_000, doc_id="PoC", para="15",
        quote="Loss of profit during the peak trading period of November and "
              "December 2024: £4,200,000")),
    # Market and DCF: no comparable / projection data in the bundle -> ungrounded,
    # kept out of the headline. Wiring points for real inputs.
    methods.market_basis(),
    methods.dcf_basis(),
]

# ---- Causation deductions (Greenhalgh) ------------------------------------ #
# £4.2m pleaded -> £1.3m supportable: £1.5m non-Platform + £1.4m unsupported.
CAUSATION = [
    CausationAdjustment(
        amount=1_500_000,
        reason="Lutterworth DC flood + sector-wide downturn (pre-go-live)",
        source=Cited(amount=1_500_000, doc_id="GREENHALGH", para="5",
                     quote="approximately £1,500,000 of the claimed decline is "
                           "attributable to these non-Platform factors")),
    CausationAdjustment(
        amount=1_400_000,
        reason="Balance unsupported on the material seen",
        source=Cited(amount=1_400_000, doc_id="GREENHALGH", para="4",
                     quote="incremental lost gross profit capable of being "
                           "attributed to the Platform's defects is of the order "
                           "of £1,300,000, not £4,200,000")),
]

# ---- 3a: contract clauses (MSA clause 14) --------------------------------- #
CLAUSES = [
    ContractClause(
        clause_id="14.1",
        summary="Excludes liability for loss of profit / consequential loss.",
        source=Cited(amount=0, doc_id="MSA", para="14.1",
                     quote="Subject to clause 14.3, the Supplier shall not be "
                           "liable to the Customer, whether in contract, tort "
                           "(including negligence) or otherwise, for any loss "
                           "of profit"),
        excludes_loss_of_profit=True),
    ContractClause(
        clause_id="14.2",
        summary="Caps total liability at charges actually paid (~£1.8m).",
        source=Cited(amount=1_800_000, doc_id="MSA", para="14.2",
                     quote="Subject to clause 14.3, the Supplier’s total "
                           "aggregate liability arising under or in connection "
                           "with this Agreement shall not exceed the total "
                           "charges actually paid by the Customer under this "
                           "Agreement in the twelve months preceding the event "
                           "giving rise to the claim"),
        caps_total=True, cap_value=1_800_000),
]

# ---- 3b: enforceability — LEGAL JUDGEMENT inputs (tune these) -------------- #
# Probabilities a litigator sets, NOT facts from the bundle. UCTA 1977 s.3/s.11
# reasonableness governs whether each B2B exclusion/cap survives challenge.
ENFORCEABILITY = [
    EnforceabilityView(clause_id="14.1", p_upheld=0.60,
                       rationale="UCTA s.3/s.11 reasonableness; standard B2B "
                                 "exclusion of profit loss — likely but not certain"),
    EnforceabilityView(clause_id="14.2", p_upheld=0.70,
                       rationale="UCTA reasonableness; cap at price paid is a "
                                 "commonly upheld form of cap"),
]

# ---- 14.3 fraud carve-out (the route that unlocks the cap) ---------------- #
FRAUD = FraudRoute(
    p_proven=0.25,
    summary="Pleaded misrepresentation (Frost: 10,000 concurrent transactions). "
            "If fraudulent, clause 14.3 removes BOTH exclusion and cap.",
    source=Cited(amount=0, doc_id="PoC", para="7",
                 quote="orally represented to Meridian that the Platform would "
                       "reliably support at least 10,000 concurrent transactions"))


def result():
    return compute(CASE, HEADS, CAUSATION, CLAUSES, ENFORCEABILITY, FRAUD)


if __name__ == "__main__":
    print(render_text(result()))
