"""
quantum — damages calculation module for Crucible.

Self-contained, pure-stdlib (no API key, no model call). Computes quantum four
ways (cost / value / market / DCF), strips causation, then runs the two-step
legal-constraint layer (3a contract clauses -> 3b enforceability) to turn "size
of the harm" into "probability-weighted recoverable".

    from quantum.case_meridian import result
    from quantum.waterfall import render_text
    print(render_text(result()))

Or:  python -m quantum
"""
from .models import (
    Basis,
    CausationAdjustment,
    Cited,
    ContractClause,
    EnforceabilityView,
    FraudRoute,
    HeadOfLoss,
    QuantumResult,
)
from .waterfall import compute, render_text

__all__ = [
    "Basis", "Cited", "HeadOfLoss", "CausationAdjustment", "ContractClause",
    "EnforceabilityView", "FraudRoute", "QuantumResult", "compute", "render_text",
]
