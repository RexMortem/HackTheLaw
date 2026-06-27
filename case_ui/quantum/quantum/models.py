"""
quantum/models.py — the data model for damages calculation.

Every monetary figure is a `Cited` value: an amount plus the bundle reference
(doc_id + paragraph + quote) it was taken from. Anything without a bundle source
is marked grounded=False so the waterfall can refuse to put it in the headline —
same anti-hallucination contract as the rest of Crucible.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Basis(str, Enum):
    """The four ways to compute quantum (see methods.py)."""
    COST = "cost"        # reliance: money actually spent and wasted
    VALUE = "value"      # expectation: the benefit the bargain would have produced
    MARKET = "market"    # comparable: what the same harm is worth on the market
    DCF = "dcf"          # discounted cash flow: PV of the lost future cash stream


@dataclass
class Cited:
    """A figure with its evidential source. quote must appear in the cited doc
    (verify with grounding_guard). grounded=False => assumption, not evidence."""
    amount: float
    doc_id: str = ""
    para: str = ""
    quote: str = ""
    grounded: bool = True

    @property
    def ref(self) -> str:
        return f"{self.doc_id} ¶{self.para}" if self.doc_id else "(assumption)"


@dataclass
class HeadOfLoss:
    """One head of loss, computed on one basis."""
    name: str
    basis: Basis
    value: Cited
    is_loss_of_profit: bool = False  # gates against an exclusion clause (e.g. 14.1)
    note: str = ""


@dataclass
class CausationAdjustment:
    """A reduction to a head of loss attributed to a non-defendant cause."""
    amount: float            # positive number = amount REMOVED from the claim
    reason: str
    source: Cited


@dataclass
class ContractClause:
    """A term that limits liability. excludes_loss_of_profit -> zeroes LP heads.
    caps_total -> total recoverable cannot exceed cap_value."""
    clause_id: str
    summary: str
    source: Cited
    excludes_loss_of_profit: bool = False
    caps_total: bool = False
    cap_value: Optional[float] = None


@dataclass
class EnforceabilityView:
    """Step 3b: a clause SAYS something; the law decides if it holds. This is the
    probabilistic legal-judgement layer — NOT a fact from the bundle."""
    clause_id: str
    p_upheld: float          # P(court enforces the clause as written)
    rationale: str           # e.g. "UCTA 1977 s.3/s.11 reasonableness"


@dataclass
class FraudRoute:
    """Clause 14.3-style carve-out: prove fraud/misrep and BOTH the exclusion and
    the cap fall away. p_proven is a legal-judgement input, not a bundle fact."""
    p_proven: float
    summary: str
    source: Cited


@dataclass
class WaterfallStep:
    label: str
    amount: float
    note: str = ""


@dataclass
class QuantumResult:
    case: str
    heads: list[HeadOfLoss]
    causation: list[CausationAdjustment]
    clauses: list[ContractClause]
    enforceability: list[EnforceabilityView]
    fraud: Optional[FraudRoute]
    headline: float                 # post-causation, pre-legal-constraint
    scenarios: dict                 # named point scenarios -> recoverable £
    expected_recoverable: float     # probability-weighted across the scenario tree
    waterfall: list[WaterfallStep]
