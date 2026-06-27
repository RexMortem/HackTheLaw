"""
quantum/bridge.py — connect the LLM pipeline and the HTTP layer to the calculator.

Two directions:

  from_spec(dict)  -> QuantumResult   : build inputs from plain JSON. The figures
                                        engine.py extracts already carry doc_id/para,
                                        so they drop straight in here.
  to_dict(result)  -> dict            : JSON-serialise a QuantumResult for the API
                                        / frontend (waterfall, scenarios, citations).

Nothing here calls a model; it is pure translation, so it runs without an API key.
"""
from __future__ import annotations

from .methods import cost_basis, dcf_basis, market_basis, value_basis
from .models import (
    Basis,
    CausationAdjustment,
    Cited,
    ContractClause,
    EnforceabilityView,
    FraudRoute,
    QuantumResult,
)
from .waterfall import compute


# --------------------------------------------------------------------------- #
#  dict -> inputs
# --------------------------------------------------------------------------- #
def _cited(d: dict | None, default_amount: float = 0.0) -> Cited:
    d = d or {}
    return Cited(
        amount=float(d.get("amount", default_amount)),
        doc_id=d.get("doc_id", ""),
        para=str(d.get("para", "")),
        quote=d.get("quote", ""),
        grounded=bool(d.get("grounded", bool(d.get("doc_id")))),
    )


def from_spec(spec: dict) -> QuantumResult:
    """Build a QuantumResult from a JSON spec. Shape (all keys optional except a
    wasted/loss-of-profit figure to compute anything meaningful):

        {
          "case": "...",
          "wasted":       {"amount":1800000,"doc_id":"GREENHALGH","para":"2","quote":"..."},
          "loss_of_profit":{"amount":4200000,"doc_id":"PoC","para":"15","quote":"..."},
          "market":       {"amount":..., ...}          # optional, grounded only if doc_id
          "dcf":          {"cash_flows":[...], "rate":0.10, "doc_id":"...","para":"..."},
          "causation":    [{"amount":1500000,"reason":"flood","doc_id":"...","para":"5","quote":"..."}],
          "clauses":      [{"clause_id":"14.1","summary":"...","excludes_loss_of_profit":true,
                            "doc_id":"MSA","para":"14.1","quote":"..."},
                           {"clause_id":"14.2","caps_total":true,"cap_value":1800000, ...}],
          "enforceability":[{"clause_id":"14.1","p_upheld":0.6,"rationale":"UCTA ..."}],
          "fraud":        {"p_proven":0.25,"summary":"...","doc_id":"PoC","para":"7","quote":"..."}
        }
    """
    heads = []
    if "wasted" in spec:
        heads.append(cost_basis(_cited(spec["wasted"])))
    if "loss_of_profit" in spec:
        heads.append(value_basis(_cited(spec["loss_of_profit"])))
    if "market" in spec:
        m = spec["market"]
        heads.append(market_basis(_cited(m) if m else None))
    else:
        heads.append(market_basis())
    if "dcf" in spec:
        d = spec["dcf"]
        heads.append(dcf_basis(d.get("cash_flows"), float(d.get("rate", 0.10)),
                               _cited(d) if d.get("doc_id") else None))
    else:
        heads.append(dcf_basis())

    causation = [
        CausationAdjustment(amount=float(a["amount"]), reason=a.get("reason", ""),
                            source=_cited(a, a.get("amount", 0.0)))
        for a in spec.get("causation", [])
    ]

    clauses = [
        ContractClause(
            clause_id=c["clause_id"], summary=c.get("summary", ""),
            source=_cited(c, c.get("cap_value", 0.0)),
            excludes_loss_of_profit=bool(c.get("excludes_loss_of_profit", False)),
            caps_total=bool(c.get("caps_total", False)),
            cap_value=(float(c["cap_value"]) if c.get("cap_value") is not None else None))
        for c in spec.get("clauses", [])
    ]

    enforceability = [
        EnforceabilityView(clause_id=e["clause_id"], p_upheld=float(e["p_upheld"]),
                           rationale=e.get("rationale", ""))
        for e in spec.get("enforceability", [])
    ]

    fraud = None
    if spec.get("fraud"):
        f = spec["fraud"]
        fraud = FraudRoute(p_proven=float(f["p_proven"]), summary=f.get("summary", ""),
                           source=_cited(f))

    return compute(spec.get("case", "Untitled matter"), heads, causation,
                   clauses, enforceability, fraud)


# --------------------------------------------------------------------------- #
#  QuantumResult -> dict
# --------------------------------------------------------------------------- #
def _cited_dict(c: Cited) -> dict:
    return {"amount": c.amount, "doc_id": c.doc_id, "para": c.para,
            "quote": c.quote, "grounded": c.grounded, "ref": c.ref}


def to_dict(r: QuantumResult) -> dict:
    return {
        "case": r.case,
        "headline": r.headline,
        "expected_recoverable": r.expected_recoverable,
        "scenarios": r.scenarios,
        "heads": [{"name": h.name, "basis": h.basis.value,
                   "is_loss_of_profit": h.is_loss_of_profit, "note": h.note,
                   "value": _cited_dict(h.value)} for h in r.heads],
        "causation": [{"amount": a.amount, "reason": a.reason,
                       "source": _cited_dict(a.source)} for a in r.causation],
        "clauses": [{"clause_id": c.clause_id, "summary": c.summary,
                     "excludes_loss_of_profit": c.excludes_loss_of_profit,
                     "caps_total": c.caps_total, "cap_value": c.cap_value,
                     "source": _cited_dict(c.source)} for c in r.clauses],
        "enforceability": [{"clause_id": e.clause_id, "p_upheld": e.p_upheld,
                            "rationale": e.rationale} for e in r.enforceability],
        "fraud": ({"p_proven": r.fraud.p_proven, "summary": r.fraud.summary,
                   "source": _cited_dict(r.fraud.source)} if r.fraud else None),
        "waterfall": [{"label": s.label, "amount": s.amount, "note": s.note}
                      for s in r.waterfall],
    }
